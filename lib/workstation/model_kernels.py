"""Opt-in offline kernel plans and evidence checks. Never apply or restart a Pod."""
import argparse
import copy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

from serving import validate, finite, summary


def load(path):
    return json.loads(Path(path).read_text())


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def mib(value):
    match = re.fullmatch(r"([1-9][0-9]*)(Mi|Gi)", str(value))
    if not match:
        raise ValueError("use explicit integer Mi/Gi memory limits")
    return int(match[1]) * (1024 if match[2] == "Gi" else 1)


def evidence(directory):
    directory = Path(directory)
    pod, runtime, compiler = (load(directory / name) for name in ("pod.json", "runtime.json", "compiler.json"))
    lock = dict(line.split("=", 1) for line in (Path(__file__).resolve().parents[2] / "versions.lock").read_text().splitlines()
                if line and not line.startswith("#"))
    if (pod["image"] != lock["SGLANG_ROCM_IMAGE"] or not pod.get("image_id") or not pod.get("uid")
            or compiler["status"] != "observed-not-qualified" or compiler["schema"] != 1):
        raise ValueError("require observed compiler evidence from the pinned image")
    actual_digest = re.search(r"sha256:[a-f0-9]{64}$", pod["image_id"])
    if not actual_digest or actual_digest[0] not in (lock["SGLANG_ROCM_IMAGE"].split("@")[-1], lock["SGLANG_ROCM_CONFIG_DIGEST"]):
        raise ValueError("runtime image ID does not identify the locked amd64 manifest/config")
    if compiler["sources"].get("srt/server_args.py") != lock["SGLANG_SERVER_ARGS_SHA256"]:
        raise ValueError("image CLI source differs from reviewed contract; review before planning")
    if compiler["sources"].get("srt/compilation/torch_compile_decoration.py") != lock["SGLANG_COMPILE_DECORATION_SHA256"]:
        raise ValueError("image compilation source differs from reviewed contract")
    if compiler["sources"].get("srt/models/qwen3_5.py") != lock["SGLANG_QWEN35_SOURCE_SHA256"]:
        raise ValueError("image model implementation differs from reviewed contract")
    devices = runtime["devices"]
    uuids = [d["uuid"] for d in devices]
    if (len(devices) not in (1, 2) or len(set(uuids)) != len(uuids)
            or any(not u or u.lower() in ("unknown", "none") for u in uuids)
            or any(d["gfx"].split(":")[0] != "gfx1201" for d in devices)):
        raise ValueError("require distinct observed gfx1201 GPU identities, not ordinal assumptions")
    if not compiler["compiler"] or not compiler["loaded_libraries"] or not compiler["packages"]:
        raise ValueError("compiler and loaded GPU-library identity unavailable")
    if runtime.get("model_contract", {}).get("architectures") != ["Qwen3_5ForConditionalGeneration"]:
        raise ValueError("model architecture not reviewed for this opt-in workflow")
    return pod, runtime, compiler


def runtime_key(pod, runtime, compiler):
    # Deliberately excludes mutable cache contents, PIDs, clocks and memory usage.
    return {"image": pod["image"], "image_id": pod["image_id"], "resources": pod["resources"],
            "runtime": runtime, "compiler": {key: compiler[key] for key in
                ("python", "host_kernel", "amdgpu_srcversion", "packages", "hip", "compiler", "sources", "loaded_libraries", "settings")}}


def plan(deployment, workload, observed, profile, workers, reserve_mib, worker_mib, reserve_cpus, experimental=False):
    pod, runtime, compiler = observed
    validate(workload, runtime)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    indexes = [i for i, c in enumerate(containers) if c["name"] == "sglang"]
    if len(indexes) != 1:
        raise ValueError("expected one SGLang container")
    index, container = indexes[0], containers[indexes[0]]
    resources = container["resources"]
    if resources != pod["resources"] or resources["requests"] != resources["limits"] or container["image"] != pod["image"]:
        raise ValueError("rendered baseline must match observed Guaranteed Pod resources/image")
    spec = deployment["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    if mounts.get("cache") != "/cache" or "persistentVolumeClaim" not in volumes["cache"]:
        raise ValueError("reuse the existing persistent cache mount")
    shm = mib(volumes["shm"]["emptyDir"]["sizeLimit"])
    tp = int(runtime["settings"]["TENSOR_PARALLEL"])
    if tp != len(runtime["devices"]) or tp != int(resources["limits"]["amd.com/gpu"]):
        raise ValueError("TP ranks must match observed exclusive GPU allocation")
    max_requests = int(runtime["settings"]["MAX_RUNNING_REQUESTS"])
    batches = sorted(workload["concurrency"])
    if max(batches) > max_requests:
        raise ValueError("workload exceeds running-request baseline; qualify concurrency separately")
    cg = compiler["cgroup"]
    quota, period = cg["cpu.max"].split()
    if quota == "max" or cg["memory.max"] in (None, "max"):
        raise ValueError("finite observed pod CPU/RAM cgroup limits are required")
    cpu = min(float(resources["limits"]["cpu"]), int(quota) / int(period), compiler["affinity_cpus"])
    memory = min(mib(resources["limits"]["memory"]), int(cg["memory.max"]) // 1024**2)
    if (type(reserve_mib) is not int or type(worker_mib) is not int or worker_mib < 256
            or not finite(reserve_cpus) or reserve_cpus < 1
            or reserve_mib < max(shm, math.ceil(int(cg["memory.current"]) / 1024**2))):
        raise ValueError("reserve the observed host-model envelope including shm; workers need >=256 MiB each")
    maximum = min(16, math.floor((cpu - reserve_cpus) / tp), (memory - reserve_mib) // (worker_mib * tp))
    if maximum < 1 or not compiler["compile_threads_supported"]:
        raise ValueError("no safe compilation worker fits, or Inductor worker control is unsupported")
    if (not workers or len(set(workers)) != len(workers)
            or any(type(w) is not int or w < 1 or w > maximum for w in workers)):
        raise ValueError(f"each requested per-rank worker count must fit 1..{maximum}; no silent clipping")
    args = list(container["args"])
    if any(a in args for a in ("--dp", "--dp-size", "--nnodes", "--pp-size", "--enable-dp-attention")):
        raise ValueError("this bounded planner supports only the existing single-node TP deployment")
    if any(a.startswith(("--cuda-graph", "--torch-compile", "--enable-torch-compile", "--disable-cuda-graph")) for a in args):
        raise ValueError("start from the unmodified baseline, not a previous compilation candidate")
    required = []
    if profile in ("capture", "legacy-compile"):
        required = ["--cuda-graph-bs-decode"]
        args += [required[0], *map(str, batches)]
    if profile == "legacy-compile":
        if not experimental:
            raise ValueError("legacy torch.compile requires --experimental; model execution remains unqualified")
        required += ["--enable-torch-compile", "--torch-compile-max-bs"]
        args += ["--enable-torch-compile", "--torch-compile-max-bs", str(max(batches))]
    elif profile not in ("cache", "capture"):
        raise ValueError("unsupported profile; no forced piecewise/Instinct defaults")
    if not set(required) <= set(compiler["flags"]):
        raise ValueError("requested flags not supported by exact-image help")
    base = {"software_hardware_model": runtime_key(pod, runtime, compiler), "deployment_sha256": identity(deployment),
            "workload_sha256": identity(workload), "profile": profile, "batches": batches,
            "memory_reserve_mib": reserve_mib, "worker_mib": worker_mib, "cpu_reserve": reserve_cpus}
    cases = {}
    for count in workers:
        key = identity({**base, "workers_per_rank": count})
        env = {"TORCHINDUCTOR_COMPILE_THREADS": str(count), "TORCHINDUCTOR_FX_GRAPH_CACHE": "1",
               "TRITON_CACHE_DIR": f"/cache/triton/workstation/{key}",
               "TORCHINDUCTOR_CACHE_DIR": f"/cache/torchinductor/workstation/{key}"}
        # Keep verbose compile/cache diagnostics out of ordinary serving profiles.
        if profile == "legacy-compile":
            env["TORCH_LOGS"] = "+inductor"
        entries = [e for e in container.get("env", []) if e["name"] not in env]
        entries += [{"name": name, "value": value} for name, value in env.items()]
        root = f"/spec/template/spec/containers/{index}"
        cases[f"workers-{count}"] = {"cache_identity": key, "workers_per_rank": count, "total_workers": count * tp,
            "patch": [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(spec)},
                      {"op": "replace", "path": root + "/args", "value": args},
                      {"op": "add", "path": root + "/env", "value": entries}]}
    return {"schema": 1, "status": "plan-only-unqualified", "identity": base, "tp": tp,
            "maximum_workers_per_rank": maximum, "cases": cases,
            "compatibility": "CLI/source checked; model may disable compilation. Require actual dispatch, cache reuse, numerical and memory checks."}


def quality_compare(first, second, atol, rtol):
    if not (finite(atol) and finite(rtol) and 0 <= atol <= .1 and 0 <= rtol <= .1):
        raise ValueError("explicit numerical tolerances must be finite within 0..0.1")
    if not first or len(first) != len(second):
        raise ValueError("missing numerical workload cases")
    differences = []
    for a, b in zip(first, second):
        if a["case"] != b["case"] or a["tokens"] != b["tokens"] or not a["tokens"]:
            raise ValueError("deterministic output tokens or workload shapes differ")
        if len(a["logprobs"]) != len(a["tokens"]) or len(b["logprobs"]) != len(b["tokens"]):
            raise ValueError("missing output logprobs")
        for x, y in zip(a["logprobs"], b["logprobs"]):
            if not finite(x) or not finite(y) or abs(x-y) > atol + rtol * abs(x):
                raise ValueError("nonfinite or changed numerical output")
            differences.append(abs(x-y))
    return {"status": "sampled-output-check-passed", "atol": atol, "rtol": rtol,
            "absolute_logprob_difference": summary(differences), "scope": "not full model or coding-task qualification"}


def compare_quality(baseline, candidate, atol, rtol, memory_only=False):
    for result in (baseline, candidate):
        if (result["status"] != "measured-not-qualified" or result.get("kind") != "kernel-warmup"
                or result.get("memory", {}).get("status") != "checked"):
            raise ValueError("require successful unprofiled, memory-checked runs")
        contract = result["warmup_contract"]
        if contract["repetitions"] != 3 or not contract["cases"] or not contract["concurrency"]:
            raise ValueError("missing complete warmup contract")
        expected = [(repeat, case["context_tokens"], batch, index, case["output_tokens"])
                    for repeat in range(3) for case in contract["cases"]
                    for batch in contract["concurrency"] for index in range(batch)]
        actual = [(*row["case"], len(row["tokens"])) for row in result["quality"]]
        if expected != actual:
            raise ValueError("missing, repeated or incompatible numerical workload cases")
    if baseline["workload_sha256"] != candidate["workload_sha256"]:
        raise ValueError("quality workload differs")
    if baseline["warmup_contract"] != candidate["warmup_contract"]:
        raise ValueError("warmup shapes differ")
    a, b = baseline["identity"], candidate["identity"]
    if memory_only:
        # Explicitly compare a smaller host-memory envelope, not another model,
        # compiler, GPU, launch configuration or CPU allocation. Keep the
        # existing strict comparison unchanged for all other callers.
        normalized = []
        for item in (a, b):
            item = copy.deepcopy(item)
            resources = item["resources"]
            if resources["requests"] != resources["limits"]:
                raise ValueError("memory-only quality requires equal requests/limits")
            for scope in ("requests", "limits"):
                resources[scope].pop("memory")
            normalized.append(item)
        if normalized[0] != normalized[1] or mib(b["resources"]["limits"]["memory"]) >= mib(a["resources"]["limits"]["memory"]):
            raise ValueError("memory-only comparison permits only a smaller host-memory request/limit")
    for key in ("image", "image_id", "resources"):
        if memory_only and key == "resources":
            continue
        if a[key] != b[key]:
            raise ValueError("quality comparison changed image/resources")
    for key in ("model_files", "packages", "devices", "hip"):
        if a["runtime"][key] != b["runtime"][key]:
            raise ValueError("quality comparison changed model/software/GPU identity")
    return {"schema": 1, "status": "sampled-quality-passed-not-qualified",
            "comparison": "host-memory-only" if memory_only else "kernel",
            "baseline_sha256": identity(baseline), "candidate_sha256": identity(candidate),
            "quality": quality_compare(baseline["quality"], candidate["quality"], atol, rtol)}


def compare(cold, warm, cold_start, warm_start, logs, atol, rtol):
    a, b = load(Path(cold)/"result.json"), load(Path(warm)/"result.json")
    if any(r["status"] != "measured-not-qualified" or r.get("kind") != "kernel-warmup" for r in (a, b)):
        raise ValueError("require successful unprofiled kernel warmup/steady runs")
    if a["identity"] != b["identity"] or a["workload_sha256"] != b["workload_sha256"]:
        raise ValueError("software/GPU/model/settings/workload drift across restart")
    if (a["pod"]["uid"], a["pod"]["started_at"]) == (b["pod"]["uid"], b["pod"]["started_at"]):
        raise ValueError("no observed process restart")
    starts = [load(cold_start), load(warm_start)]
    for start, result in zip(starts, (a, b)):
        if (start["status"] != "observed-not-qualified" or start["pod"]["uid"] != result["pod"]["uid"]
                or start["pod"]["started_at"] != result["pod"]["started_at"]
                or not finite(start["elapsed_seconds"]) or start["elapsed_seconds"] < 0):
            raise ValueError("startup observation belongs to a different/failed process")
    checks = compare_quality(a, b, atol, rtol)
    before, after = load(Path(cold)/"compiler-after.json"), load(Path(warm)/"compiler-before.json")
    unchanged = 0
    for name, cache in before["caches"].items():
        other = after["caches"][name]
        if cache["root"] != other["root"]:
            raise ValueError("restart did not retain the same cache namespace")
        unchanged += sum(other["files"].get(path) == record for path, record in cache["files"].items())
    if not unchanged:
        raise ValueError("no retained identical compiler artifacts")
    # Logs are separately retained per TP worker. Files alone do not prove hits.
    tp = int(a["identity"]["runtime"]["settings"]["TENSOR_PARALLEL"])
    if len(logs) != tp or len({file_hash(path) for path in logs}) != tp:
        raise ValueError("provide distinct restart cache-hit logs for every TP rank")
    hits = []
    for path in logs:
        text = Path(path).read_text()
        count = len(re.findall(r"fx graph cache hit", text, re.I))
        if not count:
            raise ValueError("no explicit Inductor cache hit; persisted Triton files alone are inconclusive")
        hits.append({"sha256": file_hash(path), "hits": count})
    return {"schema": 1, "status": "reuse-observed-not-qualified", "identity": a["identity"],
            "workload_sha256": a["workload_sha256"], "quality": checks, "retained_files": unchanged,
            "operator_attributed_rank_logs": hits, "startup_seconds": [s["elapsed_seconds"] for s in starts],
            "warmup_seconds": [a["warmup_seconds"], b["warmup_seconds"]],
            "steady_state": [a["steady_state"], b["steady_state"]],
            "limitations": ["Startup includes model load/JIT/capture; not pure compilation time.",
                            "Rank-log attribution needs owner review. A single restart pair is not promotion evidence.",
                            "Disk/JIT cache state and prefix-cache state are distinct; no automatic cache deletion occurs."]}


def dispatch(trace, run):
    if run["status"] != "measured-not-qualified" or run.get("kind") != "kernel-profile":
        raise ValueError("trace requires a successful, identity-bound profile run")
    opener = gzip.open if str(trace).endswith(".gz") else open
    with opener(trace, "rt") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
        if len(raw) > 128 * 1024 * 1024:
            raise ValueError("trace exceeds bounded 128 MiB parser; reduce profiling steps")
        events = json.loads(raw)["traceEvents"]
    kernels, operators = {}, set()
    for event in events:
        name = event.get("name", "")
        if event.get("cat") == "kernel" and event.get("ph") == "X":
            duration = event.get("dur")
            if not finite(duration) or duration < 0 or not name:
                raise ValueError("invalid GPU kernel event")
            kernels[name] = kernels.get(name, 0) + duration
        if event.get("cat") in ("cpu_op", "hip_runtime", "cuda_runtime"):
            operators.add(name)
    if not kernels:
        raise ValueError("no actual GPU dispatch in trace; CPU-only profiling is insufficient")
    return {"schema": 1, "status": "dispatch-observed-not-qualified", "trace_sha256": file_hash(trace),
            "run_sha256": identity(run), "identity": run["identity"], "workload_sha256": run["workload_sha256"],
            "kernels_us": kernels, "host_operators": sorted(operators),
            "scope": "owner must verify trace belongs to recorded profile path/ranks; kernel names alone do not establish library ownership"}


def seal_tuning(run, observed, proof, artifact, kind, output):
    # A reviewed API/library log plus an exact observed kernel is required before
    # treating an offline tuning result as eligible for an experiment. No loader
    # override is enabled here, and a saved tuning file is not model qualification.
    if observed["run_sha256"] != identity(run) or observed["identity"] != run["identity"]:
        raise ValueError("dispatch/run provenance mismatch")
    record = load(proof)
    if record["backend"] != kind or record["kernel"] not in observed["kernels_us"]:
        raise ValueError("review must identify an actually dispatched kernel")
    filename = record["library_log"]
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", filename):
        raise ValueError("library log must be a direct sibling filename")
    log = Path(proof).parent / filename
    if log.is_symlink() or not log.is_file() or not 0 < log.stat().st_size <= 8 * 1024**2:
        raise ValueError("require a bounded regular dispatch log")
    if file_hash(log) != record["library_log_sha256"]:
        raise ValueError("library log changed")
    marker = "hipblasLtMatmul" if kind == "hipblaslt" else "triton"
    if marker not in log.read_text():
        raise ValueError("no library dispatch marker; do not infer tuning eligibility from GPU model")
    if (not isinstance(record.get("shape"), list) or not record["shape"]
            or any(type(n) is not int or n < 1 for n in record["shape"]) or not record.get("dtype")):
        raise ValueError("review must record the actual operation shape and dtype")
    if Path(artifact).is_symlink() or not 0 < Path(artifact).stat().st_size <= 64 * 1024**2:
        raise ValueError("require a bounded regular tuning result")
    output = Path(output)
    output.mkdir(mode=0o700)
    shutil.copyfile(artifact, output / "tuning-result")
    write(output / "manifest.json", {"schema": 1, "status": "eligible-experiment-not-qualified", "kind": kind,
          "identity": run["identity"], "workload_sha256": run["workload_sha256"],
          "dispatch_sha256": identity(observed), "review_sha256": file_hash(proof),
          "reviewed_operation": {key: record[key] for key in ("kernel", "shape", "dtype", "library_log_sha256")},
          "artifact_sha256": file_hash(output/"tuning-result"),
          "suggested_persistent_directory": "/cache/xdg/workstation-tuning/" + identity(run["identity"]),
          "scope": "operator-reviewed evidence, not automatic attribution or promotion; rerun numerical/memory/performance checks with this exact result"})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    for name in ("deployment", "workload", "evidence", "output"):
        p.add_argument(name)
    p.add_argument("--profile", choices=("cache", "capture", "legacy-compile"), required=True)
    p.add_argument("--workers", default="1,2")
    p.add_argument("--reserve-mib", type=int, required=True)
    p.add_argument("--worker-mib", type=int, required=True)
    p.add_argument("--reserve-cpus", type=float, default=2)
    p.add_argument("--experimental", action="store_true")
    p = sub.add_parser("compare")
    for name in ("cold", "warm", "cold_startup", "warm_startup", "output"):
        p.add_argument(name)
    p.add_argument("--rank-log", action="append", required=True)
    p.add_argument("--atol", type=float, required=True)
    p.add_argument("--rtol", type=float, required=True)
    p = sub.add_parser("dispatch")
    for name in ("trace", "run", "output"):
        p.add_argument(name)
    p = sub.add_parser("quality")
    for name in ("baseline", "candidate", "output"):
        p.add_argument(name)
    p.add_argument("--atol", type=float, required=True)
    p.add_argument("--rtol", type=float, required=True)
    p.add_argument("--memory-only", action="store_true")
    p = sub.add_parser("seal-tuning")
    for name in ("run", "dispatch", "review", "artifact", "output"):
        p.add_argument(name)
    p.add_argument("--kind", choices=("triton", "hipblaslt"), required=True)
    args = parser.parse_args()
    if args.mode == "plan":
        result = plan(load(args.deployment), load(args.workload), evidence(args.evidence), args.profile,
                      [int(v) for v in args.workers.split(",")], args.reserve_mib, args.worker_mib, args.reserve_cpus, args.experimental)
        output = Path(args.output)
        output.mkdir(mode=0o700)
        write(output/"plan.json", result)
        for name, case in result["cases"].items():
            write(output/(name + ".json"), case["patch"])
    elif args.mode == "compare":
        write(args.output, compare(args.cold, args.warm, args.cold_startup, args.warm_startup, args.rank_log, args.atol, args.rtol))
    elif args.mode == "dispatch":
        write(args.output, dispatch(args.trace, load(args.run)))
    elif args.mode == "quality":
        write(args.output, compare_quality(load(args.baseline), load(args.candidate), args.atol, args.rtol, args.memory_only))
    else:
        seal_tuning(load(args.run), load(args.dispatch), args.review, args.artifact, args.kind, args.output)


if __name__ == "__main__":
    main()
