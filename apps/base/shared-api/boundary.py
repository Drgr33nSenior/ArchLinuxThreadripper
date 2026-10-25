"""Bounded ASGI boundary around the pinned LiteLLM application.

LiteLLM still authenticates every translated virtual key and owns its database,
allowlists, expiry, revocation and quotas. No custom authentication bypass.
"""
import asyncio
import hmac
import json
import time
from pathlib import Path


CHAT_MODELS = {"Qwen3.8-27B-FP8": (32768, 4096), "Qwen3.5-9B": (4096, 1024)}
MAX_BODY = 1024 * 1024
CHAT_FIELDS = {
    "model", "messages", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "temperature", "top_p", "top_k", "seed", "stop",
    "tools", "tool_choice", "parallel_tool_calls", "response_format",
    "presence_penalty", "frequency_penalty", "chat_template_kwargs", "n",
}


class Refusal(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def secret_json(path):
    # Kubernetes projected Secret symlinks are intentional. Never log the data.
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid secret document")
    return data


def browser_key(headers, path, credentials, now=None, need_virtual_key=True):
    """Only the internal /webui route can translate a trusted application user."""
    auth = headers.get(b"authorization", b"").decode("latin1")
    expected = credentials.get("service_key", "")
    if len(expected) < 32 or not hmac.compare_digest(auth, "Bearer " + expected):
        raise Refusal(401, "invalid_application_credential")
    uid = headers.get(b"x-openwebui-user-id", b"").decode("latin1")
    record = credentials.get("users", {}).get(uid)
    if not record or record.get("expires_at", 0) <= (time.time() if now is None else now):
        raise Refusal(403, "browser_user_not_provisioned")
    if path.endswith("images/generations") and not record.get("images", False):
        raise Refusal(403, "image_permission_denied")
    key = record.get("key", "")
    if need_virtual_key and (not key.startswith("sk-") or len(key) < 24):
        raise Refusal(403, "browser_user_not_provisioned")
    return key, uid


async def reply(send, status, value):
    body = json.dumps(value, separators=(",", ":")).encode()
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
    if status in {429, 503}:
        headers.append((b"retry-after", b"5"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def read_body(receive, limit=MAX_BODY):
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise asyncio.CancelledError()
        body.extend(message.get("body", b""))
        if len(body) > limit:
            raise Refusal(413, "request_too_large")
        if not message.get("more_body", False):
            return bytes(body)


class GatewayBoundary:
    def __init__(self, app, browser_file, control_file, request_timeout=180):
        self.app, self.browser_file, self.control_file = app, browser_file, control_file
        self.request_timeout = request_timeout
        # A process restart never restores a saved ready flag or completed job.
        self.state = {"phase": "unavailable", "model": None, "images": False}
        self.active = {}
        self.requests = {"ok": 0, "refused": 0, "backend_error": 0, "cancelled": 0, "limited": 0}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        path = scope["path"]
        response_started = False
        original_send = send

        async def send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await original_send(message)

        try:
            if path == "/_service/status":
                return await reply(send, 200, {**self.state, "active": len(self.active)})
            if path == "/_service/metrics":
                phase = self.state["phase"]
                lines = [f'workstation_service_phase{{phase="{name}"}} {int(name == phase)}'
                         for name in ("ready", "starting", "draining", "unavailable", "maintenance")]
                lines += [f"workstation_service_active_requests {len(self.active)}",
                          f"workstation_service_oldest_request_seconds {max((time.monotonic() - t for t in self.active.values()), default=0):.3f}"]
                lines += [f'workstation_service_requests_total{{result="{name}"}} {value}'
                          for name, value in self.requests.items()]
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain; version=0.0.4")]})
                return await send({"type": "http.response.body", "body": ("\n".join(lines) + "\n").encode()})
            if path == "/_service/control":
                return await self.control(scope, headers, receive, send)
            if path.startswith("/webui/"):
                if path not in {"/webui/v1/models", "/webui/v1/chat/completions"}:
                    raise Refusal(404, "unsupported_route")
                key, _ = browser_key(headers, path, secret_json(self.browser_file))
                scope = dict(scope, path=path.removeprefix("/webui"))
                scope["raw_path"] = scope["path"].encode()
                headers[b"authorization"] = ("Bearer " + key).encode()
            elif any(k.startswith(b"x-openwebui-") for k in headers):
                raise Refusal(400, "untrusted_identity_header")
            # Do not forward names, email addresses, caller-supplied accounting
            # tags, cookies or arbitrary provider credentials to LiteLLM.
            scope = dict(scope, headers=[(k, v) for k, v in headers.items()
                         if k in {b"authorization", b"content-type", b"accept"}])
            if scope["path"] == "/v1/chat/completions":
                return await self.chat(scope, receive, send)
            if scope["path"] in {"/v1/responses", "/responses"}:
                raise Refusal(501, "responses_api_not_qualified")
            return await self.app(scope, receive, send)
        except Refusal as exc:
            self.requests["limited" if exc.status == 429 else "refused"] += 1
            return await reply(send, exc.status, {"error": {"code": exc.code, "message": exc.code}})
        except json.JSONDecodeError:
            if response_started:
                raise
            return await reply(send, 400, {"error": {"code": "invalid_json"}})
        except (OSError, ValueError, TypeError, KeyError):
            self.requests["backend_error"] += 1
            if response_started:
                raise
            return await reply(send, 503, {"error": {"code": "service_configuration_unavailable"}})

    async def control(self, scope, headers, receive, send):
        key = Path(self.control_file).read_text().strip()
        if scope["method"] != "POST" or len(key) < 32 or not hmac.compare_digest(
                headers.get(b"authorization", b""), ("Bearer " + key).encode()):
            raise Refusal(401, "invalid_control_credential")
        data = json.loads(await read_body(receive, 4096))
        phase, model = data.get("phase"), data.get("model")
        if phase not in {"ready", "starting", "draining", "unavailable", "maintenance"}:
            raise Refusal(400, "invalid_phase")
        if phase in {"ready", "starting"} and model not in CHAT_MODELS:
            raise Refusal(400, "invalid_model")
        images = data.get("images", False)
        if type(images) is not bool or (images and model != "Qwen3.5-9B"):
            raise Refusal(400, "images_require_split_profile")
        if data.get("cancel") is True:
            if phase not in {"draining", "unavailable"}:
                raise Refusal(400, "invalid_cancel_phase")
        self.state = {"phase": phase, "model": model, "images": images and phase == "ready"}
        if data.get("cancel") is True:
            for task in tuple(self.active):
                task.cancel()
        return await reply(send, 200, {**self.state, "active": len(self.active)})

    async def chat(self, scope, receive, send):
        if scope["method"] != "POST":
            raise Refusal(405, "method_not_allowed")
        data = json.loads(await read_body(receive))
        if not isinstance(data, dict):
            raise Refusal(400, "invalid_request")
        # Open WebUI may supply a user field. Key ownership is authoritative.
        data.pop("user", None)
        if set(data) - CHAT_FIELDS:
            raise Refusal(400, "unsupported_request_field")
        model = data.get("model")
        if model not in CHAT_MODELS:
            raise Refusal(404, "unknown_model")
        if self.state["phase"] != "ready" or self.state["model"] != model:
            raise Refusal(503, "requested_model_unavailable")
        if len(self.active) >= 4:
            raise Refusal(429, "service_concurrency_limit")
        if data.get("n", 1) != 1:
            raise Refusal(400, "one_completion_per_request")
        if "max_tokens" in data and "max_completion_tokens" in data:
            raise Refusal(400, "ambiguous_output_limit")
        field = "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens"
        cap = CHAT_MODELS[model][1]
        limit = data.get(field, cap)
        if type(limit) is not int or not 1 <= limit <= cap:
            raise Refusal(400, "output_limit_exceeded")
        data[field] = limit
        body = json.dumps(data).encode()
        scope["headers"] += [(b"content-length", str(len(body)).encode())]
        sent = False
        started = False
        response_status = 500

        async def tracked_send(message):
            nonlocal started, response_status
            if message["type"] == "http.response.start":
                started = True
                response_status = message["status"]
            await send(message)

        disconnected = asyncio.Event()

        async def watch_disconnect():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    disconnected.set()
                    return

        # The body is already buffered. Own the original receive channel so a
        # non-streaming backend is also cancelled when its caller disconnects.
        async def backend_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        task = asyncio.current_task()
        self.active[task] = time.monotonic()
        backend_task = asyncio.create_task(self.app(scope, backend_receive, tracked_send))
        disconnect_task = asyncio.create_task(watch_disconnect())
        try:
            async with asyncio.timeout(self.request_timeout):
                done, _ = await asyncio.wait((backend_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED)
                if disconnect_task in done:
                    raise asyncio.CancelledError()
                await backend_task
            result = "ok" if response_status < 400 else "limited" if response_status == 429 else "backend_error" if response_status >= 500 else "refused"
            self.requests[result] += 1
        except TimeoutError:
            self.requests["backend_error"] += 1
            if not started:
                await reply(send, 504, {"error": {"code": "request_deadline"}})
            else:
                # Terminate the stream without a fabricated success/[DONE].
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            self.requests["cancelled"] += 1
            if not started and not disconnected.is_set():
                await reply(send, 503, {"error": {"code": "request_cancelled"}})
            raise
        finally:
            for child in (backend_task, disconnect_task):
                if not child.done():
                    child.cancel()
            await asyncio.gather(backend_task, disconnect_task, return_exceptions=True)
            self.active.pop(task, None)
