"""Software boundaries with deterministic backends; never a GPU/DR qualification."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import boundary
import service_backup
import service


async def request(app, path, body=None, headers=None, method="POST"):
    queue = asyncio.Queue()
    await queue.put({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})
    messages = []
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "method": method,
             "headers": list((headers or {}).items()), "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await app(scope, queue.get, send)
    return messages



def requires_input(available, reason):
    """Register a test only when its input exists; otherwise name the skip.

    check-strict refuses anonymous unittest skips. A test that needs private
    evidence or host features absent from CI prints a fixed SKIP line instead,
    which check-strict accepts only when listed explicitly.
    """
    def register(test):
        if available:
            return test
        print(f"SKIP: {reason}")
        return None
    return register

class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.control = self.root / "control"
        self.control.write_text("c" * 32)
        self.mapping = self.root / "mapping"
        self.mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {
            "alice": {"key": "sk-" + "a" * 32, "expires_at": 9999999999, "images": True},
            "expired": {"key": "sk-" + "e" * 32, "expires_at": 1}}}))
        self.seen = []
        async def backend(scope, receive, send):
            self.seen.append((scope, json.loads((await receive())["body"])))
            await boundary.reply(send, 200, {"choices": [{"message": {"tool_calls": [{"id": "call-1"}]}}],
                                             "usage": {"completion_tokens": 1}})
        self.gateway = boundary.GatewayBoundary(backend, self.mapping, self.control)
        self.gateway.state = {"phase": "ready", "model": "Qwen3.8-27B-FP8", "images": False}
        self.chat = {"model": "Qwen3.8-27B-FP8", "messages": [{"role": "user", "content": "fixture"}]}

    async def test_model_identity_and_output_limits(self):
        for data, expected in (({**self.chat, "model": "other"}, 404),
                               ({**self.chat, "model": "Qwen3.5-9B"}, 503),
                               ({**self.chat, "max_tokens": 4097}, 400),
                               ({**self.chat, "api_base": "https://unexpected.invalid"}, 400)):
            result = await request(self.gateway, "/v1/chat/completions", data)
            self.assertEqual(expected, result[0]["status"])
        self.assertEqual([], self.seen)

    async def test_browser_identity_translation_removes_untrusted_fields(self):
        headers = {b"authorization": b"Bearer " + b"s" * 32, b"x-openwebui-user-id": b"alice",
                   b"cookie": b"private", b"x-openwebui-user-email": b"private"}
        result = await request(self.gateway, "/webui/v1/chat/completions", {**self.chat, "user": "mallory"}, headers)
        self.assertEqual(200, result[0]["status"])
        scope, body = self.seen[0]
        self.assertEqual(b"Bearer sk-" + b"a" * 32, dict(scope["headers"])[b"authorization"])
        self.assertNotIn("user", body)
        self.assertNotIn(b"cookie", dict(scope["headers"]))
        self.assertEqual(4096, body["max_tokens"])
        self.assertIn(b'"tool_calls"', result[-1]["body"])

    async def test_identity_spoof_expired_and_unknown_browser_are_denied(self):
        for path, auth, uid, expected in (("/v1/chat/completions", b"s" * 32, b"alice", 400),
            ("/webui/v1/chat/completions", b"bad", b"alice", 401),
            ("/webui/v1/chat/completions", b"s" * 32, b"expired", 403),
            ("/webui/v1/chat/completions", b"s" * 32, b"other", 403)):
            result = await request(self.gateway, path, self.chat,
                {b"authorization": b"Bearer " + auth, b"x-openwebui-user-id": uid})
            self.assertEqual(expected, result[0]["status"])
        self.assertEqual([], self.seen)

    async def test_drain_cancel_and_restart_never_fabricate_ready(self):
        result = await request(self.gateway, "/_service/control", {"phase": "ready", "model": "Qwen3.5-9B", "cancel": True},
                               {b"authorization": b"Bearer " + b"c" * 32})
        self.assertEqual(400, result[0]["status"])
        self.assertEqual("Qwen3.8-27B-FP8", self.gateway.state["model"])
        await request(self.gateway, "/_service/control", {"phase": "draining"},
                      {b"authorization": b"Bearer " + b"c" * 32})
        self.assertEqual(503, (await request(self.gateway, "/v1/chat/completions", self.chat))[0]["status"])
        restarted = boundary.GatewayBoundary(self.gateway.app, self.mapping, self.control)
        self.assertEqual("unavailable", restarted.state["phase"])

    async def test_deadline_releases_slot_and_stream_has_no_fake_done(self):
        async def slow(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})
            await asyncio.sleep(60)
        self.gateway.app = slow
        self.gateway.request_timeout = 0.01
        result = await request(self.gateway, "/v1/chat/completions", self.chat)
        self.assertEqual(1, len([m for m in result if m["type"] == "http.response.start"]))
        self.assertNotIn(b"[DONE]", b"".join(m.get("body", b"") for m in result))
        self.assertFalse(self.gateway.active)

    async def test_active_cancellation_is_bounded(self):
        entered = asyncio.Event()
        async def slow(scope, receive, send):
            entered.set()
            await asyncio.sleep(60)
        self.gateway.app = slow
        task = asyncio.create_task(request(self.gateway, "/v1/chat/completions", self.chat))
        await entered.wait()
        await request(self.gateway, "/_service/control", {"phase": "draining", "cancel": True},
                      {b"authorization": b"Bearer " + b"c" * 32})
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.gateway.active)

    async def test_browser_tool_roundtrip_preserves_reasoning_and_result_identity(self):
        tool = {"type": "function", "function": {"name": "search_web", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
        call = {"id": "call-search-1", "type": "function", "function": {
            "name": "search_web", "arguments": '{"query":"SGLang documentation"}'}}
        answer = {"role": "assistant", "content": "17 entries [source](https://example.com/cache)"}
        result = {"role": "tool", "tool_call_id": call["id"], "content": '{"capacity":17}'}
        headers = {b"authorization": b"Bearer " + b"s" * 32, b"x-openwebui-user-id": b"alice"}
        for thinking in (False, True):
            seen = []
            async def backend(scope, receive, send):
                body = json.loads((await receive())["body"])
                seen.append((scope, body))
                message = {"role": "assistant", "content": None, "tool_calls": [call]} if len(seen) == 1 else answer
                await boundary.reply(send, 200, {"choices": [{"message": message,
                    "finish_reason": "tool_calls" if len(seen) == 1 else "stop"}]})
            self.gateway.app = backend
            initial = {**self.chat, "tools": [tool], "tool_choice": "auto",
                       "chat_template_kwargs": {"enable_thinking": thinking}, "temperature": 1.0}
            first = await request(self.gateway, "/webui/v1/chat/completions", initial, headers)
            message = json.loads(first[-1]["body"])["choices"][0]["message"]
            self.assertEqual([call], message["tool_calls"])
            followup = {**initial, "messages": initial["messages"] + [message, result]}
            final = await request(self.gateway, "/webui/v1/chat/completions", followup, headers)
            self.assertEqual(answer, json.loads(final[-1]["body"])["choices"][0]["message"])
            self.assertEqual(2, len(seen))
            self.assertEqual(followup["messages"], seen[1][1]["messages"])
            for scope, body in seen:
                self.assertEqual(b"Bearer sk-" + b"a" * 32, dict(scope["headers"])[b"authorization"])
                self.assertEqual({"enable_thinking": thinking}, body["chat_template_kwargs"])
                self.assertEqual([tool], body["tools"])
                self.assertEqual("auto", body["tool_choice"])
            self.assertFalse(self.gateway.active)


    async def test_video_clips_in_history_become_a_note_and_photos_pass(self):
        # Open WebUI resends a generate_video clip (animated WEBP, up to 4 MiB) as base64 on
        # every later turn; that once failed the next message with request_too_large.
        def webp(flags, size):
            raw = b"RIFF" + (size - 8).to_bytes(4, "little") + b"WEBPVP8X" + b"\x0a\x00\x00\x00" + bytes([flags])
            return "data:image/webp;base64," + base64.b64encode(raw + b"\x00" * (size - len(raw))).decode()
        clip, still = webp(0x1a, 4 << 20), webp(0x10, 256)
        photo = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"\x00" * 64).decode()
        history = [{"role": "user", "content": "make a fox video"},
                   {"role": "user", "content": [
                       {"type": "text", "text": "Here are the images from the tool results above."},
                       {"type": "image_url", "image_url": {"url": clip}},
                       {"type": "image_url", "image_url": {"url": still}},
                       {"type": "image_url", "image_url": {"url": photo}}]},
                   {"role": "user", "content": "now a poem"}]
        result = await request(self.gateway, "/v1/chat/completions", {**self.chat, "messages": history})
        self.assertEqual(200, result[0]["status"])
        parts = self.seen[-1][1]["messages"][1]["content"]
        self.assertEqual({"type": "text", "text": boundary.VIDEO_NOTE}, parts[1])
        self.assertEqual([still, photo], [p["image_url"]["url"] for p in parts[2:]])
        # The chat body bound is still enforced.
        huge = {**self.chat, "messages": [{"role": "user", "content": "x" * (boundary.MAX_CHAT_BODY + 1)}]}
        self.assertEqual(413, (await request(self.gateway, "/v1/chat/completions", huge))[0]["status"])

    async def test_selected_profile_fields_reach_backend_unchanged(self):
        # Open WebUI 0.12.5 sends max_tokens and copies DEFAULT_MODEL_PARAMS to
        # the top level; this is that request shape for the selected profile.
        body = {**self.chat, "stream": True, "stop": ["</s>"], "max_tokens": 2048, "temperature": 0.0,
                "top_k": 1, "top_p": 1.0, "reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": True},
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]}
        result = await request(self.gateway, "/v1/chat/completions", body)
        self.assertEqual(200, result[0]["status"])
        self.assertEqual(body, self.seen[-1][1])  # nothing dropped, renamed or added

    async def test_reasoning_and_template_fields_are_narrowly_validated(self):
        for change, code in (({"reasoning_effort": "max"}, "invalid_reasoning_effort"),
                             ({"reasoning_effort": ["low"]}, "invalid_reasoning_effort"),
                             ({"chat_template_kwargs": {"enable_thinking": "true"}}, "invalid_chat_template_kwargs"),
                             ({"chat_template_kwargs": {"preserve_thinking": False}}, "invalid_chat_template_kwargs"),
                             ({"chat_template_kwargs": {"chat_template": "{{ x }}"}}, "invalid_chat_template_kwargs"),
                             ({"chat_template_kwargs": []}, "invalid_chat_template_kwargs"),
                             ({"min_p": 0.1}, "unsupported_request_field"),
                             ({"repetition_penalty": 1.1}, "unsupported_request_field"),
                             ({"separate_reasoning": False}, "unsupported_request_field")):
            with self.subTest(change=change):
                result = await request(self.gateway, "/v1/chat/completions", {**self.chat, **change})
                self.assertEqual(400, result[0]["status"])
                self.assertEqual(code, json.loads(result[-1]["body"])["error"]["code"])
        self.assertEqual([], self.seen)
        for effort, sent in (("low", "low"), ("medium", "medium"), ("high", "xhigh"), ("xhigh", "xhigh")):
            result = await request(self.gateway, "/v1/chat/completions", {**self.chat, "reasoning_effort": effort})
            self.assertEqual(200, result[0]["status"])
            # "high" is accepted and sent as the template's xhigh level.
            self.assertEqual(sent, self.seen[-1][1]["reasoning_effort"])
        # The contract's default effort fills only requests that name none; a request's own wins.
        with patch.object(boundary, "DEFAULT_REASONING_EFFORT", "low"):
            await request(self.gateway, "/v1/chat/completions", dict(self.chat))
            self.assertEqual("low", self.seen[-1][1]["reasoning_effort"])
            await request(self.gateway, "/v1/chat/completions", {**self.chat, "reasoning_effort": "high"})
            self.assertEqual("xhigh", self.seen[-1][1]["reasoning_effort"])
        with patch.object(boundary, "DEFAULT_REASONING_EFFORT", None):
            await request(self.gateway, "/v1/chat/completions", dict(self.chat))
            self.assertNotIn("reasoning_effort", self.seen[-1][1])
        # "none" (an IDE's "Thinking off") becomes the Fast request: thinking off, no effort
        # sent unless the contract has a default, and the normal output cap.
        bare_none = {k: v for k, v in self.chat.items() if k not in ("max_tokens", "max_completion_tokens")}
        with patch.object(boundary, "DEFAULT_REASONING_EFFORT", None):
            result = await request(self.gateway, "/v1/chat/completions", {**bare_none, "reasoning_effort": "none"})
            self.assertEqual(200, result[0]["status"])
            sent = self.seen[-1][1]
            self.assertEqual({"enable_thinking": False}, sent["chat_template_kwargs"])
            self.assertNotIn("reasoning_effort", sent)
            self.assertEqual(boundary.CHAT_MODELS[self.chat["model"]][1], sent["max_tokens"])
        await request(self.gateway, "/v1/chat/completions",
                      {**self.chat, "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual({"enable_thinking": False}, self.seen[-1][1]["chat_template_kwargs"])
        count = len(self.seen)
        for change in ({"chat_template_kwargs": {"enable_thinking": True}}, {"chat_template_kwargs": []}):
            result = await request(self.gateway, "/v1/chat/completions", {**self.chat, "reasoning_effort": "none", **change})
            self.assertEqual("invalid_reasoning_effort", json.loads(result[-1]["body"])["error"]["code"])
        with patch.dict(boundary.TEMPLATE_KWARGS, clear=True):
            result = await request(self.gateway, "/v1/chat/completions", {**self.chat, "reasoning_effort": "none"})
            self.assertEqual("invalid_reasoning_effort", json.loads(result[-1]["body"])["error"]["code"])
        self.assertEqual(count, len(self.seen))
        # Think (effort xhigh) without a cap runs to the context limit: no max_tokens is filled
        # in, so a long conversation cannot fail input + max_tokens > context. Anything else, or
        # Think with its own cap, keeps the gateway cap.
        bare = {k: v for k, v in self.chat.items() if k not in ("max_tokens", "max_completion_tokens")}
        await request(self.gateway, "/v1/chat/completions", {**bare, "reasoning_effort": "high"})
        self.assertEqual("xhigh", self.seen[-1][1]["reasoning_effort"])
        self.assertNotIn("max_tokens", self.seen[-1][1])
        await request(self.gateway, "/v1/chat/completions", {**bare, "reasoning_effort": "low"})
        self.assertEqual(boundary.CHAT_MODELS[self.chat["model"]][1], self.seen[-1][1]["max_tokens"])
        await request(self.gateway, "/v1/chat/completions", {**bare, "reasoning_effort": "xhigh", "max_tokens": 64})
        self.assertEqual(64, self.seen[-1][1]["max_tokens"])
        too_many = boundary.CHAT_MODELS[self.chat["model"]][1] + 1
        result = await request(self.gateway, "/v1/chat/completions", {**bare, "reasoning_effort": "xhigh", "max_tokens": too_many})
        self.assertEqual("output_limit_exceeded", json.loads(result[-1]["body"])["error"]["code"])

    async def test_candidate_alias_tool_roundtrip_with_reasoning_and_later_turn(self):
        """Fixture backend only: proves the boundary path, not native parsing or model behaviour."""
        contract = {"client_alias": "Qwen3.8-27B-MXFP4", "served_model": "fp8-serving-screen",
                    "context_length": 131040, "max_total_tokens": 262144, "max_running_requests": 2,
                    "request_defaults": {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "low",
                                         "temperature": 0.0, "top_k": 1, "top_p": 1.0}}
        rendered = service.candidate_render(contract, 4096, 180, 16)
        tool = {"type": "function", "function": {"name": "search_web", "parameters": {
            "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
        frames = [{"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Need a search."}}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call-7", "type": "function",
                                "function": {"name": "search_web", "arguments": '{"que'}}]}}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ry":"cache"}'}}]}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
        stream = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
        answers = [{"role": "assistant", "reasoning_content": "Cite it.", "content": "17 entries (https://example.com/cache)"},
                   {"role": "assistant", "reasoning_content": "Prior answer applies.", "content": "Still 17."}]
        seen = []
        async def backend(scope, receive, send):
            seen.append(json.loads((await receive())["body"]))
            if len(seen) == 1:
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
                await send({"type": "http.response.body", "body": stream.encode(), "more_body": False})
            else:
                await boundary.reply(send, 200, {"choices": [{"index": 0, "message": answers[len(seen) - 2], "finish_reason": "stop"}]})
        with patch.dict(boundary.CHAT_MODELS, {alias: tuple(limits) for alias, limits in rendered["gateway"]["chat_models"].items()}):
            self.gateway.app = backend
            self.gateway.state = {"phase": "ready", "model": "Qwen3.8-27B-MXFP4", "images": False}
            base = {**rendered["request"], "stream": True, "tools": [tool],
                    "messages": [{"role": "user", "content": "What is the cache capacity?"}]}
            first = await request(self.gateway, "/v1/chat/completions", base)
            body = b"".join(m.get("body", b"") for m in first).decode()
            self.assertTrue(body.endswith("data: [DONE]\n\n"))
            call = {"id": "call-7", "type": "function", "function": {"name": "search_web", "arguments": '{"query":"cache"}'}}
            history = base["messages"] + [{"role": "assistant", "content": "", "tool_calls": [call]},
                                          {"role": "tool", "tool_call_id": "call-7", "content": '{"capacity":17}'}]
            second = await request(self.gateway, "/v1/chat/completions", {**base, "stream": False, "messages": history})
            final = json.loads(second[-1]["body"])["choices"][0]
            self.assertEqual(("stop", "17 entries (https://example.com/cache)", "Cite it."),
                             (final["finish_reason"], final["message"]["content"], final["message"]["reasoning_content"]))
            later = history + [{"role": "assistant", "content": final["message"]["content"]},
                               {"role": "user", "content": "And now?"}]
            third = await request(self.gateway, "/v1/chat/completions", {**base, "stream": False, "messages": later})
            self.assertEqual(200, third[0]["status"])
        self.assertEqual(3, len(seen))
        for sent in seen:
            self.assertEqual(("low", {"enable_thinking": True}, 0.0, 1, 4096),
                             (sent["reasoning_effort"], sent["chat_template_kwargs"], sent["temperature"], sent["top_k"], sent["max_tokens"]))
        self.assertEqual(history, seen[1]["messages"])
        self.assertEqual(later, seen[2]["messages"])
        self.assertFalse(self.gateway.active)

CLI = ["--model-path", "/mnt/models", "--served-model-name", "fp8-serving-screen", "--revision", "b" * 40,
       "--host", "127.0.0.1", "--port", "18088",
       "--context-length", "131040", "--max-total-tokens", "262144", "--chunked-prefill-size", "2048",
       "--max-running-requests", "2", "--tp-size", "2", "--quantization", "quark", "--kv-cache-dtype", "bf16",
       "--speculative-algorithm", "DFLASH", "--speculative-draft-model-path", "/mnt/draft-model",
       "--speculative-draft-model-revision", "c" * 40, "--speculative-num-draft-tokens", "8",
       "--speculative-dflash-block-size", "8", "--speculative-draft-kv-cache-dtype", "bf16",
       "--mamba-max-states-per-path", "3", "--api-key", "synthetic-value-must-not-leave-input"]
RESOLVED = {"context_length": 131040, "max_total_tokens": 262144, "max_running_requests": 2, "tp_size": 2,
            "chunked_prefill_size": 2048, "kv_cache_dtype": "bf16", "speculative_draft_kv_cache_dtype": "bf16",
            "model_config_sha256": "d" * 64, "prefix_cache": {"mamba_max_states_per_path": 3}}


class CandidatePlanTests(unittest.TestCase):
    """Fixture pointers in the selected-pointer shape; the real import is checked separately."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.pointer = self.root / "pointer.json"
        package = {key: "a" * 64 for key in ("package_sha256", "archive_sha256", "manifest_sha256",
                                              "source_manifest_sha256", "worker_source_manifest_sha256")}
        self.record = {"schema": 1, "candidate_selected": True, "deployment": False,
            "candidate": "Private Qwen3.8-27B MXFP4 experimental candidate",
            "candidate_package_base": {**package,
                "selected_arm": {"logical": "mtp", "physical": "kv4", "start": 2, "operation": 234}},
            "rollback": {"logical_arm": "mtp", "physical_arm": "bf16", "start": 1, "operation": 232,
                         "launch_authorized": False},
            "selected_runtime": {"logical_arm": "mtp", "physical_arm": "kv4", "start": 2, "operation": 234,
                "dflash_width": 8, "canonical_server_cli": list(CLI), "resolved_contract": json.loads(json.dumps(RESOLVED)),
                "runtime_identity": {"sha256": "e" * 64}, "http_binding": {"sha256": "f" * 64},
                "tested_deviations": ["mamba_max_states_per_path=3 (cap3)"]}}
        self.selection = {"operation": 234, "selected_arm": {"logical": "mtp", "physical": "kv4", "start": 2},
            "previous_pointer_sha256": "1" * 64, "superseded_pointer_sha256": ["1" * 64, "2" * 64],
            "package": package, "canonical_server_cli": list(CLI), "resolved_contract": json.loads(json.dumps(RESOLVED)),
            "rollback": {"operation": 232, "logical": "mtp", "physical": "bf16", "start": 1, "launch_authorized": False},
            "limits_and_deviations": ["fixture"], "measured_request_profile": {"temperature": 0.7}}

    def save(self):
        self.pointer.write_text(json.dumps(self.record))
        digest = service.sha(self.pointer)
        self.selection["pointer_sha256"] = digest
        return digest

    def contract(self, digest, **kwargs):
        return service.candidate_contract(self.pointer, digest, selection=self.selection, **kwargs)

    def test_contract_binds_selection_and_exports_only_allowlisted_metadata(self):
        contract = self.contract(self.save())
        self.assertEqual(234, contract["operation"])
        self.assertEqual({"logical": "mtp", "physical": "kv4", "start": 2}, contract["selected_arm"])
        self.assertEqual(232, contract["rollback"]["operation"])
        self.assertEqual("Qwen3.8-27B-MXFP4", contract["client_alias"])
        self.assertEqual("fp8-serving-screen", contract["served_model"])
        self.assertEqual("mxfp4", contract["weight_format"])
        self.assertEqual({"algorithm": "DFLASH", "width": 8}, contract["speculative"])
        self.assertEqual((131040, 262144, 2), (contract["context_length"], contract["max_total_tokens"],
                                               contract["max_running_requests"]))
        self.assertIsNone(contract["reasoning_parser"])
        self.assertFalse(contract["runtime_observed"])
        self.assertFalse(contract["installed"])
        # Without the package root, identity is missing evidence, not assumed.
        self.assertIsNone(contract["identity"])
        self.assertIsNone(contract["request_defaults"])
        self.assertTrue(contract["missing_evidence"])
        text = json.dumps(contract)
        for secret in ("synthetic-value-must-not-leave-input", "--api-key", "/mnt/", "127.0.0.1"):
            self.assertNotIn(secret, text)

    def test_stale_mixed_arm_and_contract_drift_are_refused_with_actionable_errors(self):
        cases = {
            "stale": (lambda: self.selection.update(superseded_pointer_sha256=[None]), "stale candidate selection"),
            "unselected-current": (lambda: None, "not the selection named"),
            "selected": (lambda: self.record.update(candidate_selected=False), "explicitly selected"),
            "deployed": (lambda: self.record.update(deployment=True), "claims deployment"),
            "arm": (lambda: self.record["selected_runtime"].update(physical_arm="bf16"), "mixed arms"),
            "operation": (lambda: self.record["candidate_package_base"]["selected_arm"].update(operation=232), "mixed arms"),
            "context": (lambda: (self.record["selected_runtime"]["resolved_contract"].update(context_length=32768),
                                 self.selection["resolved_contract"].update(context_length=32768)), "context_length"),
            "cli-vs-finalization": (lambda: self.record["selected_runtime"]["canonical_server_cli"].append("--x"),
                                    "differs from the finalization"),
            "duplicate": (lambda: [r["canonical_server_cli"].extend(["--context-length", "32768"])
                                   for r in (self.record["selected_runtime"], self.selection)], "duplicate"),
            "rollback": (lambda: self.record["rollback"].update(operation=226), "rollback differs"),
            "package": (lambda: self.record["candidate_package_base"].update(package_sha256="9" * 64), "package differs"),
            "draft-kv": (lambda: [r["canonical_server_cli"].__setitem__(r["canonical_server_cli"].index("--speculative-draft-kv-cache-dtype") + 1, "fp8")
                                  for r in (self.record["selected_runtime"], self.selection)], "BF16 target and draft"),
            "symlink": (lambda: None, "bounded regular"),
        }
        original_record, original_selection = json.dumps(self.record), json.dumps(self.selection)
        for name, (mutate, message) in cases.items():
            with self.subTest(case=name):
                self.record, self.selection = json.loads(original_record), json.loads(original_selection)
                mutate()
                digest = self.save()
                target = self.pointer
                if name == "stale":
                    self.selection["superseded_pointer_sha256"] = [digest]
                if name == "unselected-current":
                    self.selection["pointer_sha256"] = "3" * 64
                if name == "symlink":
                    target = self.root / "link.json"
                    target.unlink(missing_ok=True)
                    target.symlink_to(self.pointer)
                with self.assertRaisesRegex(ValueError, message):
                    service.candidate_contract(target, digest, selection=self.selection)

    def test_package_evidence_is_authenticated_through_the_manifest(self):
        import base64, zlib
        package = self.root / "package"
        (package / "review").mkdir(parents=True)
        body = {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "low", "temperature": 0.0,
                "top_k": 1, "top_p": 1.0, "max_completion_tokens": 4096, "messages": [{"role": "user", "content": "private"}]}
        binding = zlib.compress(json.dumps({"rows": [{"body": {**body, "seed": 1}}, {"body": {**body, "seed": 2}}]}).encode())
        members = {
            "review/MODEL-INVENTORY.json": json.dumps([{"name": "config.json", "sha256": "d" * 64},
                {"name": "tokenizer_config.json", "sha256": "4" * 64}, {"name": "tokenizer.json", "sha256": "5" * 64}]).encode(),
            "review/DRAFT-MODEL.json": json.dumps({"config": {"architectures": ["DFlash2DraftModel"],
                "dflash_config": {"block_size": 8}, "dtype": "bfloat16"}}).encode(),
            "review/COMMON-STACK-RECEIPT.json": json.dumps({"target": {"sglang": "0.5.21", "source_revision": "6" * 40},
                "components": {"sdk": {"version": "10.1.0"}}, "image_manifest": "7" * 64}).encode(),
            "review/RESOURCE-MANIFEST.json": json.dumps({"workload_argv_sha256": {"kv4": "8" * 64}, "workload_argv": {
                "kv4": ["--setenv", "LLVM_PATH", "/fixture/llvm", "--setenv", "SECRET_TOKEN", "never-exported"]}}).encode(),
            "review/FINAL-REQUESTS.zlib": zlib.compress(json.dumps({"CLIENT-BINDING.zlib": base64.b64encode(binding).decode()}).encode()),
        }
        for name, raw in members.items():
            (package / name).write_bytes(raw)
        manifest = json.dumps({"launch_authorized": False, "runtime_files": {"a": "b"},
                               "package_files": {name: service.hashlib.sha256(raw).hexdigest() for name, raw in members.items()}}).encode()
        (package / "MANIFEST.json").write_bytes(manifest)
        self.record["candidate_package_base"]["manifest_sha256"] = service.hashlib.sha256(manifest).hexdigest()
        self.selection["package"]["manifest_sha256"] = self.record["candidate_package_base"]["manifest_sha256"]
        contract = self.contract(self.save(), package_root=package)
        self.assertEqual([], contract["missing_evidence"])
        self.assertEqual("tokenizer_config.json", contract["identity"]["chat_template_source"])
        self.assertEqual("4" * 64, contract["identity"]["model_files"]["tokenizer_config.json"])
        self.assertEqual({"LLVM_PATH": "/fixture/llvm"}, contract["identity"]["runtime_environment"])
        self.assertEqual("varies", contract["package_request_profile"]["seed"])
        self.assertEqual({"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "low",
                          "temperature": 0.0, "top_k": 1, "top_p": 1.0}, contract["request_defaults"])
        self.assertEqual({"temperature": 0.7}, contract["selection_measured_profile"])
        self.assertNotIn("never-exported", json.dumps(contract))
        self.assertNotIn("private", json.dumps(contract))
        (package / "review/MODEL-INVENTORY.json").write_bytes(b"[]")
        with self.assertRaisesRegex(ValueError, "package member changed"):
            self.contract(service.sha(self.pointer), package_root=package)
        (package / "MANIFEST.json").write_bytes(manifest + b" ")
        with self.assertRaisesRegex(ValueError, "does not match the selected pointer"):
            self.contract(service.sha(self.pointer), package_root=package)

    def test_render_projects_one_contract_consistently_into_every_layer(self):
        contract = dict(self.contract(self.save()), request_defaults={"chat_template_kwargs": {"enable_thinking": True},
            "reasoning_effort": "low", "temperature": 0.0, "top_k": 1, "top_p": 1.0})
        rendered = service.candidate_render(contract, 8192, 180, 16)
        self.assertTrue(all(check["matches"] for check in rendered["checks"]), rendered["checks"])
        self.assertEqual("openai/fp8-serving-screen", rendered["route"]["litellm_params"]["model"])
        self.assertEqual(131040 - 8192 - 16, rendered["route"]["model_info"]["max_input_tokens"])
        self.assertEqual({"Qwen3.8-27B-MXFP4": [131040, 8192]}, rendered["gateway"]["chat_models"])
        params = json.loads(rendered["webui"]["DEFAULT_MODEL_PARAMS"])
        self.assertEqual({"chat_template_kwargs": {"enable_thinking": True}}, params["custom_params"])
        self.assertNotIn("max_tokens", params)  # shared with the 9B profile; the gateway applies its cap
        self.assertEqual("low", rendered["client"]["AGENT_REASONING_EFFORT"])
        # Admission four and native running two are separate, documented limits.
        self.assertEqual((4, 2, 2), (rendered["capacity"]["gateway_admission"], rendered["capacity"]["native_running"],
                                     rendered["capacity"]["queued_at_native_when_full"]))
        without = service.candidate_render(dict(contract, request_defaults=None), 4096, 180, 16)
        self.assertFalse(next(c for c in without["checks"] if c["id"] == "request-defaults")["matches"])
        for budget, deadline in ((127, 180), (65521, 180), (True, 180), (4096, 10)):
            with self.assertRaises(ValueError):
                service.candidate_render(contract, budget, deadline, 16)

    def test_plan_records_mismatches_and_keeps_candidate_inactive(self):
        expected = self.save()
        before = service.sha(ROOT / "apps/base/shared-api/boundary.py")
        output = self.root / "plan"
        with patch("stack_inventory.serving_project", return_value={"selection": self.selection}), \
                self.assertRaisesRegex(ValueError, "unreviewed launch flag.*--api-key"):
            service.candidate_plan(SimpleNamespace(pointer=self.pointer, pointer_sha256=expected, output=output,
                                                   output_tokens=4096, package_root=None))
        self.assertFalse(output.exists())
        for record in (self.record["selected_runtime"], self.selection):
            del record["canonical_server_cli"][-2:]
        expected = self.save()
        with patch("stack_inventory.serving_project", return_value={"selection": self.selection}):
            service.candidate_plan(SimpleNamespace(pointer=self.pointer, pointer_sha256=expected, output=output,
                                                   output_tokens=4096, package_root=None))
        report = json.loads((output / "integration-plan.json").read_text())
        self.assertEqual("blocked-missing-evidence", report["status"])
        self.assertFalse(report["deployment_ready"])
        self.assertFalse(report["evidence_levels"]["runtime_observed"])
        self.assertFalse(report["output_cap_qualified"])
        checks = {check["id"]: check for check in report["checks"]}
        self.assertTrue(checks["gateway-request-fields"]["matches"])
        self.assertTrue(all(not checks[name]["matches"] for name in ("upstream-model", "gateway-context", "router-context")))
        deployment = yaml.safe_load((output / "candidate-sglang.yaml").read_text())
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(0, deployment["spec"]["replicas"])
        self.assertIn("UNBUILT", container["image"])
        self.assertEqual([{"name": "gpu-0", "resourceClaimName": "gpu-0"}, {"name": "gpu-1", "resourceClaimName": "gpu-1"}],
                         deployment["spec"]["template"]["spec"]["resourceClaims"])
        self.assertEqual([{"name": "gpu-0"}, {"name": "gpu-1"}], container["resources"]["claims"])
        self.assertNotIn("amd.com/gpu", container["resources"]["limits"])
        self.assertEqual(["--reasoning-parser", "qwen3", "--tool-call-parser", "qwen3_coder"], container["args"][-4:])
        runtime = json.loads((output / "candidate-runtime.json").read_text())
        self.assertTrue(any("startup numerical probes" in item for item in runtime["blockers"]))
        for filename, digest in report["files"].items():
            self.assertEqual(digest, service.sha(output / filename))
        for path in output.iterdir():
            self.assertNotIn("synthetic-value-must-not-leave-input", path.read_text())
        self.assertEqual(before, service.sha(ROOT / "apps/base/shared-api/boundary.py"))
        self.assertEqual(expected, service.sha(self.pointer))

    def test_imported_selection_refuses_superseded_real_pointer_digests(self):
        selection = __import__("stack_inventory").serving_project(ROOT)["selection"]
        for stale in selection["superseded_pointer_sha256"]:
            self.pointer.write_text("{}")
            with self.assertRaisesRegex(ValueError, "digest differs"):
                service.candidate_contract(self.pointer, stale)
        self.assertNotIn(selection["pointer_sha256"], selection["superseded_pointer_sha256"])

    @requires_input(os.environ.get("CANDIDATE_POINTER") and os.environ.get("CANDIDATE_PACKAGE_ROOT"),
                    "set CANDIDATE_POINTER, CANDIDATE_POINTER_SHA256 and CANDIDATE_PACKAGE_ROOT for private inputs")
    def test_private_selected_pointer_and_package(self):
        contract = service.candidate_contract(os.environ["CANDIDATE_POINTER"], os.environ["CANDIDATE_POINTER_SHA256"],
                                              os.environ["CANDIDATE_PACKAGE_ROOT"])
        self.assertEqual([], contract["missing_evidence"])
        self.assertTrue(all(c["matches"] for c in service.candidate_render(contract, 4096, 180, 16)["checks"]))

    def test_quality_menu_requires_actual_tokenizer_and_declares_unrun_cases(self):
        menu = json.loads((ROOT / "infrastructure/sglang/assistant-quality.json").read_text())
        self.assertEqual("prepared-not-run", menu["status"])
        self.assertEqual(3, menu["execution"]["repetitions"])
        cases = {case["id"]: case for case in menu["cases"]}
        self.assertEqual(len(cases), len(menu["cases"]))
        self.assertEqual([16384, 65536, 120000], cases["long-context-retrieval"]["target_input_tokens"])
        self.assertEqual("tool_calls", cases["search-tool"]["grading"]["require_finish_reason"])
        self.assertEqual("answer-and-citation", cases["tool-result-followup"]["grading"]["kind"])
        self.assertEqual("both", cases["untrusted-page"]["mode"])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.capture = self.root / "capture"
        self.capture.mkdir()
        with sqlite3.connect(self.capture / "state.db") as database:
            database.execute("CREATE TABLE fixture (id INTEGER)")
            database.execute("INSERT INTO fixture VALUES (7)")
        for name in ("server-token", "server.tar", "config.tar", "litellm.dump", "roles.sql",
                     "webui-rag-qwen3-data.tar", "creative-models.tar", "service-tools-state.tar",
                     "models.json", "rag-SHA256SUMS"):
            (self.capture / name).write_text("synthetic fixture; no real credentials")
        record = {"schema_version": 1, "status": "consistent-capture", "database": "k3s-sqlite",
                  "claims": sorted(service_backup.CLAIMS),
                  "files": {p.name: service_backup.digest(p) for p in self.capture.iterdir()}}
        (self.capture / "manifest.json").write_text(json.dumps(record))

    def test_isolated_staging_checks_database_and_token_pair(self):
        output = self.root / "restore"
        service_backup.restore_stage(self.capture, output)
        with sqlite3.connect(output / "state.db") as db:
            self.assertEqual((7,), db.execute("SELECT id FROM fixture").fetchone())
        with self.assertRaises(ValueError):
            service_backup.restore_stage(self.capture, output)
        (self.capture / "server-token").write_text("different synthetic token")
        with self.assertRaisesRegex(ValueError, "pairing"):
            service_backup.validate_capture(self.capture)

    def test_active_targets_and_incomplete_capture_refused(self):
        for path in ("/etc/restore-test", "/var/lib/rancher/k3s/restore", "/var/lib/kubelet/restore", "/var"):
            with self.assertRaises(ValueError):
                service_backup.restore_stage(self.capture, path)
        (self.capture / "manifest.json").write_text('{"status":"partial"}')
        with self.assertRaises(ValueError):
            service_backup.validate_capture(self.capture)

    def test_failed_scale_preserves_original_identity_before_mutation(self):
        capture = object.__new__(service_backup.Capture)
        capture.kube = ["fixture-kubectl"]
        capture.get = lambda *args: {"metadata": {"uid": "original", "resourceVersion": "7"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "webui"}}}}
        def failed_run(argv, **kwargs):
            journal = json.loads((self.root / "journal.json").read_text())
            self.assertEqual({"uid": "original", "replicas": 1}, journal["deployments"]["open-webui"])
            self.assertIn("--resource-version=7", argv)
            raise ValueError("fixture mutation failed")
        with patch.object(service_backup, "run", failed_run), self.assertRaisesRegex(ValueError, "fixture"):
            capture.scale_zero("open-webui", {}, self.root)

    def test_quiescence_orders_writers_dump_database_then_cri(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = False
        capture.kube = ["fixture-kubectl"]
        events = []
        capture.get = lambda *args: {"items": [{"metadata": {"name": "pg"}, "status": {"phase": "Running"}}]}
        capture.scale_zero = lambda name, *args: events.append(name)
        capture.stopped = lambda: events.append("all-pods-and-cri-stopped")
        def dump(argv, output, **kwargs):
            events.append(argv[argv.index("--") + 1])
            output.write(b"synthetic database dump")
        with patch.object(service_backup, "run", dump):
            capture.quiesce_apps(self.root, {})
        self.assertEqual(["open-webui", "service-tools", "litellm", "pg_dump", "pg_dumpall",
                          "gateway-postgres", "all-pods-and-cri-stopped"], events)

    def test_failed_database_dump_never_claims_quiescence(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = False
        capture.kube = ["fixture-kubectl"]
        events = []
        capture.get = lambda *args: {"items": [{"metadata": {"name": "pg"}, "status": {"phase": "Running"}}]}
        capture.scale_zero = lambda name, *args: events.append(name)
        capture.stopped = lambda: self.fail("capture must not continue after dump failure")
        with patch.object(service_backup, "run", side_effect=ValueError("dump failed")), self.assertRaises(ValueError):
            capture.quiesce_apps(self.root, {})
        self.assertNotIn("gateway-postgres", events)

    def test_baseline_cannot_omit_existing_gateway_database(self):
        capture = object.__new__(service_backup.Capture)
        capture.baseline = True
        capture.get = lambda *args: {"items": [{"metadata": {"name": "gateway-postgres"}}]}
        with self.assertRaisesRegex(ValueError, "pre-service"):
            capture.quiesce_apps(self.root, {})

    def test_model_inventory_preserves_hashes_and_rejects_changed_revision(self):
        model = self.root / "model"
        model.mkdir()
        (model / "model.safetensors").write_bytes(b"synthetic weights")
        path = self.root / "model-inventory.json"
        service_backup.inventory_model("Qwen3.5-9B", model, path)
        record = service_backup.model_inventory(path)
        self.assertEqual(service_backup.digest(model / "model.safetensors"),
                         record["files"]["model.safetensors"]["sha256"])
        record["revision"] = "incorrect-revision"
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            service_backup.model_inventory(path)


class ResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.large = service.kustomize(ROOT / "apps/overlays/service")
        cls.split = service.kustomize(ROOT / "apps/overlays/service-split")
        cls.telemetry = service.kustomize(ROOT / "infrastructure/observability")

    def test_observed_128_firmware_reservations_and_over_budget(self):
        resources = {"schema_version": 1, "status": "offline-plan-not-applied", "allocatable_logical_cpus": 42,
                     "gpu_count": 2, "memory": {"total_mib": 130900, "allocatable_mib": 112468, "dimms": None}}
        result = service.capacity(resources, self.large, {"reserve_mib": 6144}, self.telemetry, "large")
        self.assertTrue(result["fits"])
        self.assertEqual(130900, result["observed_total_mib"])
        self.assertIsNone(result["observed_dimms"])
        self.assertFalse(service.capacity(resources, self.large, {"reserve_mib": 6144}, self.telemetry,
                                         "large", vm_mib=100000)["fits"])
        # SwarmUI at its measured 4 CPUs (2026-11-11): the 9B split mode now fits 42 CPUs ...
        split = service.capacity(resources, self.split, {"reserve_mib": 6144}, self.telemetry, "split")
        self.assertTrue(split["fits"])
        self.assertGreaterEqual(split["remaining_cpus"], 0)
        # ... and the same plan on a smaller node is still reported over budget.
        smaller = {**resources, "allocatable_logical_cpus": 36}
        over = service.capacity(smaller, self.split, {"reserve_mib": 6144}, self.telemetry, "split")
        self.assertFalse(over["fits"])
        self.assertLess(over["remaining_cpus"], 0)

    def test_private_ingress_has_no_native_admin_route(self):
        ingress = next(o for o in self.large if o["kind"] == "Ingress" and o["metadata"]["name"] == "shared-model-api")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertTrue(all(p["pathType"] == "Exact" for p in paths))
        self.assertFalse(any(p["path"].startswith(("/key", "/user", "/webui", "/_service/control")) for p in paths))

    def test_rendered_policy_union_allows_required_flows_and_denies_lateral_access(self):
        # Evaluate both directions and the union of ALL rendered policies. This
        # catches an inherited broad allow even when the new policy is narrow.
        policies = [o for o in self.large if o["kind"] == "NetworkPolicy"]
        def pod(name, namespace="ai-home-lab"):
            return namespace, {"app.kubernetes.io/name": name}
        def matches(selector, labels):
            self.assertFalse(selector.get("matchExpressions"), "extend evaluator for selector expressions")
            return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())
        def peer_matches(peer, other, namespace):
            if "ipBlock" in peer:
                self.fail("address policy needs explicit pod address evidence")
            ns, labels = other
            ns_match = (matches(peer["namespaceSelector"], {"kubernetes.io/metadata.name": ns})
                        if "namespaceSelector" in peer else ns == namespace)
            return ns_match and matches(peer.get("podSelector", {}), labels)
        def direction_allowed(subject, other, port, direction):
            namespace, labels = subject
            selected = [p for p in policies if p["metadata"]["namespace"] == namespace and
                        direction.capitalize() in p["spec"]["policyTypes"] and
                        matches(p["spec"]["podSelector"], labels)]
            if not selected:
                return True
            peer_field = "from" if direction == "ingress" else "to"
            return any((not rule.get("ports") or any(p.get("protocol", "TCP") == "TCP" and
                       p["port"] == port for p in rule["ports"])) and
                       (peer_field not in rule or any(peer_matches(p, other, namespace) for p in rule[peer_field]))
                       for policy in selected for rule in policy["spec"].get(direction, []))
        def allowed(source, target, port):
            return direction_allowed(source, target, port, "egress") and direction_allowed(target, source, port, "ingress")
        for source, target, port in (("open-webui", "litellm", 4000), ("litellm", "sglang", 30000),
            ("litellm", "gateway-postgres", 5432), ("service-tools", "open-webui", 8080),
            ("open-webui", "service-tools", 8000), ("service-tools", "swarmui", 7801),
            ("swarmui", "comfyui-gpu0", 7821),
            # generate_video (video.py) submits the fixed Wan workflow to the second ComfyUI
            ("service-tools", "comfyui-gpu0", 7821)):
            self.assertTrue(allowed(pod(source), pod(target), port), (source, target))
        for target, port in (("open-webui", 8080), ("litellm", 4000), ("service-tools", 8000)):
            self.assertTrue(allowed(pod("traefik", "kube-system"), pod(target), port))
        for source in (pod("unrelated"), pod("unrelated", "unrelated-namespace")):
            for target, port in (("litellm", 4000), ("gateway-postgres", 5432), ("sglang", 30000),
                                 ("open-webui", 8080), ("service-tools", 8000), ("swarmui", 7801)):
                self.assertFalse(allowed(source, pod(target), port), (source, target))
        self.assertFalse(allowed(pod("open-webui"), pod("sglang"), 30000))
        for source in ("open-webui", "litellm", "sglang"):
            self.assertFalse(allowed(pod(source), pod("comfyui-gpu0"), 7821), source)
        self.assertFalse(allowed(pod("swarmui"), pod("sglang"), 30000))
        self.assertFalse(allowed(pod("comfyui-gpu0"), pod("swarmui"), 7801))
        self.assertFalse(allowed(pod("service-tools"), pod("gateway-postgres"), 5432))

    def test_site_renderer_keeps_secrets_out_and_refuses_public_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = {"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
                "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
                "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}
            path = root / "site.json"
            # Older private site files still carry the retired lifecycle label.
            path.write_text(json.dumps(dict(site, environment="prd")))
            self.assertEqual(service.site_values(path)["context"], "fixture")
            path.write_text(json.dumps(site))
            service.render(SimpleNamespace(site=path, output=root / "render", mode="large"))
            objects = service.load_objects((root / "render/workloads.yaml").read_text())
            self.assertTrue(all(o["spec"]["storageClassName"] == "workstation-retain"
                                for o in objects if o["kind"] == "PersistentVolumeClaim"))
            self.assertFalse(any(o["kind"] == "Secret" for o in objects))
            tools = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "service-tools")
            secret_names = {v["secret"]["secretName"] for v in tools["spec"]["template"]["spec"]["volumes"] if "secret" in v}
            self.assertEqual({"gateway-image-permissions"}, secret_names)
            # Standard HTTPS port: no port in client or citation URLs.
            client = dict(line.split("=", 1) for line in (root / "render/client.conf").read_text().splitlines())
            self.assertEqual("https://api.fixture.test/v1", client["AGENT_BASE_URL"])
            tools_env = {e["name"]: e.get("value") for e in tools["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual("https://chat.fixture.test", tools_env["PUBLIC_WEBUI_BASE_URL"])
            # Traefik is reachable only on the site address, TCP 443, with source preservation.
            traefik = yaml.safe_load((root / "render/traefik.yaml").read_text())
            values = yaml.safe_load(traefik["spec"]["valuesContent"])
            self.assertEqual({"type": "ClusterIP", "externalTrafficPolicy": "Local", "externalIPs": ["192.168.77.9"]},
                             values["service"]["spec"])
            self.assertEqual(443, values["ports"]["websecure"]["exposedPort"])
            # A streamed reply is one long write: Traefik must not cut it before the longest
            # gateway deadline (large mode, for Think).
            write = values["ports"]["websecure"]["transport"]["respondingTimeouts"]["writeTimeout"]
            self.assertGreater(int(write.rstrip("s")), max(service.DEADLINE_SECONDS.values()))
            self.assertEqual({"default": False}, values["ports"]["web"]["expose"])
            # Staging issues into its own Secret; production into the Ingress TLS Secret.
            for name, issuer, secret in (("certificate-staging.yaml", "letsencrypt-staging-cloudflare", "fixture-tls-staging"),
                                         ("certificate.yaml", "letsencrypt-production-cloudflare", "fixture-tls")):
                certificate = yaml.safe_load((root / "render" / name).read_text())
                self.assertEqual((issuer, secret, ["chat.fixture.test", "api.fixture.test"]),
                                 (certificate["spec"]["issuerRef"]["name"], certificate["spec"]["secretName"],
                                  certificate["spec"]["dnsNames"]))
            self.assertTrue(all(tls["secretName"] == "fixture-tls" for o in objects if o["kind"] == "Ingress"
                                for tls in o["spec"]["tls"]))
            existing = root / "claims.json"
            existing.write_text(json.dumps({"kind": "List", "items": [{"kind": "PersistentVolumeClaim",
                "metadata": {"name": "webui-rag-qwen3-data", "namespace": "ai-home-lab", "uid": "existing-claim"},
                "spec": {"storageClassName": "local-path"}}]}))
            service.render(SimpleNamespace(site=path, output=root / "existing", mode="large", existing_claims=existing))
            migrated = service.load_objects((root / "existing/workloads.yaml").read_text())
            data = next(o for o in migrated if o["kind"] == "PersistentVolumeClaim" and
                        o["metadata"]["name"] == "webui-rag-qwen3-data")
            self.assertEqual("local-path", data["spec"]["storageClassName"])
            self.assertEqual("existing-claim", json.loads((root / "existing/evidence.json").read_text())[
                "existing_claims"]["webui-rag-qwen3-data"]["uid"])
            site["client_cidrs"] = ["0.0.0.0/0"]
            path.write_text(json.dumps(site))
            with self.assertRaises(ValueError):
                service.site_values(path)
            site["client_cidrs"], site["tls_secret"] = ["192.168.77.0/24"], "Fixture/TLS"
            path.write_text(json.dumps(site))
            with self.assertRaises(ValueError):
                service.site_values(path)

    def test_site_default_group_reaches_webui_only_when_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = {"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
                "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
                "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}
            path = root / "site.json"

            def webui_env(output, **extra):
                path.write_text(json.dumps(dict(site, **extra)))
                service.render(SimpleNamespace(site=path, output=root / output, mode="large"))
                objects = service.load_objects((root / output / "workloads.yaml").read_text())
                webui = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "open-webui")
                return {e["name"]: e.get("value") for e in webui["spec"]["template"]["spec"]["containers"][0]["env"]}
            group = "0b7e6f1a-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
            self.assertNotIn("DEFAULT_GROUP_ID", webui_env("absent"))
            self.assertNotIn("DEFAULT_GROUP_ID", webui_env("empty", webui_default_group=""))
            self.assertEqual(group, webui_env("set", webui_default_group=group)["DEFAULT_GROUP_ID"])
            for bad in ("family", "0B7E6F1A-2C3D-4E5F-8A9B-0C1D2E3F4A5B", group + "\n", 7):
                path.write_text(json.dumps(dict(site, webui_default_group=bad)))
                with self.assertRaises(ValueError):
                    service.site_values(path)



class GpuClaimTests(unittest.TestCase):
    """DRA: shared claims count once, so SGLang TP2 plus SwarmUI needs two cards."""

    def deployment(self, claims, legacy=False):
        resources = {"limits": {"cpu": "1", "memory": "1Gi"}, "requests": {"cpu": "1", "memory": "1Gi"}}
        if legacy:
            resources["limits"]["amd.com/gpu"] = "1"
        return {"kind": "Deployment", "spec": {"template": {"spec": {
            "resourceClaims": [{"name": c, "resourceClaimName": c} for c in claims],
            "containers": [{"name": "c", "resources": resources}]}}}}

    def test_shared_claims_are_counted_once_and_legacy_requests_refused(self):
        sglang, swarmui = self.deployment(["gpu-0", "gpu-1"]), self.deployment(["gpu-1"])
        self.assertEqual(["gpu-0", "gpu-1"], service.gpu_claims(sglang))
        self.assertEqual(2, len(set().union(*(service.gpu_claims(d) for d in (sglang, swarmui)))))
        self.assertEqual([], service.gpu_claims(self.deployment(["other-claim"])))
        with self.assertRaisesRegex(ValueError, "amd.com/gpu is retired"):
            service.gpu_claims(self.deployment([], legacy=True))

    def test_set_gpu_claims_replaces_the_device_plugin_resource(self):
        spec = {"containers": [{"resources": {"requests": {"amd.com/gpu": "2"}, "limits": {"amd.com/gpu": "2"}}}]}
        service.set_gpu_claims(spec, spec["containers"][0], 2)
        self.assertEqual(["gpu-0", "gpu-1"], [c["resourceClaimName"] for c in spec["resourceClaims"]])
        self.assertEqual({"requests": {}, "limits": {}, "claims": [{"name": "gpu-0"}, {"name": "gpu-1"}]},
                         spec["containers"][0]["resources"])
        for bad in (0, 3, "2"):
            with self.assertRaises(ValueError):
                service.set_gpu_claims(spec, spec["containers"][0], bad)


class CredentialFileTests(unittest.TestCase):
    def test_env_consumed_service_key_has_no_trailing_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "credentials"
            service.credentials(SimpleNamespace(output=output))
            key = (output / "service-key").read_bytes()
            self.assertFalse(key.endswith(b"\n"))
            self.assertTrue(key and key == key.strip())
            for name in ("mapping.json", "image-users.json"):
                self.assertEqual(key.decode(), json.loads((output / name).read_text())["service_key"])
            self.assertTrue((output / "master-key").read_text().strip().startswith("sk-"))


class KubeYamlTests(unittest.TestCase):
    """Manifests are read as YAML 1.1 by Kubernetes and Prometheus: y/n/yes/no/on/off are booleans there."""

    def test_yaml11_boolean_like_strings_are_quoted(self):
        for value in ("Y", "y", "N", "yes", "No", "on", "OFF", "true", "False", "null", "~"):
            text = service.kube_yaml({"metadata": {"labels": {"ai-stack/arm": value}}})
            self.assertIn(f"'{value}'", text, value)
            self.assertEqual(value, yaml.safe_load(text)["metadata"]["labels"]["ai-stack/arm"])
        self.assertIn("ai-stack/arm: X\n", service.kube_yaml({"ai-stack/arm": "X"}))
        self.assertIn("|", service.kube_yaml({"script": "a\nb"}))


class ServingContractTests(unittest.TestCase):
    """One contract, read from the serving owner's profile, drives every consumer (offline)."""

    PROFILE = ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json"
    IMAGE = "registry.lan.thespry.family/local/sglang-gfx1201@sha256:" + "a" * 64

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = json.loads(self.PROFILE.read_text())

    def write_profile(self, profile):
        path = self.root / "profile.json"
        path.write_text(json.dumps(profile))
        return path

    def test_selected_arm_is_read_from_the_profile_not_fixed(self):
        default = next(name for name, arm in self.profile["arms"].items() if arm.get("default"))
        contract = service.serving_contract(self.PROFILE)
        self.assertEqual((default, "profile-default"), (contract["selection"]["arm"], contract["selection"]["arm_source"]))
        self.assertEqual(f"{self.profile['profile']}.{default}", contract["contract"])
        other = next(name for name in self.profile["arms"] if name != default)
        for name, arm in self.profile["arms"].items():
            arm["default"] = name == other
        moved = service.serving_contract(self.write_profile(self.profile))
        render = service.ai_stack_render()
        self.assertEqual(other, moved["selection"]["arm"])
        self.assertEqual(render.server_argv(self.profile, json.loads((ROOT / "infrastructure/ai-stack/models" /
                         (self.profile["model"] + ".json")).read_text()), other), moved["_render"]["argv"])
        self.assertEqual(render.environment(self.profile, other), moved["_render"]["environment"])

    def test_status_and_missing_inputs_are_explicit(self):
        # Fixture states, independent of the live catalogue/profile promotion.
        default = next(name for name, arm in self.profile["arms"].items() if arm.get("default"))
        unqualified = self.write_profile({k: v for k, v in dict(self.profile, status="unqualified").items()
                                          if k != "qualification"})
        not_promoted = {"candidate": False, "selected": False}
        with patch.dict(service.CATALOGUE_STATES, not_promoted):
            incomplete = service.serving_contract(unqualified)
            self.assertEqual("incomplete", incomplete["status"])
            self.assertIsNone(incomplete["runtime"]["image"])
            self.assertTrue(any(item.startswith("runtime image") for item in incomplete["missing_inputs"]))
            self.assertTrue(any(item.startswith("model promotion") for item in incomplete["missing_inputs"]))
            provisional = service.serving_contract(unqualified, runtime_image=self.IMAGE)
            self.assertEqual("provisional", provisional["status"])
            for wrong in ("localhost/sglang@sha256:" + "a" * 64, "registry.lan.thespry.family/local/sglang:latest"):
                self.assertEqual("incomplete", service.serving_contract(unqualified, runtime_image=wrong)["status"])
        promoted = {"candidate": True, "selected": True}
        record = {"arm": default, "image": self.IMAGE}
        qualified = self.write_profile(dict(self.profile, status="qualified", qualification=record))
        with patch.dict(service.CATALOGUE_STATES, promoted):
            active = service.serving_contract(qualified, runtime_image=self.IMAGE)
            self.assertEqual(("active", []), (active["status"], active["missing_inputs"]))
            # Qualified for one arm on one image: another image or arm is provisional, never active.
            other_image = self.IMAGE[:-1] + "b"
            moved = service.serving_contract(qualified, runtime_image=other_image)
            self.assertEqual("provisional", moved["status"])
            self.assertTrue(any("not --runtime-image" in item for item in moved["missing_inputs"]))
            other = next(name for name in self.profile["arms"] if name != default)
            wrong_arm = self.write_profile(dict(self.profile, status="qualified", qualification=dict(record, arm=other)))
            self.assertEqual("provisional", service.serving_contract(wrong_arm, runtime_image=self.IMAGE)["status"])
            no_record = self.write_profile({k: v for k, v in dict(self.profile, status="qualified").items()
                                            if k != "qualification"})
            self.assertEqual("provisional", service.serving_contract(no_record, runtime_image=self.IMAGE)["status"])
        # A catalogue status the contract does not know (whatever the live value is) is refused.
        with patch.object(service, "CATALOGUE_STATES", {"other": True}), \
                self.assertRaisesRegex(ValueError, "unknown model catalogue status"):
            service.serving_contract(self.PROFILE)

    def test_session_gate_admits_sglang_only_with_an_owner_qualified_contract(self):
        # Run session.sh's own Deployment gate (extracted, so it cannot drift) on the rendered
        # SGLang Deployment: an active contract (owner record for this arm and image) passes,
        # a provisional one is refused before any workload would be stopped.
        import re
        import shutil
        import subprocess
        if shutil.which("jq") is None:
            self.skipTest("jq is required to run the session gate")
        source = (ROOT / "lib/workstation/session.sh").read_text()
        gate = re.search(r"jq -e '(\.metadata\.annotations\[\"workstation\.ai/qualification\"\].*?)' <<<\"\$document\"",
                         source, re.S).group(1)
        default = next(name for name, arm in self.profile["arms"].items() if arm.get("default"))
        records = [{"arm": default, "image": self.IMAGE}]
        path = self.write_profile(dict(self.profile, status="qualified", qualification=records))
        with patch.dict(service.CATALOGUE_STATES, {"candidate": True, "selected": True}):
            for image, status, admitted in ((self.IMAGE, "active", True), (self.IMAGE[:-1] + "b", "provisional", False)):
                contract = service.serving_contract(path, runtime_image=image)
                self.assertEqual(status, contract["status"])
                objects, _, _ = service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant"),
                                                       contract, "large", "assistant")
                sglang = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "sglang")
                marker = sglang["metadata"]["annotations"]["workstation.ai/qualification"]
                self.assertEqual("qualified" if admitted else "pending-serving-contract-provisional", marker)
                result = subprocess.run(["jq", "-e", gate], input=json.dumps(sglang), capture_output=True, text=True)
                self.assertEqual(admitted, result.returncode == 0, (status, result.stderr))

    def test_each_qualified_arm_is_active_on_its_own_image_and_shared_mode_defaults_to_its_arm(self):
        default = next(name for name, arm in self.profile["arms"].items() if arm.get("default"))
        other = next(name for name in self.profile["arms"] if name != default)
        other_image = self.IMAGE[:-1] + "b"
        arms = {name: dict(spec, shared_default=(name == other)) for name, spec in self.profile["arms"].items()}
        records = [{"arm": default, "image": self.IMAGE}, {"arm": other, "image": other_image}]
        path = self.write_profile(dict(self.profile, status="qualified", qualification=records, arms=arms))
        with patch.dict(service.CATALOGUE_STATES, {"candidate": True, "selected": True}):
            large = service.serving_contract(path, runtime_image=self.IMAGE)
            self.assertEqual((default, "profile-default", "active"),
                             (large["selection"]["arm"], large["selection"]["arm_source"], large["status"]))
            shared = service.serving_contract(path, runtime_image=other_image, mode="shared")
            self.assertEqual((other, "profile-shared-default", "active"),
                             (shared["selection"]["arm"], shared["selection"]["arm_source"], shared["status"]))
            # Each record covers its own image only; an explicit arm still wins.
            self.assertEqual("provisional", service.serving_contract(path, runtime_image=self.IMAGE, mode="shared")["status"])
            explicit = service.serving_contract(path, arm=default, runtime_image=self.IMAGE, mode="shared")
            self.assertEqual((default, "explicit", "active"),
                             (explicit["selection"]["arm"], explicit["selection"]["arm_source"], explicit["status"]))
        # Without a shared default, shared mode uses the general default; two are refused.
        unmarked = {name: {k: v for k, v in spec.items() if k != "shared_default"} for name, spec in self.profile["arms"].items()}
        plain = self.write_profile(dict(self.profile, qualification=records, arms=unmarked))
        self.assertEqual(default, service.serving_contract(plain, mode="shared")["selection"]["arm"])
        both = {name: dict(spec, shared_default=True) for name, spec in self.profile["arms"].items()}
        with self.assertRaisesRegex(ValueError, "at most one arm"):
            service.serving_contract(self.write_profile(dict(self.profile, arms=both)), mode="shared")

    def test_measured_host_resources_come_from_the_qualification_record(self):
        measured = {"requests": {"cpu": "8", "memory": "12Gi"}, "limits": {"cpu": "16", "memory": "24Gi"},
                    "evidence": "fixture"}
        default = next(name for name, arm in self.profile["arms"].items() if arm.get("default"))
        record = {"arm": default, "image": self.IMAGE, "host_resources": measured}
        contract = service.serving_contract(self.write_profile(dict(self.profile, qualification=record)))
        self.assertEqual({k: measured[k] for k in ("requests", "limits")},
                         {k: contract["requirements"]["host_resources"][k] for k in ("requests", "limits")})
        without = {k: v for k, v in self.profile.items() if k != "qualification"}
        self.assertIsNone(service.serving_contract(self.write_profile(without))["requirements"]["host_resources"])
        for bad in ({"requests": {"cpu": "8", "memory": "12G"}}, {"requests": {"cpu": "32", "memory": "12Gi"}},
                    {"evidence": ""}):
            broken = dict(record, host_resources={**measured, **bad})
            with self.assertRaises(ValueError):
                service.serving_contract(self.write_profile(dict(self.profile, qualification=broken)))

    def test_reasoning_parameters_come_from_the_verified_template(self):
        contract = service.serving_contract(self.PROFILE, runtime_image=self.IMAGE)
        template = contract["chat_template"]
        self.assertTrue(template["verified"])
        self.assertEqual(["low", "medium", "xhigh"], template["reasoning_efforts"])
        server = json.loads(next(entry["value"][0] for entry in self.profile["args"]
                                 if entry["flag"] == "--default-chat-template-kwargs"))
        # The default effort is the profile's request_reasoning_effort, never an SGLang server default.
        self.assertNotIn("reasoning_effort", server)
        self.assertEqual({"chat_template_kwargs": {"enable_thinking": server["enable_thinking"]},
                          "reasoning_effort": self.profile["request_reasoning_effort"]},
                         contract["reasoning"]["request_defaults"])
        # high is not a template value; clients that offer it get the nearest accepted level.
        self.assertEqual({"low": "low", "medium": "medium", "xhigh": "xhigh", "high": "xhigh"},
                         contract["reasoning"]["client_efforts"])
        self.assertEqual(["enable_thinking"], contract["reasoning"]["client_template_kwargs"])
        with patch.dict(service.VERIFIED_TEMPLATES, clear=True):
            unread = service.serving_contract(self.PROFILE, runtime_image=self.IMAGE)
        self.assertEqual("incomplete", unread["status"])
        self.assertIsNone(unread["reasoning"]["request_defaults"])
        self.assertTrue(any(item.startswith("reasoning parameters") for item in unread["missing_inputs"]))
        with self.assertRaisesRegex(ValueError, "not accepted by the checkpoint template"):
            service.serving_contract(self.write_profile(dict(self.profile, request_reasoning_effort="high")))
        # A server-default effort would override every request's own in the pinned SGLang.
        for entry in self.profile["args"]:
            if entry["flag"] == "--default-chat-template-kwargs":
                entry["value"] = [json.dumps({"enable_thinking": True, "reasoning_effort": "low"})]
        with self.assertRaisesRegex(ValueError, "overrides every per-request effort"):
            service.serving_contract(self.write_profile(self.profile))

    def test_a_gateway_config_change_rolls_litellm(self):
        # The rewritten config keeps its kustomize name, so the hash of its final contents
        # on LiteLLM's pod template is what makes a contract change restart the gateway.
        def litellm(output_tokens):
            contract = service.serving_contract(self.PROFILE, output_tokens=output_tokens)
            objects, _, _ = service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant"),
                                                  contract, "large", "assistant")
            config = next(o for o in objects if o["kind"] == "ConfigMap" and "litellm.yaml" in o.get("data", {}))
            pod = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "litellm")
            return config["metadata"]["name"], pod["spec"]["template"]["metadata"]["annotations"].get(
                "ai-home-lab.local/gateway-config-sha256")
        (name_a, digest_a), (name_b, digest_b), (_, digest_a2) = litellm(4096), litellm(12288), litellm(4096)
        self.assertEqual(name_a, name_b)  # why the name alone cannot roll the gateway
        self.assertTrue(digest_a and digest_b and digest_a != digest_b)
        self.assertEqual(digest_a, digest_a2)  # deterministic: an unchanged config does not roll it

    def test_each_mode_has_its_own_deadline_and_no_gateway_timeout_undercuts_it(self):
        # Large mode serves 262K contexts (a cold 235K-token prompt takes ~240 s to its first
        # token); shared mode keeps 180 s. LiteLLM's global fallbacks must not cap the route.
        for mode, deadline in (("large", 1200), ("shared", 600)):
            contract = service.serving_contract(self.PROFILE, mode=mode)
            self.assertEqual({"gateway_seconds": deadline, "route_seconds": deadline - 10,
                              "client_milliseconds": deadline * 1000}, contract["deadlines"])
            objects, _, _ = service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant"),
                                                  contract, mode, "assistant")
            data = next(o for o in objects if o["kind"] == "ConfigMap" and "litellm.yaml" in o.get("data", {}))["data"]
            config = yaml.safe_load(data["litellm.yaml"])
            route = next(r for r in config["model_list"] if r["model_name"] == contract["model"]["alias"])
            self.assertEqual(deadline - 10, route["litellm_params"]["timeout"])
            self.assertEqual(deadline - 10, config["router_settings"]["timeout"])
            self.assertEqual(deadline - 10, config["litellm_settings"]["request_timeout"])
            self.assertEqual(deadline, json.loads(data["gateway-policy.json"])["request_timeout"])
        self.assertEqual(180, service.serving_contract(self.PROFILE, mode="large", deadline_seconds=180)["deadlines"]["gateway_seconds"])

    def test_every_consumer_is_rendered_from_the_contract_and_none_targets_fp8(self):
        contract = service.serving_contract(self.PROFILE)
        objects, rendered, checks = service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant"),
                                                           contract, "large", "assistant")
        self.assertTrue(all(check["matches"] for check in checks), checks)
        named = lambda kind, name: next(o for o in objects if o["kind"] == kind and o["metadata"]["name"] == name)
        alias, budgets = contract["model"]["alias"], contract["budgets"]
        sglang = named("Deployment", "sglang")
        container = sglang["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(0, sglang["spec"]["replicas"])
        self.assertEqual(contract["_render"]["argv"], container["args"])
        self.assertIn("CONTRACT-INCOMPLETE", container["image"])
        names = [f"gpu-{i}" for i in range(contract["requirements"]["gpus"])]
        self.assertEqual([{"name": n, "resourceClaimName": n} for n in names], sglang["spec"]["template"]["spec"]["resourceClaims"])
        self.assertEqual([{"name": n} for n in names], container["resources"]["claims"])
        self.assertNotIn("amd.com/gpu", container["resources"]["limits"])
        self.assertNotIn("envFrom", container)
        models = next(v for v in sglang["spec"]["template"]["spec"]["volumes"] if v["name"] == "models")
        self.assertEqual({"claimName": "ai-models", "readOnly": True}, models["persistentVolumeClaim"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        gateway = next(o for o in objects if o["kind"] == "ConfigMap" and "litellm.yaml" in o["data"])
        routes = {r["model_name"]: r for r in yaml.safe_load(gateway["data"]["litellm.yaml"])["model_list"]}
        self.assertEqual({alias, "Qwen3.5-9B"}, set(routes))
        self.assertEqual(budgets["context_length"], routes[alias]["model_info"]["max_tokens"])
        policy = json.loads(gateway["data"]["gateway-policy.json"])
        self.assertEqual([budgets["context_length"], budgets["output_tokens"]], policy["chat_models"][alias])
        self.assertNotIn(service.LEGACY_ALIAS, policy["chat_models"])
        webui = {e["name"]: e.get("value") for e in named("Deployment", "open-webui")["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(alias, webui["DEFAULT_MODELS"])
        self.assertEqual("native", json.loads(webui["DEFAULT_MODEL_PARAMS"])["function_calling"])
        tools = {e["name"]: e.get("value") for e in named("Deployment", "service-tools")["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual((contract["contract"], "incomplete"), (tools["SERVING_CONTRACT"], tools["SERVING_CONTRACT_STATUS"]))
        self.assertFalse(any(o["kind"] == "ConfigMap" and "MODEL_REVISION" in o.get("data", {}) for o in objects))
        for mode, experience in (("split", "assistant"), ("large", "service")):
            with self.assertRaises(ValueError):
                service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant"), contract, mode, experience)
        shared, rendered, _ = service.apply_contract(service.kustomize(ROOT / "apps/overlays/assistant-shared"), contract,
                                                     "shared", "assistant")
        policy = next(json.loads(o["data"]["gateway-policy.json"]) for o in shared
                      if o["kind"] == "ConfigMap" and "gateway-policy.json" in o.get("data", {}))
        self.assertEqual(["Qwen3.5-9B", alias], policy["image_models"])
        deployments = {o["metadata"]["name"]: o for o in shared if o["kind"] == "Deployment"}
        self.assertEqual({"swarmui", "comfyui-gpu0"} <= deployments.keys(), True)
        claims = {name: [c["resourceClaimName"] for c in deployments[name]["spec"]["template"]["spec"]["resourceClaims"]]
                  for name in ("sglang", "swarmui", "comfyui-gpu0")}
        self.assertEqual({"sglang": ["gpu-0", "gpu-1"], "swarmui": ["gpu-1"], "comfyui-gpu0": ["gpu-0"]}, claims)

    def test_shared_render_serves_uqs_with_both_image_generators(self):
        site = self.root / "site.json"
        site.write_text(json.dumps({"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
            "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
            "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}))
        args = dict(site=site, mode="shared", experience="assistant-memory", serving_profile=self.PROFILE,
                    arm="UQS", runtime_image=None, output_tokens=4096)
        output = self.root / "shared"
        service.render(SimpleNamespace(output=output, **args))
        objects = service.load_objects((output / "workloads.yaml").read_text())
        deployments = {o["metadata"]["name"]: o for o in objects if o["kind"] == "Deployment"}
        self.assertTrue({"sglang", "swarmui", "comfyui-gpu0"} <= deployments.keys())
        argv = deployments["sglang"]["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual("163840", argv[argv.index("--max-total-tokens") + 1])
        memory = next(o for o in objects if o["kind"] == "ConfigMap" and "MEMORY_LLM_MODEL" in o.get("data", {}))
        self.assertEqual("Qwen3.8-27B-MXFP4", memory["data"]["MEMORY_LLM_MODEL"])
        self.assertEqual("shared", json.loads((output / "evidence.json").read_text())["mode"])
        # shared exists only as a serving contract, and only for the assistant experiences
        for change, name in (({"serving_profile": None}, "no-profile"), ({"experience": "service"}, "service")):
            with self.subTest(name), self.assertRaises(ValueError):
                service.render(SimpleNamespace(output=self.root / name, **{**args, **change}))

    def test_render_writes_contract_client_values_and_missing_inputs(self):
        site = self.root / "site.json"
        site.write_text(json.dumps({"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
            "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
            "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}))
        output = self.root / "render"
        service.render(SimpleNamespace(site=site, output=output, mode="large", experience="assistant-memory",
                                       serving_profile=self.PROFILE, arm=None, runtime_image=None, output_tokens=4096))
        client = dict(line.split("=", 1) for line in (output / "client.conf").read_text().splitlines())
        contract = json.loads((output / "serving-contract.json").read_text())
        self.assertEqual((contract["model"]["alias"], str(contract["budgets"]["context_length"]), contract["contract"]),
                         (client["AGENT_MODEL"], client["AGENT_CONTEXT_TOKENS"], client["AGENT_SERVING_CONTRACT"]))
        self.assertEqual("true", client["AGENT_ENABLE_THINKING"])
        self.assertNotIn("_render", contract)
        evidence = json.loads((output / "evidence.json").read_text())
        self.assertEqual("incomplete", evidence["serving_contract"]["status"])
        self.assertEqual(contract["missing_inputs"], evidence["serving_contract"]["missing_inputs"])
        self.assertIn("serving-contract.json", evidence["files"])
        self.assertNotIn(service.LEGACY_ALIAS, (output / "client.conf").read_text())
        memory = next(o for o in service.load_objects((output / "workloads.yaml").read_text())
                      if o["kind"] == "ConfigMap" and "MEMORY_LLM_MODEL" in o.get("data", {}))
        self.assertEqual(contract["model"]["alias"], memory["data"]["MEMORY_LLM_MODEL"])


class GatewayPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        saved = (dict(boundary.CHAT_MODELS), dict(boundary.REASONING_EFFORTS), dict(boundary.TEMPLATE_KWARGS),
                 boundary.ADMISSION_LIMIT, set(boundary.IMAGE_MODELS), boundary.DEFAULT_REASONING_EFFORT)
        def restore():
            for target, value in zip((boundary.CHAT_MODELS, boundary.REASONING_EFFORTS, boundary.TEMPLATE_KWARGS), saved):
                target.clear()
                target.update(value)
            boundary.ADMISSION_LIMIT = saved[3]
            boundary.IMAGE_MODELS.clear()
            boundary.IMAGE_MODELS.update(saved[4])
            boundary.DEFAULT_REASONING_EFFORT = saved[5]
        self.addCleanup(restore)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "gateway-policy.json"

    async def test_rendered_policy_replaces_source_defaults(self):
        contract = service.serving_contract(ServingContractTests.PROFILE)
        rendered = service.candidate_render(service.contract_view(contract), 4096, 180, 16)
        self.path.write_text(json.dumps(service.gateway_policy(contract, rendered)))
        self.assertEqual(contract["deadlines"]["gateway_seconds"], boundary.apply_policy(self.path))
        self.assertEqual(contract["reasoning"]["request_defaults"]["reasoning_effort"], boundary.DEFAULT_REASONING_EFFORT)
        self.assertNotIn("Qwen3.8-27B-FP8", boundary.CHAT_MODELS)
        self.assertEqual((contract["budgets"]["context_length"], 4096), boundary.CHAT_MODELS["Qwen3.8-27B-MXFP4"])
        self.assertEqual("xhigh", boundary.REASONING_EFFORTS["high"])
        gateway = boundary.GatewayBoundary(None, "/nonexistent", "/nonexistent")
        gateway.state = {"phase": "ready", "model": "Qwen3.8-27B-MXFP4", "images": False}
        result = await request(gateway, "/v1/chat/completions",
                               {"model": "Qwen3.8-27B-FP8", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual("unknown_model", json.loads(result[-1]["body"])["error"]["code"])
        for change in ({"template_kwargs": ["chat_template"]}, {"admission_limit": 0},
                       {"chat_models": {"m": [4096, 4096]}}, {"request_timeout": 5},
                       {"default_reasoning_effort": "extreme"}):
            self.path.write_text(json.dumps({**service.gateway_policy(contract, rendered), **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                boundary.apply_policy(self.path)

    async def test_shared_mode_policy_admits_images_with_the_contract_model(self):
        contract = service.serving_contract(ServingContractTests.PROFILE)
        rendered = service.candidate_render(service.contract_view(contract), 4096, 180, 16)
        alias = "Qwen3.8-27B-MXFP4"
        self.assertEqual(["Qwen3.5-9B"], service.gateway_policy(contract, rendered)["image_models"])
        self.assertEqual(["Qwen3.5-9B", alias], service.gateway_policy(contract, rendered, "shared")["image_models"])
        control = Path(self.directory.name) / "control"
        control.write_text("c" * 32)
        headers = {b"authorization": b"Bearer " + b"c" * 32}
        ready = {"phase": "ready", "model": alias, "images": True}
        for mode, status in (("large", 400), ("shared", 200)):
            self.path.write_text(json.dumps(service.gateway_policy(contract, rendered, mode)))
            boundary.apply_policy(self.path)
            gateway = boundary.GatewayBoundary(None, "/nonexistent", str(control))
            result = await request(gateway, "/_service/control", ready, headers)
            with self.subTest(mode=mode):
                self.assertEqual(status, result[0]["status"])
                if status == 400:
                    self.assertEqual("images_not_admitted_for_model", json.loads(result[-1]["body"])["error"]["code"])
                    self.assertNotEqual(alias, gateway.state.get("model"))
                else:
                    self.assertEqual(ready, gateway.state)
        # an image model must also be an admitted chat model
        self.path.write_text(json.dumps({**service.gateway_policy(contract, rendered, "shared"), "image_models": ["other"]}))
        with self.assertRaises(ValueError):
            boundary.apply_policy(self.path)

    def test_contract_metric_follows_the_rendered_environment(self):
        import assistant_metrics
        metrics = assistant_metrics.Metrics()
        with patch.object(assistant_metrics, "METRICS", metrics):
            assistant_metrics.record_serving_contract({})
            self.assertNotIn("workstation_assistant_serving_contract_info{", metrics.render())
            assistant_metrics.record_serving_contract({"SERVING_CONTRACT": "qwen3.8-27b-mxfp4-tp2.Y",
                                                       "SERVING_CONTRACT_STATUS": "incomplete"})
        self.assertIn('workstation_assistant_serving_contract_info{contract="qwen3.8-27b-mxfp4-tp2.Y",'
                      'status="incomplete"} 1', metrics.render())

    def test_network_policies_never_carry_empty_rule_lists(self):
        # The API server drops an empty ingress/egress list, so a manifest that carries one
        # differs from the live object forever and every apply re-saves it (generation churn).
        # "Deny all" is the direction in policyTypes with no rules.
        files = [f for f in ROOT.glob("apps/**/*.yaml")] + [f for f in ROOT.glob("infrastructure/**/*.yaml")]
        checked = 0
        for path in files:
            try:
                docs = [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]
            except yaml.YAMLError:
                continue  # templates rendered elsewhere
            for doc in docs:
                if doc.get("kind") != "NetworkPolicy":
                    continue
                checked += 1
                spec = doc.get("spec", {})
                for direction in ("ingress", "egress"):
                    self.assertNotEqual([], spec.get(direction, None), f"{path.relative_to(ROOT)} {doc['metadata']['name']}")
        self.assertGreater(checked, 20)

    def test_every_tool_series_exists_at_zero_before_the_first_call(self):
        # Dashboards show zero, not "No data", for a tool unused since the pod started,
        # and rate() sees the first event. Every label value is enumerated, so this is bounded.
        import assistant_metrics
        metrics = assistant_metrics.Metrics()
        text = metrics.render()
        for line in ('workstation_assistant_retrieval_requests_total{client="webui",outcome="error"} 0',
                     'workstation_assistant_reranker_fallback_total{reason="unavailable"} 0',
                     'workstation_assistant_memory_operations_total{client="mcp",op="recall",outcome="ok"} 0',
                     'workstation_assistant_retrieval_duration_seconds_count{client="mcp"} 0',
                     'workstation_assistant_retrieval_duration_seconds_bucket{client="mcp",le="+Inf"} 0'):
            self.assertIn(line, text.splitlines())
        series = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertLess(len(series), 400)
        self.assertFalse(any('="other"' in line for line in series))  # only enumerated values are seeded
        # Gauges are not seeded: no contract or reranker state until it is known.
        self.assertFalse(any(line.startswith(("workstation_assistant_serving_contract_info",
                                              "workstation_assistant_reranker_state")) for line in series))
        metrics.inc("workstation_assistant_retrieval_requests_total", client="webui", outcome="error")
        self.assertIn('workstation_assistant_retrieval_requests_total{client="webui",outcome="error"} 1',
                      metrics.render().splitlines())


if __name__ == "__main__":
    unittest.main()
