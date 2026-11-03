"""Bounded owner operation: fresh source-bound packet, inactive proposal, one finite run.

Three explicit steps with JSON-serializable records; the parent controller
calls them in order and publishes the outcome through its own exchange:

  prepare(snapshot, bound, ...)  -> fresh inert packet, caller and grading
                                    template, sealed source/preparation roots
  propose(preparation, bound, ...) -> exact inactive registration proposal bound
                                    to the current idle checkpoint
  execute(proposal, authorization, bound, ...) -> one finite owner invocation,
                                    original replay, native grading, disposition

Importing this module and calling prepare/propose performs no native action.
execute runs only the fresh launcher/execute_once.py through the pinned finite
local_commands helper, never the legacy hyperloom_owner.run path. A caller exit
code or copied summary is never release or success: release is the original
retained replay (bind_future.bind), grading is the materialized CPU packet, and
all failures and partial receipts survive in the campaign directory.
"""

from datetime import datetime, timezone
import base64
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
INFRA = Path(__file__).resolve().parents[3]
LIB = INFRA / "lib/workstation"
sys.path.insert(0, str(LIB))
import hyperloom_exchange as exchange
import hyperloom_latency as latency
import hyperloom_owner_backend as backend
import hyperloom_owner_result as projection
from hyperloom_sglang import checked_bytes, checked_json, decoded_json, require

PYTHON = "/opt/local/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11"
MX = INFRA / "scratchpad/mxfp4-ultraquant-feasibility-20261026-v1"
R2 = MX / "fp8-fastpath-research-v2"
SEALER = MX / "unroll4-model-run-fdb1c227c4911f07/seal_evidence.py"
SEALER_SHA = "082835dabf0bf1e96d9bba6f08918914b01a1306a5a2cd939a0c9309ad334e18"
COMMANDS_SHA = "71ac3ebfc4eef1c0b373a7a8317c1f72d300a375f307cce1500bbdb0a659711a"
DONOR_PROPOSAL = R2 / "phase1-w1-campaign-17dca9a455b6906d-v2/REGISTRATION-PROPOSED.json"
DONOR_PROPOSAL_SHA = "f4d93da7a1388d11bbd06fe3350a51617be2536eeaccbce04bef47bebed0f59f"
C4_SHA = "b5d145e55f391de7e515cfe164b6011444fe1696188e2c0f900e338ce36c22f0"
EVALUABLE = "extend_attention_fwd_candidate.py"
REQUESTS = {"serving": 30, "profile": 5}
PIN_FIELDS = ("archive_sha256", "manifest_sha256", "package_sha256", "source_manifest_sha256", "session_sha256")

OWNER_SECONDS, CALLER_SECONDS, LAUNCH_SECONDS = 6360, 7160, 6440
WORKER_SECONDS, CLIENT_SECONDS, MODEL_STARTS = 2280, 2040, 2
TERM_SECONDS = REAP_SECONDS = 5
# The helper terminates and reaps inside its own envelope; the outer command
# work must leave that headroom inside the registered 7,160-second ceiling.
CALLER_WORK_SECONDS = CALLER_SECONDS - TERM_SECONDS - REAP_SECONDS
GRADING_SECONDS = exchange.GRADING_SECONDS
GRADING_WORK_SECONDS = GRADING_SECONDS - TERM_SECONDS - REAP_SECONDS
REPLAY_WORK_SECONDS = READOUT_WORK_SECONDS = 40
POST_RELEASE_SECONDS = exchange.POST_RELEASE_SECONDS["serving"]
PUBLICATION_SECONDS = 30
BUILD_SECONDS = 40
LIMITS = dict(owner_seconds=OWNER_SECONDS, caller_seconds=CALLER_SECONDS, native_launch_seconds=LAUNCH_SECONDS,
              model_starts=MODEL_STARTS, worker_seconds=WORKER_SECONDS, client_seconds=CLIENT_SECONDS,
              cpu_grading_seconds=GRADING_SECONDS)
REGISTERED_STATUS = "REGISTERED_ONE_HYPERLOOM_SERVING"
PROPOSED_STATUS = "PROPOSED_ONE_HYPERLOOM_SERVING"
VARIABLE = ("registered", "proposal", "gpu_allowance", "owner_answer", "approval_record",
            "registration_authority", "registered_utc", "status")
AUTHORIZATION_REQUIRED = (
    "schema", "kind", "campaign", "proposal_sha256", "execute_once_sha256", "nonce", "global_gpu_attempt_index",
    "request_sha256", "source_set_sha256", "candidate_sha256", "campaign_sha256", "controller_sha256",
    "owner", "owner_answer", "approval", "no_retry", "one_invocation", "deployment", "promotion",
    "publication", "adoption_authorized", "qualification", "utc")
CHECKPOINT = re.compile(r"CHECKPOINT-(\d+)\.json\Z")
NONCE = re.compile(r"[0-9a-f]{16}\Z")
# This is the external-command boundary. Timeouts are subprocess errors, not
# RuntimeError/OSError; all ordinary failures must reach the retained closeout.
# Process-control BaseExceptions still propagate through the finally blocks.
REFUSALS = (Exception,)


class Unsupported(ValueError):
    """A declared refusal that must never reach the owner."""


class Cancelled(ValueError):
    """The parent cancelled the request; no owner may start for it."""


def _clock(now):
    return time.time() if now is None else float(now)


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _error(error):
    return type(error).__name__ + ": " + str(error)[:512]


def _canonical_dir(path, label):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and path.is_dir() and not path.is_symlink(),
            "canonical existing " + label + " directory required")
    return path


def _fresh(path, label):
    path = Path(path)
    require(path.is_absolute() and path.parent.resolve() == path.parent and path.parent.is_dir()
            and not path.exists() and not path.is_symlink(), "fresh canonical " + label + " required")
    return path


def _json(path, cap=exchange.MAX_JSON):
    return decoded_json(exchange._read(path, cap))


def _pinned(pin, label, cap=exchange.MAX_JSON):
    require(isinstance(pin, dict) and set(pin) == {"path", "sha256"} and isinstance(pin["path"], str),
            "explicit {path, sha256} " + label + " pin required")
    path = Path(pin["path"])
    require(path.is_absolute() and path.resolve() == path, "canonical " + label + " path required")
    return path, checked_json(path, pin["sha256"], cap)


def _pin(path):
    return dict(path=str(path), sha256=exchange.digest(exchange._read(path, 16 << 20)))


def _dump(path, value):
    """The caller's own record style: indented, sorted, exclusive."""
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return _pin(path)


def _write(path, value):
    raw = exchange.canonical(value)
    require(len(raw) <= exchange.MAX_RESULT, "bounded record required")
    with Path(path).open("xb") as stream:
        stream.write(raw)
    return dict(path=str(path), sha256=exchange.digest(raw))


def _source(path, sha256, name, maximum=4 << 20):
    """Execute authenticated source bytes as a module without a bytecode writer."""
    raw = checked_bytes(path, sha256, maximum)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    return module


def sealer(path=SEALER):
    return _source(Path(path), SEALER_SHA, "_hyperloom_owner_operation_sealer", 64 << 10)


def commands(helper, root):
    """The pinned finite local_commands helper, writing its receipts under root."""
    module = _source(Path(helper), COMMANDS_SHA, "_hyperloom_owner_operation_commands_" + Path(root).name, 64 << 10)
    module.ROOT = _canonical_dir(root, "receipt")
    return module


def _bound(bound):
    require(isinstance(bound, dict) and bound.get("backend") == backend.NAME, "owner backend binding required")
    for name in ("campaign_sha256", "selection_sha256", "controller_sha256", "runtime_sha256",
                 "workload_sha256", "selected_source_set_sha256"):
        exchange._identity(bound.get(name), name)
    caps = bound.get("caller_seconds")
    require(isinstance(caps, dict) and caps.get("serving") == caps.get("profile") == CALLER_SECONDS,
            "serving and profile caller ceilings must be the registered 7,160 seconds")
    require(type(bound.get("campaign_deadline_unix_ms")) is int and bound["campaign_deadline_unix_ms"] > 0,
            "campaign deadline required")
    require(isinstance(bound.get("benchmark"), dict) and isinstance(bound["benchmark"].get("model"), str),
            "fixed selected benchmark required")
    latency._policy(bound.get("latency_policy"))
    require(bound.get("automatic_retry") is False and bound.get("adoption_authorized") is False,
            "binding cannot retry or adopt")
    return bound


def _runtime_configuration(bound, template):
    """A cache-policy experiment has a distinct, explicitly pinned identity."""
    option = "mamba_max_states_per_path"
    if option not in template:
        require("runtime_configuration" not in bound, "runtime override is absent from the owner template")
        return None
    require(type(template[option]) is int and template[option] == 3, "only the reviewed cache cap 3 is supported")
    reference = bound.get("runtime_configuration")
    _, value = _pinned(reference, "experimental runtime configuration")
    require(reference["sha256"] == bound["runtime_sha256"]
            and value.get("schema") == 1 and value.get("selection_sha256") == bound["selection_sha256"]
            and value.get("configuration_overrides") == {option: 3}
            and type(value["configuration_overrides"][option]) is int
            and value.get("selected_runtime_behavior_unchanged") is False
            and value.get("adopted") is False and value.get("physically_qualified") is False,
            "experimental runtime identity differs from the campaign binding")
    require(set(value.get("serving_arms", {})) == {"control", "mtp"}, "both experimental runtime arms required")
    return value


def _serving_surface(bound):
    family = bound.get("serving_source_family", "extend_attention")
    if family == "extend_attention":
        return family, EVALUABLE, C4_SHA
    require(family == "verify_prefetch", "unknown serving source family")
    module = _source(HERE / "verify_serving.py", bound["verify_serving_sha256"], "_owner_verify_serving")
    return family, module.FILE, module.BASELINE_SHA


def _snapshot(snapshot_root, bound):
    """Authenticate the controller-private snapshot; refuse undeclared surfaces."""
    root = _canonical_dir(snapshot_root, "snapshot")
    request_raw = exchange._read(root / "REQUEST.json")
    request = exchange._request(decoded_json(request_raw))
    request_sha256 = exchange.digest(exchange.canonical(request))
    require(request_sha256 == exchange.digest(request_raw), "snapshot request is not canonical")
    require(request["campaign_sha256"] == bound["campaign_sha256"]
            and request["selection_sha256"] == bound["selection_sha256"], "snapshot belongs to another campaign")
    if request["kind"] not in REQUESTS:
        raise Unsupported("component requests are routed by the parent, not this serving/profile seam")
    require(request["caller_seconds"] == CALLER_SECONDS
            and request["expires_unix_ms"] <= bound["campaign_deadline_unix_ms"], "request exceeds the bound ceiling")
    config_raw = exchange._read(root / "CONFIG.json")
    require(exchange.digest(config_raw) == request["config_sha256"], "snapshot config changed")
    kind = backend.validate_config(decoded_json(config_raw), bound)
    require(kind == request["kind"], "request/config kind mismatch")
    claim = _json(root / "CLAIM.json")
    require(claim.get("id") == request["id"] and claim.get("request_sha256") == request_sha256
            and claim.get("controller_sha256") == bound["controller_sha256"] and claim.get("gpu_admission") is False,
            "snapshot claim mismatch")
    family, evaluable, baseline = _serving_surface(bound)
    require(family == "extend_attention" or request["kind"] == "serving", "verification serving only")
    sources = request["sources"]
    if len(sources) != 1 or sources[0]["name"] != evaluable:
        raise Unsupported("only the declared " + evaluable + " source surface is evaluable")
    row = sources[0]
    source_dir = _canonical_dir(root / "sources", "snapshot source")
    require({p.name for p in source_dir.iterdir()} == {evaluable}, "unexpected snapshot source member")
    raw = exchange._read(source_dir / evaluable, exchange.MAX_SOURCE)
    require(len(raw) == row["bytes"] and exchange.digest(raw) == row["sha256"],
            "snapshot source bytes differ from the transmitted request")
    return dict(root=root, request=request, request_sha256=request_sha256, claim=claim, kind=kind, source=row,
                source_path=source_dir / evaluable, family=family, baseline_sha256=baseline)


def _work_budget(deadline, maximum, label, reserve=0):
    """Leave the helper's TERM/reap allowance inside the shared deadline."""
    remaining = int(deadline - time.monotonic() - TERM_SECONDS - REAP_SECONDS - reserve)
    require(remaining > 0, label + " has no remaining finite work budget")
    return min(maximum, remaining)


def _builder(commands_module, root, name, script, arguments, deadline):
    argv = [PYTHON, "-I", "-S", "-B", str(HERE / script), *arguments]
    seconds = _work_budget(deadline, BUILD_SECONDS, name, reserve=5)
    receipt = commands_module.run(name, argv, seconds, cwd=str(root), output_limit=1 << 20)
    stdout = checked_bytes(root / (name + ".stdout"), receipt["stdout"]["sha256"], 1 << 20)
    lines = [line for line in stdout.splitlines() if line.strip()]
    require(lines, "builder produced no receipt line")
    return decoded_json(lines[-1]), dict(receipt, argv=argv)


def _checkpoint(root):
    root = _canonical_dir(root, "checkpoint")
    numbered = {}
    for path in root.iterdir():
        match = CHECKPOINT.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            numbered[int(match.group(1))] = path
    require(numbered, "no numeric CHECKPOINT found")
    number = max(numbered)
    raw = exchange._read(numbered[number])
    state = decoded_json(raw)
    require(isinstance(state, dict) and state.get("schema") == 1, "checkpoint schema")
    return dict(path=str(numbered[number]), sha256=exchange.digest(raw), number=number, state=state)


def _idle(state):
    """No active owner, caller, CPU owner, reservation or current index; released."""
    require(all(state.get(k) is None for k in ("active_gpu_owner", "active_cpu_owner", "active_caller",
                                                 "active_owner", "reservation", "current_global_attempt_index")),
            "checkpoint shows active or reserved work; owner busy")
    require(state.get("independent_release_verified") is True or state.get("independent_gpu_release") is True,
            "previous work is not independently released")
    last = state.get("last_completed_global_attempt_index")
    require(type(last) is int and last >= 0, "completed attempt index required")
    for name in ("cumulative_owner_seconds", "cumulative_caller_seconds"):
        require(type(state.get(name)) in (int, float) and state[name] >= 0, "cumulative accounting required")
    return last + 1, state["cumulative_owner_seconds"], state["cumulative_caller_seconds"]


def prepare(snapshot_root, bound, *, destination, owner_inputs, support_inputs, commands_module=None,
            sealer_path=SEALER, now=None):
    """Build, verify and seal a fresh inert owner packet from the private snapshot.

    No registration, checkpoint, GPU or native work. The record is written beside
    the sealed preparation root as <root>.owner-preparation.json.
    """
    started = _clock(now)
    record = dict(schema=1, status="PREPARING", stage="snapshot", reason=None, launch_authorized=False,
                  registered=False, gpu_work=False, native_action=False, adoption_authorized=False,
                  qualification=False, utc=_utc(started))
    try:
        _bound(bound)
        snapshot = _snapshot(snapshot_root, bound)
        kind, request, row = snapshot["kind"], snapshot["request"], snapshot["source"]
        admission_end = min(request["created_unix_ms"] / 1000 + exchange.ADMISSION_SECONDS,
                            request["expires_unix_ms"] / 1000 - CALLER_SECONDS - POST_RELEASE_SECONDS,
                            bound["campaign_deadline_unix_ms"] / 1000 - CALLER_SECONDS - POST_RELEASE_SECONDS)
        deadline = time.monotonic() + admission_end - _clock(now)
        _work_budget(deadline, BUILD_SECONDS, "preparation", reserve=5)
        record.update(kind=kind, request_sha256=snapshot["request_sha256"], source=dict(row),
                      snapshot_root=str(snapshot["root"]))
        root = _fresh(destination, "preparation root")
        record.update(stage="owner-build", preparation_root=str(root))
        template_path, template = _pinned(owner_inputs, "owner build inputs")
        require(template.get("schema") == 1 and template.get("kind", "serving") == kind
                and template.get("inert_preview") is True, "owner build input template kind mismatch")
        require(template.get("source_family", "extend_attention") == snapshot["family"],
                "owner source family differs from the explicit binding")
        runtime_configuration = _runtime_configuration(bound, template)
        root.mkdir(mode=0o700)
        build = root / "build"
        build.mkdir(mode=0o700)
        runner = commands_module if commands_module is not None else commands(
            R2 / "mxfp4-quality-gate-p3-preparation-v1/caller-preview/local_commands.py", build)
        inputs = dict(template, candidate=str(snapshot["source_path"]), candidate_sha256=row["sha256"])
        owner_pin = _write(build / "OWNER-BUILD-INPUTS.json", inputs)
        author = root / "author"
        built, owner_receipt = _builder(runner, build, "owner-build", "build_owner.py",
                                        ["--inputs", owner_pin["path"], "--inputs-sha256", owner_pin["sha256"],
                                         "--output", str(author)], deadline)
        require(built.get("status") == "INERT_HYPERLOOM_SERVING_PACKET", "builder did not produce an inert packet")
        record["stage"] = "support-build"
        ids_raw = exchange._read(author / "PACKAGE-IDENTITIES.json")
        ids = decoded_json(ids_raw)
        count = REQUESTS[kind]
        nonce = ids.get("nonce")
        require(isinstance(nonce, str) and NONCE.fullmatch(nonce) and built.get("nonce") == nonce
                and ids.get("status") == "INERT_HYPERLOOM_SERVING_PACKET" and ids.get("kind") == kind
                and ids.get("launch_authorized") is False and ids.get("qualification") is False
                and ids.get("adoption_authorized") is False and ids.get("automatic_retry") is False
                and ids.get("selected") is False and ids.get("physical_ready") is False
                and ids.get("candidate_sha256") == row["sha256"]
                and ids.get("baseline_sha256") == snapshot["baseline_sha256"]
                and ids.get("source_family", "extend_attention") == snapshot["family"]
                and ids.get("requests_per_start") == count and ids.get("starts") == MODEL_STARTS
                and ids.get("owner_seconds") == OWNER_SECONDS and ids.get("caller_seconds") == CALLER_SECONDS
                and ids.get("diagnostic_only") is (kind == "profile")
                and ids.get("target_root") == "/home/developer/fp8-kv-benchmark." + nonce
                and all(exchange.SHA.fullmatch(str(ids.get(k))) for k in PIN_FIELDS),
                "fresh packet identities differ from the bounded owner contract")
        deviation = ids.get("runtime_config_deviation")
        require((deviation is not None) == (runtime_configuration is not None), "undeclared runtime configuration change")
        if runtime_configuration is not None:
            require(deviation.get("option") == "--mamba-max-states-per-path" and type(deviation.get("value")) is int
                    and deviation["value"] == 3 and deviation.get("selected_native_default") == -1
                    and deviation.get("selected_runtime_behavior_unchanged") is False
                    and deviation.get("adopted_runtime") is False, "packet runtime deviation differs")
            for arm in ("control", "mtp"):
                name = "HTTP-BINDING-" + arm + ".json"
                actual = checked_json(author / "package-v2/review" / name,
                    deviation["bindings"][arm]["sha256"], 1 << 20)["binding_template"]
                require(runtime_configuration["serving_arms"][arm]
                        == {key: actual[key] for key in ("canonical_server_cli", "resolved_contract")},
                        "generated serving settings differ from the experimental runtime identity")
        workload = _json(author / "WORKLOAD.json")
        require(exchange.digest(exchange.canonical(workload)) == ids["workload_sha256"]
                and workload.get("launch_authorized") is False and workload.get("requests") == count,
                "packet workload identity")
        if kind == "serving":
            require(ids["workload_sha256"] == bound["workload_sha256"], "prepared workload differs from the bound workload")
        support_template_path, support_template = _pinned(support_inputs, "support build inputs")
        require(support_template.get("observed_power_cap_microwatts", 235000000)
                == bound.get("observed_power_cap_microwatts", 235000000), "power observation binding differs")
        support_inputs_value = dict(support_template,
                                    manifest_sha256=exchange.digest(exchange._read(author / "package-v2/MANIFEST.json")),
                                    identities_sha256=exchange.digest(ids_raw))
        support_pin = _write(build / "SUPPORT-BUILD-INPUTS.json", support_inputs_value)
        support, support_receipt = _builder(runner, build, "support-build", "build_support.py",
                                            ["--inputs", support_pin["path"], "--inputs-sha256", support_pin["sha256"],
                                             "--root", str(root)], deadline)
        record["stage"] = "verify"
        require(support.get("status") == "INERT_OWNER_CALLER_AND_GRADING_TEMPLATE"
                and support.get("execution_authorized") is False and support.get("adoption_authorized") is False
                and support.get("automatic_retry") is False and support.get("natural_grades") == 2 * count
                and support.get("grading_seconds") == GRADING_SECONDS and support.get("owner_seconds") == OWNER_SECONDS
                and support.get("caller_seconds") == CALLER_SECONDS, "support derivation contract")
        require(_json(root / "SUPPORT-RESULT.json") == support, "support result receipt differs")
        plan_raw = checked_bytes(root / "launcher/CALLER-PLAN.json", support["caller_plan_sha256"], 1 << 20)
        plan = decoded_json(plan_raw)
        expected_limits = dict(LIMITS, requests=2 * count, natural_grades=2 * count, native_decodes=2 * count)
        require(plan.get("limits") == expected_limits and plan.get("label") == "hyperloom-" + kind
                and plan.get("registration") is False and plan.get("physical_operation_authority") is False
                and plan.get("status") == "PREPARED_NOT_REGISTERED_OR_RUN" and plan.get("nonce") == nonce
                and plan.get("expected_package_pins") == {k: ids[k] for k in PIN_FIELDS}
                and plan.get("source_root") == str(author) and plan.get("quality_template") == str(root / "quality-template-c4ab")
                and plan.get("caller_root") == str(root / "launcher/caller-preview"),
                "finite caller plan differs from the registered limits")
        inventory = plan["caller_inventory"]
        for name, pin in inventory.items():
            checked_bytes(root / "launcher/caller-preview" / name, pin, 2 << 20)
        require(inventory["local_commands.py"] == COMMANDS_SHA and inventory["seal_evidence.py"] == SEALER_SHA,
                "finite helper or sealer pin changed")
        caller_raw = checked_bytes(root / "launcher/execute_once.py", support["caller_sha256"], 1 << 20)
        require(("LAUNCH_SECONDS = %d\n" % LAUNCH_SECONDS).encode() in caller_raw
                and ("PREP = Path(" + repr(str(root / "launcher")) + ")").encode() in caller_raw
                and REGISTERED_STATUS.encode() in caller_raw and b"MX = R2.parent" in caller_raw
                and b"def admit(registration_sha256):" in caller_raw, "derived caller does not bind this preparation")
        bindings = _json(author / "LAUNCHER-BINDINGS.json")
        require(bindings.get("nonce") == nonce and bindings.get("launch_authorized") is False
                and bindings.get("executed") is False and bindings.get("uploaded") is False
                and bindings.get("qualification") is False and bindings.get("retry_authorized") is False
                and bindings.get("target_root") == ids["target_root"]
                and bindings.get("protected_root") == "/var/tmp/fp8-benchmark-source." + nonce
                and bindings.get("archive_sha256") == ids["archive_sha256"]
                and bindings.get("manifest_sha256") == ids["manifest_sha256"], "launcher bindings differ from the packet")
        for name, pin in bindings["upload_inventory"].items():
            checked_bytes(author / "owner-upload" / name, pin, 32 << 20)
        template = root / "quality-template-c4ab"
        inventory_sha = support["quality_inventory_sha256"]
        template_inventory = decoded_json(checked_bytes(template / "PROJECTION-INVENTORY.json", inventory_sha, 1 << 20))
        require(template_inventory.get("rows") == 2 * count and template_inventory.get("native_execution") is False
                and template_inventory.get("numerical_selection") is False
                and str(template_inventory.get("authority", "")).startswith("Inert Hyperloom"), "grading template authority")
        binder = _source(template / "bind_future.py", template_inventory["files"]["bind_future.py"]["sha256"],
                         "_hyperloom_owner_operation_template_check_" + nonce)
        binder.authenticate_projection(template, inventory_sha)
        power = _json(root / "POWER-CAP.json")
        cap = bound.get("observed_power_cap_microwatts", 235000000)
        require(type(cap) is int and cap in (235000000, 300000000)
                and power.get("watts") == cap // 1000000 and power.get("power_microwatts") == cap,
                "fixed observed power cap required; no power-setting changes")
        record["stage"] = "seal"
        binding_doc = dict(schema=1, kind=kind, evaluable_surface=("extend_attention_fwd"
                           if snapshot["family"] == "extend_attention" else "verify_prefetch"),
                           source_file=row["name"], source=dict(row), candidate_sha256=row["sha256"],
                           baseline_sha256=snapshot["baseline_sha256"], changed_functions=ids["changed_functions"], nonce=nonce,
                           request=request, request_sha256=snapshot["request_sha256"], config_sha256=request["config_sha256"],
                           claim=snapshot["claim"], source_set_sha256=request["source_set_sha256"],
                           campaign_sha256=bound["campaign_sha256"], selection_sha256=bound["selection_sha256"],
                           controller_sha256=bound["controller_sha256"], runtime_sha256=bound["runtime_sha256"],
                           selected_source_set_sha256=bound["selected_source_set_sha256"],
                           workload_sha256=ids["workload_sha256"], fixed_benchmark=bound["benchmark"],
                           package=dict(ids), caller_sha256=support["caller_sha256"],
                           caller_plan_sha256=support["caller_plan_sha256"], quality_inventory_sha256=inventory_sha,
                           model_precision_context_flags_power_overlays_unchanged=runtime_configuration is None,
                           launch_authorized=False, adoption_authorized=False, qualification=False)
        if runtime_configuration is not None:
            binding_doc.update(runtime_configuration=dict(bound["runtime_configuration"]),
                               runtime_config_deviation=dict(deviation))
        source_binding = _write(root / "SOURCE-BINDING.json", binding_doc)
        seals_module = sealer(sealer_path)
        author_seal = seals_module.seal(author)
        preparation_seal = seals_module.seal(root)
        for seal in (author_seal, preparation_seal):
            seal.pop("verified_utc", None)
            seal.pop("qualification", None)
        require(time.monotonic() < deadline, "preparation exceeded the admission budget")
        record.update(status="PREPARED_NOT_REGISTERED_OR_RUN", stage="complete", nonce=nonce,
                      author_root=str(author), seals=dict(author=author_seal, preparation=preparation_seal),
                      source_binding=source_binding, package=dict(ids), support=support, limits=expected_limits,
                      caller=dict(path=str(root / "launcher/execute_once.py"), sha256=support["caller_sha256"]),
                      caller_plan=dict(path=str(root / "launcher/CALLER-PLAN.json"), sha256=support["caller_plan_sha256"]),
                      quality_template=dict(root=str(template), inventory_sha256=inventory_sha),
                      workload=dict(path=str(author / "WORKLOAD.json"), sha256=ids["workload_sha256"]),
                      launcher_bindings=dict(path=str(author / "LAUNCHER-BINDINGS.json"),
                                             sha256=exchange.digest(exchange._read(author / "LAUNCHER-BINDINGS.json"))),
                      inputs=dict(owner=dict(template=dict(path=str(template_path), sha256=owner_inputs["sha256"]),
                                             derived=owner_pin),
                                  support=dict(template=dict(path=str(support_template_path), sha256=support_inputs["sha256"]),
                                               derived=support_pin)),
                      receipts=dict(owner_build=owner_receipt, support_build=support_receipt),
                      binding_identities={k: bound[k] for k in ("campaign_sha256", "selection_sha256", "controller_sha256",
                                                               "workload_sha256", "runtime_sha256",
                                                               "selected_source_set_sha256")})
        record["record"] = _write(root.with_name(root.name + ".owner-preparation.json"), record)
    except Unsupported as error:
        record.update(status="UNSUPPORTED", reason=_error(error))
    except REFUSALS as error:
        record.update(status="REFUSED", reason=_error(error))
    return record


def _verified_preparation(preparation, bound):
    path, record = _pinned(preparation, "preparation record")
    require(record.get("schema") == 1 and record.get("status") == "PREPARED_NOT_REGISTERED_OR_RUN"
            and record.get("launch_authorized") is False and record.get("registered") is False
            and record.get("kind") in REQUESTS, "prepared, unregistered owner packet required")
    require(record["binding_identities"] == {k: bound[k] for k in record["binding_identities"]},
            "preparation belongs to another campaign binding")
    root, author = Path(record["preparation_root"]), Path(record["author_root"])
    seals_module = sealer()
    seals = record["seals"]
    require(author.parent == root and seals["author"]["root"] == str(author) and seals["preparation"]["root"] == str(root),
            "sealed roots differ from the record")
    require(seals_module.verify(author, seals["author"]["manifest_sha256"]) == seals["author"]
            and seals_module.verify(root, seals["preparation"]["manifest_sha256"]) == seals["preparation"],
            "sealed source/preparation roots changed")
    binding_path, binding_doc = _pinned(record["source_binding"], "source binding")
    require(binding_path.parent == root and binding_doc["request_sha256"] == record["request_sha256"]
            and binding_doc["nonce"] == record["nonce"] and binding_doc["kind"] == record["kind"]
            and exchange.digest(exchange.canonical(binding_doc["request"])) == record["request_sha256"],
            "source binding does not match the preparation")
    checked_bytes(Path(record["caller"]["path"]), record["caller"]["sha256"], 1 << 20)
    plan = checked_json(Path(record["caller_plan"]["path"]), record["caller_plan"]["sha256"], 1 << 20)
    return dict(path=path, record=record, root=root, author=author, binding=binding_doc, plan=plan)


def propose(preparation, bound, *, campaign_parent, checkpoint_root, mx_root=MX, now=None):
    """Create the campaign directory and the exact inactive registration proposal.

    Every pin comes from the sealed fresh preparation. Nothing is registered; the
    native run root is only named. mx_root must be the real checkpoint tree for a
    live operation because the derived caller admits run roots under it only.
    """
    started = _clock(now)
    record = dict(schema=1, status="PROPOSING", stage="preparation", reason=None, launch_authorized=False,
                  registered=False, gpu_work=False, native_action=False, adoption_authorized=False,
                  qualification=False, utc=_utc(started))
    try:
        _bound(bound)
        verified = _verified_preparation(preparation, bound)
        prep, root, author, plan = verified["record"], verified["root"], verified["author"], verified["plan"]
        kind, nonce, ids, count = prep["kind"], prep["nonce"], prep["package"], REQUESTS[prep["kind"]]
        record.update(kind=kind, nonce=nonce, preparation=dict(preparation))
        record["stage"] = "checkpoint"
        checkpoint = _checkpoint(checkpoint_root)
        index, owner0, caller0 = _idle(checkpoint["state"])
        record["stage"] = "campaign"
        parent = _canonical_dir(campaign_parent, "campaign parent")
        mx = _canonical_dir(mx_root, "run root parent")
        campaign = parent / ("hyperloom-" + kind + "-campaign-" + nonce)
        native_root = mx / ("hyperloom-" + kind + "-run-" + nonce)
        require(not native_root.exists() and not native_root.is_symlink(), "native run root already exists")
        campaign.mkdir(mode=0o700)
        record["campaign_root"] = str(campaign)
        caller_raw = checked_bytes(Path(prep["caller"]["path"]), prep["caller"]["sha256"], 1 << 20)
        with (campaign / "execute_once.py").open("xb") as stream:
            stream.write(caller_raw)
        execute_once_sha256 = exchange.digest(exchange._read(campaign / "execute_once.py", 1 << 20))
        require(execute_once_sha256 == prep["caller"]["sha256"], "campaign caller copy differs")
        delta = _json(author / "PACKET-DELTA.json")
        power = _json(root / "POWER-CAP.json")
        donor = checked_json(DONOR_PROPOSAL, DONOR_PROPOSAL_SHA)
        template = root / "quality-template-c4ab"
        runtime_scope = ("Fixed selected stack in both starts: model, precision, context capacity, flags, "
                         "power cap and selected overlays unchanged. ")
        if ids.get("runtime_config_deviation") is not None:
            runtime_scope = ("Explicit unadopted configuration experiment in both starts: "
                             "--mamba-max-states-per-path 3 replaces the selected native default -1. "
                             "Model, precision, context capacity, power cap and all other flags remain fixed. ")
        numerical_contract = (runtime_scope + "Start 1 serves the immutable selected C4 "
            "extend attention (" + C4_SHA[:16] + "); start 2 the source-bound Hyperloom candidate ("
            + ids["candidate_sha256"][:16] + ") whose AST scope is limited to the _fwd_kernel and "
            "extend_attention_fwd bodies; correct within tolerance, not bitwise. No precision "
            "acceptance, selection or adoption.")
        if ids.get("source_family") == "verify_prefetch":
            numerical_contract = (runtime_scope + "Selected C4 extend attention in both starts. "
                "Start 1 uses native verification attention (" + ids["baseline_sha256"] + "); start 2 "
                "uses the operation-211 prefetch source (" + ids["candidate_sha256"] + ") only in the "
                "existing eligible BF16 verification adapter. Ordinary decode, prefill and fallbacks "
                "retain their selected paths. Component bitwise equality passed; serving quality, "
                "equal-work and latency gates remain required. No adoption or selection change.")
        proposal = dict(donor)
        proposal.update(
            label="hyperloom-" + kind, global_gpu_attempt_index=index, previous_global_attempt_index=index - 1,
            nonce=nonce, native_root=str(native_root), source_root=str(author), source_seal=dict(prep["seals"]["author"]),
            seals={str(root): prep["seals"]["preparation"]["manifest_sha256"],
                   str(author): prep["seals"]["author"]["manifest_sha256"]},
            caller_plan_sha256=prep["caller_plan"]["sha256"], execute_once_sha256=execute_once_sha256,
            launcher_bindings_sha256=prep["launcher_bindings"]["sha256"],
            launcher_sha256=exchange.digest(exchange._read(author / "owner-upload/RUN-ON-WORKSTATION.sh", 1 << 20)),
            protected_controller_diff={name: row for name, row in delta.items() if row.get("protected")},
            numerical_contract=numerical_contract,
            quality_template=str(template), quality_inventory_sha256=prep["quality_template"]["inventory_sha256"],
            final_native_cpu_quality=("%d complete wire decodes and natural-answer checks after independent GPU release; "
                                      "320-second CPU envelope (Hyperloom owner grading template derived from the C4 A/B one)"
                                      % (2 * count)),
            generations=2 * count, owner_seconds=OWNER_SECONDS, caller_total_ceiling_seconds=CALLER_SECONDS,
            worker_seconds=WORKER_SECONDS, client_seconds=CLIENT_SECONDS,
            caller_step_seconds=dict(donor["caller_step_seconds"], launch=LAUNCH_SECONDS + 120),
            local_four_phase_seconds=OWNER_SECONDS + 345,
            prior_model_scope_owner_seconds_charged=owner0, prior_model_scope_caller_seconds_charged=caller0,
            maximum_cumulative_model_scope_owner_seconds=round(owner0 + OWNER_SECONDS, 1),
            cumulative_model_scope_caller_ceiling_seconds=round(caller0 + CALLER_SECONDS, 1),
            power_wake=("Existing bounded render-node open during pre/post read only; both caps %d W (POWER-CAP.json); "
                        "no setting changes" % power["watts"]),
            schedule_prerequisite=("Supplied source-bound campaign authorization record naming this proposal, request and "
                                   "source set; previous operation independently released (idle checkpoint); cancellation "
                                   "and remaining caller plus post-release budget rechecked immediately before invocation."),
            scope="hyperloom-sglang-owner-" + kind + "-v1",
            experiment_contract=dict(prep["source_binding"]),
            schedule_order=1, schedule_total_operations=1, schedule_total_owner_seconds=OWNER_SECONDS,
            schedule_total_caller_seconds=CALLER_SECONDS, schedule_total_cpu_grading_seconds=GRADING_SECONDS,
            schedule_total_model_starts=MODEL_STARTS, schedule_total_requests=2 * count,
            replaces_failed_schedule=False, supersedes_unconsumed_campaigns=[], new_precision_acceptance=False,
            model_starts=MODEL_STARTS, model_starts_allowed=MODEL_STARTS, owner_invocations_allowed=1,
            maximum_invocations=1, no_automatic_retry=True, no_automatic_second_order=True,
            deployment_authorized=False, promotion_authorized=False, precision_accepted=False, qualification=False,
            current_locked_admission_required=True, park_after=True, actual_gpu="NOT RUN",
            registered=False, proposal=True, gpu_allowance=False, reservation=False, owner_answer=None,
            approval_record=None, registration_authority=None, registered_utc=None, status=PROPOSED_STATUS)
        for key in PIN_FIELDS:
            proposal[key] = ids[key]
        require(set(proposal) == set(donor), "proposal field shape differs from the donor registration lineage")
        proposal_pin = _dump(campaign / "REGISTRATION-PROPOSED.json", proposal)
        record.update(status="PROPOSED_NOT_REGISTERED_OR_RUN", stage="complete", index=index,
                      previous_index=index - 1, native_root=str(native_root),
                      proposal=proposal_pin, execute_once=dict(path=str(campaign / "execute_once.py"), sha256=execute_once_sha256),
                      checkpoint={k: checkpoint[k] for k in ("path", "sha256", "number")},
                      cumulative=dict(owner_seconds=owner0, caller_seconds=caller0), limits=dict(prep["limits"]),
                      caller_work_seconds=CALLER_WORK_SECONDS, post_release_seconds=POST_RELEASE_SECONDS,
                      authorization_contract=list(AUTHORIZATION_REQUIRED),
                      registration_status_required=REGISTERED_STATUS)
        record["record"] = _write(campaign / "OWNER-PROPOSAL.json", record)
    except REFUSALS as error:
        record.update(status="REFUSED", reason=_error(error))
    return record


def _authorization(authorization, prop, verified, bound):
    """A supplied source-bound owner approval; never synthesized here."""
    path, record = _pinned(authorization, "campaign authorization")
    require(all(name in record for name in AUTHORIZATION_REQUIRED), "incomplete campaign authorization record")
    binding = verified["binding"]
    require(record["schema"] == 1 and record["kind"] == prop["kind"] and record["campaign"] == prop["campaign_root"]
            and record["proposal_sha256"] == prop["proposal"]["sha256"]
            and record["execute_once_sha256"] == prop["execute_once"]["sha256"] and record["nonce"] == prop["nonce"]
            and record["global_gpu_attempt_index"] == prop["index"]
            and record["request_sha256"] == binding["request_sha256"]
            and record["source_set_sha256"] == binding["source_set_sha256"]
            and record["candidate_sha256"] == binding["candidate_sha256"]
            and record["campaign_sha256"] == bound["campaign_sha256"]
            and record["controller_sha256"] == bound["controller_sha256"], "authorization does not bind this proposal and source")
    require(isinstance(record["owner"], str) and record["owner"].strip()
            and isinstance(record["owner_answer"], str) and record["owner_answer"].strip()
            and isinstance(record["utc"], str), "explicit owner answer required")
    approval = record["approval"]
    require(isinstance(approval, dict) and set(approval) == {"path", "sha256"}, "approval pointer required")
    approval_path = Path(approval["path"])
    require(approval_path.is_absolute() and approval_path.resolve() == approval_path, "canonical approval path")
    checked_bytes(approval_path, approval["sha256"], 1 << 20)
    require(record["no_retry"] is True and record["one_invocation"] is True and record["deployment"] is False
            and record["promotion"] is False and record["publication"] is False
            and record["adoption_authorized"] is False and record["qualification"] is False,
            "authorization boundaries differ from the single finite operation")
    return dict(path=str(path), sha256=authorization["sha256"], record=record)


def _registration(proposal, auth, now):
    grant = dict(proposal)
    pin = dict(path=auth["path"], sha256=auth["sha256"])
    grant.update(registered=True, proposal=False, gpu_allowance=True, owner_answer=auth["record"]["owner_answer"],
                 approval_record=pin, registration_authority=pin, registered_utc=_utc(now), status=REGISTERED_STATUS)
    require(set(grant) == set(proposal) and all(grant[k] == proposal[k] for k in proposal if k not in VARIABLE),
            "registration differs from the proposed exact operation")
    return grant


def _checkpoint_record(previous, now, **fields):
    state = previous["state"]
    value = dict(schema=1, utc=_utc(now), previous_checkpoint=previous["path"], previous_checkpoint_sha256=previous["sha256"],
                 deployed=False, selected=False, reservation=None, active_gpu_owner=None, active_cpu_owner=None,
                 cumulative_owner_seconds=state["cumulative_owner_seconds"],
                 cumulative_caller_seconds=state["cumulative_caller_seconds"],
                 remaining_gpu_allowance=state.get("remaining_gpu_allowance"),
                 independent_implementation_review=False, retry_authorized=False)
    value.update(fields)
    return value


def _write_checkpoint(root, number, value):
    path = Path(root) / ("CHECKPOINT-%d.json" % number)
    pin = _dump(path, value)
    return dict(pin, number=number)


def _release(run_root, bindings, index, stdout):
    """Observed GPU release products; still not release until the replay joins them."""
    summary = _json(run_root / "EXECUTION-SUMMARY.json")
    require(summary.get("status") == "COMPLETE" and summary.get("physical_success") is True
            and summary.get("global_operation") == index and summary.get("automatic_retry") is False
            and summary.get("qualification") is False and summary.get("run_root") == str(run_root),
            "caller summary is not a complete successful operation")
    operation_seal = exchange.digest(exchange._read(run_root / "FINAL-ARTIFACTS.sha256", 4 << 20))
    closeout = _json(run_root.with_name(run_root.name + ".closeout.json"))
    require(closeout.get("manifest_sha256") == operation_seal and closeout.get("root") == str(run_root),
            "operation seal differs from its closeout")
    lines = [line for line in stdout.splitlines() if line.strip()]
    require(lines and decoded_json(lines[-1]) == dict(status="COMPLETE", seal=operation_seal),
            "caller receipt does not name the sealed operation")
    workload = run_root / "collected/workload" / Path(bindings["target_root"]).name
    protected = run_root / "collected/protected" / Path(bindings["protected_root"]).name
    workload_seal = _json(workload.with_name(workload.name + ".closeout.json"))
    protected_close = _json(protected.with_name(protected.name + ".closeout.json"))
    # Both closeouts were written on the workstation and name the remote roots.
    require(workload_seal.get("root") == bindings["target_root"] and workload_seal.get("qualification") is False
            and protected_close.get("root") == bindings["protected_root"]
            and protected_close.get("workload_evidence_root") == bindings["target_root"]
            and protected_close.get("physical_success") is True and protected_close.get("qualification") is False,
            "collected workload/protected closeouts incomplete or foreign")
    for seal in (operation_seal, workload_seal.get("manifest_sha256"), protected_close.get("manifest_sha256")):
        exchange._identity(seal, "retained seal")
    launch = [s for s in summary.get("steps", []) if isinstance(s, dict) and s.get("name") == "launch"]
    return dict(operation_seal_sha256=operation_seal, workload_seal_sha256=workload_seal["manifest_sha256"],
                protected_seal_sha256=protected_close["manifest_sha256"],
                launch_elapsed_seconds=launch[0].get("elapsed_seconds") if len(launch) == 1 else None)


def _template_binder(template_root, inventory_sha256):
    template = _canonical_dir(template_root, "quality template")
    inventory = decoded_json(checked_bytes(template / "PROJECTION-INVENTORY.json", inventory_sha256, 1 << 20))
    return _source(template / "bind_future.py", inventory["files"]["bind_future.py"]["sha256"],
                   "_hyperloom_owner_operation_binder_" + template.parent.name)


def _failed_campaign_release(reader, workload, protected, bindings, manifest):
    """Replay the existing observer predicates for a failed, closed campaign.

    This accepts failed workload supervision only at this boundary. The original
    release_phase still requires successful root observers and exact process,
    namespace, device, allocation and cgroup absence. No HTTP result is graded.
    """
    evidence = protected / "evidence"
    report = reader.read(workload / "results/CONTROLLER-RESULT.json")
    campaign = report["campaign"]
    require(report["status"] == "INCOMPLETE" and report["qualification"] is False
            and campaign["adapter_complete"] is False and campaign["physical_campaign_complete"] is False
            and campaign["independent_release_verified"] is True
            and campaign["unresolved"] is False and campaign["cleanup_errors"] == []
            and campaign["qualification"] is False, "failed campaign has no independent release")
    grant, receipt = reader.read(evidence / "GRANT.json"), reader.read(evidence / "RECEIPT.json")
    require(receipt["grant_sha256"] == reader.sha(reader.held(evidence / "GRANT.json"))
            and receipt["archive_sha256"] == bindings["archive_sha256"]
            and receipt["active_deadline_monotonic"] == grant["received_monotonic"] + OWNER_SECONDS - 300
            and receipt["seal_deadline_monotonic"] == grant["received_monotonic"] + OWNER_SECONDS
            and receipt["root_performs_gpu_work"] is False, "failed owner grant receipt differs")
    transport = reader.read(evidence / "ROOT-TRANSPORT.json")
    require(transport["physical_success"] is False and type(transport["controller_exit"]) is int
            and transport["controller_exit"] == 1 and transport["root_performed_gpu_work"] is False
            and all(transport[k] == [] for k in ("errors", "survivors", "owner_signals", "escalation_receipts")),
            "failed controller transport cleanup is uncertain")
    remote = Path(bindings["protected_root"]) / "evidence/controller-capture"
    reader.capture(transport["controller_capture"], lambda ch: evidence / "controller-capture" / (ch + ".raw"),
                   lambda ch: remote / (ch + ".raw"))
    identity = reader.read(evidence / "CONTROLLER-IDENTITY.json")
    observed = [row for row in transport["observed_processes"]
                if all(row[k] == identity[k] for k in ("boot_id", "pid", "start_ticks"))]
    require(len(observed) == 1 and identity["uid"] == identity["gid"] == 1000
            and identity["ppid"] == grant["transport_parent"]["pid"] and identity["boot_id"] == manifest["boot_id"]
            and all(observed[0][k] == identity[k] for k in ("uid", "gid", "ppid", "pgid", "sid", "pid_namespace")),
            "failed controller identity is foreign")
    arms = campaign["arms"]
    names = [row["arm"] for row in arms]
    require(names in (["bf16"], ["bf16", "kv4"]) and all(row["release_verified"] is True for row in arms),
            "failed operation has missing arm release")
    requests = report["observer_requests"]
    require([(q["mode"], q["arm"]) for q in requests]
            == [("locked-admission", None)] + [("release", arm) for arm in names] + [("independent", None)]
            and len({(q["lock_device"], q["lock_inode"]) for q in requests}) == 1
            and all(q["deadline_monotonic"] <= receipt["seal_deadline_monotonic"] for q in requests),
            "failed operation observer schedule or deadline differs")
    with reader.modules(workload / "review") as controller:
        controller.validate_physical_grant(grant, manifest, bindings["manifest_sha256"], grant["transport_parent"],
            clock=lambda: grant["received_monotonic"],
            utc_now=lambda: datetime.fromisoformat(grant["received_utc"].replace("Z", "+00:00")))
        owner, results = controller.adapter.owner, {}
        for row in arms:
            value = row["supervision"]
            result = owner.SupervisionResult(root_process=owner.ProcessIdentity(**value["root_process"]),
                observed_processes=tuple(owner.ProcessIdentity(**p) for p in value["observed_processes"]),
                **{k: tuple(v) if k in {"cleanup_errors", "survivors", "escalation_receipts"} else v
                   for k, v in value.items() if k not in {"root_process", "observed_processes"}})
            require(type(result.exit_code) is int and result.child_reaped is True
                    and not result.cleanup_errors and not result.survivors and not result.escalation_receipts
                    and result.owner_signal is None and result.process_cleanup_observed,
                    "failed workload process cleanup is uncertain")
            results[row["arm"]] = result
        return [reader.release_phase(controller, campaign, results, q, evidence, protected, bindings, manifest)
                for q in requests]


def _failed_release(run_root, verified, index, registration, caller_receipt, stdout):
    """Authenticate retained failure closeout; never produce quality inputs."""
    require(type(caller_receipt.get("exit_code")) is int and caller_receipt["exit_code"] == 1
            and caller_receipt.get("child_reaped") is True
            and caller_receipt.get("owned_group_absent") is True and caller_receipt.get("signals", []) == [],
            "failed caller did not exit and reap naturally")
    prep = verified["record"]
    bindings = checked_json(Path(prep["launcher_bindings"]["path"]), prep["launcher_bindings"]["sha256"], 1 << 20)
    template = Path(prep["quality_template"]["root"])
    inventory = checked_json(template / "PROJECTION-INVENTORY.json",
                             prep["quality_template"]["inventory_sha256"], 1 << 20)
    reader = _source(template / "retained_replay.py", inventory["files"]["retained_replay.py"]["sha256"],
                     "_hyperloom_failed_release_" + prep["nonce"])
    summary = _json(run_root / "EXECUTION-SUMMARY.json")
    require(summary.get("status") == "INCOMPLETE_RETAINED" and summary.get("physical_success") is False
            and summary.get("global_operation") == index and summary.get("run_root") == str(run_root)
            and all(summary.get(k) is False for k in ("automatic_retry", "qualification", "selection")),
            "failed caller summary is foreign or incomplete")
    steps = summary["steps"]
    require([s["name"] for s in steps] == ["power-pre", "upload", "launch", "host-sample", "collect", "closeout", "power-post", "host-faults"]
            and all(type(s["exit_code"]) is int and s["exit_code"] == (1 if s["name"] == "launch" else 0) for s in steps),
            "failed operation collection/closeout did not complete")
    operation_pin = reader.sha(reader.held(run_root / "FINAL-ARTIFACTS.sha256", maximum=4 << 20))
    seals = {"operation": sealer().verify(run_root, operation_pin)}
    closeout = _json(run_root.with_name(run_root.name + ".closeout.json"))
    lines = [line for line in stdout.splitlines() if line.strip()]
    require(closeout["root"] == str(run_root) and closeout["manifest_sha256"] == operation_pin
            and lines and decoded_json(lines[-1]) == dict(status="INCOMPLETE_RETAINED", seal=operation_pin),
            "failed caller receipt does not bind the operation seal")
    require(reader.read(run_root / "LAUNCHER-BINDINGS.json") == bindings, "failed operation launcher differs")
    actual_registration = checked_json(run_root / "REGISTRATION.json", registration["sha256"], 1 << 20)
    authorization = reader.read(run_root / "RUN-AUTHORIZATION.json")
    require(authorization.pop("registration_sha256") == registration["sha256"] and authorization == actual_registration
            and actual_registration["global_gpu_attempt_index"] == index
            and actual_registration["source_root"] == str(verified["author"])
            and actual_registration["owner_invocations_allowed"] == 1 and actual_registration["model_starts_allowed"] == 2
            and actual_registration["no_automatic_retry"] is True and actual_registration["promotion_authorized"] is False,
            "failed operation registration differs")
    workload = run_root / "collected/workload" / Path(bindings["target_root"]).name
    protected = run_root / "collected/protected" / Path(bindings["protected_root"]).name
    native_closeouts = {}
    for name, path, remote_root in (("workload", workload, bindings["target_root"]),
                                    ("protected", protected, bindings["protected_root"])):
        value = reader.read(path.with_name(path.name + ".closeout.json"))
        seals[name] = sealer().verify(path, value["manifest_sha256"])
        require(value["root"] == remote_root and value["qualification"] is False
                and value["member_count"] == seals[name]["members"], "failed native closeout is foreign")
        native_closeouts[name] = value
    for name, code in (("native-launch", 1), ("collect-native-results", 0), ("observe-root-failed-closeout", 0)):
        value = reader.read(run_root / (name + ".json"))
        require(type(value["exit_code"]) is int and value["exit_code"] == code
                and value["child_reaped"] is value["owned_group_absent"] is True
                and value.get("failure") is None and value.get("signals", []) == [], "failed local transport cleanup uncertain")
        for channel in ("stdout", "stderr"):
            raw = reader.held(run_root / (name + "." + channel), value[channel]["sha256"])
            require(len(raw) == value[channel]["bytes"], "failed transport stream extent differs")
    observed = reader.read(run_root / "observe-root-failed-closeout.stdout")
    protected_raw = reader.held(protected.with_name(protected.name + ".closeout.json"), observed["sha256"], 1 << 20)
    require(observed["value"] == reader.strict(protected_raw) == native_closeouts["protected"]
            and observed["uid"] == observed["gid"] == 0 and observed["mode"] == 0o444
            and observed["path"] == bindings["protected_root"] + ".closeout.json"
            and observed["value"]["physical_success"] is False
            and observed["value"]["workload_evidence_root"] == bindings["target_root"],
            "failed protected closeout was not observed as root-owned")
    expected = verified["plan"]["expected_package_pins"]
    package_root = verified["author"] / "package-v2"
    manifest_raw = reader.held(workload / "MANIFEST.json", expected["manifest_sha256"], 4 << 20)
    require(reader.held(package_root / "MANIFEST.json") == manifest_raw, "failed authored manifest differs")
    manifest = reader.strict(manifest_raw)
    computed = dict(manifest_sha256=reader.sha(manifest_raw), archive_sha256=reader.sha(reader.held(protected / "payload.tar")),
        package_sha256=reader.sha(reader.canonical(manifest["package_files"])),
        source_manifest_sha256=reader.sha(reader.canonical({n: p for n, p in manifest["package_files"].items() if n.endswith(".py")})),
        session_sha256=manifest["session_sha256"])
    package = reader.read(run_root / "PACKAGE-IDENTITIES.json")
    require(computed == expected and all(package.get(k) == v for k, v in computed.items())
            and bindings["archive_sha256"] == computed["archive_sha256"]
            and bindings["manifest_sha256"] == computed["manifest_sha256"] and bindings["target_root"] == manifest["target_root"],
            "failed package/source/session identity differs")
    for name, pin in manifest["package_files"].items():
        require(reader.held(workload / name, pin, 16 << 20) == reader.held(package_root / name, pin, 16 << 20),
                "failed actual/authored source closure differs")
    for name, pin in bindings["upload_inventory"].items():
        authored = reader.held(verified["author"] / "owner-upload" / name, pin, 16 << 20)
        if name != "RUN-ON-WORKSTATION.sh":
            require(reader.held(protected / name, pin, 16 << 20) == authored, "failed protected helper differs")
    phases = _failed_campaign_release(reader, workload, protected, bindings, manifest)
    return dict(operation_seal_sha256=operation_pin, workload_seal_sha256=seals["workload"]["manifest_sha256"],
                protected_seal_sha256=seals["protected"]["manifest_sha256"],
                launch_elapsed_seconds=next(s["elapsed_seconds"] for s in steps if s["name"] == "launch"),
                phases=phases, grading_performed=False, workload_failed=True)


def _finite_replay(runner, campaign, prep, request, deadline):
    """Run the original authenticated replay in a finite local CPU child."""
    template = Path(prep["quality_template"]["root"])
    inventory = checked_json(template / "PROJECTION-INVENTORY.json",
                             prep["quality_template"]["inventory_sha256"], 1 << 20)
    path = template / "bind_future.py"
    pin = inventory["files"]["bind_future.py"]["sha256"]
    checked_bytes(path, pin, 1 << 20)
    payload = exchange.canonical(dict(path=str(path), sha256=pin, request=request))
    recipe = '''import hashlib, importlib.util, json, pathlib, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (40, 45))
resource.setrlimit(resource.RLIMIT_FSIZE, (128 << 20, 128 << 20))
def audit(event, args):
    if event == 'import' and args[0].split('.')[0] in {'torch', 'triton', 'sglang', 'numpy'}:
        raise RuntimeError('framework import refused in retained replay')
    if event in {'subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn', 'socket.__new__'} or event.startswith('os.exec'):
        raise RuntimeError('child or network action refused in retained replay')
sys.addaudithook(audit)
value = json.loads(sys.stdin.buffer.read(2 << 20))
path = pathlib.Path(value['path'])
raw = path.read_bytes()
assert len(raw) <= 1 << 20 and hashlib.sha256(raw).hexdigest() == value['sha256']
spec = importlib.util.spec_from_file_location('finite_retained_replay', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
proof = module.bind(**value['request'])
print(json.dumps(proof, sort_keys=True, separators=(',', ':'), allow_nan=False))
'''
    receipt = runner.run("retained-replay-once", [PYTHON, "-I", "-B", "-S", "-c", recipe],
                         _work_budget(deadline, REPLAY_WORK_SECONDS, "retained replay"),
                         stdin=payload, cwd=str(campaign), output_limit=2 << 20)
    raw = checked_bytes(campaign / "retained-replay-once.stdout", receipt["stdout"]["sha256"], 2 << 20)
    proof = decoded_json(raw.splitlines()[-1])
    require(proof == _json(Path(request["destination"]) / "cpu/ACTUAL-INPUT-BINDING.json"),
            "finite replay output differs from retained proof")
    return proof, receipt


def _profile_capture(verified, proof, binding_root):
    """Revalidate the original sealed C2 capture schema and project its bytes.

    The original replay authenticates the complete native bundle and workers.
    This additional check makes its diagnostic profile status a hard requirement
    for Hyperloom, without changing the historical HTTP replay or any seal.
    """
    review = verified["author"] / "package-v2/review"
    manifest = _json(review.parent / "MANIFEST.json", 4 << 20)
    capture_module = _source(review / "serving_profile.py",
                             manifest["package_files"]["review/serving_profile.py"],
                             "_hyperloom_profile_projection_" + verified["record"]["nonce"])
    captures = {}
    for arm, logical in projection.ARMS:
        reference = proof["replay_provenance"]["artifacts"][arm]
        require(reference["owner_arm"] == arm and reference["logical_arm"] == logical
                and reference["binding"]["arm"] == logical, "profile replay arm identity")
        bundle = checked_json(binding_root / "serving-replay" / (arm + "-artifacts") / "native-http-bundle.json",
                              reference["native_bundle_sha256"], 17 << 20)
        observations = {}
        for name, row in bundle["observations"].items():
            if not name.startswith("PROFILE-"):
                continue
            require(isinstance(row, dict) and set(row) == {"bytes", "sha256", "base64"}
                    and type(row["bytes"]) is int and 0 < row["bytes"] <= 768 << 10
                    and isinstance(row["base64"], str) and len(row["base64"]) <= 1 << 20,
                    "bounded profile member required")
            raw = base64.b64decode(row["base64"], validate=True)
            require(len(raw) == row["bytes"] and exchange.digest(raw) == row["sha256"], "profile member identity")
            observations[name] = raw
        validated = capture_module.validate_observations(observations, bundle["workers"], reference["binding"])
        require(validated["status"] == ("OFF" if arm == "bf16" else "COMPLETE"),
                "profile capture is incomplete or present in the control")
        captures[arm] = (validated, observations)
    validated, observations = captures["kv4"]
    ranks = validated["ranks"]
    require([row["rank"] for row in ranks] == [0, 1], "ordered profile ranks required")
    profiles = []
    for phase, count in (("prefill", 3), ("decode", 4)):
        for index in range(count):
            pair = [next(cap for cap in row["captures"] if (cap["phase"], cap["index"]) == (phase, index))
                    for row in ranks]
            require(pair[0]["batch"] == pair[1]["batch"], "profile rank batch mismatch")
            profiles.append(dict(capture_id=phase + "-" + str(index), phase=phase, index=index,
                batch=pair[0]["batch"], traces=[dict(rank=rank, bytes=cap["bytes"], sha256=cap["sha256"],
                    gzip_base64=base64.b64encode(observations[cap["file"]]).decode()) for rank, cap in enumerate(pair)]))
    return dict(profiles=profiles, request=capture_module.REQUEST,
        ranks=[dict(rank=row["rank"], request_progress=row["request_progress"],
        prefill_batches=row["prefill_batches"], prefill_tokens=row["prefill_tokens"],
        decode_batches=row["decode_batches"]) for row in ranks])


def execute(proposal, authorization, bound, *, checkpoint_root, cancelled, commands_module=None, binder=None, now=None):
    """Register once against an idle checkpoint, run the finite caller once, authenticate.

    cancelled() is the parent's cancellation observation. commands_module and
    binder default to the pinned finite helper and the fresh template's binder;
    tests substitute them at those external boundaries only.
    """
    started = _clock(now)
    result = dict(schema=1, status="EXECUTING", stage="proposal", reason=None, owner_started=False,
                  caller_invoked=False, independent_release=False, release_disposition="NOT_STARTED",
                  park_campaign=False, grading_performed=False, owner_seal_sha256=None, quality_sha256=None,
                  receipts={}, checkpoints={}, payload={}, adoption_authorized=False, automatic_retry=False,
                  qualification=False, retry_authorized=False, publishable=False, utc=_utc(started))
    campaign = checkpoint = registration = None
    try:
        _bound(bound)
        require(callable(cancelled), "parent cancellation observation required")
        prop_path, prop = _pinned(proposal, "proposal record")
        require(prop.get("schema") == 1 and prop.get("status") == "PROPOSED_NOT_REGISTERED_OR_RUN"
                and prop.get("registered") is False and prop.get("launch_authorized") is False, "inactive proposal required")
        verified = _verified_preparation(prop["preparation"], bound)
        binding, prep = verified["binding"], verified["record"]
        kind, nonce, index = prop["kind"], prop["nonce"], prop["index"]
        campaign = _canonical_dir(prop["campaign_root"], "campaign")
        run_root = Path(prop["native_root"])
        request = binding["request"]
        result.update(kind=kind, nonce=nonce, index=index, id=request["id"], request_sha256=binding["request_sha256"],
                      campaign_sha256=bound["campaign_sha256"], controller_sha256=bound["controller_sha256"],
                      source_set_sha256=request["source_set_sha256"], candidate_sha256=binding["candidate_sha256"],
                      campaign_root=str(campaign), native_root=str(run_root), proposal=dict(proposal))
        require(prop_path.parent == campaign, "proposal record belongs to another campaign directory")
        proposed = checked_json(Path(prop["proposal"]["path"]), prop["proposal"]["sha256"], 1 << 20)
        checked_bytes(Path(prop["execute_once"]["path"]), prop["execute_once"]["sha256"], 1 << 20)
        require(Path(prop["proposal"]["path"]).parent == campaign == Path(prop["execute_once"]["path"]).parent
                and proposed["nonce"] == nonce and proposed["global_gpu_attempt_index"] == index
                and proposed["native_root"] == str(run_root) and proposed["status"] == PROPOSED_STATUS
                and proposed["registered"] is False and proposed["gpu_allowance"] is False, "campaign proposal changed")
        for name in ("REGISTRATION.json", "CALLER-INVOKED.json", "OWNER-EXECUTION.json", "caller-once.json"):
            require(not (campaign / name).exists() and not (campaign / name).is_symlink(),
                    "campaign already registered or invoked; single use")
        require(not run_root.exists() and not run_root.is_symlink(), "native run root already exists")
        result["stage"] = "authorization"
        auth = _authorization(authorization, prop, verified, bound)
        result["authorization"] = dict(path=auth["path"], sha256=auth["sha256"])
        result["stage"] = "checkpoint"
        checkpoint = _checkpoint(checkpoint_root)
        current_index, owner0, caller0 = _idle(checkpoint["state"])
        require(current_index == index and owner0 == proposed["prior_model_scope_owner_seconds_charged"]
                and caller0 == proposed["prior_model_scope_caller_seconds_charged"],
                "checkpoint changed since the proposal; index or cumulative accounting differs")
        result["stage"] = "admission"

        def admissible(label):
            if cancelled():
                raise Cancelled("request cancelled " + label + "; no owner started")
            instant = int(_clock(now) * 1000)
            needed = (CALLER_SECONDS + POST_RELEASE_SECONDS) * 1000
            require(instant + needed <= request["expires_unix_ms"] and instant + needed <= bound["campaign_deadline_unix_ms"],
                    "remaining caller plus post-release budget does not fit " + label)

        admissible("before registration")
        clock = _clock(now)
        grant = _registration(proposed, auth, clock)
        grant_raw = (json.dumps(grant, indent=2, sort_keys=True) + "\n").encode()
        registration = dict(path=str(campaign / "REGISTRATION.json"), sha256=exchange.digest(grant_raw))
        label = "hyperloom-" + kind
        opened = _checkpoint_record(checkpoint, clock, status="APPROVED_CALLER_STARTING_ADMISSION_NOT_YET_CONFIRMED",
                                    active_caller=str(campaign), active_owner=label, current_global_attempt_index=index,
                                    last_completed_global_attempt_index=index - 1, registration=registration,
                                    caller_deadline_utc=_utc(clock + CALLER_SECONDS),
                                    hyperloom_request_sha256=binding["request_sha256"],
                                    hyperloom_campaign_sha256=bound["campaign_sha256"],
                                    next_action=("Observe this one Hyperloom " + kind + " caller; then original replay, "
                                                 "independent release and all native grades. No retry."))
        try:
            result["checkpoints"]["registered"] = _write_checkpoint(checkpoint_root, checkpoint["number"] + 1, opened)
        except FileExistsError as error:
            raise RuntimeError("competing registration: " + _error(error)) from error
        try:
            with (campaign / "REGISTRATION.json").open("xb") as stream:
                stream.write(grant_raw)
        except FileExistsError as error:
            raise RuntimeError("competing registration file: " + _error(error)) from error
        result.update(registration=registration, registered=True)
        result["stage"] = "pre-invocation"
        admissible("immediately before invocation")
        runner = commands_module if commands_module is not None else commands(
            verified["root"] / "launcher/caller-preview/local_commands.py", campaign)
        result["stage"] = "caller"
        argv = [PYTHON, "-B", str(campaign / "execute_once.py"), "--registration-sha256", registration["sha256"]]
        result.update(owner_started=True, caller_invoked=True, release_disposition="UNVERIFIED_PARK_CAMPAIGN",
                      park_campaign=True, publishable=True)
        caller_failure = None
        try:
            receipt = runner.run("caller-once", argv, CALLER_WORK_SECONDS, cwd=str(campaign), output_limit=16 << 20)
            result["receipts"]["caller"] = dict(receipt, argv=argv)
        except Exception as error:
            caller_failure = _error(error)
            receipt_path = campaign / "caller-once.json"
            if receipt_path.is_file():
                result["receipts"]["caller"] = _json(receipt_path, 1 << 20)
        if run_root.is_dir() and (run_root / "EXECUTION-SUMMARY.json").is_file():
            result["receipts"]["execution_summary"] = _pin(run_root / "EXECUTION-SUMMARY.json")
        if caller_failure is not None:
            try:
                receipt = result["receipts"]["caller"]
                stdout = checked_bytes(campaign / "caller-once.stdout", receipt["stdout"]["sha256"], 16 << 20)
                observed = _failed_release(run_root, verified, index, registration, receipt, stdout)
                result.update(independent_release=True, observed_release=observed,
                              owner_seal_sha256=observed["operation_seal_sha256"],
                              release_disposition="RELEASED_WORKLOAD_FAILED", workload_failed=True,
                              payload={}, grading_performed=False)
            except Exception as error:
                result["release_authentication_error"] = _error(error)
            raise RuntimeError("finite caller failed; retained: " + caller_failure)
        release_started = time.monotonic()
        deadline = release_started + min(POST_RELEASE_SECONDS,
            request["expires_unix_ms"] / 1000 - _clock(now),
            bound["campaign_deadline_unix_ms"] / 1000 - _clock(now)) - PUBLICATION_SECONDS
        result["stage"] = "release"
        stdout = checked_bytes(campaign / "caller-once.stdout", receipt["stdout"]["sha256"], 16 << 20)
        bindings = checked_json(Path(prep["launcher_bindings"]["path"]), prep["launcher_bindings"]["sha256"], 1 << 20)
        observed = _release(run_root, bindings, index, stdout)
        result["observed_release"] = observed
        binding_root = campaign / "quality-binding"
        bind_request = dict(operation_root=str(run_root), operation_seal_sha256=observed["operation_seal_sha256"],
                            workload_seal_sha256=observed["workload_seal_sha256"],
                            protected_seal_sha256=observed["protected_seal_sha256"],
                            package_root=str(verified["author"] / "package-v2"),
                            expected_package_pins=dict(verified["plan"]["expected_package_pins"]),
                            destination=str(binding_root),
                            projection_inventory_sha256=prep["quality_template"]["inventory_sha256"])
        _work_budget(deadline, REPLAY_WORK_SECONDS, "retained replay")
        if binder is None:
            proof, replay_receipt = _finite_replay(runner, campaign, prep, bind_request, deadline)
            result["receipts"]["retained_replay"] = replay_receipt
        else:
            proof = binder(**bind_request)
        count = REQUESTS[kind]
        seals = proof["replay_provenance"]["seals"]
        require(proof.get("schema") == 1
                and proof.get("status") == "ACTUAL_%d_INPUT_BOUND_BY_RECOMPUTED_OWNER_RESOURCE_RELEASE_HTTP" % (2 * count)
                and proof.get("rows") == 2 * count and proof.get("cancellations") == 0
                and proof.get("request") == {k: v for k, v in bind_request.items() if k not in ("destination", "projection_inventory_sha256")}
                and all(proof.get(k) is False for k in ("precision_accepted", "qualification", "native_quality_grade",
                                                        "execution_authorized"))
                and seals["operation"]["manifest_sha256"] == observed["operation_seal_sha256"]
                and seals["workload"]["manifest_sha256"] == observed["workload_seal_sha256"]
                and seals["protected"]["manifest_sha256"] == observed["protected_seal_sha256"]
                and seals["source_author"] == prep["seals"]["author"]
                and seals["operation"]["root"] == str(run_root), "original replay did not authenticate this operation")
        cpu = binding_root / "cpu"
        summaries = {}
        for arm in ("bf16", "kv4"):
            summaries[arm] = _json(binding_root / "serving-replay" / (arm + "-http-client-tree") / "paired/RUN-SUMMARY.json",
                                   16 << 20)
        result.update(independent_release=True, release_disposition="RELEASED", park_campaign=False,
                      owner_seal_sha256=observed["operation_seal_sha256"], replay_proof=_pin(cpu / "ACTUAL-INPUT-BINDING.json"))
        result["stage"] = "grading"
        result["grading_performed"] = True
        grade = runner.run("cpu-grade-once", [PYTHON, "-B", "workstation.py", "--execute-once"],
                           _work_budget(deadline, GRADING_WORK_SECONDS, "native CPU grading", reserve=60),
                           cwd=str(cpu), output_limit=16 << 20)
        result["receipts"]["cpu_grade"] = grade
        readout = runner.run("cpu-readout-once", [PYTHON, "-B", "readout.py"],
                             _work_budget(deadline, READOUT_WORK_SECONDS, "native CPU readout", reserve=10), cwd=str(cpu),
                             output_limit=4 << 20)
        result["receipts"]["cpu_readout"] = readout
        quality = decoded_json(checked_bytes(campaign / "cpu-readout-once.stdout", readout["stdout"]["sha256"], 4 << 20))
        require(quality == _json(cpu / "RESULT-SUMMARY.json", 4 << 20), "independent readout differs from the grade summary")
        decoded = _json(cpu / "retained-cpu/output/quality/DECODE-RESULT.json", 4 << 20)
        quality_sha256 = exchange.digest(exchange.canonical(quality))
        result.update(quality_sha256=quality_sha256, decode_result=_pin(cpu / "retained-cpu/output/quality/DECODE-RESULT.json"))
        result["stage"] = "report"
        workload = checked_json(Path(prep["workload"]["path"]), prep["workload"]["sha256"], 1 << 20)
        arguments = dict(summaries=summaries, decoded=decoded, quality=quality, quality_sha256=quality_sha256,
                         workload=workload, request=request, binding=bound,
                         owner_seal_sha256=observed["operation_seal_sha256"], observation_id=nonce)
        if kind == "profile":
            report = projection.profile_report(**arguments, capture=_profile_capture(verified, proof, binding_root))
        else:
            report = projection.serving_report(**arguments)
        envelope = dict(status="SUCCEEDED", independent_release=True, payload={"benchmark_report": report},
                        owner_seal_sha256=observed["operation_seal_sha256"], request_sha256=binding["request_sha256"])
        require(backend.validate_report(envelope, request, bound) == report, "projected report failed the backend validator")
        post_release = time.monotonic() - release_started
        result["post_release_elapsed_seconds"] = post_release
        require(time.monotonic() <= deadline, "post-release grading and reporting exceeded the reserved budget")
        result.update(status="SUCCEEDED", stage="complete", payload={"benchmark_report": report})
    except Unsupported as error:
        result.update(status="UNSUPPORTED", reason=_error(error), publishable=True)
    except Cancelled as error:
        result.update(status="CANCELLED", reason=_error(error), publishable=True)
    except REFUSALS as error:
        result["reason"] = _error(error)
        consumed = result["owner_started"] or result.get("registered") or bool(result["checkpoints"])
        result.update(status="FAILED" if consumed else "REFUSED", publishable=bool(consumed), park_campaign=bool(consumed))
    finally:
        if result["checkpoints"].get("registered") and "closed" not in result["checkpoints"]:
            _close(result, checkpoint_root, checkpoint, registration, _clock(now))
        if campaign is not None and result["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            if not (campaign / "OWNER-EXECUTION.json").exists():
                result["record"] = _write(campaign / "OWNER-EXECUTION.json", result)
    return result


def _close(result, checkpoint_root, checkpoint, registration, clock):
    """Closing checkpoint: verified release completes the index; otherwise park."""
    kind = result["kind"].upper()
    index = result["index"]
    opened = dict(result["checkpoints"]["registered"])
    previous = dict(path=opened["path"], sha256=opened["sha256"], state=checkpoint["state"])
    fields = dict(registration=registration, campaign=result["campaign_root"], native_root=result["native_root"])
    if not result["owner_started"]:
        # Registered but never invoked: the index is released unused and the
        # previous work's release observation is carried forward unchanged.
        state = checkpoint["state"]
        fields.update(active_caller=None, active_owner=None, current_global_attempt_index=None,
                      last_completed_global_attempt_index=index - 1,
                      independent_release_verified=state.get("independent_release_verified"),
                      independent_gpu_release=state.get("independent_gpu_release"),
                      unused_registration=registration if result.get("registered") else None,
                      status=("HYPERLOOM_%s_CANCELLED_BEFORE_OWNER_START" % kind if result["status"] == "CANCELLED"
                              else "HYPERLOOM_%s_NOT_STARTED_INDEX_RELEASED" % kind),
                      next_action="Registration consumed unused; a fresh preparation is required. No retry.")
    elif result["independent_release"]:
        caller_seconds = result["receipts"]["caller"]["elapsed_seconds"]
        observed = result.get("observed_release") or {}
        owner_seconds = observed.get("launch_elapsed_seconds")
        if type(owner_seconds) not in (int, float):
            owner_seconds = caller_seconds
        state = checkpoint["state"]
        fields.update(active_caller=None, active_owner=None, current_global_attempt_index=None,
                      last_completed_global_attempt_index=index, independent_release_verified=True,
                      cumulative_owner_seconds=round(state["cumulative_owner_seconds"] + owner_seconds, 1),
                      cumulative_caller_seconds=round(state["cumulative_caller_seconds"] + caller_seconds, 1),
                      owner_seal_sha256=result["owner_seal_sha256"], quality_sha256=result.get("quality_sha256"),
                      status=("HYPERLOOM_%s_COMPLETE_RELEASED_GRADED_PARKED" % kind if result["status"] == "SUCCEEDED"
                              else "HYPERLOOM_%s_RELEASED_WORKLOAD_FAILED_PARKED" % kind if result.get("workload_failed")
                              else "HYPERLOOM_%s_RELEASED_QUALITY_FAILED_PARKED" % kind),
                      next_action="PARK. Experimental result only; no retry, adoption or selection change.")
    else:
        fields.update(active_caller=result["campaign_root"], active_owner="hyperloom-" + result["kind"],
                      current_global_attempt_index=index, last_completed_global_attempt_index=index - 1,
                      independent_release_verified=False, status="HYPERLOOM_%s_OWNER_UNRESOLVED_PARK_CAMPAIGN" % kind,
                      next_action=("PARK the campaign: owner release unverified; the retained journal stays open until "
                                   "an independent observation resolves it. No retry."))
    closed = _checkpoint_record(previous, clock, **fields)
    try:
        current = _checkpoint(checkpoint_root)
        require(all(current[k] == opened[k] for k in ("path", "sha256", "number")),
                "checkpoint advanced or changed during this owner; refusing a stale closeout")
        result["checkpoints"]["closed"] = _write_checkpoint(checkpoint_root, opened["number"] + 1, closed)
    except REFUSALS as error:
        result["checkpoints"]["closed"] = dict(failed=_error(error), intended=closed)
        result.update(status="FAILED", park_campaign=True, payload={})
        result["reason"] = (result.get("reason") or "") + "; closing checkpoint refused: " + _error(error)
