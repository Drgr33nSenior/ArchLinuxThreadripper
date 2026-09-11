"""Private SGLang native streaming benchmark. Standard library only.

Input IDs must come from the staged model's tokenizer. No cache flush, restart,
server configuration mutation or output-text retention occurs here.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

from measurement import Sampler, sha256, snapshot, pod_cgroup, cgroup_values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("benchmark redirects are forbidden")


def endpoint(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in ("http", "https") or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.username or url.password or url.query or url.fragment or url.path != "/v1"):
        raise ValueError("use an owner-established loopback /v1 tunnel, not a public endpoint")
    return urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", ""))


def opener():
    # Do not leak loopback requests or authentication to environment HTTP proxies.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(base, path, payload=None, timeout=600):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("WORKSTATION_AGENT_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["WORKSTATION_AGENT_API_KEY"]
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    return opener().open(req, timeout=timeout)


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def summary(values):
    if not values:
        return None
    if not all(finite(v) and v >= 0 for v in values):
        raise ValueError("invalid measurement")
    values = sorted(values)
    return {"n": len(values), "min": values[0], "median": statistics.median(values),
            "p95": values[math.ceil(.95 * len(values)) - 1],
            "p99": values[math.ceil(.99 * len(values)) - 1], "max": values[-1],
            "stdev": statistics.stdev(values) if len(values) > 1 else 0}


def validate(workload, evidence):
    if workload["schema"] != 1 or workload["profile"] not in ("interactive", "batch"):
        raise ValueError("unsupported workload profile")
    if (type(workload["repetitions"]) is not int or type(workload["requests_per_worker"]) is not int
            or not 3 <= workload["repetitions"] <= 20 or not 1 <= workload["requests_per_worker"] <= 16):
        raise ValueError("require 3..20 repetitions and 1..16 requests per worker")
    concurrency = workload["concurrency"]
    if (not isinstance(concurrency, list) or not concurrency
            or any(type(c) is not int or not 1 <= c <= 8 for c in concurrency)
            or len(set(concurrency)) != len(concurrency)):
        raise ValueError("concurrency must be unique integers in 1..8")
    if workload["prefix_state"] not in ("new-prefix", "warm-prefix"):
        raise ValueError("prefix_state must be new-prefix or warm-prefix")
    if workload["model_revision"] != evidence["settings"]["MODEL_REVISION"]:
        raise ValueError("workload tokenizer and served model revisions differ")
    if not evidence["model_files"] or not evidence["launch"]:
        raise ValueError("model hashes and actual launch settings are required")
    context = int(evidence["settings"]["CONTEXT_LENGTH"])
    cases = workload["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > 3:
        raise ValueError("require one to three coding context cases")
    for case in cases:
        if case["context_tokens"] not in (4096, 8192, 32768) or case["context_tokens"] > context:
            raise ValueError("requested context does not fit qualified server capacity")
        ids = case["input_ids"]
        if not ids or any(type(t) is not int or t < 0 for t in ids):
            raise ValueError("input_ids must be tokenizer-produced nonnegative integers")
        if (type(case["output_tokens"]) is not int or not 1 <= case["output_tokens"] <= 2048
                or len(ids) + case["output_tokens"] != case["context_tokens"]):
            raise ValueError("input plus output budget must match the requested context")
        if len(case["source_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in case["source_sha256"]):
            raise ValueError("coding corpus SHA256 required")


def stream(base, case):
    start = time.monotonic()
    first = last = None
    count = 0
    first_chunk_tokens = 0
    gaps = []
    coalesced = False
    cached = None
    done = False
    try:
        payload = {"input_ids": case["input_ids"], "stream": True,
                   "sampling_params": {"temperature": 0, "max_new_tokens": case["output_tokens"],
                                       "ignore_eos": True}}
        with request(base, "/generate", payload) as response:
            while True:
                line = response.readline(8 * 1024 * 1024)
                if not line:
                    break
                if not line.startswith(b"data: "):
                    continue
                if line.strip() == b"data: [DONE]":
                    done = True
                    break
                data = json.loads(line[6:])
                if "error" in data:
                    raise ValueError("server generation failure")
                meta = data["meta_info"]
                finish = meta.get("finish_reason")
                finish_type = finish.get("type") if isinstance(finish, dict) else finish
                if finish_type in ("abort", "error"):
                    raise ValueError("server aborted or failed generation")
                current = meta["completion_tokens"]
                if type(current) is not int or current < count:
                    raise ValueError("non-monotonic token accounting")
                now = time.monotonic()
                if current > count:
                    if first is None:
                        first = now
                        first_chunk_tokens = current
                        coalesced |= current > 1
                    else:
                        gaps.append((now - last) / (current - count))
                        coalesced |= current - count > 1
                    last, count = now, current
                cached = meta.get("cached_tokens", cached)
        if not done or first is None or count != case["output_tokens"]:
            raise ValueError("incomplete stream or output length differs from fixed workload")
        elapsed = time.monotonic() - start
        return {"ok": True, "ttft_seconds": first - start, "latency_seconds": elapsed,
                "itl_seconds": gaps, "itl_coalesced": coalesced,
                "first_chunk_tokens": first_chunk_tokens,
                "tpot_seconds": (last - first) / (count - first_chunk_tokens) if count > first_chunk_tokens else None,
                "tpot_scope": "observed-chunk estimate after first arrival" if count > first_chunk_tokens else "unavailable: no later token arrival",
                "output_tokens": count, "output_tokens_per_second": count / elapsed,
                "cached_tokens": cached}
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        # Exception strings may contain endpoint credentials or response content.
        return {"ok": False, "error_class": type(error).__name__, "latency_seconds": time.monotonic() - start}


def server_metrics(base):
    # Only metric names/values, never label values containing request identities.
    try:
        with request(base, "/metrics", timeout=10) as response:
            text = response.read(8 * 1024 * 1024).decode()
        wanted = ("sglang:num_queue_reqs", "sglang:num_running_reqs", "sglang:queue_time_seconds",
                  "sglang:time_to_first_token_seconds", "sglang:inter_token_latency_seconds")
        rows = []
        for line in text.splitlines():
            if line.startswith(wanted):
                parts = line.split()
                value = float(parts[-1])
                if finite(value):
                    rows.append({"name": parts[0].split("{")[0], "value": value})
        return {"metrics": rows, "status": "observed" if rows else "unavailable"}
    except (OSError, ValueError, urllib.error.URLError):
        return {"metrics": [], "status": "unavailable"}


def server_load(base):
    try:
        with request(base, "/get_load", timeout=5) as response:
            data = json.loads(response.read(1024 * 1024))
        fields = ("dp_rank", "num_reqs", "num_waiting_reqs", "num_tokens", "num_pending_tokens")
        return [{key: row[key] for key in fields if key in row and finite(row[key])} for row in data]
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return None


def run(base, workload_path, evidence_dir, output):
    workload = json.loads(Path(workload_path).read_text())
    evidence_path = Path(evidence_dir) / "runtime.json"
    evidence = json.loads(evidence_path.read_text())
    pod = json.loads((Path(evidence_dir) / "pod.json").read_text())
    if not pod.get("uid") or "@sha256:" not in pod["image"] or not pod.get("image_id"):
        raise ValueError("actual pod UID, pinned image and runtime image ID are required")
    validate(workload, evidence)
    with request(base, "/model_info", timeout=10) as response:
        model_info = json.loads(response.read(1024 * 1024))
    if model_info.get("model_path") != evidence["settings"]["MODEL_PATH"]:
        raise ValueError("HTTP endpoint does not serve the observed model path")
    output = Path(output)
    output.mkdir(mode=0o700)
    report = {"schema": 1, "status": "incomplete", "workload_sha256": sha256(workload_path),
              "runtime_sha256": sha256(evidence_path), "pod": pod, "runtime": evidence,
              "profile": workload["profile"], "prefix_state": workload["prefix_state"],
              "startup": "excluded; use serving-startup separately", "runs": [],
              "limitations": ["Client TTFT includes transport, prefill and queueing; it is not queue latency.",
                              "ITL is chunk-normalized when server SSE batches tokens.",
                              "Cold-prefix labels require zero observed cached tokens; no cache flush occurs.",
                              "Host samples are not pod peaks; retain matching pod telemetry separately."]}
    try:
        pod_scope = pod_cgroup(pod["uid"])
        def collect():
            sample = snapshot()
            sample["server_load"] = server_load(base)
            sample["pod_cgroup"] = cgroup_values(pod_scope)
            return sample
        report["measurement_started_monotonic_ns"] = time.monotonic_ns()
        with Sampler(output / "host-telemetry.jsonl", collect=collect):
            for case in workload["cases"]:
                # Warmup is explicit and excluded. A new-prefix test must supply
                # distinct tokenized variants for EVERY request; never fake coldness
                # by flushing a shared server cache or relabelling warm repetitions.
                if workload["prefix_state"] == "new-prefix":
                    needed = sum(workload["concurrency"]) * workload["requests_per_worker"] * workload["repetitions"]
                    variants = iter(case["cold_variants"])
                    if len(case["cold_variants"]) != needed:
                        raise ValueError("new-prefix requires one explicit tokenized variant per request")
                    for ids in case["cold_variants"]:
                        if len(ids) != len(case["input_ids"]) or any(type(t) is not int or t < 0 for t in ids):
                            raise ValueError("cold variants must preserve exact input token count")
                else:
                    warmup = stream(base, case)
                    if not warmup["ok"]:
                        raise ValueError("warmup failed")
                for repetition in range(workload["repetitions"]):
                    order = workload["concurrency"][::1 if repetition % 2 == 0 else -1]
                    for concurrency in order:
                        before = server_metrics(base)
                        requests = [dict(case) for _ in range(concurrency * workload["requests_per_worker"])]
                        if workload["prefix_state"] == "new-prefix":
                            for item in requests:
                                item["input_ids"] = next(variants)
                        started = time.monotonic()
                        with ThreadPoolExecutor(max_workers=concurrency) as pool:
                            results = list(pool.map(lambda item: stream(base, item), requests))
                        elapsed = time.monotonic() - started
                        good = [r for r in results if r["ok"]]
                        cache_verified = all(r.get("cached_tokens") == 0 for r in good) if workload["prefix_state"] == "new-prefix" else all((r.get("cached_tokens") or 0) > 0 for r in good)
                        report["runs"].append({"context_tokens": case["context_tokens"], "repetition": repetition,
                            "concurrency": concurrency, "requests": results, "failures": len(results) - len(good),
                            "cache_state_verified": bool(good) and cache_verified,
                            "ttft_seconds": summary([r["ttft_seconds"] for r in good]),
                            "request_latency_seconds": summary([r["latency_seconds"] for r in good]),
                            "itl_seconds": summary([v for r in good for v in r["itl_seconds"]]),
                            "aggregate_output_tokens_per_second": sum(r["output_tokens"] for r in good) / elapsed,
                            "wall_seconds": elapsed, "queue_before": before, "queue_after": server_metrics(base)})
                        (output / "result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        report["workload_finished_monotonic_ns"] = time.monotonic_ns()
        # Sample after the last response as well: the lifetime pod high-water
        # must cover the entire workload, not just the last periodic tick.
        with (output / "host-telemetry.jsonl").open("a") as telemetry:
            telemetry.write(json.dumps(collect(), allow_nan=False) + "\n")
        report["measurement_finished_monotonic_ns"] = time.monotonic_ns()
        if sha256(evidence_path) != report["runtime_sha256"] or sha256(workload_path) != report["workload_sha256"]:
            raise ValueError("input provenance changed during benchmark")
        report["repeat_summary"] = []
        for context, concurrency in sorted({(r["context_tokens"], r["concurrency"]) for r in report["runs"]}):
            runs = [r for r in report["runs"] if r["context_tokens"] == context and r["concurrency"] == concurrency]
            requests = [request for r in runs for request in r["requests"] if request["ok"]]
            report["repeat_summary"].append({"context_tokens": context, "concurrency": concurrency,
                "failures": sum(r["failures"] for r in runs),
                "throughput_across_repetitions": summary([r["aggregate_output_tokens_per_second"] for r in runs]),
                "ttft_seconds": summary([r["ttft_seconds"] for r in requests]),
                "request_latency_seconds": summary([r["latency_seconds"] for r in requests]),
                "itl_seconds": summary([t for r in requests for t in r["itl_seconds"]])})
        report["status"] = "measured-awaiting-provenance" if all(not r["failures"] and r["cache_state_verified"] for r in report["runs"]) else "failed-or-cache-unverified"
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0 if report["status"] == "measured-awaiting-provenance" else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload")
    parser.add_argument("evidence")
    parser.add_argument("output")
    args = parser.parse_args()
    base = endpoint(os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:18000/v1"))
    raise SystemExit(run(base, args.workload, args.evidence, args.output))


if __name__ == "__main__":
    main()
