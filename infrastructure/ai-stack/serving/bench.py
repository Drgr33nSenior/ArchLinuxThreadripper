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

Prints one BENCH_RESULT JSON summary line, then BENCH_RAW with the full
results (zlib + base64). Exits 0 when every request succeeded.

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
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zlib

REQUEST_TIMEOUT_SECONDS = 300  # op233 used 120; a timeout only fails a request, it never changes a rate


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
               "reference_op233_op234": {"C1_tps": "96.3-104.3", "C2_tps": "172.8-194.5", "cap_w": 235}}
    print("BENCH_RESULT " + json.dumps(summary), flush=True)
    print("BENCH_RAW " + base64.b64encode(zlib.compress(json.dumps(raw).encode(), 9)).decode(), flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
