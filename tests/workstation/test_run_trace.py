"""Run identity through the real request paths: the gateway boundary, service-tools and the runners.

Deterministic backends only: no model, cluster, PSU or GPU is contacted. The
gateway and service-tools here are the shipped ASGI applications; the benchmark
runner and the acceptance runner are started through their command lines.
"""
import asyncio
import copy
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import assistant_metrics
import boundary
import run_record as rr
import run_trace
from service_tools import RequestLifecycle, ServiceTools, mcp_operation

FIXTURES = ROOT / "tests/fixtures/run-record"
MODEL = "Qwen3.8-27B-FP8"
PLAN = json.loads((FIXTURES / "plan.json").read_text())
DIGEST = rr.plan_digest(PLAN)
ATTEMPTS = [json.loads(line) for line in (FIXTURES / "attempts.jsonl").read_text().splitlines()]
IDENTITY = ATTEMPTS[3]["identity"]  # a retry: every field, including the causal parent, is set
CONTEXT = rr.context_value(IDENTITY, DIGEST)
IDENTIFIERS = [value for value in IDENTITY.values() if value not in (None, "retry")] + [DIGEST]


async def call(app, path, body=None, headers=None, method="POST", disconnect_after=None):
    """One HTTP request to an ASGI application; returns the messages it sent."""
    queue = asyncio.Queue()
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    await queue.put({"type": "http.request", "body": payload, "more_body": False})
    if disconnect_after is not None:
        asyncio.get_running_loop().call_later(disconnect_after, queue.put_nowait, {"type": "http.disconnect"})
    messages = []

    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "method": method,
             "headers": [(key.lower(), value) for key, value in (headers or {}).items()],
             "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await app(scope, queue.get, send)
    return messages


def status(messages):
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


class ContextTests(unittest.TestCase):
    def test_service_parser_is_identical_to_the_record_parser(self):
        self.assertEqual((run_trace.ID.pattern, run_trace.SHA256.pattern, run_trace.TOKEN.pattern, run_trace.FIELDS,
                          run_trace.RESULTS, run_trace.MAX_CONTEXT, run_trace.HEADER.decode()),
                         (rr.ID.pattern, rr.SHA256.pattern, rr.TOKEN.pattern, rr.CONTEXT_FIELDS, rr.TRACE_RESULTS,
                          rr.MAX_CONTEXT, rr.CONTEXT_HEADER.lower()))
        values = [rr.context_value(attempt["identity"], DIGEST) for attempt in ATTEMPTS]
        values += [CONTEXT.replace("v1;", "v2;"), CONTEXT.replace(";plan=", ";plan=x"), CONTEXT + ";extra=1",
                   CONTEXT.replace("task=", "task=a b"), CONTEXT.replace("cause=retry", "cause=initial"),
                   CONTEXT.replace("parent=fixture-retried-1", "parent=fixture-retried-2"), CONTEXT.replace("run=", "arm=", 1),
                   CONTEXT.replace("parent=fixture-retried-1", "parent=-"), CONTEXT.rsplit(";", 1)[0], CONTEXT.upper(),
                   CONTEXT.replace("=", ":"), CONTEXT.replace("task=fixture-retried", "task=" + "a" * 129),
                   CONTEXT.replace("task=fixture-retried", "task=café"), "", "v1", "x" * 2000]
        accepted = 0
        for value in values:
            outcomes = []
            for parse in (rr.parse_context, run_trace.parse):
                try:
                    outcomes.append(parse(value))
                except ValueError:
                    outcomes.append("refused")
            with self.subTest(value):
                self.assertEqual(outcomes[0], outcomes[1])
            accepted += outcomes[0] != "refused"
        self.assertEqual(accepted, len(ATTEMPTS))
        self.assertEqual(run_trace.parse(CONTEXT.encode()), (IDENTITY, DIGEST))
        with self.assertRaises(ValueError):
            run_trace.parse(CONTEXT.encode() + b"\xff")

    def test_an_event_holds_only_fixed_names_and_validated_identifiers(self):
        records = []
        events = run_trace.Events("gateway", records.append, clock=iter(range(100, 200)).__next__)
        self.assertIs(events.begin("chat", {}), run_trace.UNTRACED)
        run_trace.UNTRACED.finish("ok", 200)
        trace = events.begin("chat", {run_trace.HEADER: CONTEXT.encode()})
        # Free text offered as an operation, result, status or code never reaches the event.
        trace.finish("Traceback (most recent call last)", "200 OK", "password=DUMMY_SECRET_SENTINEL",
                     operation="GET /v1/models?key=DUMMY_SECRET_SENTINEL")
        trace.finish("ok", 200)  # The first outcome stands.
        self.assertEqual(records, [{
            "schema": 1, "kind": "request-trace", "interface": "run-record-v1", "service": "gateway",
            "operation": "other", "identity": IDENTITY, "plan_sha256": DIGEST, "started_unix_ns": 100,
            "ended_unix_ns": 101, "result": "error", "status": None, "code": None}])
        rr.validate_trace(records[0])
        self.assertEqual(set(records[0]), {"schema", "kind", "interface", "service", "operation", "identity",
                                           "plan_sha256", "started_unix_ns", "ended_unix_ns", "result", "status", "code"})
        # Without a writer the context is still validated and nothing is written.
        run_trace.Events("gateway").begin("chat", {run_trace.HEADER: CONTEXT.encode()}).finish("ok", 200)
        with self.assertRaises(ValueError):
            run_trace.Events("gateway").begin("chat", {run_trace.HEADER: b"v1;task=x"})

    def test_descriptor_writer_writes_one_line_per_event_and_never_raises(self):
        read, write = os.pipe()
        try:
            writer = run_trace.descriptor_writer(write)
            writer({"b": 1, "a": "x"})
            writer({"c": 2})
            self.assertEqual(os.read(read, 4096), b'{"a":"x","b":1}\n{"c":2}\n')
        finally:
            os.close(read)
            os.close(write)
        writer({"lost": True})  # The descriptor is gone: the event is lost, the request is not failed.


class GatewayTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.control, self.mapping = root / "control", root / "mapping"
        self.control.write_text("c" * 32)
        self.mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {
            "alice": {"key": "sk-" + "a" * 32, "expires_at": 9999999999, "images": True}}}))
        self.seen, self.events = [], []
        self.response_status = 200

        async def backend(scope, receive, send):
            self.seen.append(scope)
            await receive()
            await boundary.reply(send, self.response_status, {"choices": [{"message": {"content": "fixture"}}]})
        self.backend = backend
        self.gateway = self.boundary(backend)
        self.chat = {"model": MODEL, "messages": [{"role": "user", "content": "fixture prompt DUMMY_PROMPT_SENTINEL"}]}
        self.headers = {b"authorization": b"Bearer sk-" + b"d" * 32, run_trace.HEADER: CONTEXT.encode()}

    def boundary(self, backend, **options):
        gateway = boundary.GatewayBoundary(backend, self.mapping, self.control,
                                           events=run_trace.Events("gateway", self.events.append), **options)
        gateway.state = {"phase": "ready", "model": MODEL, "images": False}
        return gateway

    async def metrics(self, gateway=None):
        messages = await call(gateway or self.gateway, "/_service/metrics", None, {}, "GET")
        return b"".join(message.get("body", b"") for message in messages).decode()

    async def test_an_identified_chat_writes_one_event_and_the_context_stops_at_the_boundary(self):
        self.assertEqual(status(await call(self.gateway, "/v1/chat/completions", self.chat, self.headers)), 200)
        self.assertEqual(len(self.events), 1)
        event = rr.validate_trace(self.events[0])
        self.assertEqual((event["service"], event["operation"], event["identity"], event["plan_sha256"],
                          event["result"], event["status"], event["code"]),
                         ("gateway", "chat", IDENTITY, DIGEST, "ok", 200, None))
        # Only the three reviewed headers reach LiteLLM: the context is read at the boundary and dropped.
        forwarded = {key for key, _ in self.seen[0]["headers"]}
        self.assertTrue({b"authorization"} <= forwarded <= {b"authorization", b"content-type", b"accept", b"content-length"})
        # The event holds no request content, credential or path.
        text = json.dumps(self.events)
        for forbidden in ("DUMMY_PROMPT_SENTINEL", "fixture prompt", "sk-", "Bearer", "/v1/", MODEL):
            self.assertNotIn(forbidden, text)
        # Identifiers never reach the metrics: the series are the fixed ones.
        metrics = await self.metrics()
        self.assertIn('workstation_service_requests_total{result="ok"} 1', metrics)
        for identifier in IDENTIFIERS:
            self.assertNotIn(identifier, metrics)

    async def test_ordinary_traffic_writes_nothing(self):
        plain = {key: value for key, value in self.headers.items() if key != run_trace.HEADER}
        self.assertEqual(status(await call(self.gateway, "/v1/chat/completions", self.chat, plain)), 200)
        browser = {b"authorization": b"Bearer " + b"s" * 32, b"x-openwebui-user-id": b"alice"}
        self.assertEqual(status(await call(self.gateway, "/webui/v1/chat/completions", self.chat, browser)), 200)
        self.assertEqual(self.events, [])
        # Routes other than chat write nothing even with a context.
        await call(self.gateway, "/_service/status", None, {run_trace.HEADER: CONTEXT.encode()}, "GET")
        self.assertEqual(self.events, [])

    async def test_a_malformed_context_is_refused_before_admission_and_never_logged(self):
        for value in (b"v1;task=fixture", CONTEXT.encode().replace(b"cause=retry", b"cause=initial"),
                      CONTEXT.encode() + b";note=DUMMY_SECRET_SENTINEL", b"\xff\xfe", b""):
            messages = await call(self.gateway, "/v1/chat/completions", self.chat,
                                  {**self.headers, run_trace.HEADER: value})
            self.assertEqual((status(messages), json.loads(messages[-1]["body"])["error"]["code"]),
                             (400, "invalid_run_context"))
        self.assertEqual((self.events, self.seen, self.gateway.requests["refused"]), ([], [], 5))

    async def test_refused_limited_and_failed_requests_are_traced_with_fixed_codes(self):
        cases = []
        self.gateway.state = {"phase": "draining", "model": MODEL, "images": False}
        await call(self.gateway, "/v1/chat/completions", self.chat, self.headers)
        cases.append(("refused", 503, "requested_model_unavailable"))
        self.gateway.state = {"phase": "ready", "model": MODEL, "images": False}
        await call(self.gateway, "/v1/chat/completions", {**self.chat, "user_note": "x"}, self.headers)
        cases.append(("refused", 400, "unsupported_request_field"))
        await call(self.gateway, "/v1/chat/completions", b"{not json", self.headers)
        cases.append(("refused", 400, "invalid_json"))
        self.gateway.active = dict.fromkeys(range(boundary.ADMISSION_LIMIT), 0.0)
        await call(self.gateway, "/v1/chat/completions", self.chat, self.headers)
        cases.append(("limited", 429, "service_concurrency_limit"))
        self.gateway.active = {}
        self.response_status = 502
        await call(self.gateway, "/v1/chat/completions", self.chat, self.headers)
        cases.append(("backend_error", 502, None))
        self.assertEqual([(event["result"], event["status"], event["code"]) for event in self.events], cases)
        for event in self.events:
            self.assertEqual(rr.validate_trace(event)["identity"], IDENTITY)

    async def test_deadline_and_cancellation_are_traced(self):
        async def silent(scope, receive, send):
            await asyncio.sleep(30)
        slow = self.boundary(silent, request_timeout=0.05)
        self.assertEqual(status(await call(slow, "/v1/chat/completions", self.chat, self.headers)), 504)
        self.assertEqual([(event["result"], event["status"], event["code"]) for event in self.events],
                         [("backend_error", 504, "request_deadline")])
        self.assertGreater(self.events[0]["ended_unix_ns"], self.events[0]["started_unix_ns"])
        self.events.clear()
        leaving = self.boundary(silent, request_timeout=5)
        with self.assertRaises(asyncio.CancelledError):
            await call(leaving, "/v1/chat/completions", self.chat, self.headers, disconnect_after=0.05)
        self.assertEqual([(event["result"], event["status"], event["code"]) for event in self.events],
                         [("cancelled", None, "request_cancelled")])
        self.assertEqual(leaving.active, {})

    async def test_an_unclassified_failure_still_writes_one_event(self):
        async def broken(scope, receive, send):
            raise RuntimeError("DUMMY_SECRET_SENTINEL in an exception message")
        with self.assertRaises(RuntimeError):
            await call(self.boundary(broken), "/v1/chat/completions", self.chat, self.headers)
        self.assertEqual([(event["result"], event["status"], event["code"]) for event in self.events],
                         [("error", None, None)])
        self.assertNotIn("DUMMY_SECRET_SENTINEL", json.dumps(self.events))


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    """The service-tools request lifecycle on its own: a response counts until it ends."""

    def setUp(self):
        self.events = []
        self.service = type("Service", (), {})()
        self.service.events = run_trace.Events("service-tools", self.events.append)
        self.service.requests_active = {}
        self.service.requests_started = dict.fromkeys(assistant_metrics.ROUTES, 0)

    async def test_a_streamed_response_counts_until_its_last_byte(self):
        streaming, release = asyncio.Event(), asyncio.Event()

        async def stream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b": keepalive\n\n", "more_body": True})
            streaming.set()
            await release.wait()
            await send({"type": "http.response.body", "body": b"data: done\n\n", "more_body": False})
        lifecycle = RequestLifecycle(stream, self.service)
        request = asyncio.create_task(call(lifecycle, "/mcp", {}, {run_trace.HEADER: CONTEXT.encode()}))
        await streaming.wait()
        self.assertEqual((list(self.service.requests_active.values()), self.service.requests_started["mcp"], self.events),
                         (["mcp"], 1, []))
        release.set()
        await request
        self.assertEqual((self.service.requests_active, self.events[0]["result"], self.events[0]["operation"]),
                         ({}, "ok", "mcp"))

    async def test_failures_and_cancellation_release_the_count(self):
        async def broken(scope, receive, send):
            raise RuntimeError("fixture failure")

        async def hanging(scope, receive, send):
            await asyncio.sleep(30)
        with self.assertRaises(RuntimeError):
            await call(RequestLifecycle(broken, self.service), "/mcp", {}, {run_trace.HEADER: CONTEXT.encode()})
        request = asyncio.create_task(call(RequestLifecycle(hanging, self.service), "/webui/search", {},
                                           {run_trace.HEADER: CONTEXT.encode()}))
        await asyncio.sleep(0.01)
        self.assertEqual(list(self.service.requests_active.values()), ["web"])
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(self.service.requests_active, {})
        self.assertEqual([(event["result"], event["operation"], event["status"]) for event in self.events],
                         [("error", "mcp", None), ("cancelled", "web", None)])

    async def test_other_routes_pass_through_uncounted(self):
        async def plain(scope, receive, send):
            await boundary.reply(send, 200, {})
        lifecycle = RequestLifecycle(plain, self.service)
        for path in ("/health", "/metrics", "/mcp/", "/unknown"):
            self.assertEqual(status(await call(lifecycle, path, None, {run_trace.HEADER: CONTEXT.encode()}, "GET")), 200)
        self.assertEqual((self.service.requests_started, self.events), (dict.fromkeys(assistant_metrics.ROUTES, 0), []))

    def test_operation_names_are_fixed(self):
        self.assertEqual([mcp_operation(method, tool) for method, tool in (
            ("tools/call", "search_web"), ("tools/call", "retrieve_project_passage"), ("tools/call", "save_task_handoff"),
            ("tools/call", "rm -rf /"), ("tools/call", None), ("tools/call", ["search_web"]), ("tools/list", None),
            ("initialize", None), ("ping", None), ("notifications/initialized", None), ("resources/read", None),
            (None, None), (["tools/call"], None))],
            ["tool:search_web", "tool:retrieve_project_passage", "tool:save_task_handoff", "tool:other", "tool:other",
             "tool:other", "mcp:tools-list", "mcp:initialize", "mcp:ping", "mcp:notification", "mcp:other", "mcp:other",
             "mcp:other"])
        for method, tool in (("tools/call", "search_web"), ("tools/list", None), ("x", None)):
            self.assertIsNotNone(rr.TOKEN.match(mcp_operation(method, tool)))


class ServiceToolsTraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        mapping = root / "mapping.json"
        mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {}}))
        self.gate = None  # when set, the identity lookup waits for it
        self.entered = asyncio.Event()

        async def upstream(request):
            if request.url.path == "/api/v1/users/user/status":
                if self.gate is not None:
                    self.entered.set()
                    await self.gate.wait()
                return httpx.Response(200, json={"id": "alice", "role": "user"})
            raise AssertionError("unexpected upstream route")
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.events = []
        # A registry of its own: the shipped families and renderer, without other tests' counts.
        self.metrics = assistant_metrics.Metrics()
        patcher = mock.patch.multiple("service_tools", METRICS=self.metrics)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tools = ServiceTools(self.upstream, "http://webui", ("http://comfy1", "http://comfy0"), "http://gateway",
                                  mapping, root / "state", public_webui="https://chat.test",
                                  events=run_trace.Events("service-tools", self.events.append))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.tools.app), base_url="http://tools")
        self.headers = {"Authorization": "Bearer fixture-user-token", rr.CONTEXT_HEADER: CONTEXT}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()

    async def mcp(self, method, params=None, headers=None):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return await self.client.post("/mcp", headers=self.headers if headers is None else headers, json=message)

    async def test_identified_tool_requests_write_events_with_fixed_operations(self):
        self.assertEqual((await self.mcp("tools/list")).status_code, 200)
        self.assertEqual((await self.mcp("tools/call", {"name": "search_web", "arguments": {"query": "DUMMY_QUERY_SENTINEL"}}))
                         .status_code, 503)  # Web search is disabled in this fixture.
        self.assertEqual((await self.mcp("tools/call", {"name": "DUMMY_TOOL_SENTINEL", "arguments": {}})).status_code, 200)
        self.assertEqual((await self.mcp("resources/read")).status_code, 200)
        self.assertEqual([(event["operation"], event["result"], event["status"]) for event in self.events], [
            ("mcp:tools-list", "ok", 200), ("tool:search_web", "backend_error", 503), ("tool:other", "ok", 200),
            ("mcp:other", "ok", 200)])
        for event in self.events:
            self.assertEqual((rr.validate_trace(event)["identity"], event["plan_sha256"], event["service"]),
                             (IDENTITY, DIGEST, "service-tools"))
        text = json.dumps(self.events)
        for forbidden in ("DUMMY_QUERY_SENTINEL", "DUMMY_TOOL_SENTINEL", "fixture-user-token", "alice", "/mcp"):
            self.assertNotIn(forbidden, text)
        # The events join the fixture run like any other request trace.
        report = rr.report(PLAN, ATTEMPTS, [], traces=self.events)
        row = next(row for task in report["tasks"] for row in task["attempt_records"]
                   if row["attempt_id"] == IDENTITY["attempt_id"])
        self.assertEqual(row["request_trace"]["services"]["service-tools"]["by_operation"],
                         {"mcp:tools-list": 1, "tool:search_web": 1, "tool:other": 1, "mcp:other": 1})

    async def test_a_tool_level_error_is_not_reported_as_success(self):
        self.tools.handoffs = __import__("handoff").HandoffStore(Path(self.directory.name) / "handoffs")
        response = await self.mcp("tools/call", {"name": "load_task_handoff", "arguments": {"id": "missing-handoff"}})
        self.assertEqual((response.status_code, response.json()["result"]["isError"]), (200, True))
        self.assertEqual([(event["operation"], event["result"], event["status"]) for event in self.events],
                         [("tool:load_task_handoff", "tool_error", 200)])

    async def test_ordinary_requests_write_nothing_and_a_malformed_context_is_refused(self):
        plain = {"Authorization": "Bearer fixture-user-token"}
        self.assertEqual((await self.mcp("tools/list", headers=plain)).status_code, 200)
        self.assertEqual(self.events, [])
        started = self.tools.request_activity()["started_total"]["mcp"]
        response = await self.mcp("tools/list", headers={**plain, rr.CONTEXT_HEADER: "v1;task=x;note=DUMMY_SECRET_SENTINEL"})
        self.assertEqual((response.status_code, response.json()), (400, {"error": "invalid_run_context"}))
        self.assertEqual((self.events, self.tools.request_activity()["started_total"]["mcp"]), ([], started))

    async def test_active_requests_are_observable_while_work_is_in_flight(self):
        idle = (await self.client.get("/health")).json()
        self.assertEqual(idle["requests"], {"active": 0, "active_by_route": {"mcp": 0, "image": 0, "web": 0},
                                            "started_total": {"mcp": 0, "image": 0, "web": 0}})
        self.assertEqual(idle["process_started_unix_seconds"], self.tools.process_started)
        metrics = (await self.client.get("/metrics")).text
        for line in ('workstation_assistant_active_requests{route="mcp"} 0',
                     'workstation_assistant_active_requests{route="image"} 0',
                     'workstation_assistant_active_requests{route="web"} 0',
                     'workstation_assistant_requests_started_total{route="mcp"} 0',
                     f"workstation_assistant_process_start_time_seconds {self.tools.process_started:.12g}"):
            self.assertIn(line + "\n", metrics)
        self.gate = asyncio.Event()
        request = asyncio.create_task(self.mcp("tools/list"))
        await self.entered.wait()
        # A short tool call is in flight: no completion counter has moved, yet the work is visible.
        busy = (await self.client.get("/health")).json()["requests"]
        self.assertEqual((busy["active"], busy["active_by_route"]["mcp"], busy["started_total"]["mcp"]), (1, 1, 1))
        metrics = (await self.client.get("/metrics")).text
        self.assertIn('workstation_assistant_active_requests{route="mcp"} 1\n', metrics)
        self.assertIn('workstation_assistant_requests_started_total{route="mcp"} 1\n', metrics)
        self.gate.set()
        self.assertEqual((await request).status_code, 200)
        after = (await self.client.get("/health")).json()["requests"]
        self.assertEqual((after["active"], after["started_total"]["mcp"]), (0, 1))
        metrics = (await self.client.get("/metrics")).text
        self.assertIn('workstation_assistant_active_requests{route="mcp"} 0\n', metrics)
        # Reading health and metrics is not work; run identifiers never become labels or values.
        self.assertEqual((await self.client.get("/health")).json()["requests"], after)
        for identifier in IDENTIFIERS:
            self.assertNotIn(identifier, metrics)
        self.assertEqual(len(self.events), 1)
        # Web and picture routes are counted on their own series; a refusal still counts as started work.
        self.assertEqual((await self.client.post("/webui/search", json={})).status_code, 401)
        self.assertEqual((await self.client.post("/webui/images/generations", json={})).status_code, 401)
        self.assertEqual(self.tools.request_activity()["started_total"], {"mcp": 1, "image": 1, "web": 1})

    def test_counter_values_are_rendered_exactly(self):
        metrics = assistant_metrics.Metrics()
        metrics.inc("workstation_assistant_requests_started_total", 12345678, route="mcp")
        metrics.set("workstation_assistant_process_start_time_seconds", 1763190000.125)
        text = metrics.render()
        self.assertIn('workstation_assistant_requests_started_total{route="mcp"} 12345678\n', text)
        self.assertIn("workstation_assistant_process_start_time_seconds 1763190000.12\n", text)
        self.assertEqual(assistant_metrics.FAMILIES["workstation_assistant_active_requests"][2],
                         {"route": ("mcp", "image", "web")})


class Bridge(http.server.BaseHTTPRequestHandler):
    """HTTP front of the gateway boundary, so the runners reach it as they reach the deployed one."""
    gateway = None

    def log_message(self, *arguments):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        messages = asyncio.run(call(self.gateway, self.path, body,
                                    {key.encode(): value.encode() for key, value in self.headers.items()}))
        payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        self.send_response(status(messages))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RunnerPathTests(unittest.TestCase):
    """Benchmark runner -> acceptance runner -> gateway -> container log -> join, through the command lines."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "control").write_text("c" * 32)
        (self.root / "mapping").write_text(json.dumps({"service_key": "s" * 32, "users": {}}))
        self.forwarded = []

        async def backend(scope, receive, send):
            self.forwarded.append(dict(scope["headers"]))
            await receive()
            await boundary.reply(send, 200, {"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "content": '{"status":"ready","count":3}'}}], "usage": {"prompt_tokens": 11, "completion_tokens": 3}})
        # The gateway writes its events to a file descriptor, as it writes to its container log.
        self.log = self.root / "gateway.log"
        descriptor = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.addCleanup(os.close, descriptor)
        gateway = boundary.GatewayBoundary(backend, self.root / "mapping", self.root / "control",
                                           events=run_trace.Events("gateway", run_trace.descriptor_writer(descriptor)))
        gateway.state = {"phase": "ready", "model": MODEL, "images": False}
        self.gateway = gateway
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("GatewayBridge", (Bridge,), {"gateway": gateway}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        self.environment = {key: value for key, value in os.environ.items() if key != rr.CONTEXT_ENV}
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def acceptance(self, report, *extra):
        return [sys.executable, "-B", str(ROOT / "lib/workstation/assistant_acceptance.py"), "run", str(report),
                "--boundary", "gateway", "--base-url", self.base, "--model", MODEL, "--case", "instruction-format",
                "--repetitions", "2", "--timeout", "20", *extra]

    def test_identity_travels_from_the_benchmark_runner_to_the_gateway_log_and_joins(self):
        plan = dict(PLAN, declared_unix_ns=1)
        digest = rr.plan_digest(plan)
        identity = dict(ATTEMPTS[0]["identity"])
        (self.root / "plan.json").write_text(json.dumps(plan))
        (self.root / "identity.json").write_text(json.dumps(identity))
        run = self.root / "run"
        command = [sys.executable, "-B", str(ROOT / "lib/workstation/measurement.py"), "--output", str(run),
                   "--timeout", "120", "--run-plan", str(self.root / "plan.json"),
                   "--run-identity", str(self.root / "identity.json"), "run", "--",
                   *self.acceptance(self.root / "acceptance-report.json")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=self.environment)
        self.assertEqual(result.returncode, 0, (run / "stderr.txt").read_text() if run.exists() else result.stderr)
        # An unidentified run of the same runner through the same gateway leaves no event.
        plain = subprocess.run(self.acceptance(self.root / "plain-report.json"), capture_output=True, text=True,
                               timeout=120, env=self.environment)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(len(self.forwarded), 4)
        for headers in self.forwarded:
            self.assertTrue(set(headers) <= {b"authorization", b"content-type", b"accept", b"content-length"})
        # The acceptance runner's own record names the attempt it ran under.
        report = json.loads((self.root / "acceptance-report.json").read_text())
        self.assertEqual(report["settings"]["run_context"], {"identity": identity, "plan_sha256": digest, "sent": True})
        self.assertEqual((report["executed_attempts"], report["metrics"]["task_pass_rate"]), (2, 1.0))
        self.assertIsNone(json.loads((self.root / "plain-report.json").read_text())["settings"]["run_context"])
        # The gateway log holds exactly the identified requests; unrelated log lines are ignored by the extractor.
        with self.log.open("a") as log:
            log.write("unrelated process output\n")
        summary = rr.traces_from_logs([self.log], plan["run_id"], self.root / "traces.jsonl")
        self.assertEqual(summary, {"kept": 2, "other_runs": 0, "ignored_lines": 1})
        traces = [json.loads(line) for line in (self.root / "traces.jsonl").read_text().splitlines()]
        self.assertEqual([(t["service"], t["operation"], t["result"], t["status"], t["identity"], t["plan_sha256"])
                          for t in traces], [("gateway", "chat", "ok", 200, identity, digest)] * 2)
        # Receipt, resources and request traces join on the identity bound at the start.
        attempt = rr.attempt_from_receipt(json.loads((run / "run.json").read_text()))
        rr.resources_from_telemetry(run / "telemetry.jsonl", self.root / "psu.jsonl", self.root / "gpu.jsonl")
        psu = [json.loads(line) for line in (self.root / "psu.jsonl").read_text().splitlines()]
        joined = rr.report(plan, [attempt], [], psu, [], traces)
        row = joined["tasks"][0]["attempt_records"][0]
        self.assertEqual(row["request_trace"], {"requests": 2, "outside_attempt_window": 0, "services": {
            "gateway": {"requests": 2, "by_result": {"ok": 2}, "by_operation": {"chat": 2}}}})
        # Requests succeeded and the runner graded its own answers as passed: the task is still unverified.
        self.assertEqual((row["acceptance"], joined["tasks"][0]["status"], joined["summary"]["tasks_accepted"]),
                         ("missing", "unverified", 0))
        # The same records are refused once a target is edited after the run.
        edited = dict(plan, latency_targets={"task_wall_seconds": 1})
        with self.assertRaisesRegex(ValueError, "different plan"):
            rr.report(edited, [dict(attempt, plan_sha256=rr.plan_digest(edited))], [], psu, [], traces)

    def test_the_runner_refuses_a_malformed_context_and_sends_none_to_other_boundaries(self):
        environment = dict(self.environment, **{rr.CONTEXT_ENV: "v1;task=x"})
        refused = subprocess.run(self.acceptance(self.root / "bad.json"), capture_output=True, text=True, timeout=60,
                                 env=environment)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("invalid run context", refused.stderr)
        self.assertFalse((self.root / "bad.json").exists())
        self.assertEqual(self.forwarded, [])
        # A boundary that does not read the context (native SGLang, Open WebUI) is sent none.
        import assistant_acceptance
        seen = []

        class Recorder(http.server.BaseHTTPRequestHandler):
            def log_message(self, *arguments):
                pass

            def do_POST(self):
                seen.append({key.lower() for key in self.headers})
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        menu = assistant_acceptance.load_menu()
        for name, sent in (("direct", False), ("webui-api", False), ("gateway", True), ("fixture", True)):
            seen.clear()
            assistant_acceptance.run(menu, name, base, None, MODEL, 1024, 10, repetitions=1,
                                     cases=["instruction-format"], context=CONTEXT)
            self.assertEqual(run_trace.HEADER.decode() in seen[0], sent, name)
            self.assertEqual(assistant_acceptance.run_context({rr.CONTEXT_ENV: CONTEXT}, name),
                             (CONTEXT, {"identity": IDENTITY, "plan_sha256": DIGEST, "sent": sent}))
        self.assertEqual(assistant_acceptance.run_context({}, "gateway"), (None, None))


if __name__ == "__main__":
    unittest.main()
