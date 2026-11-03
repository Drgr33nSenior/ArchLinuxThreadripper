"""Map owner-authenticated C2 trace windows into Hyperloom's profile action."""

import asyncio
import base64
import copy
from pathlib import Path
import zlib

import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
from hyperloom_sglang import decoded_json, positive, require


WINDOWS = tuple((phase, index) for phase, count in (("prefill", 3), ("decode", 4)) for index in range(count))
PACKED_CAP = {"prefill": 768 << 10, "decode": 320 << 10}
RAW_CAP = 32 << 20


def _trace(raw):
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decoded = inflater.decompress(raw, RAW_CAP + 1)
    require(len(decoded) <= RAW_CAP and inflater.eof and not inflater.unused_data
            and not inflater.unconsumed_tail, "bounded complete gzip trace required")
    value = decoded_json(decoded)
    events = value.get("traceEvents") if isinstance(value, dict) else None
    require(isinstance(events, list) and any(isinstance(e, dict) and e.get("cat") == "kernel"
            and e.get("ph") == "X" and positive(e.get("dur")) for e in events),
            "capture contains no GPU kernel events")


def materialize_captures(report, request, output):
    """Verify all seven windows before copying them into analysis directories.

    Each window stays separate. Only the candidate arm is projected; the full
    paired owner evidence remains retained by the controller.
    """
    capture = report.get("profile_capture")
    require(isinstance(capture, dict) and type(capture.get("schema")) is int and capture["schema"] == 1
            and capture.get("status") == "complete" and capture.get("arm") == "mtp",
            "complete candidate-arm profile required")
    require(capture.get("owner_seal_sha256") == report["owner_binding"]["owner_seal_sha256"]
            and capture.get("request_sha256") == exchange.digest(exchange.canonical(request))
            and capture.get("source_set_sha256") == request["source_set_sha256"],
            "profile belongs to another owner or candidate")
    windows = capture.get("profiles")
    require(isinstance(windows, list) and len(windows) == len(WINDOWS), "seven separate capture windows required")
    verified = []
    seen_digests = set()
    pair = None
    for window, (phase, index) in zip(windows, WINDOWS, strict=True):
        require(isinstance(window, dict) and window.get("phase") == phase
                and type(window.get("index")) is int and window["index"] == index
                and window.get("capture_id") == phase + "-" + str(index), "ordered unique profile windows required")
        batch = window.get("batch")
        require(isinstance(batch, dict) and isinstance(batch.get("rids"), list)
                and len(batch["rids"]) in (1, 2) and all(isinstance(r, str) and r for r in batch["rids"])
                and len(set(batch["rids"])) == len(batch["rids"])
                and batch.get("batch_size") == len(batch["rids"]), "actual scheduler batch witness required")
        if phase == "decode":
            require(len(batch["rids"]) == 2, "C1 decode cannot stand in for C2 coverage")
            pair = set(batch["rids"]) if pair is None else pair
            require(set(batch["rids"]) == pair, "decode pair changed between windows")
        traces = window.get("traces")
        require(isinstance(traces, list) and len(traces) == 2, "both TP rank traces required")
        contents = []
        for trace, rank in zip(traces, (0, 1), strict=True):
            require(isinstance(trace, dict) and type(trace.get("rank")) is int and trace["rank"] == rank
                    and type(trace.get("bytes")) is int and 0 < trace["bytes"] <= PACKED_CAP[phase],
                    "bounded ordered TP rank trace required")
            encoded = trace.get("gzip_base64")
            require(isinstance(encoded, str) and len(encoded) <= 4 * ((PACKED_CAP[phase] + 2) // 3),
                    "bounded encoded capture required")
            raw = base64.b64decode(encoded, validate=True)
            require(len(raw) == trace["bytes"] and exchange.digest(raw) == trace.get("sha256")
                    and trace["sha256"] not in seen_digests, "trace size/digest changed or trace was reused")
            seen_digests.add(trace["sha256"])
            _trace(raw)
            contents.append(raw)
        verified.append((window, contents))
    require(all(set(window["batch"]["rids"]) <= pair for window in windows),
            "prefill and decode requests differ")
    destination = Path(output) / "owner-traces"
    require(destination.parent.is_absolute() and destination.parent.resolve() == destination.parent
            and destination.parent.is_dir(), "canonical profile analysis directory required")
    destination.mkdir(mode=0o700)
    projected = []
    for window, contents in verified:
        directory = destination / window["capture_id"]
        directory.mkdir(mode=0o700)
        paths = []
        for rank, raw in enumerate(contents):
            path = directory / ("rank" + str(rank) + ".trace.json.gz")
            exchange._publish(path, raw)
            paths.append(str(path))
        projected.append({"capture_id": window["capture_id"], "phase": window["phase"], "index": window["index"],
                          "batch": copy.deepcopy(window["batch"]), "trace_files": paths})
    return projected


async def run(executor, ctx):
    """Route the actual ProfileExecutor through its finite owner client."""
    from hyperloom_sdk_adapter import binding
    from hyperloom.orchestrator.actions.executors.profile import _map_native_capture

    bound = binding()
    params = ctx.task.params or {}
    allowed = {"output_dir", "config_path", "timeout_sec", "reason", "framework", "gpu_type", "model_path",
               "source", "recipe_generation", "sbd_event_id", "base_extra_args", "extra_server_args",
               "extra_envs", "base_extra_envs", "remove_args", "unset_envs", "benchmark_script"}
    require(isinstance(params, dict) and set(params) <= allowed, "unsupported owner profile action parameters")
    require(not any(params.get(key) for key in ("base_extra_args", "extra_server_args", "extra_envs",
                "base_extra_envs", "remove_args", "unset_envs", "benchmark_script")),
            "owner profile cannot change the frozen serving flags or environment")
    require(params.get("framework", "sglang") == "sglang" and params.get("gpu_type", "r9700") == "r9700"
            and params.get("model_path", bound["benchmark"]["model"]) == bound["benchmark"]["model"],
            "profile action changed the selected stack")
    config_path = Path(params.get("config_path") or executor.default_config_path or executor._resolve_default_config())
    config = backend.read_config(config_path)
    backend.validate_config(config, bound)
    config = copy.deepcopy(config)
    config["benchmark"]["profiler"] = {"torch_profiler": {"enabled": True}}
    if "timeout_sec" in params:
        require(positive(params["timeout_sec"])
                and params["timeout_sec"] >= exchange.envelope_seconds("profile", bound["caller_seconds"]["profile"]),
                "profile action timeout truncates owner closeout")
    output = executor._resolve_workspace(ctx, "profile")
    output.mkdir(parents=True, exist_ok=True)
    report = None
    if params.get("reason") == "prelude_initial" and not getattr(executor, "_owner_qualification_reused", False):
        report = backend.reuse_qualification("profile", output, bound)
        executor._owner_qualification_reused = report is not None
    if report is None:
        report = await backend.run_async(config, output, bound)
    request = decoded_json(exchange._read(output / "OWNER-JOB.json"))
    windows = materialize_captures(report, request, output)
    result = dict(status="succeeded", framework="sglang", tp=2, workspace=str(output),
                  benchmark_backend=backend.NAME, diagnostic_only=True, trace_input_ready=False,
                  profile_coverage=report["profile_coverage"], trace_capture_status="complete",
                  owner_binding=report["owner_binding"], profile_captures=[])
    # Certification is CPU analysis. No detached to_thread wait can survive
    # cancellation and return a later result for an abandoned action.
    for window in windows:
        await asyncio.sleep(0)
        mapped = _map_native_capture(window, result)
        mapped.update(phase=window["phase"], index=window["index"], batch=window["batch"])
        result["profile_captures"].append(mapped)
    if not all(window.get("trace_input_ready") for window in result["profile_captures"]):
        result.update(status="failed", error_class="owner_profile_window_unusable")
    else:
        primary = bound["profile_primary_window"]
        result.update(next(window for window in result["profile_captures"] if window["capture_id"] == primary))
        result["primary_window_selection"] = primary
    return result
