"""Exercise the selected native gateway; print only fixed pass/fail evidence."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

CHECKPOINTS = frozenset({
    "startup", "activate", "restart-load", "restart-valid", "restart-revoked", "restart-chat",
    "user-create", "valid-key", "revoked-key", "self-primary", "self-secondary", "cross-user", "expired-key", "expired-model",
    "invalid-key", "chat", "model-list", "stream", "tool-call", "tool-usage", "tool-result",
    "switch-model", "wrong-model", "reset-model", "rpm-key", "rpm-first", "rpm-limit",
    "tpm-key", "tpm-first", "tpm-limit", "concurrency-slow", "concurrency-overlap",
    "concurrency-complete", "disconnect-stream", "cancel-backend", "cancel-admission",
    "cancel-complete", "backend-unavailable", "backend-no-retry", "revoke-key",
    "revoked-model", "draining", "maintenance", "save-keys", "result",
})


def checkpoint(name, status):
    # This file contains only fixed names and status codes, never request data.
    if name not in CHECKPOINTS or not (status in {"start", "pass", "fail", "transport"}
            or isinstance(status, int) and 100 <= status <= 599):
        raise ValueError("invalid diagnostic checkpoint")
    with diagnostics.open("a") as output:
        output.write(f"{name}\t{status}\n")


def expect(name, condition):
    checkpoint(name, "pass" if condition else "fail")
    assert condition, "native fixture assertion"


def call(name, path, key, body=None):
    checkpoint(name, "start")
    req = urllib.request.Request("http://gateway:4000" + path, headers={"Authorization": "Bearer " + key,
        "Content-Type": "application/json"}, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read(262144)
            checkpoint(name, response.status)
            return response.status, data
    except urllib.error.HTTPError as exc:
        checkpoint(name, exc.code)
        return exc.code, b""
    except OSError:
        checkpoint(name, "transport")
        raise


def key(name, user, **limits):
    status, raw = call(name, "/key/generate", master, {"user_id": user, "models": ["Qwen3.8-27B-FP8"],
        "duration": "1h", "rpm_limit": 30, "tpm_limit": 65536, "max_parallel_requests": 1, **limits})
    expect(name, status == 200)
    return json.loads(raw)["key"]


def fixture_status(name):
    checkpoint(name, "start")
    with urllib.request.urlopen("http://sglang:30000/fixture/status", timeout=2) as response:
        checkpoint(name, response.status)
        return json.load(response)


def main(mode):
    global master
    os.umask(0o077)
    diagnostics.touch(mode=0o600, exist_ok=False)
    checkpoint("startup", "start")
    secret = Path("/run/fixture")
    master = (secret / "master-key").read_text().strip()
    control = (secret / "control-key").read_text().strip()
    for _ in range(90):
        try:
            if call("startup", "/health/readiness", master)[0] == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        expect("startup", False)
    expect("startup", True)
    expect("activate", call("activate", "/_service/control", control,
                           {"phase": "ready", "model": "Qwen3.8-27B-FP8"})[0] == 200)
    chat = {"model": "Qwen3.8-27B-FP8", "messages": [{"role": "user", "content": "fixture"}]}
    if mode == "restart":
        checkpoint("restart-load", "start")
        saved = json.loads(Path("/state/keys.json").read_text())
        expect("restart-load", True)
        expect("restart-valid", call("restart-valid", "/v1/models", saved["valid"])[0] == 200)
        expect("restart-revoked", call("restart-revoked", "/v1/models", saved["revoked"])[0] in {401, 403})
        expect("restart-chat", call("restart-chat", "/v1/chat/completions", saved["valid"], chat)[0] == 200)
        checkpoint("result", "pass")
        print("PASS: native key persistence and revocation across gateway/database restarts")
        return
    for user in ("fixture-alice", "fixture-bob"):
        expect("user-create", call("user-create", "/user/new", master,
                                   {"user_id": user, "auto_create_key": False})[0] == 200)
    valid, revoked = key("valid-key", "fixture-alice"), key("revoked-key", "fixture-bob")
    for name, user, credential in (("self-primary", "fixture-alice", valid),
                                    ("self-secondary", "fixture-bob", revoked)):
        status, raw = call(name, "/v2/user/info?user_id=" + user, credential)
        expect(name, status == 200)
        expect(name, json.loads(raw)["user_id"] == user)
    # The pinned native API conceals an unauthorized user lookup with 404.
    expect("cross-user", call("cross-user", "/v2/user/info?user_id=fixture-bob", valid)[0] in {401, 403, 404})
    expired = key("expired-key", "fixture-alice", duration="1s")
    time.sleep(2)
    expect("expired-model", call("expired-model", "/v1/models", expired)[0] in {401, 403})
    expect("invalid-key", call("invalid-key", "/v1/models", "invalid-fixture")[0] in {401, 403})
    expect("chat", call("chat", "/v1/chat/completions", valid, chat)[0] == 200)
    listed = json.loads(call("model-list", "/v1/models", valid)[1])
    expect("model-list", "Qwen3.8-27B-FP8" in {m["id"] for m in listed["data"]})
    status, stream = call("stream", "/v1/chat/completions", valid, {**chat, "stream": True})
    expect("stream", status == 200 and b"[DONE]" in stream)
    status, result = call("tool-call", "/v1/chat/completions", valid, {**chat, "tools": [{"type": "function", "function": {
        "name": "fixture_tool", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}}}}]})
    expect("tool-call", status == 200 and json.loads(result)["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "fixture_tool")
    expect("tool-usage", json.loads(result)["usage"]["total_tokens"] == 10)
    tool_message = json.loads(result)["choices"][0]["message"]
    expect("tool-result", call("tool-result", "/v1/chat/completions", valid, {**chat, "messages": chat["messages"] + [tool_message,
        {"role": "tool", "tool_call_id": "call-fixture", "content": '{"value":7}'}]})[0] == 200)
    expect("switch-model", call("switch-model", "/_service/control", control, {"phase": "ready", "model": "Qwen3.5-9B"})[0] == 200)
    expect("wrong-model", call("wrong-model", "/v1/chat/completions", valid, {**chat, "model": "Qwen3.5-9B"})[0] in {401, 403})
    expect("reset-model", call("reset-model", "/_service/control", control, {"phase": "ready", "model": "Qwen3.8-27B-FP8"})[0] == 200)
    limited = key("rpm-key", "fixture-alice", rpm_limit=1)
    expect("rpm-first", call("rpm-first", "/v1/chat/completions", limited, chat)[0] == 200)
    expect("rpm-limit", call("rpm-limit", "/v1/chat/completions", limited, chat)[0] == 429)
    token_limited = key("tpm-key", "fixture-alice", tpm_limit=1)
    first = call("tpm-first", "/v1/chat/completions", token_limited, chat)[0]
    expect("tpm-first", first in {200, 429})
    time.sleep(1)
    expect("tpm-limit", call("tpm-limit", "/v1/chat/completions", token_limited, chat)[0] == 429)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        slow = pool.submit(call, "concurrency-slow", "/v1/chat/completions", valid,
                           {**chat, "messages": [{"role": "user", "content": "slow-fixture"}]})
        time.sleep(0.5)
        expect("concurrency-overlap", call("concurrency-overlap", "/v1/chat/completions", valid, chat)[0] == 429)
        expect("concurrency-complete", slow.result()[0] == 200)
    checkpoint("disconnect-stream", "start")
    streaming_request = urllib.request.Request("http://gateway:4000/v1/chat/completions",
        headers={"Authorization": "Bearer " + valid, "Content-Type": "application/json"},
        data=json.dumps({**chat, "stream": True,
            "messages": [{"role": "user", "content": "disconnect-fixture"}]}).encode())
    with urllib.request.urlopen(streaming_request, timeout=10) as response:
        checkpoint("disconnect-stream", response.status)
        expect("disconnect-stream", response.readline().startswith(b"data:"))
    for _ in range(30):
        if fixture_status("cancel-backend")["active_streams"] == 0 and json.loads(call("cancel-admission", "/_service/status", valid)[1])["active"] == 0:
            break
        time.sleep(0.1)
    else:
        expect("cancel-complete", False)
    expect("cancel-complete", True)
    expect("backend-unavailable", call("backend-unavailable", "/v1/chat/completions", valid, {**chat,
        "messages": [{"role": "user", "content": "unavailable-fixture"}]})[0] in {502, 503})
    expect("backend-no-retry", fixture_status("backend-no-retry")["unavailable_calls"] == 1)
    expect("revoke-key", call("revoke-key", "/key/delete", master, {"keys": [revoked]})[0] == 200)
    expect("revoked-model", call("revoked-model", "/v1/models", revoked)[0] in {401, 403})
    expect("draining", call("draining", "/_service/control", control, {"phase": "draining"})[0] == 200)
    expect("maintenance", call("maintenance", "/v1/chat/completions", valid, chat)[0] == 503)
    checkpoint("save-keys", "start")
    Path("/state/keys.json").write_text(json.dumps({"valid": valid, "revoked": revoked}))
    expect("save-keys", True)
    checkpoint("result", "pass")
    print("PASS: native keys/expiry/isolation, model ACLs, RPM/TPM/concurrency, streaming/tools, cancellation and failure")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"initial", "restart"}:
            raise ValueError("invalid fixture mode")
        diagnostics = Path("/state") / f"checkpoints-{sys.argv[1]}.tsv"
        main(sys.argv[1])
    except Exception:
        try:
            checkpoint("result", "fail")
        except (NameError, OSError):
            pass
        print("FAIL: native integration assertion; inspect privately without printing keys or bodies", file=sys.stderr)
        sys.exit(1)
