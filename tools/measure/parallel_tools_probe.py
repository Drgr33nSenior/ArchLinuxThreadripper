#!/usr/bin/env python3
"""Probe SGLang PR 37408's symptom: in a streamed reply with two tool calls, does whitespace-only content
arrive between one call's argument fragments? Talks to SGLang directly (no credentials).

Usage: parallel_tools_probe.py SGLANG_URL [RUNS]   (RUNS default 5)
"""
import json
import sys
import urllib.request

if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    print(__doc__.strip())
    raise SystemExit(0 if len(sys.argv) > 1 else 2)
url = sys.argv[1].rstrip("/") + "/v1/chat/completions"
runs = int(sys.argv[2]) if len(sys.argv) > 2 else 5
tools = [{"type": "function", "function": {
    "name": "get_weather", "description": "Current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string"}},
                   "required": ["city"]}}}]
body = {"model": "Qwen3.8-27B-MXFP4", "stream": True, "tools": tools, "max_tokens": 1500,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content":
                      "Call get_weather for Paris, for London and for Tokyo, all three in one reply, "
                      "celsius. Do not write any other text."}]}
summary = []
for run in range(runs):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    events, calls = [], {}
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            delta = json.loads(line[5:])["choices"][0].get("delta") or {}
            if delta.get("content"):
                events.append(("text", delta["content"]))
            for tc in delta.get("tool_calls") or []:
                i = tc.get("index", 0)
                args = (tc.get("function") or {}).get("arguments") or ""
                calls.setdefault(i, {"name": "", "args": ""})
                calls[i]["name"] += (tc.get("function") or {}).get("name") or ""
                calls[i]["args"] += args
                events.append(("tool", i, args))
    # Text that arrives while the most recent tool call's arguments are not yet complete JSON.
    interleaved, seen, last = 0, {}, None
    for e in events:
        if e[0] == "tool":
            last = e[1]
            seen[last] = seen.get(last, "") + e[2]
        elif last is not None:
            try:
                json.loads(seen[last])
            except ValueError:
                interleaved += 1
    valid = sum(1 for c in calls.values() if c["args"] and json.loads(c["args"] or "null") is not None)
    texts = [e[1] for e in events if e[0] == "text"]
    summary.append({"run": run + 1, "calls": len(calls), "valid_json_args": valid,
                    "text_deltas": len(texts), "whitespace_only_text": sum(1 for t in texts if not t.strip()),
                    "text_inside_open_args": interleaved})
    print(json.dumps(summary[-1]))
print("TOTAL", json.dumps({k: sum(s[k] for s in summary) for k in summary[0] if k != "run"}))
