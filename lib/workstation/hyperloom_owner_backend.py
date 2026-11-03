"""Hyperloom benchmark backend for the workstation's existing owner controller.

This is the optimizer-side client. A separately qualified controller must be
running. It never falls back to Magpie, a shell benchmark or a direct GPU run.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import sys
from types import MappingProxyType

import hyperloom_exchange as exchange
import hyperloom_latency as latency
from hyperloom_sglang import checked_json, decoded_json, positive, require


NAME = "workstation_owner"
BINDING_ENV = "HYPERLOOM_WORKSTATION_BINDING"
BINDING_SHA_ENV = "HYPERLOOM_WORKSTATION_BINDING_SHA256"


class OwnerBackend:
    """Implement the pinned Hyperloom BenchmarkBackend interface."""

    name = NAME
    boot_phase_envs = MappingProxyType({})

    def resolve_interpreter(self):
        return sys.executable

    def lifecycle_eligibility(self, bench):
        return {"eligible": False, "framework": "sglang", "port": 0,
                "reason": "the workstation owner controls each finite server lifetime"}

    def build_command(self, *, python_exe, config_path, output_dir):
        return [python_exe, "-B", str(Path(__file__).resolve()), "benchmark",
                "--benchmark-config", str(config_path), "--output-dir", str(output_dir),
                "--run-mode", "local"]


def read_config(path):
    raw = exchange._read(path)
    try:
        value = decoded_json(raw)
    except json.JSONDecodeError:
        # PyYAML is already a dependency of the pinned Hyperloom runtime. The
        # standalone CPU JSON fixtures do not require it.
        import yaml

        class UniqueLoader(yaml.SafeLoader):
            pass

        def mapping(loader, node):
            loader.flatten_mapping(node)
            value = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                require(isinstance(key, str) and key not in value, "duplicate/non-string YAML key")
                value[key] = loader.construct_object(value_node)
            return value

        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        value = yaml.load(raw.decode(), Loader=UniqueLoader)
    require(isinstance(value, dict), "benchmark configuration must be an object")
    return value


def read_binding(path, sha256):
    path = Path(path)
    require(path.resolve() == path, "canonical runner binding required")
    binding = checked_json(path, sha256, exchange.MAX_JSON)
    require(type(binding.get("schema")) is int and binding["schema"] == 1
            and binding.get("backend") == NAME, "owner backend binding schema")
    for name in ("campaign_sha256", "selection_sha256", "controller_sha256",
                 "runtime_sha256", "workload_sha256"):
        exchange._identity(binding.get(name), name)
    require(type(binding.get("campaign_deadline_unix_ms")) is int
            and binding["campaign_deadline_unix_ms"] > 0, "campaign deadline required")
    requests, replies = (exchange._directory(binding[name]) for name in ("requests_root", "replies_root"))
    require(requests != replies and not requests.is_relative_to(replies)
            and not replies.is_relative_to(requests), "separate request/reply roots required")
    require(isinstance(binding.get("sources"), dict) and 0 < len(binding["sources"]) <= 64,
            "declared candidate source closure required")
    for name, source in binding["sources"].items():
        require(isinstance(name, str) and exchange.NAME.fullmatch(name)
                and isinstance(source, str) and Path(source).is_absolute(), "candidate source mapping")
    fixed = binding.get("benchmark")
    require(isinstance(fixed, dict) and fixed.get("framework") == "sglang"
            and fixed.get("runner_type") == "gfx12" and isinstance(fixed.get("model"), str)
            and fixed["model"] and isinstance(fixed.get("envs"), dict), "selected SGLang/gfx12 workload required")
    require(fixed.get("workstation_workload") == "balanced_c1_c2", "explicit mixed-concurrency workload required")
    caps = binding.get("caller_seconds")
    require(isinstance(caps, dict) and set(caps) == {"serving", "profile", "component"}
            and all(type(v) is int and 0 < v <= 7160 for v in caps.values()), "finite caller limits required")
    latency._policy(binding.get("latency_policy"))
    exchange._identity(binding.get("selected_source_set_sha256"), "selected source set")
    require(binding.get("profile_primary_window") in {"decode-0", "prefill-1", "prefill-2"},
            "explicit informative primary trace window required")
    require(binding.get("automatic_retry") is False and binding.get("adoption_authorized") is False,
            "runner cannot retry or adopt")
    return binding


def validate_config(config, binding):
    require(set(config) == {"benchmark"} and isinstance(config["benchmark"], dict),
            "one explicit benchmark configuration required")
    benchmark = dict(config["benchmark"])
    profiler = benchmark.pop("profiler", {})
    component = benchmark.pop("workstation_component", None)
    timeout = benchmark.pop("timeout_seconds", None)
    result_dir = benchmark.pop("result_dir", None)
    require(result_dir is None or isinstance(result_dir, str), "benchmark result directory")
    require(isinstance(profiler, dict) and set(profiler).issubset({"torch_profiler"}),
            "only the bound Kineto profiling route is supported")
    torch_profile = profiler.get("torch_profiler", {})
    require(isinstance(torch_profile, dict) and set(torch_profile).issubset({"enabled"})
            and type(torch_profile.get("enabled", False)) is bool, "profiler policy is owner-bound")
    kind = "profile" if torch_profile.get("enabled", False) else "serving"
    if component is not None:
        require(component == {"contract": "c4-fixed-geometry-v1"} and kind == "serving",
                "one fixed component contract without serving profiling required")
        require(binding["caller_seconds"]["component"] == 2190, "component caller is fixed at 2,190 seconds")
        kind = "component"
    require(benchmark == binding["benchmark"], "benchmark changed fixed selected workload; owner refused")
    require(type(timeout) in (int, float) and positive(timeout)
            and timeout >= exchange.envelope_seconds(kind, binding["caller_seconds"][kind]),
            "upstream timeout cannot truncate the caller/closeout envelope")
    return kind


def validate_report(result, request, binding):
    """Check owner provenance before returning an upstream benchmark report."""
    require(result["status"] == "SUCCEEDED" and result["independent_release"] is True,
            "owner job did not complete and release")
    report = result["payload"].get("benchmark_report")
    require(isinstance(report, dict) and report.get("success") is True
            and report.get("framework") == "sglang" and report.get("model") == binding["benchmark"]["model"],
            "owner report subject/status mismatch")
    require(report.get("benchmark_backend") == NAME and report.get("benchmark_valid") is True,
            "owner report backend/validity mismatch")
    owner = report.get("owner_binding", {})
    require(isinstance(owner, dict) and owner.get("owner_seal_sha256") == result["owner_seal_sha256"]
            and owner.get("request_sha256") == result["request_sha256"]
            and owner.get("source_set_sha256") == request["source_set_sha256"], "report belongs to another owner job")
    require(report.get("quality_gate", {}).get("passed") is True, "owner task quality gate failed")
    require(report.get("adoption_authorized") is False, "report cannot authorize adoption")
    if request["kind"] == "serving":
        require(report.get("diagnostic_only") is False, "diagnostic evidence cannot become serving timing")
        observed = report.get("workstation_latency")
        latency._series(observed, binding["latency_policy"])
        require(all(observed[k] == binding[k] for k in latency.FIXED_IDENTITIES)
                and observed["candidate_sha256"] == request["source_set_sha256"]
                and observed["owner_seal_sha256"] == result["owner_seal_sha256"],
                "latency series provenance mismatch")
        control = report.get("workstation_latency_control")
        latency._series(control, binding["latency_policy"])
        require(control.get("arm") == "control" and observed.get("arm") == "mtp"
                and control["candidate_sha256"] == binding["selected_source_set_sha256"]
                and all(control[k] == observed[k] for k in
                        (*latency.FIXED_IDENTITIES, "observation_id", "owner_seal_sha256", "quality_sha256")),
                "same-job selected-C4 control provenance mismatch")
    else:
        require(request["kind"] == "profile" and report.get("diagnostic_only") is True
                and report.get("workstation_latency") is None and report.get("workstation_latency_control") is None,
                "profiled durations cannot be a selectable latency series")
        coverage = report.get("profile_coverage")
        require(isinstance(coverage, dict) and coverage.get("complete") is True
                and coverage.get("owner_seal_sha256") == result["owner_seal_sha256"], "incomplete owner profile")
    return report


def _begin(config, output, binding):
    exchange.require_campaign_open(binding["replies_root"], campaign_sha256=binding["campaign_sha256"],
                                   controller_sha256=binding["controller_sha256"])
    kind = validate_config(config, binding)
    output = Path(output)
    require(output.is_absolute() and output.resolve() == output
            and output.is_dir() and not output.is_symlink(), "existing canonical upstream output directory required")
    require(not any((output / name).exists() or (output / name).is_symlink() for name in
                    ("benchmark_report.json", "OWNER-STARTED.json", "OWNER-JOB.json", "OWNER-RESULT.json")),
            "upstream output already contains an invocation")
    exchange._publish(output / "OWNER-STARTED.json", exchange.canonical({
        "campaign_sha256": binding["campaign_sha256"], "config_sha256": exchange.digest(exchange.canonical(config)),
        "kind": kind, "automatic_retry": False, "adoption_authorized": False}))


def _submit(config, output, binding, *, expected_source_set=None):
    kind = validate_config(config, binding)
    output = Path(output)
    if expected_source_set is not None:
        require(source_set(binding) == expected_source_set, "candidate changed after its component screen")
    job, request = exchange.submit(binding["requests_root"],
        campaign_sha256=binding["campaign_sha256"], selection_sha256=binding["selection_sha256"],
        kind=kind, config=config, sources=binding["sources"], caller_seconds=binding["caller_seconds"][kind],
        campaign_deadline_unix_ms=binding["campaign_deadline_unix_ms"], expected_source_set=expected_source_set)
    exchange._publish(output / "OWNER-JOB.json", exchange.canonical(request))
    return job, request


def start(config, output, binding, *, expected_source_set=None):
    _begin(config, output, binding)
    return _submit(config, output, binding, expected_source_set=expected_source_set)


def finish(result, request, output, binding):
    output = Path(output)
    exchange._publish(output / "OWNER-RESULT.json", exchange.canonical(result))
    if request["kind"] == "component":
        report = validate_component(result, request, binding)
        exchange._publish(output / "component_report.json", exchange.canonical(report))
    else:
        report = validate_report(result, request, binding)
        exchange._publish(output / "benchmark_report.json", exchange.canonical(report))
    return report


def validate_component(result, request, binding):
    require(result["status"] == "SUCCEEDED" and result["independent_release"] is True,
            "component owner did not complete and release")
    report = result["payload"].get("component_report")
    require(isinstance(report, dict) and report.get("schema") == 1 and report.get("diagnostic_only") is True
            and report.get("serving_tested") is False and report.get("adoption_authorized") is False,
            "component timing is diagnostic, not a serving result")
    owner = report.get("owner_binding", {})
    require(owner.get("owner_seal_sha256") == result["owner_seal_sha256"]
            and owner.get("request_sha256") == exchange.digest(exchange.canonical(request))
            and owner.get("source_set_sha256") == request["source_set_sha256"]
            and owner.get("controller_sha256") == binding["controller_sha256"], "foreign component result")
    require(type(report.get("correct_both_cards")) is bool and type(report.get("useful_both_cards")) is bool
            and type(report.get("zero_prefix_no_regression")) is bool
            and report.get("eligible_for_serving") is (report["correct_both_cards"] and report["useful_both_cards"]
                                                         and report["zero_prefix_no_regression"]),
            "component screening disposition mismatch")
    return report


def reuse_qualification(kind, output, binding):
    """Copy same-campaign qualification after authenticating its original nonce.

    The trusted controller stages these files in the optimizer's read-only
    inputs. Agent-writable results are never a source of reused measurements.
    """
    from hyperloom_sdk_adapter import require_qualified
    require(kind in ("serving", "profile"), "only baseline and initial profile can reuse qualification")
    proof = require_qualified("r9700", "sglang", bound=binding)
    name = "serving_aa" if kind == "serving" else "c1_c2_profile"
    reference = proof.get("measurements", {}).get(name)
    if reference is None:
        return None
    require(isinstance(reference, dict) and set(reference) == {
        "workspace", "request_sha256", "result_sha256", "report_sha256"}, "qualification measurement reference")
    root = Path(binding["requests_root"]).parent / "inputs/qualification"
    workspace = Path(reference["workspace"])
    require(workspace.is_absolute() and workspace.resolve() == workspace
            and workspace.parent == root and workspace.name == kind,
            "qualification measurement must be a read-only input")
    raw = {}
    for filename, key in (("OWNER-JOB.json", "request_sha256"), ("OWNER-RESULT.json", "result_sha256"),
                          ("benchmark_report.json", "report_sha256")):
        raw[filename] = exchange._read(workspace / filename, exchange.MAX_RESULT)
        require(exchange.digest(raw[filename]) == reference[key], "qualification measurement bytes changed")
    request = decoded_json(raw["OWNER-JOB.json"])
    require(request.get("kind") == kind
            and request.get("campaign_sha256") == binding["campaign_sha256"]
            and request.get("selection_sha256") == binding["selection_sha256"]
            and request.get("source_set_sha256") == binding["selected_source_set_sha256"]
            and source_set(binding) == binding["selected_source_set_sha256"],
            "qualification measurement is not this campaign's selected source")
    result = exchange.validate_result(decoded_json(raw["OWNER-RESULT.json"]), request,
                                     controller_sha256=binding["controller_sha256"])
    report = validate_report(result, request, binding)
    require(exchange.canonical(report) == raw["benchmark_report.json"], "qualification report differs from owner result")
    output = exchange._directory(output)
    for filename in (*raw, "REUSED-FROM.json"):
        require(not (output / filename).exists() and not (output / filename).is_symlink(),
                "qualification output was already used")
    for filename, contents in raw.items():
        exchange._publish(output / filename, contents)
    exchange._publish(output / "REUSED-FROM.json", exchange.canonical({
        "schema": 1, "qualification": binding["qualification"], "measurement": reference,
        "request_sha256": result["request_sha256"], "new_measurement": False,
        "automatic_retry": False, "adoption_authorized": False}))
    return report


class ComponentRejected(ValueError):
    error_class = "owner_component_rejected"


def source_set(binding):
    rows = []
    for name, path in sorted(binding["sources"].items()):
        raw = exchange._read(path, exchange.MAX_SOURCE)
        rows.append(dict(name=name, bytes=len(raw), sha256=exchange.digest(raw)))
    return exchange.digest(exchange.canonical(rows))


def _screen_config(config, output, binding):
    kind = validate_config(config, binding)
    current = source_set(binding)
    if kind == "component" or current == binding["selected_source_set_sha256"]:
        return None, current
    required = (exchange.envelope_seconds(kind, binding["caller_seconds"][kind])
                + exchange.envelope_seconds("component", binding["caller_seconds"]["component"]))
    require(config["benchmark"]["timeout_seconds"] >= required,
            "changed-source benchmark must include the component screen and serving/profile envelopes")
    screened = copy.deepcopy(config)
    screened["benchmark"].pop("profiler", None)
    screened["benchmark"]["workstation_component"] = {"contract": "c4-fixed-geometry-v1"}
    screened["benchmark"]["timeout_seconds"] = exchange.envelope_seconds("component", binding["caller_seconds"]["component"])
    return screened, current


def _screen_gate(report):
    if report["eligible_for_serving"] is not True:
        raise ComponentRejected("Component screen refused serving; retained component_report.json contains compiler, "
                                "correctness and paired timing evidence. " + json.dumps({k: report[k] for k in
                                ("correct_both_cards", "useful_both_cards", "zero_prefix_no_regression")}, sort_keys=True))


def run(config, output, binding, *, wait=exchange.wait_result):
    _begin(config, output, binding)
    screened, expected = _screen_config(config, output, binding)
    if screened is not None:
        screen_output = Path(output) / "component-screen"
        screen_output.mkdir(mode=0o700)
        _screen_gate(run(screened, screen_output, binding, wait=wait))
    job, request = _submit(config, output, binding, expected_source_set=expected)
    result = wait(job, binding["replies_root"], request, controller_sha256=binding["controller_sha256"])
    return finish(result, request, output, binding)


async def run_async(config, output, binding, *, wait=exchange.wait_result_async):
    _begin(config, output, binding)
    screened, expected = _screen_config(config, output, binding)
    if screened is not None:
        screen_output = Path(output) / "component-screen"
        screen_output.mkdir(mode=0o700)
        _screen_gate(await run_async(screened, screen_output, binding, wait=wait))
    job, request = _submit(config, output, binding, expected_source_set=expected)
    result = await wait(job, binding["replies_root"], request, controller_sha256=binding["controller_sha256"])
    return finish(result, request, output, binding)


def benchmark(config, output_dir, binding, *, wait=exchange.wait_result):
    """Create the fresh benchmark_* workspace consumed by the upstream runner."""
    root = exchange._directory(output_dir)
    workspace = root / "benchmark_owner"
    workspace.mkdir(mode=0o700)
    return run(config, workspace, binding, wait=wait)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["benchmark"])
    parser.add_argument("--benchmark-config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-mode", choices=["local"], required=True)
    args = parser.parse_args(argv)
    try:
        binding = read_binding(os.environ.get(BINDING_ENV, ""), os.environ.get(BINDING_SHA_ENV, ""))
        benchmark(read_config(args.benchmark_config), args.output_dir, binding)
    except (ValueError, OSError, TimeoutError) as error:
        print("OWNER_BACKEND_REFUSED: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
