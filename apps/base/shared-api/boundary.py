"""Bounded ASGI boundary around the pinned LiteLLM application.

LiteLLM still authenticates every translated virtual key and owns its database,
allowlists, expiry, revocation and quotas. No custom authentication bypass.
"""
import asyncio
import base64
import hmac
import json
import time
from pathlib import Path

import httpx


CHAT_MODELS = {"Qwen3.8-27B-FP8": (32768, 4096), "Qwen3.5-9B": (4096, 1024)}
# Models that may serve with image generation on. Source default: the one-GPU 9B split
# profile. A rendered policy replaces it (shared mode adds the two-GPU contract model).
IMAGE_MODELS = {"Qwen3.5-9B"}
MAX_BODY = 1024 * 1024
# The gateway is SGLang's only client; service-tools reaches the elastic KV tail through it.
SGLANG_URL = "http://sglang:30000"
KV_TAIL_ACTIONS = {"status", "shrink", "grow"}


def sglang_client():
    return httpx.AsyncClient(trust_env=False, follow_redirects=False)
# Chat bodies may be larger: Open WebUI resends every image a tool returned earlier in the chat
# as base64 on each later turn, and a generate_video clip is an animated WEBP of up to 8 MiB
# (10.7 MiB as base64; 2.3-5.2 MiB measured). 64 MiB holds a chat with several clips; the clips
# are replaced below, so the model never receives them.
MAX_CHAT_BODY = 64 * 1024 * 1024
VIDEO_NOTE = "[A short video clip made earlier in this chat was shown to the user here.]"
CHAT_FIELDS = {
    "model", "messages", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "temperature", "top_p", "top_k", "seed", "stop",
    "tools", "tool_choice", "parallel_tool_calls", "response_format",
    "presence_penalty", "frequency_penalty", "chat_template_kwargs", "n",
    "reasoning_effort",
}
# Client effort levels mapped to the levels the pinned checkpoint template
# accepts (low, medium, xhigh). Clients such as Open WebUI offer the OpenAI
# "high" level, which that template would reject, so it is sent as its
# nearest level, xhigh. The selected profile uses low.
REASONING_EFFORTS = {"low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh"}
# Template effort whose requests, when they set no output cap, run to the context limit
# instead of the default cap ("Think"): SGLang rejects input + max_tokens > context, so
# a large fixed cap would fail long conversations, while an unset cap ends at the
# context and the gateway deadline bounds the time.
UNCAPPED_EFFORTS = {"xhigh"}
# Effort for requests that name none (the contract's request default). Applied here, not as an
# SGLang server default: the pinned SGLang lets a server-default effort override every request's.
DEFAULT_REASONING_EFFORT = None
# Reasoning levels advertised in the model list, in increasing order, with the labels Open
# WebUI shows on its reasoning slider (Model Controls declared by the provider).
REASONING_ORDER = ("low", "medium", "high", "xhigh")
REASONING_LABELS = {"none": "Off", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Max"}
# Template variables a client may set. Other variables can change the rendered
# prompt in unreviewed ways, so they are refused rather than forwarded.
TEMPLATE_KWARGS = {"enable_thinking": bool}
# Requests admitted at once; the selected runtime may execute fewer and queue.
ADMISSION_LIMIT = 4
# Streaming budgets (gateway-policy.json): the wait for a stream's first content and the
# longest silence once content flows. A long reply that keeps streaming runs to the overall
# request_timeout; a silent one is ended early. None: the overall deadline only.
FIRST_TOKEN_TIMEOUT = None
IDLE_TIMEOUT = None
# Boolean template switches a rendered policy may allow. Anything else is refused.
POLICY_TEMPLATE_KWARGS = {"enable_thinking", "preserve_thinking"}


def apply_policy(path):
    """Replace the source defaults above with a rendered serving-contract policy.

    `service render --serving-profile` writes gateway-policy.json from the same
    contract as the LiteLLM route, so the gateway admits only the contract's
    models, budgets and reasoning values. The defaults are replaced, not merged:
    a model the contract omits is no longer admitted. Returns the deadline.
    """
    global ADMISSION_LIMIT, DEFAULT_REASONING_EFFORT, FIRST_TOKEN_TIMEOUT, IDLE_TIMEOUT
    policy = json.loads(Path(path).read_text())
    models = policy["chat_models"]
    efforts = policy["reasoning_efforts"]
    kwargs = policy["template_kwargs"]
    timeout, admission = policy["request_timeout"], policy["admission_limit"]
    first_token, idle = policy.get("first_token_timeout"), policy.get("idle_timeout")
    image_models = policy.get("image_models", ["Qwen3.5-9B"] if "Qwen3.5-9B" in models else [])
    if (not isinstance(models, dict) or not models
            or any(type(c) is not int or type(o) is not int or not 1 <= o <= c // 2
                   for c, o in (tuple(limits) for limits in models.values()))
            or not isinstance(efforts, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                       for k, v in efforts.items())
            or not isinstance(kwargs, list) or not set(kwargs) <= POLICY_TEMPLATE_KWARGS
            or type(timeout) is not int or not 30 <= timeout <= 3600
            or any(value is not None and (type(value) is not int or not 10 <= value <= timeout)
                   for value in (first_token, idle))
            or type(admission) is not int or not 1 <= admission <= 16
            or not isinstance(image_models, list) or not set(image_models) <= set(models)
            or policy.get("default_reasoning_effort") not in (None, *efforts)):
        raise ValueError("invalid gateway policy")
    CHAT_MODELS.clear()
    CHAT_MODELS.update({name: tuple(limits) for name, limits in models.items()})
    REASONING_EFFORTS.clear()
    REASONING_EFFORTS.update(efforts)
    TEMPLATE_KWARGS.clear()
    TEMPLATE_KWARGS.update({name: bool for name in kwargs})
    IMAGE_MODELS.clear()
    IMAGE_MODELS.update(image_models)
    ADMISSION_LIMIT = admission
    DEFAULT_REASONING_EFFORT = policy.get("default_reasoning_effort")
    FIRST_TOKEN_TIMEOUT, IDLE_TIMEOUT = first_token, idle
    return timeout


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


def is_animated_webp(url):
    """True for a data URI holding an animated WEBP: RIFF/WEBP, a VP8X chunk, animation flag set."""
    prefix = "data:image/webp;base64,"
    if not isinstance(url, str) or not url.startswith(prefix):
        return False
    try:
        head = base64.b64decode(url[len(prefix):len(prefix) + 32], validate=True)
    except ValueError:
        return False
    return len(head) > 20 and head[:4] == b"RIFF" and head[8:16] == b"WEBPVP8X" and bool(head[20] & 0x02)


def drop_video_clips(messages):
    """Replace animated WEBP image parts with a text note; returns how many were replaced.

    They are generate_video clips coming back from the chat history. The model cannot use a
    5-second animation as a picture, and each one would cost a vision encode on every turn.
    Still images, such as a photo the user uploaded, pass unchanged.
    """
    replaced = 0
    for message in messages if isinstance(messages, list) else ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") not in ("image_url", "input_image"):
                continue
            image = part.get("image_url")
            if is_animated_webp(image.get("url") if isinstance(image, dict) else image):
                content[index] = {"type": "text", "text": VIDEO_NOTE}
                replaced += 1
    return replaced


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


def reasoning_declaration():
    """The reasoning levels this gateway accepts, as Open WebUI's provider-declared Model Controls."""
    levels = (["none"] if "enable_thinking" in TEMPLATE_KWARGS else []) + [
        level for level in REASONING_ORDER if level in set(REASONING_EFFORTS.values())]
    if not levels:
        return {}
    declaration = {"supportedReasoningEfforts": [
        {"reasoningEffort": level, "label": REASONING_LABELS.get(level, level)} for level in levels]}
    default = REASONING_EFFORTS.get(DEFAULT_REASONING_EFFORT) if DEFAULT_REASONING_EFFORT else None
    if default in levels:
        declaration["defaultReasoningEffort"] = default
    return declaration


class GatewayBoundary:
    def __init__(self, app, browser_file, control_file, request_timeout=180, first_token_timeout=None, idle_timeout=None):
        self.app, self.browser_file, self.control_file = app, browser_file, control_file
        self.request_timeout = request_timeout
        self.first_token_timeout, self.idle_timeout = first_token_timeout, idle_timeout
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
            if path == "/_service/kv_tail":
                return await self.kv_tail(scope, headers, receive, send)
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
            if scope["path"] == "/v1/models" and scope["method"] == "GET":
                return await self.models(scope, receive, send)
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

    async def models(self, scope, receive, send):
        """Publish each configured chat model's context window and accepted reasoning levels.

        context_window is the serving contract's full per-request token window. Keep the router's
        max_input_tokens unchanged: it reserves output capacity and scheduler headroom. Open WebUI
        preserves these model fields for clients. Non-list responses pass through unchanged."""
        start, chunks = {}, []

        async def capture(message):
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        body = b"".join(chunks)
        headers = start.get("headers", [])
        if start.get("status") == 200 and not any(k.lower() == b"content-encoding" for k, _ in headers):
            try:
                listing = json.loads(body)
                declaration = reasoning_declaration()
                entries = listing.get("data", [])
                for model in entries if isinstance(entries, list) else ():
                    if isinstance(model, dict) and model.get("id") in CHAT_MODELS:
                        model.update(declaration)
                        model["context_window"] = CHAT_MODELS[model["id"]][0]
                body = json.dumps(listing).encode()
            except (ValueError, AttributeError):
                pass
        headers = [(k, v) for k, v in headers if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": start.get("status", 502), "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def kv_tail(self, scope, headers, receive, send):
        """Forward one elastic KV tail action from service-tools to SGLang (serving patch 0033).

        Only status, shrink and grow pass, authenticated with the service key service-tools already
        holds for pictures, so no other SGLang endpoint becomes reachable. SGLang without the tail
        (no route, or the flag off) answers "disabled", which service-tools treats as nothing to
        release. SGLang's own answer is returned unchanged: 200 when done, 409 when not."""
        key = str(secret_json(self.browser_file).get("service_key", ""))
        if scope["method"] != "POST" or len(key) < 32 or not hmac.compare_digest(
                headers.get(b"authorization", b""), ("Bearer " + key).encode()):
            raise Refusal(401, "invalid_service_credential")
        data = json.loads(await read_body(receive, 1024))
        if not isinstance(data, dict) or data.get("action") not in KV_TAIL_ACTIONS or set(data) - {"action", "timeout_s"}:
            raise Refusal(400, "invalid_kv_tail_action")
        body, wait = {"action": data["action"]}, 10
        if data["action"] == "shrink":
            limit = data.get("timeout_s", 15)
            if type(limit) not in (int, float) or not 1 <= limit <= 30:
                raise Refusal(400, "invalid_kv_tail_action")
            # SGLang never waits for a running chat (it migrates it out of the tail); it only waits up to
            # release_timeout_s for both cards to show the memory free. timeout_s bounds this HTTP wait.
            body["release_timeout_s"] = min(10, limit)
            wait = limit
        elif "timeout_s" in data:
            raise Refusal(400, "invalid_kv_tail_action")
        elif data["action"] == "grow":
            wait = 60
        try:
            async with sglang_client() as client:
                response = await client.post(SGLANG_URL + "/kv_tail", json=body, timeout=wait)
        except httpx.HTTPError:
            raise Refusal(503, "kv_tail_unavailable") from None
        if response.status_code == 404:
            return await reply(send, 200, {"ok": False, "reason": "disabled"})
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if response.status_code not in {200, 409} or not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            raise Refusal(503, "kv_tail_unavailable")
        return await reply(send, 200 if answer["ok"] else 409, answer)

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
        if type(images) is not bool or (images and model not in IMAGE_MODELS):
            raise Refusal(400, "images_not_admitted_for_model")
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
        data = json.loads(await read_body(receive, MAX_CHAT_BODY))
        if not isinstance(data, dict):
            raise Refusal(400, "invalid_request")
        drop_video_clips(data.get("messages"))
        # Open WebUI may supply a user field. Key ownership is authoritative.
        data.pop("user", None)
        if set(data) - CHAT_FIELDS:
            raise Refusal(400, "unsupported_request_field")
        model = data.get("model")
        if model not in CHAT_MODELS:
            raise Refusal(404, "unknown_model")
        if self.state["phase"] != "ready" or self.state["model"] != model:
            raise Refusal(503, "requested_model_unavailable")
        if len(self.active) >= ADMISSION_LIMIT:
            raise Refusal(429, "service_concurrency_limit")
        if data.get("n", 1) != 1:
            raise Refusal(400, "one_completion_per_request")
        # OpenAI's "none" effort (Qwen Code's "Thinking off" with the openai-effort profile)
        # is the template's thinking switch, not an effort level: send what Open WebUI's
        # Fast preset sends. Refused when it contradicts the request or the contract.
        if data.get("reasoning_effort") == "none":
            kwargs = data.get("chat_template_kwargs", {})
            if ("enable_thinking" not in TEMPLATE_KWARGS or not isinstance(kwargs, dict)
                    or kwargs.get("enable_thinking", False) is not False):
                raise Refusal(400, "invalid_reasoning_effort")
            del data["reasoning_effort"]
            data["chat_template_kwargs"] = {**kwargs, "enable_thinking": False}
        if "reasoning_effort" in data and (type(data["reasoning_effort"]) is not str
                                           or data["reasoning_effort"] not in REASONING_EFFORTS):
            raise Refusal(400, "invalid_reasoning_effort")
        if "reasoning_effort" not in data and DEFAULT_REASONING_EFFORT is not None:
            data["reasoning_effort"] = DEFAULT_REASONING_EFFORT
        if "reasoning_effort" in data:
            data["reasoning_effort"] = REASONING_EFFORTS[data["reasoning_effort"]]
        kwargs = data.get("chat_template_kwargs", {})
        if (not isinstance(kwargs, dict) or set(kwargs) - set(TEMPLATE_KWARGS)
                or any(type(value) is not TEMPLATE_KWARGS[key] for key, value in kwargs.items())):
            raise Refusal(400, "invalid_chat_template_kwargs")
        if "max_tokens" in data and "max_completion_tokens" in data:
            raise Refusal(400, "ambiguous_output_limit")
        field = "max_completion_tokens" if "max_completion_tokens" in data else "max_tokens"
        cap = CHAT_MODELS[model][1]
        if field in data or data.get("reasoning_effort") not in UNCAPPED_EFFORTS:
            limit = data.get(field, cap)
            if type(limit) is not int or not 1 <= limit <= cap:
                raise Refusal(400, "output_limit_exceeded")
            data[field] = limit
        body = json.dumps(data).encode()
        scope["headers"] += [(b"content-length", str(len(body)).encode())]
        sent = False
        started = False
        finishing = False
        response_status = 500
        streaming = data.get("stream") is True
        content = False
        last_content = time.monotonic()

        async def tracked_send(message):
            nonlocal started, finishing, response_status, content, last_content
            if message["type"] == "http.response.start":
                started = True
                response_status = message["status"]
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    content, last_content = True, time.monotonic()
                if not message.get("more_body", False):
                    # Set before sending: uvicorn reports http.disconnect as soon as the final
                    # body is written, which can reach watch_disconnect before this returns.
                    finishing = True
            await send(message)

        async def watch_budgets():
            # A stream gets first_token_timeout for its first content (queue, prefill and the
            # start of thinking) and then idle_timeout between chunks; reasoning streams as
            # content, so a model that is thinking is not idle. A non-streamed reply sends
            # nothing until it is complete, so only the overall deadline bounds it.
            while True:
                limit = (self.idle_timeout if content else self.first_token_timeout) if streaming else None
                if limit is not None and time.monotonic() - last_content > limit:
                    return "stream_idle" if content else "first_token_deadline"
                await asyncio.sleep(0.5 if limit is None or limit > 5 else 0.05)

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
        budget_task = asyncio.create_task(watch_budgets())
        deadline_code = "request_deadline"
        try:
            async with asyncio.timeout(self.request_timeout):
                done, _ = await asyncio.wait((backend_task, disconnect_task, budget_task),
                                             return_when=asyncio.FIRST_COMPLETED)
                if budget_task in done and not finishing:
                    deadline_code = budget_task.result()
                    raise TimeoutError
                # A disconnect after the final body is the server closing a finished response,
                # not the caller leaving: let the backend finish its own post-response work.
                if disconnect_task in done and not finishing:
                    raise asyncio.CancelledError()
                await backend_task
            result = "ok" if response_status < 400 else "limited" if response_status == 429 else "backend_error" if response_status >= 500 else "refused"
            self.requests[result] += 1
        except TimeoutError:
            self.requests["backend_error"] += 1
            if not started:
                await reply(send, 504, {"error": {"code": deadline_code}})
            else:
                # Terminate the stream without a fabricated success/[DONE].
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            self.requests["cancelled"] += 1
            if not started and not disconnected.is_set():
                await reply(send, 503, {"error": {"code": "request_cancelled"}})
            raise
        finally:
            for child in (backend_task, disconnect_task, budget_task):
                if not child.done():
                    child.cancel()
            await asyncio.gather(backend_task, disconnect_task, budget_task, return_exceptions=True)
            self.active.pop(task, None)
