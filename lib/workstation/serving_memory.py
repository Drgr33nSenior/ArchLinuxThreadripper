"""Offline SGLang host-memory candidates, never automatic resource changes.

Require repeated startup and serving evidence. Limits are not observations;
lifetime peaks are conservative high-water marks, not per-phase allocations.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re

from model_kernels import identity, mib, write
from serving import finite, validate

MIB = 1024**2


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("missing or invalid nonnegative memory counter")
    return int(value)


def counters(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("missing memory counters")
    result = {}
    for line in text.splitlines():
        key, value = line.split()
        if key in result:
            raise ValueError("duplicate memory counter")
        result[key] = integer(value)
    return result


def pressure(text):
    result = {}
    if not isinstance(text, str):
        raise ValueError("memory PSI unavailable")
    for line in text.splitlines():
        name, *fields = line.split()
        if name in result:
            raise ValueError("duplicate PSI scope")
        fields = dict(field.split("=") for field in fields)
        result[name] = integer(fields["total"])
    if set(result) != {"some", "full"}:
        raise ValueError("incomplete memory PSI")
    return result


def window(samples, limit_mib, shm_mib, total_mib, minimum_seconds):
    if len(samples) < 2:
        raise ValueError("at least two pod memory samples are required")
    parsed = []
    for sample in samples:
        scope = sample["pod_cgroup"]
        if not re.fullmatch(r"[0-9]+:[0-9]+", scope.get("id", "")) or not scope.get("path"):
            raise ValueError("stable pod cgroup identity unavailable")
        values = scope["values"]
        current, peak, maximum = (integer(values[key]) for key in ("memory.current", "memory.peak", "memory.max"))
        if maximum != limit_mib * MIB or not 0 < current <= peak <= maximum:
            raise ValueError("observed pod memory limit/current/peak does not match the baseline")
        stat, events = counters(values["memory.stat"]), counters(values["memory.events"])
        for key in ("anon", "file", "shmem"):
            integer(stat[key])
        if stat["shmem"] > stat["file"] or stat["shmem"] > current:
            raise ValueError("inconsistent shared-memory accounting")
        # Existing memory pressure means the baseline may already be constrained.
        # Do not recommend a smaller cap from such a window, even without an OOM.
        if any(events[key] != 0 for key in ("high", "max", "oom", "oom_kill")):
            raise ValueError("memory pressure/limit/OOM event; reduction is not justified")
        if integer(values["memory.swap.current"]) != 0:
            raise ValueError("require the no-swap pod baseline")
        psi = pressure(values["memory.pressure"])
        meminfo = {}
        for line in sample["host_memory"].splitlines():
            fields = line.split()
            if fields[0] in ("MemTotal:", "MemAvailable:", "SwapTotal:"):
                if fields[-1] != "kB":
                    raise ValueError("unexpected host memory units")
                meminfo[fields[0][:-1]] = integer(fields[1])
        if meminfo["MemTotal"] // 1024 != total_mib:
            raise ValueError("host memory inventory changed; regenerate the resource plan")
        # NoSwap can be set on the child container while its parent says max.
        # On this project's no-swap host, validate actual swap availability too.
        if meminfo["SwapTotal"] != 0:
            raise ValueError("host swap is enabled; require the no-swap baseline")
        parsed.append({"id": scope["id"], "path": scope["path"], "ns": integer(sample["monotonic_ns"]),
                       "unix_ns": integer(sample["unix_ns"]), "current": current, "peak": peak,
                       "stat": stat, "psi": psi, "available": meminfo["MemAvailable"] * 1024})
    first = parsed[0]
    for previous, row in zip(parsed, parsed[1:]):
        if (row["id"], row["path"]) != (first["id"], first["path"]):
            raise ValueError("pod cgroup changed within a memory window")
        gap = (row["ns"] - previous["ns"]) / 1e9
        if not 0 < gap <= 30 or row["unix_ns"] <= previous["unix_ns"] or row["peak"] < previous["peak"]:
            raise ValueError("memory samples have a gap, clock reversal or reset peak")
        if any(row["psi"][key] != previous["psi"][key] for key in ("some", "full")):
            raise ValueError("memory PSI increased or reset; investigate pressure before reducing")
    seconds = (parsed[-1]["ns"] - first["ns"]) / 1e9
    if seconds < minimum_seconds:
        raise ValueError("memory observation is too short")
    return {"samples": len(parsed), "duration_seconds": seconds, "cgroup_id": first["id"],
            "first_monotonic_ns": first["ns"], "last_monotonic_ns": parsed[-1]["ns"],
            "path": first["path"], "first_unix_ns": first["unix_ns"], "last_unix_ns": parsed[-1]["unix_ns"],
            "sampled_current_max_bytes": max(r["current"] for r in parsed),
            "lifetime_peak_bytes": max(r["peak"] for r in parsed),
            "sampled_stat_max_bytes": {key: max(r["stat"][key] for r in parsed) for key in ("anon", "file", "shmem")},
            "host_available_min_bytes": min(r["available"] for r in parsed),
            # stat.shmem also includes SysV/shared anonymous mappings; it does
            # not identify /dev/shm usage. Keep it informational. Reserve the
            # full ceiling as potential growth without subtracting unrelated
            # shared memory. This conservative reserve can overlap prior usage.
            "full_shm_envelope_bytes": max(r["peak"] for r in parsed) + shm_mib*MIB}


class Inputs:
    def __init__(self):
        self.hashes = {}

    def read(self, path, jsonl=False):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * MIB:
            raise ValueError("evidence must be a regular bounded file")
        data = path.read_bytes()
        self.hashes[str(path.resolve())] = hashlib.sha256(data).hexdigest()
        return [json.loads(line) for line in data.splitlines()] if jsonl else json.loads(data)

    def unchanged(self):
        for path, digest in self.hashes.items():
            if Path(path).is_symlink() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                raise ValueError("evidence changed while planning")


def pod_identity(pod):
    keys = ("uid", "node", "image", "image_id", "started_at", "container_id", "restart_count")
    if any(pod.get(key) in (None, "") for key in keys) or integer(pod["restart_count"]) != 0:
        raise ValueError("require a fresh, identifiable pod without container restarts")
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", pod["uid"]):
        raise ValueError("require the actual pod UUID")
    return {key: pod[key] for key in keys}


def check_runs(result, workload):
    expected = {(case["context_tokens"], count, repeat): case["output_tokens"]
                for case in workload["cases"] for count in workload["concurrency"]
                for repeat in range(workload["repetitions"])}
    seen = set()
    for run in result["runs"]:
        key = (run["context_tokens"], run["concurrency"], run["repetition"])
        if key not in expected or key in seen or run["failures"] != 0 or run["cache_state_verified"] is not True:
            raise ValueError("failed, missing or duplicate serving workload cases")
        seen.add(key)
        if not finite(run["wall_seconds"]) or run["wall_seconds"] <= 0:
            raise ValueError("invalid serving workload duration")
        requests = run["requests"]
        if len(requests) != run["concurrency"] * workload["requests_per_worker"]:
            raise ValueError("missing serving requests")
        for request in requests:
            if request["ok"] is not True or request["output_tokens"] != expected[key]:
                raise ValueError("failed or incomplete serving request")
            for metric in ("ttft_seconds", "latency_seconds"):
                if not finite(request[metric]) or request[metric] <= 0:
                    raise ValueError("invalid serving timing")
    if seen != set(expected):
        raise ValueError("missing required serving workload case")


def plan(deployment_path, workload_path, resource_path, observations, other_mib,
         margin_mib=2048, margin_percent=25, minimum_seconds=60, memory_mib=None):
    inputs = Inputs()
    deployment, workload, resource = (inputs.read(p) for p in (deployment_path, workload_path, resource_path))
    for number in (other_mib, margin_mib, margin_percent, minimum_seconds):
        integer(number)
    if margin_mib < 512 or not 10 <= margin_percent <= 100 or not 30 <= minimum_seconds <= 21600:
        raise ValueError("require >=512 MiB and 10..100 percent headroom, 30..21600 seconds steady observation")
    spec = deployment["spec"]["template"]["spec"]
    if (deployment.get("kind") != "Deployment" or len(spec["containers"]) != 1
            or spec.get("initContainers") or spec["containers"][0]["name"] != "sglang"):
        raise ValueError("only the existing single-container SGLang deployment is supported")
    container = spec["containers"][0]
    resources = container["resources"]
    launch_template = {key: container.get(key, []) for key in ("command", "args", "env", "envFrom")}
    launch_hash = hashlib.sha256((json.dumps(launch_template, sort_keys=True, separators=(",", ":"), ensure_ascii=False)+"\n").encode()).hexdigest()
    if resources["requests"] != resources["limits"]:
        raise ValueError("retain equal requests/limits for Guaranteed QoS")
    old = mib(resources["limits"]["memory"])
    shm = [v for v in spec["volumes"] if v.get("emptyDir", {}).get("medium") == "Memory"]
    if len(shm) != 1 or shm[0]["name"] != "shm":
        raise ValueError("require the existing single bounded shm volume")
    shm_mib = mib(shm[0]["emptyDir"]["sizeLimit"])
    if not 0 < shm_mib < old:
        raise ValueError("shared memory must fit inside the pod budget")
    memory = resource["memory"]
    reserves = sum(integer(memory[key]) for key in ("host_reserve_mib", "kube_reserve_mib", "eviction_mib"))
    if (resource.get("schema_version") != 1 or resource.get("status") != "offline-plan-not-applied" or resource.get("gpu_count") != 2
            or not 0 < integer(memory["allocatable_mib"]) == integer(memory["total_mib"]) - reserves
            or reserves <= 0):
        raise ValueError("require the existing discovered two-GPU resource plan with host headroom")
    lock_path = Path(__file__).resolve().parents[2]/"versions.lock"
    lock_data = lock_path.read_bytes()
    inputs.hashes[str(lock_path)] = hashlib.sha256(lock_data).hexdigest()
    locks = dict(line.split("=", 1) for line in lock_data.decode().splitlines()
                 if line and not line.startswith("#"))
    if container["image"] != locks["SGLANG_ROCM_IMAGE"]:
        raise ValueError("preserve the locked SGLang image")
    phases, processes, records = {"cold": 0, "warm": 0}, set(), []
    runtime_key = None
    for startup_dir, run_dir in observations:
        startup_dir, run_dir = Path(startup_dir), Path(run_dir)
        start, result = inputs.read(startup_dir/"startup.json"), inputs.read(run_dir/"result.json")
        if (start.get("schema") != 1 or start.get("status") != "observed-not-qualified"
                or start.get("memory_telemetry") != "memory.jsonl" or start.get("cache_state") not in phases
                or result.get("schema") != 1 or result.get("status") != "measured-not-qualified"):
            raise ValueError("require successful memory-enabled cold/warm startup and serving records")
        pod, runtime = result["pod"], result["runtime"]
        observed = pod_identity(pod)
        if (observed != pod_identity(start["pod"]) or observed != pod_identity(start["pod_before"])
                or start["pod_before"]["ready"] is not False or start["pod"]["ready"] is not True
                or pod.get("ready") is not True or pod["resources"] != resources
                or start["pod"]["resources"] != resources or start["pod_before"]["resources"] != resources
                or any(p.get("launch_spec_sha256") != launch_hash for p in (pod, start["pod"], start["pod_before"]))):
            raise ValueError("startup/serving identity, readiness or resources differ")
        if any(p.get("shm") != [shm[0]["emptyDir"]] for p in (pod, start["pod"], start["pod_before"])):
            raise ValueError("observed shared-memory ceiling differs from the rendered baseline")
        if pod["uid"] in processes:
            raise ValueError("each observation needs a distinct fresh pod, not relabelled evidence")
        processes.add(pod["uid"])
        if pod["image"] != container["image"] or not pod["image_id"].endswith(tuple(
                (locks["SGLANG_ROCM_IMAGE"].split("@")[-1], locks["SGLANG_ROCM_CONFIG_DIGEST"]))):
            raise ValueError("observed runtime image differs from the pinned image")
        validate(workload, runtime)
        devices = runtime["devices"]
        if (len(devices) != integer(resources["limits"]["amd.com/gpu"]) or len(devices) not in (1, 2)
                or int(runtime["settings"]["TENSOR_PARALLEL"]) != len(devices)
                or any(not d.get("uuid") or d["uuid"] == "unknown" or d["gfx"].split(":")[0] != "gfx1201" for d in devices)
                or len({d["uuid"] for d in devices}) != len(devices)):
            raise ValueError("require unique allocated gfx1201 devices matching TP")
        key = identity({"runtime": runtime, "image_id": pod["image_id"], "node": pod["node"]})
        if runtime_key not in (None, key):
            raise ValueError("model/software/GPU/launch settings changed across observations")
        runtime_key = key
        if (result["workload_sha256"] != inputs.hashes[str(Path(workload_path).resolve())]
                or inputs.read(run_dir/"after/runtime.json") != runtime or inputs.read(run_dir/"after/pod.json") != pod
                or result["runtime_sha256"] != inputs.hashes[str((run_dir/"after/runtime.json").resolve())]):
            raise ValueError("workload or final model/runtime provenance differs")
        check_runs(result, workload)
        startup = window(inputs.read(startup_dir/"memory.jsonl", True), old, shm_mib, memory["total_mib"], 0)
        steady = window(inputs.read(run_dir/"host-telemetry.jsonl", True), old, shm_mib, memory["total_mib"], minimum_seconds)
        if (startup["cgroup_id"], startup["path"]) != (steady["cgroup_id"], steady["path"]) or startup["last_unix_ns"] > steady["first_unix_ns"]:
            raise ValueError("startup and serving windows overlap or describe different cgroups")
        name = Path(steady["path"]).name
        if name != "pod"+pod["uid"] and not name.endswith("-pod"+pod["uid"].replace("-", "_")+".slice"):
            raise ValueError("sampled cgroup does not identify this pod UID")
        begin, finished, end = (integer(result[key]) for key in
                               ("measurement_started_monotonic_ns", "workload_finished_monotonic_ns", "measurement_finished_monotonic_ns"))
        if (not begin < finished <= end or not 0 <= steady["first_monotonic_ns"]-begin <= 30*10**9
                or not finished <= steady["last_monotonic_ns"] <= end
                or sum(r["wall_seconds"] for r in result["runs"]) > (finished-begin)/1e9):
            raise ValueError("telemetry does not cover the serving measurement window")
        phases[start["cache_state"]] += 1
        records.append({"phase": start["cache_state"], "pod_uid": pod["uid"], "startup": startup, "steady": steady})
    if min(phases.values()) < 2 or len(records) > 20:
        raise ValueError("require at least two cold and two warm fresh-pod observations, maximum twenty total")
    envelope = max(w["full_shm_envelope_bytes"] for r in records for w in (r["startup"], r["steady"]))
    safety = max(margin_mib*MIB, math.ceil(envelope*margin_percent/100))
    proposed = math.ceil((envelope+safety)/(256*MIB))*256
    selected = proposed if memory_mib is None else integer(memory_mib)
    if selected < proposed or selected >= old:
        raise ValueError("no justified reduction fits; selected memory must cover evidence plus headroom and be below baseline")
    if selected + other_mib > memory["allocatable_mib"]:
        raise ValueError("candidate plus other workloads exceeds the discovered resource plan")
    candidate = copy.deepcopy(spec)
    changed = candidate["containers"][0]["resources"]
    changed["requests"]["memory"] = changed["limits"]["memory"] = f"{selected}Mi"
    root = "/spec/template/spec"
    def patch(before, after):
        return [{"op": "test", "path": root, "value": before},
                {"op": "replace", "path": root+"/containers/0/resources", "value": after["containers"][0]["resources"]}]
    inputs.unchanged()
    return {"schema": 1, "kind": "sglang-host-memory-plan", "status": "plan-only-unqualified",
            "baseline_mib": old, "candidate_mib": selected, "minimum_candidate_mib": proposed,
            "workload_sha256": inputs.hashes[str(Path(workload_path).resolve())], "runtime_identity": runtime_key,
            "baseline_spec_sha256": identity(spec), "candidate_spec_sha256": identity(candidate),
            "budget": {"other_workloads_mib": other_mib, "allocatable_mib": memory["allocatable_mib"],
                       "shm_limit_mib": shm_mib, "observed_envelope_bytes": envelope, "headroom_bytes": safety},
            "observations": records, "evidence_sha256": inputs.hashes,
            "tool_sha256": {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                            for name in ("serving_memory.py", "measurement.py", "performance.sh", "serving.py", "model_kernels.py")},
            "patch": patch(spec, candidate), "rollback": patch(candidate, spec),
            "limitations": ["A plan is not measured safety at the smaller limit, qualification or permission to apply.",
                            "Cold/warm cache labels are owner declarations. Startup begins after Running; peak is lifetime.",
                            "No file/shared memory is subtracted. Full shm growth reserve may overlap prior usage; it is not measured consumption.",
                            "Other-workload budget is owner-supplied; recheck live admission and host headroom before applying.",
                            "Re-run startup, steady memory, latency/throughput and numerical/coding quality on the candidate.",
                            "Regenerate compilation worker plans after resizing; do not reuse the old worker allowance."]}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment")
    parser.add_argument("workload")
    parser.add_argument("resource_plan")
    parser.add_argument("output")
    parser.add_argument("--observation", nargs=2, action="append", required=True, metavar=("STARTUP_DIR", "SERVING_DIR"))
    parser.add_argument("--other-mib", type=int, required=True)
    parser.add_argument("--margin-mib", type=int, default=2048)
    parser.add_argument("--margin-percent", type=int, default=25)
    parser.add_argument("--minimum-seconds", type=int, default=60)
    parser.add_argument("--memory-mib", type=int)
    args = parser.parse_args()
    result = plan(args.deployment, args.workload, args.resource_plan, args.observation, args.other_mib,
                  args.margin_mib, args.margin_percent, args.minimum_seconds, args.memory_mib)
    output = Path(args.output)
    output.mkdir(mode=0o700)
    write(output/"patch.json", result.pop("patch"))
    write(output/"rollback.json", result.pop("rollback"))
    result["artifacts_sha256"] = {name: hashlib.sha256((output/name).read_bytes()).hexdigest()
                                  for name in ("patch.json", "rollback.json")}
    write(output/"plan.json", result)


if __name__ == "__main__":
    main()
