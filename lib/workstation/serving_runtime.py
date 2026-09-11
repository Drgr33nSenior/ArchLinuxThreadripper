"""Offline SGLang loading, queue and warm-status contracts.

This module only writes reviewable JSON patches and status reports.  It never
contacts a server, changes a Pod, flushes caches, or makes a warmup request.
Candidates are deliberately conditional on evidence from the exact selected
image: a current SGLang manual is discovery material, not a runtime contract.
"""
import argparse
import copy
from datetime import datetime
import json
import math
import os
from pathlib import Path

from model_kernels import evidence, identity, mib, runtime_key, write


def _single(deployment, pod):
    """Return the existing SGLang container and its JSON Patch root."""
    spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
    containers = spec.get("containers")
    if not isinstance(containers, list):
        raise ValueError("rendered SGLang Pod template is required")
    indexes = [index for index, item in enumerate(containers) if item.get("name") == "sglang"]
    if len(indexes) != 1:
        raise ValueError("expected one existing SGLang container")
    container = containers[indexes[0]]
    resources = container.get("resources")
    if (container.get("image") != pod.get("image") or not isinstance(resources, dict)
            or resources.get("requests") != resources.get("limits")
            or resources != pod.get("resources")):
        raise ValueError("rendered baseline must match observed immutable Guaranteed Pod")
    return spec, container, f"/spec/template/spec/containers/{indexes[0]}"


def _launch(runtime):
    launch = runtime.get("launch")
    if not isinstance(launch, list) or len(launch) != 1 or not isinstance(launch[0], dict):
        raise ValueError("require one observed SGLang launch; do not infer settings from a manifest")
    return launch[0]


def _positive(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer in 1..{maximum}")
    return value


def _capability(compiler, name):
    record = compiler.get("runtime_capabilities", {}).get(name)
    if not isinstance(record, dict) or record.get("status") != "supported-source-contract":
        reason = record.get("reason") if isinstance(record, dict) else "exact-image capability evidence is missing"
        raise ValueError(f"{name} is unsupported: {reason}")
    flag, key = record.get("flag"), record.get("key")
    if not isinstance(flag, str) or not flag.startswith("--") or not isinstance(key, str):
        raise ValueError(f"{name} capability record is malformed")
    if flag not in compiler.get("flags", ()):
        raise ValueError(f"{name} flag is absent from exact-image help")
    sources = record.get("sources")
    if (not isinstance(sources, dict) or not sources or any(not isinstance(v, str) or len(v) != 64
            or any(char not in "0123456789abcdef" for char in v) for v in sources.values())):
        raise ValueError(f"{name} lacks hashed exact-image source evidence")
    return record


def _limits(pod, runtime, compiler, reserve_mib, per_thread_mib):
    _positive(reserve_mib, "reserve MiB", 1024 * 1024)
    _positive(per_thread_mib, "per-thread MiB", 1024 * 1024)
    resources = pod["resources"]["limits"]
    try:
        tp = int(runtime["settings"]["TENSOR_PARALLEL"])
        cpu = float(resources["cpu"])
        cgroup_memory = int(compiler["cgroup"]["memory.max"])
        cgroup_current = int(compiler["cgroup"]["memory.current"])
        effective_cpu = min(cpu, compiler["affinity_cpus"], int(compiler["cgroup"]["cpu.max"].split()[0]) /
                            int(compiler["cgroup"]["cpu.max"].split()[1]))
        memory = min(mib(resources["memory"]), cgroup_memory // 1024**2)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise ValueError("finite observed Pod CPU/RAM cgroup limits are required") from None
    if tp != len(runtime.get("devices", ())) or tp < 1 or cgroup_memory <= 0 or cgroup_current < 0:
        raise ValueError("TP/device or cgroup identity is incomplete")
    reserve = max(reserve_mib, math.ceil(cgroup_current / 1024**2))
    maximum = min(64, math.floor(effective_cpu / tp), (memory - reserve) // (per_thread_mib * tp))
    if maximum < 1:
        raise ValueError("no loading thread fits the observed CPU/RAM envelope")
    return tp, maximum, {"effective_cpu": effective_cpu, "pod_memory_mib": memory,
                          "reserve_mib": reserve, "per_thread_mib": per_thread_mib}


def _disk_headroom(runtime, compiler):
    """Keep a sealed statvfs observation distinct from loading-time metrics."""
    storage = compiler.get("storage")
    if not isinstance(storage, dict) or storage.get("schema") != 1 or not isinstance(storage.get("observed_at"), str):
        return {"status": "unknown", "reason": "current kernel evidence has no storage observation; recollect before qualification"}
    try:
        stamp = datetime.fromisoformat(storage["observed_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        return {"status": "unknown", "reason": "storage observation timestamp is malformed"}
    roots = storage.get("roots")
    settings = runtime.get("settings")
    caches = compiler.get("caches")
    if not isinstance(settings, dict) or not isinstance(caches, dict):
        return {"status": "unknown", "reason": "runtime storage identity is incomplete"}

    def cache_path(name):
        configured = settings.get(name)
        cached = caches.get(name, {}).get("root") if isinstance(caches.get(name), dict) else None
        if configured is not None and (not isinstance(configured, str) or not configured):
            return None
        if cached is not None and (not isinstance(cached, str) or not cached):
            return None
        if configured is not None and cached is not None and configured != cached:
            return None
        return configured if configured is not None else cached

    expected = {
        "model": settings.get("MODEL_PATH"),
        "triton": cache_path("TRITON_CACHE_DIR"),
        "torchinductor": cache_path("TORCHINDUCTOR_CACHE_DIR"),
    }
    permitted = {"model": "/models", "triton": "/cache/triton", "torchinductor": "/cache/torchinductor"}
    if not isinstance(roots, dict) or set(roots) != set(expected):
        return {"status": "unknown", "reason": "storage observation roots are incomplete"}
    checked = {}
    for name, expected_path in expected.items():
        item = roots[name]
        base = permitted[name]
        if (not isinstance(expected_path, str) or not (expected_path == base or expected_path.startswith(base + "/"))
                or str(Path(expected_path)) != expected_path or ".." in Path(expected_path).parts
                or not isinstance(item, dict) or item.get("status") != "observed" or item.get("path") != expected_path
                or any(type(item.get(key)) is not int or item[key] < 0 for key in
                       ("filesystem_device", "root_inode", "total_bytes", "available_bytes"))
                or item["total_bytes"] <= 0 or item["root_inode"] <= 0
                or item["available_bytes"] > item["total_bytes"] or item.get("observed_at") != storage["observed_at"]):
            return {"status": "unknown", "reason": f"{name} disk-headroom observation is unavailable or unsafe"}
        checked[name] = {key: item[key] for key in ("path", "filesystem_device", "root_inode", "total_bytes", "available_bytes")}
    return {"status": "observed", "observed_at": storage["observed_at"], "roots": checked,
            "observation_sha256": identity(storage),
            "scope": "sealed statvfs snapshot; not sampled during model loading or a pure loader timer"}


def _replace_argument(args, flag, value):
    """Set one known scalar flag without accepting arbitrary user arguments."""
    result = list(args)
    matches = [index for index, item in enumerate(result) if item == flag or item.startswith(flag + "=")]
    if len(matches) > 1:
        raise ValueError(f"existing {flag} argument is duplicated")
    if matches:
        index = matches[0]
        if result[index].startswith(flag + "="):
            # Normalise one exact --flag=value form into the canonical pair.
            # This does not retain an opaque argument or change any other flag.
            result[index:index + 1] = [flag, value]
            return result
        if index + 1 >= len(result) or result[index + 1].startswith("--"):
            raise ValueError(f"existing {flag} argument is malformed")
        result[index + 1] = value
    else:
        result += [flag, value]
    return result


def loading_plan(deployment, observed, threads, reserve_mib, per_thread_mib):
    """Create independent loading-thread candidates for the exact observed image."""
    pod, runtime, compiler = observed
    spec, container, root = _single(deployment, pod)
    capability = _capability(compiler, "model_loader_threads")
    launch = _launch(runtime)
    flag, key = capability["flag"], capability["key"]
    enable_key = capability.get("enable_key")
    if not isinstance(enable_key, str) or not enable_key:
        raise ValueError("model-loader capability lacks an exact-source multithread enable key")
    current = launch.get(flag)
    if current is not None:
        try:
            config = json.loads(current)
        except (TypeError, json.JSONDecodeError):
            raise ValueError("observed loader configuration is not bounded JSON") from None
        if not isinstance(config, dict) or set(config) - {key, enable_key}:
            raise ValueError("do not overwrite unrelated observed model-loader configuration")
    tp, maximum, limits = _limits(pod, runtime, compiler, reserve_mib, per_thread_mib)
    disk_headroom = _disk_headroom(runtime, compiler)
    if (not isinstance(threads, list) or not threads or len(set(threads)) != len(threads)
            or any(type(count) is not int or not 1 <= count <= maximum for count in threads)):
        raise ValueError(f"each loading thread count must fit 1..{maximum}; no silent clipping")
    base_args = list(container.get("args", ()))
    cases = {}
    for count in sorted(threads):
        # Make the experimental condition explicit.  A newer manual says this
        # defaults on, but the selected image must not inherit that assumption.
        args = _replace_argument(base_args, flag, json.dumps({enable_key: True, key: count}, separators=(",", ":")))
        cases[f"loader-threads-{count}"] = {
            "threads_per_rank": count, "total_threads": count * tp,
            "patch": [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(spec)},
                      {"op": "replace", "path": root + "/args", "value": args}],
        }
    return {"schema": 1, "kind": "sglang-loading-plan", "status": "plan-only-unqualified",
            "identity": runtime_key(pod, runtime, compiler), "deployment_sha256": identity(deployment),
            "baseline_launch": {flag: current}, "capability": capability, "tp": tp,
            "maximum_threads_per_rank": maximum, "limits": limits, "disk_headroom": disk_headroom, "cases": cases,
            "unsupported": {"pre_sharded_checkpoints":
                            "unsupported until the exact loader proves a derived-artifact format and an owner supplies a disk budget"},
            "requirements": ["Compare each patch with the current observed default independently.",
                             "Collect cold and warm startup evidence, cgroup lifetime/sampled memory, per-device VRAM and disk headroom.",
                             "Disk headroom is a point-in-time statvfs observation; recollect before target qualification or any apply.",
                             "Startup combines model loading, JIT and engine warmup; do not label it pure load time.",
                             "Preserve original weights and re-run numerical/coding checks before any owner selection."]}


def queue_plan(deployment, observed, maximum_queued):
    """Create a bounded native queue candidate; current clients cannot convey priority."""
    pod, runtime, compiler = observed
    spec, container, root = _single(deployment, pod)
    capability = _capability(compiler, "bounded_queue")
    _launch(runtime)
    _positive(maximum_queued, "maximum queued requests", 256)
    try:
        running = int(runtime["settings"]["MAX_RUNNING_REQUESTS"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("observed running-request limit is required") from None
    if maximum_queued < running:
        raise ValueError("queue cap must retain at least the observed running-request capacity")
    flag = capability["flag"]
    args = _replace_argument(list(container.get("args", ())), flag, str(maximum_queued))
    return {"schema": 1, "kind": "sglang-queue-plan", "status": "plan-only-unqualified",
            "identity": runtime_key(pod, runtime, compiler), "deployment_sha256": identity(deployment),
            "capability": capability, "maximum_running_requests": running,
            "maximum_queued_requests": maximum_queued,
            "patch": [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(spec)},
                      {"op": "replace", "path": root + "/args", "value": args}],
            "priority": {"status": "unsupported-current-client-path",
                         "reason": "the existing private clients do not convey a reviewed engine priority field; do not infer priority from Kubernetes Pod priority"},
            "overload": {"status": "engine-specific-unqualified", "requirements": [
                "Prove exact-image overload response and cancellation behavior before enabling this candidate.",
                "Do not replay agent/tool requests after a queue timeout or gaming transition.",
                "The management-operation queue remains separate from this engine queue."]}}


def _warm_identity(observed):
    """Return a fresh runtime identity only when model/device evidence is usable."""
    try:
        pod, runtime, compiler = observed
        required_pod = ("uid", "started_at", "container_id", "restart_count", "image", "image_id")
        if any(not pod.get(key) for key in required_pod if key != "restart_count"):
            return None
        devices = runtime.get("devices")
        uuids = [item.get("uuid") for item in devices] if isinstance(devices, list) else []
        if (not devices or len(uuids) != len(set(uuids)) or any(not value or value.lower() in ("unknown", "none")
                for value in uuids) or any(item.get("gfx", "").split(":")[0] != "gfx1201" for item in devices)):
            return None
        return runtime_key(pod, runtime, compiler)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


_UNAVAILABLE_CONTAINER_STATES = {
    "crash-loop-oom-killed": "current container is restarting after an out-of-memory termination",
    "crash-loop": "current container is restarting repeatedly; inspect the reviewed workload logs",
    "terminated-oom-killed": "current container terminated after an out-of-memory termination",
    "terminated": "current container terminated; inspect the reviewed workload logs",
    "image-failure": "current container image cannot be started",
}


def _container_state(pod):
    """Return the bounded current state, retaining legacy readiness-only input."""
    if not isinstance(pod, dict):
        return "unknown"
    state = pod.get("container_state")
    if isinstance(state, str):
        return state
    # Older sealed observations predate the bounded state field. They cannot
    # report a current failure, but preserve their previous readiness-only
    # interpretation for read-only compatibility.
    return "running" if pod.get("ready") in (True, False) else "unknown"


def pod_status(pod):
    """Report only a freshly observed Pod readiness state without runtime reuse."""
    if not isinstance(pod, dict):
        return {"schema": 1, "kind": "sglang-warm-status", "status": "unknown",
                "reason": "current Pod observation is malformed"}
    fields = ("uid", "started_at", "container_id", "restart_count", "image", "image_id")
    result = {"schema": 1, "kind": "sglang-warm-status",
              "pod": {key: pod.get(key) for key in fields}, "identity": None,
              "scope": "current Kubernetes readiness only; no model/runtime/device evidence was collected"}
    if not isinstance(pod.get("uid"), str) or not pod["uid"]:
        result.update(status="unknown", kubernetes_readiness="unknown",
                      representative_warmup="unknown", reason="current Pod identity is incomplete")
        return result
    state = _container_state(pod)
    if state in _UNAVAILABLE_CONTAINER_STATES and pod.get("ready") is not True:
        result.update(status="unavailable", kubernetes_readiness="unavailable",
                      representative_warmup="not-applicable", reason=_UNAVAILABLE_CONTAINER_STATES[state])
    elif state in _UNAVAILABLE_CONTAINER_STATES:
        result.update(status="unknown", kubernetes_readiness="unknown",
                      representative_warmup="unknown", reason="current Pod state is inconsistent with readiness")
    elif pod.get("ready") is False and state in ("loading", "running"):
        result.update(status="model-loading", kubernetes_readiness="model-loading",
                      representative_warmup="not-applicable",
                      reason="current Pod is Running but not Ready")
    elif pod.get("ready") is True and state == "running":
        required = ("uid", "started_at", "container_id", "image", "image_id")
        if any(not isinstance(pod.get(key), str) or not pod[key] for key in required) or type(pod.get("restart_count")) is not int:
            result.update(status="unknown", kubernetes_readiness="unknown",
                          representative_warmup="unknown", reason="current Pod process identity is incomplete")
        else:
            result.update(status="unknown", kubernetes_readiness="healthy",
                          representative_warmup="unknown",
                          reason="fresh model, runtime and allocated-device evidence is required before warm status")
    elif pod.get("ready") is True:
        result.update(status="unknown", kubernetes_readiness="unknown",
                      representative_warmup="unknown", reason="current Pod state is inconsistent with readiness")
    else:
        result.update(status="unknown", kubernetes_readiness="unknown",
                      representative_warmup="unknown", reason="current Pod readiness or container state is unavailable")
    return result


def warm_status(observed, warmup, lifecycle=None):
    """Evaluate external representative-warmup evidence without changing K8s probes."""
    try:
        pod, runtime, compiler = observed
    except (TypeError, ValueError):
        return {"schema": 1, "kind": "sglang-warm-status", "status": "unknown",
                "reason": "current Pod/runtime evidence is malformed"}
    if not isinstance(pod, dict) or not isinstance(pod.get("uid"), str) or not pod["uid"]:
        return {"schema": 1, "kind": "sglang-warm-status", "status": "unknown",
                "reason": "current Pod identity is incomplete"}
    state = _container_state(pod)
    if state in _UNAVAILABLE_CONTAINER_STATES and pod.get("ready") is not True:
        return {"schema": 1, "kind": "sglang-warm-status", "status": "unavailable",
                "pod": {key: pod.get(key) for key in ("uid", "started_at", "container_id", "restart_count", "image", "image_id")},
                "identity": None, "kubernetes_readiness": "unavailable", "representative_warmup": "not-applicable",
                "reason": _UNAVAILABLE_CONTAINER_STATES[state],
                "scope": "representative warmup is separate from the established health/readiness probes"}
    if state in _UNAVAILABLE_CONTAINER_STATES:
        return {"schema": 1, "kind": "sglang-warm-status", "status": "unknown",
                "reason": "current Pod state is inconsistent with readiness"}
    identity_now = _warm_identity(observed)
    health = ("healthy" if pod.get("ready") is True and state == "running"
              else "model-loading" if pod.get("ready") is False and state in ("loading", "running")
              else "unknown")
    report = {"schema": 1, "kind": "sglang-warm-status", "status": health,
              "pod": {key: pod.get(key) for key in ("uid", "started_at", "container_id", "restart_count", "image", "image_id")},
              "identity": identity_now, "kubernetes_readiness": health,
              "scope": "representative warmup is separate from the established health/readiness probes"}
    if identity_now is None:
        report.update(status="unknown", reason="current model, runtime or allocated-device evidence is unavailable")
        return report
    if warmup is None:
        # Absence of an optional representative warmup cannot make a healthy
        # server unavailable.  Keep K8s health separate and report the missing
        # experiment on its own field.
        report.update(representative_warmup="not-applicable",
                      reason="no optional representative warmup record supplied")
        return report
    if not isinstance(warmup, dict) or warmup.get("kind") != "kernel-warmup" or not isinstance(warmup.get("identity"), dict):
        report.update(status="unknown", reason="warmup record is missing or malformed")
        return report
    prior = warmup.get("pod", {})
    if (warmup["identity"] != identity_now or any(prior.get(key) != pod.get(key) for key in
            ("uid", "started_at", "container_id", "restart_count", "image", "image_id"))):
        report.update(status="stale", reason="Pod/process, model, runtime or device identity changed")
        return report
    state = warmup.get("status")
    if state == "measured-not-qualified" and warmup.get("memory", {}).get("status") == "checked" and health == "healthy":
        report.update(status="ready", reason="representative bounded warmup and matching steady evidence completed",
                      qualification="not-qualified; retain numerical/coding and target acceptance gates")
    elif state == "measured-not-qualified" and warmup.get("memory", {}).get("status") == "checked":
        report.update(status="model-loading" if health == "model-loading" else "unknown",
                      reason="current Kubernetes readiness is not healthy; retained warmup cannot report ready")
    elif state == "incomplete" and isinstance(lifecycle, dict) and lifecycle.get("state") == "running" and all(
            lifecycle.get("pod", {}).get(key) == pod.get(key) for key in
            ("uid", "started_at", "container_id", "restart_count", "image", "image_id")):
        report.update(status="warming", reason="independently observed bounded representative warmup is in progress")
    elif state == "incomplete":
        report.update(status="unknown", reason="incomplete warmup record has no trustworthy liveness evidence")
    elif isinstance(state, str) and state.startswith("failed"):
        report.update(status="healthy" if health == "healthy" else "unknown",
                      warmup_status="failed", reason="representative warmup failed; no automatic retry")
    else:
        report.update(status="unknown", reason="warmup completion/provenance is incomplete")
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("loading-plan")
    p.add_argument("deployment"); p.add_argument("evidence"); p.add_argument("output")
    p.add_argument("--threads", required=True); p.add_argument("--reserve-mib", type=int, required=True)
    p.add_argument("--per-thread-mib", type=int, required=True)
    p = sub.add_parser("queue-plan")
    p.add_argument("deployment"); p.add_argument("evidence"); p.add_argument("output")
    p.add_argument("--maximum-queued", type=int, required=True)
    p = sub.add_parser("warm-status")
    p.add_argument("evidence"); p.add_argument("warmup"); p.add_argument("output")
    p = sub.add_parser("pod-status")
    p.add_argument("pod"); p.add_argument("output")
    args = parser.parse_args()
    if args.mode == "loading-plan":
        observed = evidence(args.evidence)
        result = loading_plan(json.loads(Path(args.deployment).read_text()), observed,
                              [int(value) for value in args.threads.split(",")], args.reserve_mib, args.per_thread_mib)
    elif args.mode == "queue-plan":
        observed = evidence(args.evidence)
        result = queue_plan(json.loads(Path(args.deployment).read_text()), observed, args.maximum_queued)
    elif args.mode == "warm-status":
        observed = evidence(args.evidence)
        warmup = None if args.warmup == "-" else json.loads(Path(args.warmup).read_text())
        result = warm_status(observed, warmup)
    else:
        result = pod_status(json.loads(Path(args.pod).read_text()))
    output = Path(args.output)
    if args.mode.endswith("plan"):
        output.mkdir(mode=0o700)
        write(output / "plan.json", result)
        for name, case in result.get("cases", {}).items():
            write(output / (name + ".json"), case["patch"])
        if "patch" in result:
            write(output / "patch.json", result["patch"])
    else:
        write(output, result)


if __name__ == "__main__":
    main()
