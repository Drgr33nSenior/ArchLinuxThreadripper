"""Run Hyperloom's baseline action through the qualified workstation owner."""

from pathlib import Path

import hyperloom_owner_backend as backend
from hyperloom_sglang import positive, require


async def run(executor, ctx):
    from hyperloom_sdk_adapter import binding, materialize_config
    from hyperloom.orchestrator.actions.executors.benchmark_result import extract_benchmark_measurement

    bound = binding()
    params = ctx.task.params or {}
    allowed = {"output_dir", "config_path", "timeout_sec", "reason", "framework", "gpu_type", "model_path",
               "extra_server_args", "extra_envs", "remove_args", "unset_envs", "benchmark_script",
               "source", "recipe_generation", "sbd_event_id"}
    require(isinstance(params, dict) and set(params) <= allowed, "unsupported owner baseline action parameters")
    require(params.get("framework", "sglang") == "sglang", "owner baseline framework is fixed")
    if "timeout_sec" in params:
        require(positive(params["timeout_sec"]) and params["timeout_sec"] >= 10570,
                "baseline timeout truncates component and serving closeout")
    output = executor._resolve_workspace(ctx, "baseline")
    output.mkdir(parents=True, exist_ok=True)
    config_path = Path(params.get("config_path") or executor.default_config_path or executor._resolve_default_config())
    overrides = {key: value for key, value in params.items() if key not in ("config_path", "output_dir")}
    config_path = materialize_config(config_path, output, **overrides)
    config = backend.read_config(config_path)
    require(backend.validate_config(config, bound) == "serving", "baseline requires unprofiled serving")
    workspace = output / "benchmark_owner"
    workspace.mkdir(mode=0o700)
    state = (getattr(ctx, "extra", None) or {}).get("shared_state") or executor.shared_state
    require(state is not None, "baseline needs the live optimizer state")
    report = None
    if state.baseline_tput <= 0 and not getattr(executor, "_owner_qualification_reused", False):
        report = backend.reuse_qualification("serving", workspace, bound)
        executor._owner_qualification_reused = report is not None
    if report is None:
        report = await backend.run_async(config, workspace, bound)
    measurement = extract_benchmark_measurement(report, config_path=config_path, workspace=workspace)
    require(measurement.get("valid_measurement") is True, "owner baseline is not a valid measurement")
    return dict(measurement, status="succeeded", framework="sglang", benchmark_backend=backend.NAME,
                workspace=str(workspace), output_dir=str(output), result_dir=str(workspace),
                report_path=str(workspace / "benchmark_report.json"), materialized_config=str(config_path))
