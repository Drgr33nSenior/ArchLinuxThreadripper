#!/usr/bin/env python3
"""Qualification probe, run beside the server in the render.py qualification Job.

Standard library only. Waits for /health, then checks the served model name,
reasoning output, a streamed decode, tool-call parsing, a long-context prefill
and prefix-cache reuse, and records the server's speculative and cache metrics.
Prints one QUALIFY_RESULT JSON line; exits 0 only if every functional check
passes. Timings are single measurements for orientation, not benchmarks.

Usage: qualify.py PORT SERVED_MODEL_NAME SERVER_PID DEADLINE_SECONDS
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request


def http(base, path, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode()


def chat(base, model, messages, **extra):
    body = {"model": model, "messages": messages, **extra}
    started = time.monotonic()
    _, text = http(base, "/v1/chat/completions", body)
    return json.loads(text), time.monotonic() - started


def stream(base, model, messages, **extra):
    """Return (ttft_seconds, total_seconds, completion_tokens, text)."""
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}, **extra}
    req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    started, first, tokens, parts = time.monotonic(), None, None, []
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            event = json.loads(line[5:])
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                piece = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
                if piece and first is None:
                    first = time.monotonic() - started
                parts.append(delta.get("content") or "")
            if event.get("usage"):
                tokens = event["usage"].get("completion_tokens")
    return first, time.monotonic() - started, tokens, "".join(parts)


def main():
    port, model, pid, deadline = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    base = f"http://127.0.0.1:{port}"
    result, checks = {"model": model}, {}
    t0 = time.monotonic()
    while True:
        if not os.path.exists(f"/proc/{pid}"):
            print("QUALIFY_RESULT " + json.dumps({"ready": False, "reason": "server exited"}), flush=True)
            return 1
        try:
            if http(base, "/health", timeout=5)[0] == 200:
                break
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() - t0 > deadline:
            print("QUALIFY_RESULT " + json.dumps({"ready": False, "reason": "startup deadline"}), flush=True)
            return 1
        time.sleep(5)
    result["startup_seconds"] = round(time.monotonic() - t0, 1)

    models = json.loads(http(base, "/v1/models")[1])
    checks["served_model_name"] = any(m.get("id") == model for m in models.get("data", []))

    reply, seconds = chat(base, model, [{"role": "user", "content": "What is 17 * 23? Reply with the number."}],
                          max_tokens=2048, temperature=0.6)
    message = reply["choices"][0]["message"]
    checks["reasoning_separated"] = bool(message.get("reasoning_content"))
    checks["arithmetic"] = "391" in (message.get("content") or "")
    result["thinking_reply_seconds"] = round(seconds, 2)

    no_think = {"chat_template_kwargs": {"enable_thinking": False}}
    ttft, total, tokens, text = stream(base, model, [{"role": "user", "content":
                                       "Write a 300-word story about a lighthouse keeper."}],
                                       max_tokens=400, temperature=0.7, top_p=0.8, **no_think)
    checks["stream_text"] = len(text) > 200
    if ttft is not None and tokens:
        result["stream"] = {"ttft_s": round(ttft, 3), "tokens": tokens,
                            "decode_tok_per_s": round((tokens - 1) / max(total - ttft, 1e-6), 1)}

    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    reply, _ = chat(base, model, [{"role": "user", "content": "What is the weather in Leeds? Use the tool."}],
                    tools=tools, max_tokens=512, temperature=0.6, **no_think)
    calls = reply["choices"][0]["message"].get("tool_calls") or []
    checks["tool_call"] = bool(calls) and calls[0]["function"]["name"] == "get_weather" and \
        "leeds" in calls[0]["function"].get("arguments", "").lower()

    filler = " ".join(f"Record {i}: the lighthouse logged calm seas and a passing ship." for i in range(1200))
    long_messages = [{"role": "user", "content": filler + "\n\nHow many records are there? Reply with the number."}]
    first, seconds = chat(base, model, long_messages, max_tokens=32, temperature=0.0, **no_think)
    second, seconds_again = chat(base, model, long_messages, max_tokens=32, temperature=0.0, **no_think)
    prompt_tokens = first.get("usage", {}).get("prompt_tokens")
    cached = (second.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    checks["long_prefill"] = bool(prompt_tokens) and prompt_tokens > 10000
    checks["prefix_cache_reuse"] = cached > 0.5 * (prompt_tokens or 1)
    result["long_context"] = {"prompt_tokens": prompt_tokens, "first_s": round(seconds, 2),
                              "repeat_s": round(seconds_again, 2), "repeat_cached_tokens": cached}

    try:
        metrics = http(base, "/metrics")[1].splitlines()
        result["metrics"] = [m for m in metrics if not m.startswith("#") and
                             any(k in m for k in ("spec_accept", "cache_hit_rate", "num_used_tokens", "token_usage"))][:20]
    except (urllib.error.URLError, OSError) as error:
        result["metrics_error"] = str(error)

    result["checks"] = checks
    result["passed"] = all(checks.values())
    print("QUALIFY_RESULT " + json.dumps(result), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
