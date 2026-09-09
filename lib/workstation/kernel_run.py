"""Owner-invoked warmup/quality/profile requests through the existing private tunnel."""
import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
import re
from pathlib import Path
import time
import sys
import uuid
from urllib.error import HTTPError

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
                report["profile_started_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                # Bounded server profiling; no unauthenticated/public new endpoint.
                # If the server requires an admin key, the existing credential must
                # already have that authority. Never remove its access controls.
                with serving.request(base, "/start_profile", {"output_dir": report["profile_path"],
                        "profile_id": report["profile_path"].rsplit("/", 1)[1],
                        "activities": ["CPU", "GPU"], "num_steps": 32,
                        "with_stack": False, "record_shapes": True, "merge_profiles": False}) as response:
                    if response.read(4097) != b"Start profiling.\n":
                        raise ValueError("unknown profile start response")
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
                with serving.request(base, "/stop_profile", {}, timeout=30) as response:
                    if response.read(4097) != b"Stop profiling. This will take some time.\n":
                        raise ValueError("unknown profile stop response")
                report["profile_stop"] = "explicit-awaiting-traces"
            except HTTPError as error:
                # The pinned route leaves RuntimeError untranslated (HTTP 500).
                # This is NOT success: finish requires exact same-run log and
                # every TP trace. Auth, transport and other statuses stay fatal.
                try:
                    pending = error.code == 500 and error.read(4097) == b"Internal Server Error"
                except Exception:
                    pending = False
                finally:
                    error.close()
                if pending:
                    report["profile_stop"] = "http-500-awaiting-verification"
                else:
                    report["profile_stop"] = "failed"
                    report["status"] = "failed-profile-cleanup"
            except BaseException as error:
                report["profile_stop"] = "failed"
                report["profile_error_class"] = type(error).__name__
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
        if report["kind"] == "kernel-profile":
            profile = load(output/"profile-traces.json")
            ranks = int(report["identity"]["runtime"]["settings"]["TENSOR_PARALLEL"])
            if (profile["profile_path"] != report["profile_path"]
                    or set(profile["traces"]) != {str(i) for i in range(ranks)}):
                raise ValueError("missing or mismatched per-rank trace evidence")
            for rank, trace in profile["traces"].items():
                expected = report["profile_path"].rsplit("/", 1)[1] + f"-TP-{rank}.trace.json.gz"
                if (trace["name"] != expected or not re.fullmatch(r"[a-f0-9]{64}", trace["sha256"])
                        or type(trace["bytes"]) is not int or not 0 < trace["bytes"] <= 128*1024**2):
                    raise ValueError("invalid trace identity")
            if report["profile_stop"] == "http-500-awaiting-verification":
                log = load(output/"profile-log.json")
                if log["profile_path"] != report["profile_path"] or not log["normal_completion"]:
                    raise ValueError("unknown profiler failure; automatic completion not verified")
                report["profile_stop"] = "automatic-completion-verified"
            elif report["profile_stop"] == "explicit-awaiting-traces":
                report["profile_stop"] = "explicit-completion-verified"
            else:
                raise ValueError("profiler lifecycle failed")
            report["profile_traces"] = profile
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


def profile_log(output, raw):
    """Minimize bounded same-Pod logs; never retain unrelated log text."""
    report = load(Path(output)/"result.json")
    if len(raw) > 262144:
        raise ValueError("profile log exceeds budget")
    text = raw.decode("utf-8", errors="strict")
    lines = text.splitlines()
    # HTTP 500 alone is ambiguous. The pinned exception AND successful export
    # messages for every rank must be present in this unique session directory.
    expected = "RuntimeError: Profiling is not in progress. Call /start_profile first."
    errors = [line for line in lines if "Error:" in line or "Exception:" in line]
    ranks = int(report["identity"]["runtime"]["settings"]["TENSOR_PARALLEL"])
    completed = [line for line in lines if "Profiling done. Traces are saved to: " + report["profile_path"] in line]
    rank_ok = all(any(f"TP{i}]" in line for line in completed) for i in range(ranks)) if ranks > 1 else bool(completed)
    normal = len(errors) == 1 and errors[0].endswith(expected) and rank_ok
    write(Path(output)/"profile-log.json", {"schema": 1, "profile_path": report["profile_path"],
        "normal_completion": normal, "log_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": "bounded same-Pod logs since this exclusive profiling session; raw unrelated text not retained"})
    if not normal:
        raise ValueError("unknown profiler error or missing per-rank completion logs")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("kernel-warmup", "kernel-profile", "finish", "profile-log"))
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.kind == "profile-log" and len(args.paths) == 1:
        profile_log(args.paths[0], sys.stdin.buffer.read(262145))
    elif args.kind == "finish" and len(args.paths) == 1:
        finish(*args.paths)
    elif args.kind != "finish" and len(args.paths) == 4:
        run(args.kind, *args.paths)
    else:
        parser.error("provide workload, evidence, compiler-before and output; finish takes only output")


if __name__ == "__main__":
    main()
