"""Exercise the selected native gateway; print only fixed pass/fail evidence."""
import concurrent.futures
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

secret = Path("/run/fixture")
master = (secret / "master-key").read_text().strip()
control = (secret / "control-key").read_text().strip()


def call(path, key, body=None):
    req = urllib.request.Request("http://gateway:4000" + path, headers={"Authorization": "Bearer " + key,
        "Content-Type": "application/json"}, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read(262144)
            return response.status, data
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def key(user, **limits):
    status, raw = call("/key/generate", master, {"user_id": user, "models": ["Qwen3.8-27B-FP8"],
        "duration": "1h", "rpm_limit": 30, "tpm_limit": 65536, "max_parallel_requests": 1, **limits})
    assert status == 200, "key generation"
    return json.loads(raw)["key"]


def fixture_status():
    with urllib.request.urlopen("http://sglang:30000/fixture/status", timeout=2) as response:
        return json.load(response)


def main():
    for _ in range(90):
        try:
            if call("/health/readiness", master)[0] == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        raise AssertionError("gateway startup")
    assert call("/_service/control", control, {"phase": "ready", "model": "Qwen3.8-27B-FP8"})[0] == 200
    chat = {"model": "Qwen3.8-27B-FP8", "messages": [{"role": "user", "content": "fixture"}]}
    if sys.argv[1] == "restart":
        saved = json.loads(Path("/state/keys.json").read_text())
        assert call("/v1/models", saved["valid"])[0] == 200
        assert call("/v1/models", saved["revoked"])[0] in {401, 403}
        assert call("/v1/chat/completions", saved["valid"], chat)[0] == 200
        print("PASS: native key persistence and revocation across gateway/database restarts")
        return
    for user in ("fixture-alice", "fixture-bob"):
        assert call("/user/new", master, {"user_id": user, "auto_create_key": False})[0] == 200
    valid, revoked = key("fixture-alice"), key("fixture-bob")
    assert call("/v2/user/info?user_id=fixture-bob", valid)[0] in {401, 403}, "cross-user policy"
    expired = key("fixture-alice", duration="1s")
    time.sleep(2)
    assert call("/v1/models", expired)[0] in {401, 403}, "expiration"
    assert call("/v1/models", "invalid-fixture")[0] in {401, 403}
    assert call("/v1/chat/completions", valid, chat)[0] == 200
    listed = json.loads(call("/v1/models", valid)[1])
    assert "Qwen3.8-27B-FP8" in {m["id"] for m in listed["data"]}
    status, stream = call("/v1/chat/completions", valid, {**chat, "stream": True})
    assert status == 200 and b"[DONE]" in stream
    status, result = call("/v1/chat/completions", valid, {**chat, "tools": [{"type": "function", "function": {
        "name": "fixture_tool", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}}}}]})
    assert status == 200 and json.loads(result)["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "fixture_tool"
    assert json.loads(result)["usage"]["total_tokens"] == 10
    tool_message = json.loads(result)["choices"][0]["message"]
    assert call("/v1/chat/completions", valid, {**chat, "messages": chat["messages"] + [tool_message,
        {"role": "tool", "tool_call_id": "call-fixture", "content": '{"value":7}'}]})[0] == 200
    assert call("/_service/control", control, {"phase": "ready", "model": "Qwen3.5-9B"})[0] == 200
    assert call("/v1/chat/completions", valid, {**chat, "model": "Qwen3.5-9B"})[0] in {401, 403}
    assert call("/_service/control", control, {"phase": "ready", "model": "Qwen3.8-27B-FP8"})[0] == 200
    limited = key("fixture-alice", rpm_limit=1)
    assert call("/v1/chat/completions", limited, chat)[0] == 200
    assert call("/v1/chat/completions", limited, chat)[0] == 429
    token_limited = key("fixture-alice", tpm_limit=1)
    first = call("/v1/chat/completions", token_limited, chat)[0]
    assert first in {200, 429}
    time.sleep(1)
    assert call("/v1/chat/completions", token_limited, chat)[0] == 429, "token quota"
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        slow = pool.submit(call, "/v1/chat/completions", valid,
                           {**chat, "messages": [{"role": "user", "content": "slow-fixture"}]})
        time.sleep(0.5)
        assert call("/v1/chat/completions", valid, chat)[0] == 429
        assert slow.result()[0] == 200
    streaming_request = urllib.request.Request("http://gateway:4000/v1/chat/completions",
        headers={"Authorization": "Bearer " + valid, "Content-Type": "application/json"},
        data=json.dumps({**chat, "stream": True,
            "messages": [{"role": "user", "content": "disconnect-fixture"}]}).encode())
    with urllib.request.urlopen(streaming_request, timeout=10) as response:
        assert response.readline().startswith(b"data:")
    for _ in range(30):
        if fixture_status()["active_streams"] == 0 and json.loads(call("/_service/status", valid)[1])["active"] == 0:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("disconnect cleanup")
    assert call("/v1/chat/completions", valid, {**chat,
        "messages": [{"role": "user", "content": "unavailable-fixture"}]})[0] in {502, 503}, "backend failure"
    assert fixture_status()["unavailable_calls"] == 1, "no retry storm"
    assert call("/key/delete", master, {"keys": [revoked]})[0] == 200
    assert call("/v1/models", revoked)[0] in {401, 403}
    assert call("/_service/control", control, {"phase": "draining"})[0] == 200
    assert call("/v1/chat/completions", valid, chat)[0] == 503
    Path("/state/keys.json").write_text(json.dumps({"valid": valid, "revoked": revoked}))
    print("PASS: native keys/expiry/isolation, model ACLs, RPM/TPM/concurrency, streaming/tools, cancellation and failure")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: native integration assertion; inspect privately without printing keys or bodies", file=sys.stderr)
        sys.exit(1)
