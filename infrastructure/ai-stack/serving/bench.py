#!/usr/bin/env python3
"""Matched BetterBench run, beside the server in the render.py bench Job.

Replays the BetterBench v0.6.1 profile the op233/op234 selection was measured
with (serving repo benchmarks/betterbench/native-v2/profile.json): eight
categories x (1 warm-up + 3 measured) capped at 4096 output tokens, prefill at
2K/8K/16K, and six requests each at concurrency 1 and 2, sampled at
temperature 0.7 / top-p 0.95 / top-k 20 / seed 1729 with thinking at low
effort and a unique cache salt per request. Like the op233 adapter it only
binds the request body and output cap; the upstream client, corpus and runner
are unchanged. It adds the server's own speculative counters per phase
(generated tokens / verify calls = mean tokens per verify step).

Then runs the assistant acceptance menu (infrastructure/sglang/assistant-quality.json,
lib/workstation/assistant_acceptance.py, unchanged) against the native
endpoint, so no throughput change is read without a quality check. Code
tasks need the bubblewrap sandbox, which the pod lacks, and long-context
cases need a tokenizer budget; the runner reports both NOT RUN rather than
passing them.

A long-context phase follows (not part of op233's profile): one request at
each of 32K, 64K and 120K prompt tokens over a unique filler document, with
exactly 1024 generated tokens, recording time to first token, decode rate and
tokens per verify at depth.

Prints one BENCH_RESULT JSON summary line and BENCH_RAW with the full
results (zlib + base64), then QUALITY_RESULT and QUALITY_RAW likewise.
Exits 0 when every BetterBench request succeeded and no executed quality
task failed.

Usage: bench.py PORT SERVED_MODEL_NAME SERVER_PID DEADLINE_SECONDS BUNDLE_TAR_GZ
"""
import asyncio
import base64
import dataclasses
import io
import itertools
import json
import os
import re
import statistics
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zlib

REQUEST_TIMEOUT_SECONDS = 300  # op233 used 120; a timeout only fails a request, it never changes a rate
# Long-context decode: one request per depth over a unique filler document,
# exactly LONG_CONTEXT_OUTPUT generated tokens so every arm decodes the same
# amount. Not part of op233's profile; added to measure verify cost at depth.
LONG_CONTEXT_DEPTHS = (32000, 64000, 120000)
LONG_CONTEXT_OUTPUT = 1024
LONG_CONTEXT_TIMEOUT_SECONDS = 900


def get(base, path, timeout=10):
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def counters(base):
    """Sum each speculative/token counter over TP ranks and label sets."""
    totals = {}
    for line in get(base, "/metrics")[1].splitlines():
        match = re.match(r"^(sglang:(?:generation_tokens_total|spec_verify_calls_total|prompt_tokens_total))"
                         r"(?:\{[^}]*\})? ([0-9.eE+-]+)$", line)
        if match:
            totals[match[1]] = totals.get(match[1], 0.0) + float(match[2])
    return totals


def delta(before, after):
    tokens = after.get("sglang:generation_tokens_total", 0) - before.get("sglang:generation_tokens_total", 0)
    verifies = after.get("sglang:spec_verify_calls_total", 0) - before.get("sglang:spec_verify_calls_total", 0)
    return {"generated_tokens": tokens, "verify_calls": verifies,
            "tokens_per_verify": round(tokens / verifies, 3) if verifies else None}


def median(values):
    values = [v for v in values if v]
    return round(statistics.median(values), 2) if values else None


def main():
    port, model, pid, deadline, bundle = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
    base = f"http://127.0.0.1:{port}"
    t0 = time.monotonic()
    while True:
        if not os.path.exists(f"/proc/{pid}"):
            print("BENCH_RESULT " + json.dumps({"ready": False, "reason": "server exited"}), flush=True)
            return 1
        try:
            if get(base, "/health", timeout=5)[0] == 200:
                break
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() - t0 > deadline:
            print("BENCH_RESULT " + json.dumps({"ready": False, "reason": "startup deadline"}), flush=True)
            return 1
        time.sleep(5)
    startup = round(time.monotonic() - t0, 1)

    root = "/tmp/betterbench"
    with tarfile.open(bundle) as archive:
        archive.extractall(root, filter="data")
    sys.path.insert(0, root)
    from betterbench import runner
    from betterbench.config import Config
    from betterbench.corpus import load_corpus

    profile = json.loads(open(f"{root}/profile.json").read())
    cfg = Config(**profile["config"])
    cfg.timeout_s = REQUEST_TIMEOUT_SECONDS
    corpus = load_corpus(f"{root}/corpus/v1")
    corpus = {name: [dataclasses.replace(p, max_tokens=profile["output_tokens"]) for p in rows]
              for name, rows in corpus.items()}
    salt = f"betterbench-{os.getpid()}-{int(time.time())}-"
    ordinal = itertools.count()
    original = runner.stream_chat
    failures = []

    async def bound(url, model_name, messages, **kwargs):
        kwargs["extra_body"] = dict(profile["request_body"], cache_salt=salt + str(next(ordinal)))
        if kwargs.get("category") != "prefill":
            kwargs["max_tokens"] = profile["output_tokens"]
        result = await original(url, model_name, messages, **kwargs)
        if not result.ok:
            failures.append({"category": kwargs.get("category"), "error": str(result.error)[:200]})
        return result

    runner.stream_chat = bound
    endpoint = base + "/v1"
    phases, raw = {}, {}
    for phase, call in (
        ("single_stream", lambda: runner.single_stream(endpoint, model, corpus, cfg)),
        ("prefill", lambda: runner.prefill_sweep(endpoint, model, cfg, max_ctx=profile["context_tokens"])),
        ("concurrency", lambda: runner.concurrency_sweep(endpoint, model, corpus, cfg)),
    ):
        before, started = counters(base), time.monotonic()
        raw[phase] = asyncio.run(call())
        phases[phase] = dict(delta(before, counters(base)), seconds=round(time.monotonic() - started, 1))

    from betterbench.prefill import _filler

    raw["long_context"], long_context = [], {}
    for depth in LONG_CONTEXT_DEPTHS:
        nonce = f"{salt}long-{depth}"
        document = _filler(depth * 4, nonce)
        messages = [{"role": "user", "content": (
            f"[bb:{nonce}] Read the following notes on distributed systems.\n\n{document}\n\n"
            "Write a detailed explanation of the consistency and availability trade-offs these notes "
            "discuss, with concrete examples for each tool they mention.")}]
        before, started = counters(base), time.monotonic()
        result = asyncio.run(original(
            endpoint, model, messages, max_tokens=LONG_CONTEXT_OUTPUT, temperature=cfg.effective_temp(),
            top_p=cfg.top_p, top_k=cfg.top_k, seed=cfg.seed, category="long_context", prompt_id=str(depth),
            timeout=LONG_CONTEXT_TIMEOUT_SECONDS,
            extra_body=dict(profile["request_body"], cache_salt=nonce, min_tokens=LONG_CONTEXT_OUTPUT)))
        if not result.ok:
            failures.append({"category": "long_context", "error": str(result.error)[:200]})
        row = result.as_dict()
        raw["long_context"].append(row)
        long_context[str(depth)] = dict(
            delta(before, counters(base)), seconds=round(time.monotonic() - started, 1),
            prompt_tokens=row.get("prompt_tokens"), completion_tokens=row.get("completion_tokens"),
            ttft_ms=round(row["ttft_ms"], 1) if row.get("ttft_ms") else None,
            decode_tps=round(row["decode_tps"], 2) if row.get("decode_tps") else None)

    single = {cat: {"decode_tps_median": median(r.get("decode_tps") for r in rows),
                    "completion_tokens_median": median(r.get("completion_tokens") for r in rows)}
              for cat, rows in raw["single_stream"].items()}
    levels = raw["concurrency"]
    levels = levels.get("levels", levels) if isinstance(levels, dict) else levels
    concurrency = {f"C{row['level']}": {"aggregate_tps": round(row["aggregate_tps"], 2),
                                         "decode_tps_median": median(row["decode_tps"]), "ok": row["ok"]}
                   for row in levels}
    prefill = raw["prefill"]
    prefill = prefill.get("depths", prefill) if isinstance(prefill, dict) else prefill
    prefill = {str(row["target_depth"]): median(row["pp_tps"]) for row in prefill if not row.get("skipped")}
    summary = {"model": model, "startup_seconds": startup, "profile": profile["name"],
               "requests": next(ordinal), "failures": failures, "phases": phases,
               "single_stream": single, "concurrency": concurrency, "prefill_pp_tps_median": prefill,
               "long_context": long_context,
               "reference_op233_op234": {"C1_tps": "96.3-104.3", "C2_tps": "172.8-194.5", "cap_w": 235}}
    print("BENCH_RESULT " + json.dumps(summary), flush=True)
    print("BENCH_RAW " + base64.b64encode(zlib.compress(json.dumps(raw).encode(), 9)).decode(), flush=True)

    report = "/tmp/quality.json"
    runner = f"{root}/quality/lib/workstation/assistant_acceptance.py"
    # -E -s -B, not -I: the runner imports its sibling modules from its own directory.
    graded = subprocess.run([sys.executable, "-E", "-s", "-B", runner, "run", report, "--boundary", "direct",
                             "--base-url", base, "--model", model, "--timeout", str(REQUEST_TIMEOUT_SECONDS)],
                            capture_output=True, text=True)
    if graded.returncode != 0 or not os.path.exists(report):
        print("QUALITY_RESULT " + json.dumps({"status": "error", "stderr": graded.stderr[-800:]}), flush=True)
        return 1
    quality = json.loads(open(report).read())
    results = [task["result"] for task in quality["tasks"]]
    print("QUALITY_RESULT " + json.dumps({
        "status": quality["status"], "metrics": quality["metrics"], "attempts": quality["attempts"],
        "executed_attempts": quality["executed_attempts"],
        "results": {name: results.count(name) for name in sorted(set(results))},
        "failed": [t for t in quality["tasks"] if t["result"] == "failed"]}), flush=True)
    print("QUALITY_RAW " + base64.b64encode(zlib.compress(json.dumps(quality).encode(), 9)).decode(), flush=True)
    return 0 if not failures and "failed" not in results else 1


if __name__ == "__main__":
    sys.exit(main())
