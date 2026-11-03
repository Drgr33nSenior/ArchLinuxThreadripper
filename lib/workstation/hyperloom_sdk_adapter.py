"""Narrow hooks for the pinned Hyperloom workstation-owner adapter build."""

import copy
import math
import os
from pathlib import Path

import hyperloom_latency as latency
from hyperloom_owner_backend import BINDING_ENV, BINDING_SHA_ENV, NAME, read_binding
from hyperloom_sglang import checked_json, require


def active():
    return os.environ.get("HYPERLOOM_BENCHMARK_BACKEND", "").strip().lower() == NAME


def binding():
    return read_binding(os.environ.get(BINDING_ENV, ""), os.environ.get(BINDING_SHA_ENV, ""))


def seed_grading():
    bound = binding()
    return {"objective": latency.OBJECTIVE, "policy": copy.deepcopy(bound["latency_policy"]),
            "campaign_sha256": bound["campaign_sha256"], "selection_sha256": bound["selection_sha256"]}


def configure_owner_benchmark(args):
    """Bind the frozen owner menu before the CLI's AgentX-only YAML checks."""
    from hyperloom_owner_backend import read_config, validate_config
    from hyperloom.common.benchmark_config import BENCHMARK_CONFIG_ENV
    bound = binding()
    reference = bound.get("benchmark_config")
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"},
            "pinned owner benchmark menu required")
    path = Path(reference["path"])
    require(path.parent == Path(bound["requests_root"]).parent / "inputs"
            and path.resolve() == path, "owner menu must be an immutable runtime input")
    config = checked_json(path, reference["sha256"])
    require(validate_config(config, bound) == "serving", "owner baseline menu must be unprofiled")
    requested = getattr(args, "benchmark_config", None)
    require(requested is None or Path(requested).resolve() == path, "CLI changed the frozen owner menu")
    require(not getattr(args, "docker_image", None) and not getattr(args, "resume_from", None),
            "owner campaign cannot select a different container or resume")
    require(not os.environ.get("HYPERLOOM_AGENTX"), "owner benchmark cannot enable AgentX")
    for key, value in {"framework":"sglang", "gpu_type":"r9700", "model":bound["benchmark"]["model"],
                       "precision":"mxfp4", "tp":2, "conc":2, "max_model_len":131040}.items():
        actual = getattr(args, key, None)
        require((str(actual) if key == "model" else actual) == value,
                "CLI changed fixed owner setting: " + key)
    os.environ[BENCHMARK_CONFIG_ENV] = str(path)
    args.enable_conc_sweep = False


def require_qualified(gpu_type, framework, *, bound=None):
    """The local backend gate never changes Magpie's supported-framework list."""
    # The CLI passes its runner alias (gfx12); preflight passes the physical
    # type (r9700). Both must resolve to the same pinned owner qualification.
    require(str(gpu_type).lower() in {"r9700", "gfx12"} and str(framework).lower() == "sglang",
            "owner adapter is scoped to SGLang/R9700")
    bound = binding() if bound is None else bound
    proof_ref = bound.get("qualification")
    require(isinstance(proof_ref, dict) and set(proof_ref) == {"path", "sha256"},
            "native owner-adapter qualification is still required")
    path = Path(proof_ref["path"])
    require(path.is_absolute() and path.resolve() == path, "canonical qualification proof required")
    proof = checked_json(path, proof_ref["sha256"])
    require(type(proof.get("schema")) is int and proof["schema"] == 1
            and proof.get("kind") == "workstation-owner-native-qualification"
            and proof.get("status") == "PASS", "native qualification did not pass")
    require(all(proof.get(k) == bound[k] for k in
                ("campaign_sha256", "campaign_deadline_unix_ms", "selection_sha256",
                 "runtime_sha256", "workload_sha256", "controller_sha256")),
            "native qualification belongs to another stack or controller")
    checks = proof.get("checks")
    expected = {"serving_aa", "c1_c2_profile", "component_oracle", "owner_release",
                "failure_retention", "response_mount_read_only", "agents_have_no_gpu_devices",
                "upstream_balanced_keep_paths", "campaign_deadline"}
    require(isinstance(checks, dict) and set(checks) == expected
            and all(v is True for v in checks.values()), "native qualification coverage is incomplete")
    require(isinstance(proof.get("owner_seals"), list) and len(proof["owner_seals"]) >= 2,
            "qualification owner evidence missing")
    seen = set()
    for row in proof["owner_seals"]:
        require(isinstance(row, dict) and set(row) == {"path", "sha256"}, "qualification seal reference")
        seal = Path(row["path"])
        require(seal.is_absolute() and seal.resolve() == seal and seal.name == "FINAL-ARTIFACTS.sha256"
                and str(seal) not in seen, "distinct canonical qualification seals required")
        seen.add(str(seal))
        from hyperloom_sglang import checked_bytes
        checked_bytes(seal, row["sha256"], 2 << 20)
    return proof


def owner_preflight(args):
    """Validate the actual owner and isolated agent runtime without installing."""
    import importlib.util
    import sys
    import hyperloom_chatgpt as chatgpt
    from hyperloom.inference_optimizer.cli import preflight as upstream

    require(args is not None, "explicit owner campaign arguments required")
    require(getattr(args, "no_kernel", False) is True
            and not getattr(args, "no_framework_agent", False),
            "campaign requires the owner-backed framework specialist; direct kernel runners are unsupported")
    require(not getattr(args, "enable_roofline", False)
            and getattr(args, "degraded_kb", False) is True
            and getattr(args, "degraded_pr", False) is True
            and not getattr(args, "proposal_scoring", False),
            "campaign uses authenticated local research, owner profiles and no external KB publication")
    hours = getattr(args, "max_hours", None)
    require(type(hours) in (int, float) and math.isfinite(hours) and 0 < hours <= 4,
            "explicit owner campaign budget of at most four hours required")
    require(not getattr(args, "resume_from", None), "owner campaign cannot resume a consumed grant")
    event = upstream._begin_install_event(args)
    args.pr_monitor_enabled = False
    upstream._run_install_step(event, step_id="qualified_workstation_owner", category="check",
        action=lambda: {"status": "checked", "detail": require_qualified(args.gpu_type, args.framework)})
    require(not Path("/dev/kfd").exists() and not Path("/dev/dri").exists(),
            "optimizer must have no GPU devices")
    bound = binding()
    require(os.statvfs(bound["replies_root"]).f_flag & os.ST_RDONLY,
            "controller replies must be mounted read-only in the optimizer")
    for name in ("openai_codex", "openai", "httpx", "yaml"):
        require(importlib.util.find_spec(name) is not None, "pinned runtime dependency unavailable: " + name)
    if chatgpt.enabled():
        chatgpt.auth_home()
        urls = None
    else:
        from hyperloom.inference_optimizer.cli.credentials import _validate_credentials
        _validate_credentials()
        urls = upstream._export_llm_endpoints()
    upstream._record_install_step(event,
        upstream.InstallStep("isolated_optimizer", "check", "checked"),
        detail={"gpu_devices": False, "response_mount_read_only": True, "auto_install": False,
                "external_kb_publication": False,
                "authentication": "chatgpt" if chatgpt.enabled() else "configured-api"})
    upstream._finish_install_event(event, args=args,
        runtime=upstream.PreflightRuntime(NAME, sys.executable, sys.executable, "", urls))
    return urls


def owner_partition(request, *, framework, gpu_type, nodes, model):
    """The device-free client cannot assert or change the owner's GPU topology."""
    require(not request.declared_mode and request.streams_per_partition is None,
            "owner campaign does not accept compute-partition or stream overrides")
    require(nodes == 1 and framework == "sglang" and gpu_type == "r9700",
            "owner campaign uses the qualified one-workstation SGLang topology")
    require(str(model.model_path) == binding()["benchmark"]["model"], "owner partition model mismatch")
    require_qualified(gpu_type, framework)
    return {}  # No client-side hardware observation; each owner retains its admission evidence.


_specialist_bases = {}


def prepare_specialist_tree(ctx, *, workspace, profile, integration_pending=lambda: False):
    """Give the specialist a plain copy of the one evaluable source file."""
    from hyperloom.inference_optimizer.framework_paths import FrameworkTree
    from hyperloom_exchange import _read, MAX_SOURCE
    if workspace is None or (profile is not None and profile.mode != "patch"):
        return None, None, ""
    require(not integration_pending(), "candidate integration is still pending")
    bound = binding()
    sources = bound["sources"]
    require(set(sources) == {"extend_attention_fwd_candidate.py"}, "one evaluable C4 source required")
    name, source = next(iter(sources.items()))
    source = Path(source)
    require(source.name == name and source.resolve() == source, "canonical candidate source mapping required")
    require(not any((parent / ".git").exists() for parent in (source.parent, *source.parents)),
            "optimizer candidate must be a plain, isolated source directory")
    content = _read(source, MAX_SOURCE)
    require(not integration_pending(), "candidate changed during specialist preparation")
    workspace = Path(workspace)
    require(workspace.is_absolute() and workspace.resolve() == workspace, "canonical specialist workspace required")
    worktree = workspace / "worktree"
    worktree.mkdir(mode=0o700)
    with (worktree / name).open("xb") as output:
        output.write(content)
    require(len(_specialist_bases) < 16, "too many uncollected specialist copies")
    _specialist_bases[str(worktree)] = (str(source), content)
    return worktree, FrameworkTree(tree=source.parent, root=source.parent, checkout=False), ""


def collect_specialist_patch(worktree, workspace, worktree_base, targets=()):
    """Render the declared source edit without creating or staging Git state."""
    import difflib
    from hyperloom_exchange import _read, MAX_SOURCE
    if worktree is None:
        return [], {}
    bound = binding()
    sources = bound["sources"]
    require(set(sources) == {"extend_attention_fwd_candidate.py"}, "one evaluable C4 source required")
    name, source = next(iter(sources.items()))
    source = Path(source)
    require(Path(worktree_base) == source.parent, "specialist patch targets another source root")
    require(not targets or set(targets) == {name}, "specialist targets fall outside the evaluable source")
    base = _specialist_bases.pop(str(worktree), None)
    require(base is not None and base[0] == str(source), "specialist source snapshot is missing")
    require(_read(source, MAX_SOURCE) == base[1], "candidate changed while specialist was working")
    before = base[1].decode()
    after = _read(Path(worktree) / name, MAX_SOURCE).decode()
    delta = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile="a/" + name, tofile="b/" + name))
    if not delta:
        return [], {}
    directory = Path(workspace) / "owner-patches"
    directory.mkdir(mode=0o700)
    patch = directory / "candidate.patch"
    with patch.open("x") as output:
        output.write("diff --git a/" + name + " b/" + name + "\n" + delta)
    return [str(patch)], {str(patch): str(source.parent)}


def snapshot(source):
    if not isinstance(source, dict) or not isinstance(source.get("workstation_latency"), dict):
        return None
    return {"workstation_latency": copy.deepcopy(source["workstation_latency"]),
            "workstation_latency_control": copy.deepcopy(source.get("workstation_latency_control")),
            "output_throughput": source.get("output_throughput", source.get("tput"))}


class FrozenConfiguration(ValueError):
    """A retained variant refusal in Hyperloom's existing grid runner."""

    error_class = "owner_frozen_configuration"


def _fixed(condition, message):
    if not condition:
        raise FrozenConfiguration(message)


def materialize_config(config_path, output_dir, **overrides):
    """Preserve the owner workload when Hyperloom materializes its YAML.

    The first campaign varies kernel source. Framework flag changes and
    concurrency sweeps need a separate workload contract, so they refuse here.
    """
    from hyperloom_owner_backend import read_config, validate_config
    from hyperloom_exchange import canonical, envelope_seconds

    bound = binding()
    config = read_config(config_path)
    for name in ("extra_server_args", "extra_envs", "remove_args", "unset_envs"):
        _fixed(not overrides.get(name), "owner campaign freezes framework options: " + name)
    _fixed(not os.environ.get("INFERENCE_OPTIMIZER_SERVER_ARGS", "").strip(),
            "owner campaign freezes inherited server arguments")
    model = overrides.get("model_path")
    _fixed(not model or str(model) == bound["benchmark"]["model"], "owner model override")
    _fixed(overrides.get("gpu_type") in (None, "", "r9700"), "owner GPU override")
    _fixed(not overrides.get("benchmark_script"), "owner does not run a replacement benchmark script")
    try:
        kind = validate_config(config, bound)
    except ValueError as error:
        raise FrozenConfiguration(str(error)) from error
    if kind in ("serving", "profile"):
        # One benchmark action can screen a changed source before starting its
        # serving owner. Each request still has its own original finite caller.
        config["benchmark"]["timeout_seconds"] = max(config["benchmark"]["timeout_seconds"],
            envelope_seconds(kind, bound["caller_seconds"][kind])
            + envelope_seconds("component", bound["caller_seconds"]["component"]))
    out_name = overrides.get("out_name", "baseline_config.with_envs.yaml")
    require(isinstance(out_name, str) and Path(out_name).name == out_name
            and out_name not in (".", ".."), "simple benchmark config filename required")
    destination = Path(output_dir) / out_name
    require(destination.parent.resolve() == destination.parent and destination.parent.is_dir()
            and not destination.is_symlink(), "canonical config output directory")
    # The upstream API deliberately re-materializes this one config path. It
    # is not an invocation or result; job identities are reserved by the runner.
    destination.write_bytes(canonical(config))
    return destination


def owner_timeouts(env=None):
    from hyperloom_exchange import envelope_seconds
    bound = binding()
    seconds = (envelope_seconds("serving", bound["caller_seconds"]["serving"])
               + envelope_seconds("component", bound["caller_seconds"]["component"]))
    require(seconds <= 14400, "owner action exceeds the campaign ceiling")
    source = os.environ if env is None else env
    for name in ("INFERENCE_OPTIMIZER_BENCHMARK_TIMEOUT_SEC", "INFERENCE_OPTIMIZER_BENCHMARK_SILENCE_TIMEOUT_SEC"):
        if name in source:
            require(float(source[name]) == seconds, "owner benchmark timeout override changed the finite envelope")
    return float(seconds), float(seconds)


def sync_owner_timeout(config_path, timeout_sec):
    from hyperloom_owner_backend import read_config, validate_config
    from hyperloom_exchange import canonical
    require(timeout_sec == owner_timeouts()[1], "outer benchmark timeout truncates the component/owner envelope")
    config = read_config(config_path)
    validate_config(config, binding())
    config["benchmark"]["timeout_seconds"] = timeout_sec
    Path(config_path).write_bytes(canonical(config))


def materialize_variant_config(config_path, base_extra_args, variant, *, output_subdir,
                               model_path=None, gpu_type=None, benchmark_script=None, server_lifecycle=None,
                               base_args_mode="append", base_extra_envs=None, base_remove_args=None,
                               base_unset_envs=None):
    """The grid runner has a separate configuration writer from baseline."""
    _fixed(base_args_mode == "append" and getattr(variant, "args_mode", "append") == "append",
           "owner campaign freezes argument replacement")
    _fixed(not server_lifecycle, "owner controls the complete finite server lifecycle")
    output = Path(output_subdir)
    output.mkdir(parents=True, exist_ok=True)
    return materialize_config(config_path, output,
        extra_server_args=" ".join(s for s in (base_extra_args, variant.extra_server_args) if s),
        extra_envs={**(base_extra_envs or {}), **variant.extra_envs},
        remove_args=list(base_remove_args or []) + list(getattr(variant, "remove_args", [])),
        unset_envs=list(base_unset_envs or []) + list(getattr(variant, "unset_envs", [])),
        model_path=model_path, gpu_type=gpu_type, benchmark_script=benchmark_script,
        out_name="variant_config.yaml")


def runtime_overrides(benchmark, *, model_path=None, gpu_type=None, benchmark_script=None, conc=None):
    from hyperloom_owner_backend import validate_config

    bound = binding()
    require(not model_path or str(model_path) == bound["benchmark"]["model"], "owner model override")
    require(gpu_type in (None, "", "r9700") and not benchmark_script and conc is None,
            "owner workload cannot change runner, script or concurrency")
    validate_config({"benchmark": benchmark}, bound)
    return benchmark["envs"]


def grade(state, measurement, *, against_baseline=False, anchor_perf=None, keep_threshold_pct=0):
    """Grade on the recorded objective; never use a throughput fallback."""
    from hyperloom.common.perf_metric import GradedComparison, constraint_veto_reason

    common = {"objective": latency.OBJECTIVE, "candidate": 0.0, "reference": 0.0, "verdict": "REVERT"}
    try:
        grading = getattr(state, "grading", {})
        require(grading.get("objective") == latency.OBJECTIVE, "balanced objective not recorded")
        require(type(keep_threshold_pct) in (int, float) and math.isfinite(keep_threshold_pct)
                and keep_threshold_pct >= 0, "invalid upstream KEEP threshold")
        policy = dict(grading["policy"])
        policy["minimum_gain_pct"] = max(policy["minimum_gain_pct"], keep_threshold_pct)
        if anchor_perf is not None:
            reference = anchor_perf
        elif against_baseline:
            reference = getattr(state, "baseline_perf", None)
        else:
            reference = getattr(state, "current_best", None) or getattr(state, "baseline_perf", None)
        require(isinstance(reference, dict) and isinstance(measurement, dict), "latency anchor or measurement missing")
        before, after = reference.get("workstation_latency"), measurement.get("workstation_latency")
        require(isinstance(after, dict) and after.get("selection_sha256") == grading.get("selection_sha256"),
                "latency measurement differs from the seeded selection")
        if before is not None and before == after:
            latency._policy(policy)
            latency._series(before, policy)
            return GradedComparison(**dict(common, candidate=1.0, reference=1.0,
                                          refused_by="same_measurement_not_a_new_gain"))
        comparison = latency.paired_comparison(before, after, reference.get("workstation_latency_control"),
                                               measurement.get("workstation_latency_control"), policy)
        common.update(candidate=comparison["candidate_score"], reference=comparison["baseline_score"],
                      verdict="KEEP" if comparison["verdict"] == "KEEP" else "REVERT",
                      refused_by=",".join(comparison["reasons"]))
        if comparison["verdict"] == "INCONCLUSIVE":
            common["degrade_reason"] = ("environment_drift" if any("environment_drift" in r for r in comparison["reasons"])
                                        else "unequal_generated_work" if any("unequal_generated_work" in r for r in comparison["reasons"])
                                        else "latency_measurement_dispersion")
        if common["verdict"] == "KEEP":
            veto = constraint_veto_reason(measurement, state)
            if veto:
                common.update(verdict="REVERT", veto_reason=veto)
        return GradedComparison(**common)
    except (ValueError, KeyError, TypeError, AttributeError):
        common.update(verdict="REVERT", degrade_reason="missing_or_invalid_balanced_latency_evidence")
        return GradedComparison(**common)
