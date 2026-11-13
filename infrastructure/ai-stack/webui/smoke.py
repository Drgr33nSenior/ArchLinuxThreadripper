"""Offline smoke test for the forked Open WebUI image: provider-declared Model Controls.

Runs inside the image (python -I /smoke.py, no network). Starts a fixture OpenAI-compatible
backend that declares reasoning levels in /models the way the gateway does and echoes the
reasoning_effort it receives, starts Open WebUI against it with a throwaway database, then
drives the HTTP API like the browser:

- the model list carries the provider's controls as a slider, labels only;
- each slider choice, and the default, reaches the backend; an unknown choice is refused;
- an ordinary user sees the slider on a stored model record without admin controls (the live shape);
- admin-defined controls replace the provider's.

Passwords are random and never printed; nothing leaves the container.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "Qwen3.8-27B-MXFP4"
DECLARED = {
    "id": MODEL, "object": "model", "owned_by": "gateway",
    "supportedReasoningEfforts": [
        {"reasoningEffort": "none", "label": "Off"}, {"reasoningEffort": "low", "label": "Low"},
        {"reasoningEffort": "medium", "label": "Medium"}, {"reasoningEffort": "xhigh", "label": "Max"}],
    "defaultReasoningEffort": "low",
}
BASE = "http://127.0.0.1:8080"
results = []


class Backend(BaseHTTPRequestHandler):
    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send({"object": "list", "data": [DECLARED]})
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        seen = json.dumps({"reasoning_effort": body.get("reasoning_effort")})
        self._send({"id": "echo", "object": "chat.completion", "created": 1, "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": seen}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def log_message(self, *args):
        pass


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}", flush=True)


def api(method, path, token=None, body=None):
    request = urllib.request.Request(
        BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, None


def listed(token):
    status, body = api("GET", "/api/models", token)
    return next((m for m in (body or {}).get("data", []) if m["id"] == MODEL), None)


def chat(token, choice=None):
    params = {"model_controls": {MODEL: {"reasoning_effort": choice}}} if choice else {}
    status, reply = api("POST", "/api/chat/completions", token, {
        "model": MODEL, "stream": False, "messages": [{"role": "user", "content": "hi"}], "params": params})
    seen = json.loads(reply["choices"][0]["message"]["content"])["reasoning_effort"] if status == 200 else None
    return status, seen


def run_checks():
    password = secrets.token_urlsafe(24)
    status, body = api("POST", "/api/v1/auths/signup", body={
        "name": "admin", "email": "admin@example.invalid", "password": password})
    admin = (body or {}).get("token")
    check("first account signs up as admin", bool(admin), str(status))
    if not admin:
        return
    model = listed(admin)
    effort = ((model or {}).get("provider_model_controls") or {}).get("reasoning_effort") or {}
    check("model list carries the provider's controls as a slider", effort.get("display") == "slider", str(effort.get("display")))
    check("labels and default", [o.get("label") for o in effort.get("options", {}).values()] == ["Off", "Low", "Medium", "Max"]
          and effort.get("default") == "low")
    check("clients see labels only", all(set(o) == {"label"} for o in effort.get("options", {}).values()))
    for choice, expected in (("xhigh", "xhigh"), ("none", "none"), (None, "low")):
        status, seen = chat(admin, choice)
        check(f"choice {choice!r} reaches the backend as {expected!r}", status == 200 and seen == expected, f"{status} {seen!r}")
    status, _ = chat(admin, "extreme")
    check("an unknown choice is refused", status == 400, str(status))

    user_password = secrets.token_urlsafe(24)
    status, _ = api("POST", "/api/v1/auths/add", admin, {
        "name": "user", "email": "user@example.invalid", "password": user_password, "role": "user"})
    check("admin adds an ordinary user", status == 200, str(status))
    status, body = api("POST", "/api/v1/auths/signin", body={"email": "user@example.invalid", "password": user_password})
    user = (body or {}).get("token")
    grant = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
    record = {"id": MODEL, "name": MODEL, "base_model_id": None, "meta": {}, "params": {}, "access_grants": grant}
    status, _ = api("POST", "/api/v1/models/create", admin, record)
    check("a stored record without controls is saved", status == 200, str(status))
    user_effort = (((listed(user) if user else None) or {}).get("provider_model_controls") or {}).get("reasoning_effort") or {}
    check("an ordinary user sees the provider's slider on the stored model", user_effort.get("display") == "slider")
    status, seen = chat(user, "xhigh") if user else (None, None)
    check("the user's choice reaches the backend", status == 200 and seen == "xhigh", f"{status} {seen!r}")

    admin_controls = {"tone": {"label": "Tone", "options": {"brief": {"label": "Brief", "params": {"temperature": 0.2}}}}}
    status, _ = api("POST", "/api/v1/models/model/update", admin, {**record, "params": {"model_controls": admin_controls}})
    check("admin defines their own controls", status == 200, str(status))
    model = listed(admin) or {}
    check("admin-defined controls replace the provider's", "provider_model_controls" not in model
          and list(((model.get("info") or {}).get("params") or {}).get("model_controls", {})) == ["tone"])
    status, seen = chat(admin, "xhigh")
    check("a stale provider choice is skipped", status == 200 and seen is None, f"{status} {seen!r}")


def main():
    backend = ThreadingHTTPServer(("127.0.0.1", 9000), Backend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    env = dict(os.environ, OPENAI_API_BASE_URL="http://127.0.0.1:9000/v1", OPENAI_API_KEY="fixture",
               ENABLE_OLLAMA_API="false", OFFLINE_MODE="true", HF_HUB_OFFLINE="1",
               RAG_EMBEDDING_MODEL_AUTO_UPDATE="false", WEBUI_SECRET_KEY=secrets.token_hex(32),
               DATA_DIR="/tmp/webui-smoke")
    os.makedirs(env["DATA_DIR"], exist_ok=True)  # a throwaway database, never the image's
    log = open("/tmp/webui-smoke.log", "wb")
    webui = subprocess.Popen(["bash", "start.sh"], cwd="/app/backend", env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and webui.poll() is None:
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=5):
                    break
            except OSError:
                time.sleep(2)
        else:
            check("Open WebUI starts", False, "exited" if webui.poll() is not None else "no /health within 300 s")
        if all(results):
            run_checks()
    finally:
        webui.terminate()
        try:
            webui.wait(timeout=30)
        except subprocess.TimeoutExpired:
            webui.kill()
    if not all(results):
        sys.stdout.write(open("/tmp/webui-smoke.log", errors="replace").read()[-4000:])
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
