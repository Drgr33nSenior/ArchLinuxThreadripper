"""UQ3 capture client: one long real-text prompt, then hand the capture file over.

Standard library only. Waits for /health, sends /qualify/prompt.txt as a single chat turn
(one output token), checks the prompt reached UQ3_CAPTURE_TOKENS, waits for the hook's
.done marker, prints a READY line with the file's size and SHA-256, then waits (bounded) for
the operator to copy it out and create the 'fetched' marker before exiting.

Usage: capture.py PORT SERVED_MODEL_NAME SERVER_PID TIMEOUT_SECONDS
"""
import hashlib
import json
import os
import sys
import time
import urllib.request


def http(base, path, body=None, timeout=900):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    port, model, server, budget = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    base, deadline = f"http://127.0.0.1:{port}", time.monotonic() + budget
    tokens = int(os.environ["UQ3_CAPTURE_TOKENS"])
    out = os.environ.get("UQ3_CAPTURE_DIR", "/cache/uq3")
    path = os.path.join(out, "capture-rank0.pt")
    while time.monotonic() < deadline:
        if not alive(server):
            sys.exit("UQ3 CAPTURE FAILED: server exited during start-up")
        try:
            urllib.request.urlopen(base + "/health", timeout=5).read()
            break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(5)
    else:
        sys.exit("UQ3 CAPTURE FAILED: server not healthy in time")
    text = open("/qualify/prompt.txt", encoding="utf-8").read()
    reply = http(base, "/v1/chat/completions", {
        "model": model, "max_tokens": 1, "temperature": 0,
        "messages": [{"role": "user", "content": "Read the following material carefully.\n\n" + text}]})
    prompt_tokens = reply["usage"]["prompt_tokens"]
    print(f"UQ3 capture prompt: {prompt_tokens} tokens (capturing the first {tokens})", flush=True)
    if prompt_tokens < tokens:
        sys.exit(f"UQ3 CAPTURE FAILED: prompt has {prompt_tokens} tokens, fewer than {tokens}")
    while time.monotonic() < deadline and not os.path.exists(path + ".done"):
        if os.path.exists(os.path.join(out, "capture-error.txt")):
            sys.exit("UQ3 CAPTURE FAILED: " + open(os.path.join(out, "capture-error.txt")).read())
        time.sleep(2)
    if not os.path.exists(path + ".done"):
        sys.exit("UQ3 CAPTURE FAILED: no capture file (the hook did not see all layers)")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    print(f"UQ3 CAPTURE READY {path} {os.path.getsize(path)} {digest.hexdigest()}", flush=True)
    fetched = os.path.join(out, "fetched")
    while time.monotonic() < deadline and not os.path.exists(fetched):
        time.sleep(5)
    if not os.path.exists(fetched):
        sys.exit("UQ3 CAPTURE FAILED: the file was not collected before the deadline")
    print("UQ3 CAPTURE COLLECTED", flush=True)


if __name__ == "__main__":
    main()
