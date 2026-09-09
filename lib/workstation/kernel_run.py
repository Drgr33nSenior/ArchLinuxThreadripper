"""Owner-invoked warmup/quality/profile requests through the existing private tunnel."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import uuid

import serving
from measurement import Sampler, snapshot, pod_cgroup, cgroup_values, interruptions
from model_kernels import evidence, file_hash, identity, load, runtime_key, write


def numerical_response(data, count):
    if "error" in data:
        raise ValueError("generation failed")
    meta = data["meta_info"]
    reason = meta.get("finish_reason")
    kind = reason.get("type") if isinstance(reason, dict) else reason
    if kind in (None, "abort", "error") or meta["completion_tokens"] != count:
        raise ValueError("incomplete/aborted numerical response")
    rows = meta["output_token_logprobs"]
    if len(rows) != count or any(len(row) < 2 or not serving.finite(row[0])
                                 or type(row[1]) is not int or row[1] < 0 for row in rows):
        raise ValueError("missing/nonfinite output token logprobs")
    return {"tokens": [row[1] for row in rows], "logprobs": [row[0] for row in rows]}


def generate(base, case):
    payload = {"input_ids": case["input_ids"], "stream": False, "return_logprob": True,
               "return_text_in_logprobs": False, "logprob_start_len": -1,
               "sampling_params": {"temperature": 0, "max_new_tokens": case["output_tokens"], "ignore_eos": True}}
    with serving.request(base, "/generate", payload) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("oversized generation response")
    return numerical_response(json.loads(raw), case["output_tokens"])


def run(kind, workload_path, evidence_dir, compiler_before, output):
    pod, runtime, recorded_compiler = evidence(evidence_dir)
    compiler = load(compiler_before)
    if runtime_key(pod, runtime, compiler) != runtime_key(pod, runtime, recorded_compiler):
        raise ValueError("compiler/runtime settings changed since evidence collection")
    workload = load(workload_path)
    serving.validate(workload, runtime)
    if max(workload["concurrency"]) > int(runtime["settings"]["MAX_RUNNING_REQUESTS"]):
        raise ValueError("workload exceeds qualified concurrent request budget")
    output = Path(output)
    output.mkdir(mode=0o700)
    write(output/"compiler-before.json", compiler)
    base = serving.endpoint(os.environ["AGENT_BASE_URL"])
    with serving.request(base, "/model_info") as response:
        if json.load(response).get("model_path") != runtime["settings"]["MODEL_PATH"]:
            raise ValueError("HTTP model differs from observed Pod")
    report = {"schema": 1, "status": "incomplete", "kind": kind, "pod": pod,
              "identity": runtime_key(pod, runtime, compiler), "workload_sha256": file_hash(workload_path),
              "quality": [], "steady_state": None, "warmup_seconds": None,
              "warmup_contract": {"repetitions": 3, "concurrency": sorted(workload["concurrency"]),
                  "cases": [{key: case[key] for key in ("context_tokens", "output_tokens")} for case in workload["cases"]]},
              "scope": "workload warmup/logprob checks; not full coding quality or qualified performance"}
    profiling = False
    try:
        with interruptions():
            if kind == "kernel-profile":
                report["profile_path"] = "/cache/xdg/workstation-profiles/" + uuid.uuid4().hex
                # Bounded server profiling; no unauthenticated/public new endpoint.
                # If the server requires an admin key, the existing credential must
                # already have that authority. Never remove its access controls.
                with serving.request(base, "/start_profile", {"output_dir": report["profile_path"],
                        "activities": ["CPU", "GPU"], "num_steps": 32,
                        "with_stack": False, "record_shapes": True, "merge_profiles": False}):
                    pass
                profiling = True
            scope = pod_cgroup(pod["uid"])
            def collect():
                result = snapshot()
                result["pod_cgroup"] = cgroup_values(scope)
                return result
            start = time.monotonic()
            with Sampler(output/"warmup-telemetry.jsonl", collect=collect):
                for repeat in range(3):
                    for case in workload["cases"]:
                        for batch in sorted(workload["concurrency"]):
                            with ThreadPoolExecutor(max_workers=batch) as pool:
                                rows = list(pool.map(lambda _: generate(base, case), range(batch)))
                            for index, row in enumerate(rows):
                                report["quality"].append({"case": [repeat, case["context_tokens"], batch, index], **row})
            report["warmup_seconds"] = time.monotonic() - start
            if kind == "kernel-warmup":
                if serving.run(base, workload_path, evidence_dir, output/"steady") != 0:
                    raise ValueError("steady-state benchmark failed or cache state unverified")
                steady = load(output/"steady/result.json")
                report["steady_state"] = {"result_sha256": file_hash(output/"steady/result.json"),
                    "runs": steady["runs"], "profile": steady["profile"], "prefix_state": steady["prefix_state"]}
            report["status"] = "measured-awaiting-provenance"
    except BaseException as error:
        report["status"] = "failed"
        report["error_class"] = type(error).__name__
        raise
    finally:
        if profiling:
            try:
                with serving.request(base, "/stop_profile", {}, timeout=30):
                    pass
            except BaseException:
                report["status"] = "failed-profile-cleanup"
                # Server-side step bound remains the fallback; never report success.
        write(output/"result.json", report)
    if report["status"] != "measured-awaiting-provenance":
        raise ValueError("profile stop failed; inspect server before another experiment")


def finish(output):
    output = Path(output)
    report = load(output/"result.json")
    try:
        if report["status"] != "measured-awaiting-provenance":
            raise ValueError("workload did not complete")
        before, after = load(output/"compiler-before.json"), load(output/"compiler-after.json")
        if runtime_key(report["pod"], report["identity"]["runtime"], after) != report["identity"]:
            raise ValueError("compiler or loaded-library identity changed")
        a, b = before["cgroup"], after["cgroup"]
        for name in ("memory.max", "cpu.max", "cpuset.cpus.effective"):
            if not a[name] or a[name] != b[name]:
                raise ValueError("effective resource limits changed or are unavailable")
        events = lambda text: {k: int(v) for k, v in (line.split() for line in text.splitlines())}
        old, new = events(a["memory.events"]), events(b["memory.events"])
        if any(new[k] != old[k] for k in ("oom", "oom_kill", "max")):
            raise ValueError("pod hit memory pressure/OOM during experiment")
        if int(b["memory.peak"]) > int(b["memory.max"]):
            raise ValueError("pod memory high-water mark exceeds limit")
        gpu_samples = {}
        files = [output/"warmup-telemetry.jsonl"]
        if report["kind"] == "kernel-warmup":
            files.append(output/"steady/host-telemetry.jsonl")
        for path in files:
            seen = set()
            with path.open() as stream:
                for line in stream:
                    sample = json.loads(line)
                    for bdf, gpu in sample["gpus_by_bdf"].items():
                        if gpu["mem_info_vram_used"] is None or gpu["mem_info_vram_total"] is None:
                            raise ValueError("GPU memory evidence unavailable")
                        used, total = int(gpu["mem_info_vram_used"]), int(gpu["mem_info_vram_total"])
                        if not 0 <= used <= total or total == 0:
                            raise ValueError("invalid GPU memory observation")
                        seen.add(bdf)
                        gpu_samples[bdf] = {"peak_used": max(used, gpu_samples.get(bdf, {}).get("peak_used", 0)), "total": total}
            if len(seen) != 2:
                raise ValueError("both physical GPU memory observations are required for every phase")
        report["memory"] = {"status": "checked", "cgroup_peak_bytes": int(b["memory.peak"]),
            "limit_bytes": int(b["memory.max"]), "gpu_samples": gpu_samples,
            "scope": "cgroup lifetime high-water mark and sampled VRAM; capture/compile spikes may be missed"}
    except BaseException as error:
        report["status"] = "failed-memory-or-provenance"
        report["error_class"] = type(error).__name__
        raise
    finally:
        # Update only this owned run, retaining failure even after a successful HTTP workload.
        (output/"result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("kernel-warmup", "kernel-profile", "finish"))
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.kind == "finish" and len(args.paths) == 1:
        finish(*args.paths)
    elif args.kind != "finish" and len(args.paths) == 4:
        run(args.kind, *args.paths)
    else:
        parser.error("provide workload, evidence, compiler-before and output; finish takes only output")


if __name__ == "__main__":
    main()
