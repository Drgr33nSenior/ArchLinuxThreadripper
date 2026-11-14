"""Elastic KV tail hand-over (kv_tail.py) and its gateway route; SGLang and ComfyUI are fixtures."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/base/shared-api"))
import boundary  # noqa: E402
from assistant_metrics import METRICS  # noqa: E402
from boundary import GatewayBoundary, Refusal  # noqa: E402
from kv_tail import KvTail  # noqa: E402

KEY = "s" * 32
GIB = 1024 ** 3


class Fixture:
    """service-tools' view: the gateway route and two ComfyUIs."""

    def __init__(self):
        self.calls = []
        self.shrink = {"ok": True, "state": "unbacked", "migrated_tokens": 0}
        self.grow = {"ok": True, "state": "backed", "tail_tokens": 444000}
        self.gateway_down = False
        self.hold = None  # an asyncio.Event the shrink waits for
        self.state = "unbacked"  # SGLang's real tail state, which status reports
        self.held = {"comfy1": 20 * GIB, "comfy0": 20 * GIB}
        self.frees = True  # ComfyUI drops its models after /free

    async def __call__(self, request):
        host, path = request.url.host, request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((host, path, body))
        if host == "gateway" and path == "/_service/kv_tail":
            if self.gateway_down:
                raise httpx.ConnectError("down")
            assert request.headers["authorization"] == "Bearer " + KEY
            if body["action"] == "status":
                return httpx.Response(200, json={"ok": True, "state": self.state})
            if body["action"] == "shrink":
                if self.hold:
                    await self.hold.wait()
                if self.shrink["ok"]:
                    self.state = "unbacked"
                return httpx.Response(200 if self.shrink["ok"] else 409, json=self.shrink)
            if self.grow["ok"]:
                self.state = "backed"
            return httpx.Response(200 if self.grow["ok"] else 409, json=self.grow)
        if host in self.held:
            if path == "/free":
                if self.frees:
                    self.held[host] = GIB // 2
                return httpx.Response(200, json={})
            if path == "/system_stats":
                return httpx.Response(200, json={"devices": [{"torch_vram_total": self.held[host]}]})
        raise AssertionError(f"unexpected route {host}{path}")

    def actions(self):
        return [(host, path, (body or {}).get("action")) for host, path, body in self.calls]


class KvTailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.mapping = Path(directory.name) / "mapping.json"
        self.mapping.write_text(json.dumps({"service_key": KEY, "users": {}}))
        self.fixture = Fixture()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.fixture))
        self.addAsyncCleanup(self.client.aclose)
        self.tail = KvTail(self.client, "http://gateway", self.mapping, ("http://comfy1", "http://comfy0", "http://comfy0"),
                           idle_seconds=0.05, shrink_wait=1, free_settle=0.2)
        self.tail.poll_seconds = 0.01

    async def settle(self):
        await asyncio.sleep(0.3)

    async def test_every_job_asks_sglang_to_release_the_tail(self):
        async with self.tail.media():
            self.assertEqual([("gateway", "/_service/kv_tail", "shrink")], self.fixture.actions())
        async with self.tail.media():
            pass
        # Never trusted from memory: SGLang may have been regrown or restarted in between.
        self.assertEqual(2, sum(a == "shrink" for _, _, a in self.fixture.actions()))
        self.assertFalse(self.tail.backed)

    async def test_a_tail_grown_behind_our_back_is_still_released_before_the_job(self):
        self.tail.backed = False  # this process believes the tail is released
        self.fixture.state = "backed"  # but something grew it
        async with self.tail.media():
            self.assertEqual("unbacked", self.fixture.state)

    async def test_a_tail_lost_behind_our_back_is_grown_again_after_a_quiet_spell(self):
        async with self.tail.media():
            pass
        await self.settle()
        self.assertTrue(self.tail.backed)
        self.fixture.state = "unbacked"  # SGLang restarted, or a shrink came from elsewhere
        self.fixture.calls.clear()
        await self.settle()
        self.assertEqual("backed", self.fixture.state)
        self.assertIn("grow", [a for _, _, a in self.fixture.actions()])

    async def test_a_backed_tail_is_only_checked_not_regrown(self):
        async with self.tail.media():
            pass
        await self.settle()
        self.fixture.calls.clear()
        await self.settle()  # further quiet spells: status checks only
        actions = [a for _, _, a in self.fixture.actions()]
        self.assertIn("status", actions)
        self.assertNotIn("grow", actions)
        self.assertFalse([path for _, path, _ in self.fixture.calls if path == "/free"])

    async def test_busy_or_unconfirmed_release_refuses_before_the_job(self):
        for answer, code in (({"ok": False, "reason": "busy"}, "chat_memory_busy"),
                             ({"ok": False, "reason": "release_unconfirmed"}, "chat_memory_unconfirmed"),
                             ({"ok": False, "reason": "cannot_migrate"}, "chat_memory_unconfirmed")):
            self.fixture.shrink = answer
            ran = False
            with self.assertRaises(Refusal) as refused:
                async with self.tail.media():
                    ran = True
            self.assertEqual((503, code), (refused.exception.status, refused.exception.code))
            self.assertFalse(ran)
            self.assertEqual(0, self.tail.jobs)

    async def test_unreachable_gateway_never_assumes_the_memory_is_free(self):
        self.fixture.gateway_down = True
        with self.assertRaises(Refusal) as refused:
            async with self.tail.media():
                self.fail("job ran without a confirmed release")
        self.assertEqual("chat_memory_unconfirmed", refused.exception.code)
        self.assertIsNone(self.tail.backed)

    async def test_sglang_without_the_tail_lets_media_run(self):
        self.fixture.shrink = {"ok": False, "reason": "disabled"}
        async with self.tail.media():
            pass
        self.assertFalse(self.tail.backed)

    async def test_media_waits_at_most_the_shrink_bound(self):
        self.fixture.hold = asyncio.Event()  # SGLang never answers
        self.tail.shrink_wait = 0.1
        started = asyncio.get_running_loop().time()
        with self.assertRaises(Refusal) as refused:
            async with self.tail.media():
                self.fail("job ran without a confirmed release")
        self.assertEqual("chat_memory_busy", refused.exception.code)
        self.assertLess(asyncio.get_running_loop().time() - started, 0.1 + 5 + 0.5)

    async def test_quiet_spell_unloads_every_comfyui_once_then_grows(self):
        async with self.tail.media():
            pass
        await self.settle()
        actions = self.fixture.actions()
        frees = [host for host, path, _ in actions if path == "/free"]
        self.assertEqual(["comfy1", "comfy0"], frees)  # each ComfyUI once, video's included
        kinds = [k for _, _, k in actions if k]
        first_grow = kinds.index("grow")
        self.assertEqual({"status"}, set(kinds[first_grow + 1:]) or {"status"})  # afterwards only re-checks
        self.assertTrue(self.tail.backed)
        # The next picture releases it again before running.
        async with self.tail.media():
            self.assertEqual("shrink", self.fixture.actions()[-1][2])
        self.assertFalse(self.tail.backed)

    async def test_no_grow_while_media_runs_or_after_a_newer_job(self):
        async with self.tail.media():
            await self.settle()  # longer than idle_seconds, but a job is running
            self.assertNotIn("grow", [a for _, _, a in self.fixture.actions()])
        self.tail.idle_seconds = 0.2
        async with self.tail.media():
            pass
        await asyncio.sleep(0.1)
        async with self.tail.media():  # restarts the quiet-spell clock
            pass
        await asyncio.sleep(0.15)
        self.assertNotIn("grow", [a for _, _, a in self.fixture.actions()])
        await asyncio.sleep(0.2)
        self.assertEqual(1, [a for _, _, a in self.fixture.actions()].count("grow"))

    async def test_comfyui_that_keeps_its_memory_blocks_the_grow(self):
        self.fixture.frees = False
        async with self.tail.media():
            pass
        await self.settle()
        self.assertNotIn("grow", [a for _, _, a in self.fixture.actions()])
        self.assertEqual({"op": "free", "outcome": "not_released"}, {k: self.tail.last[k] for k in ("op", "outcome")})
        self.assertFalse(self.tail.backed)

    async def test_failed_grow_is_reported_and_retried_later(self):
        self.fixture.grow = {"ok": False, "reason": "insufficient_vram"}
        async with self.tail.media():
            pass
        await self.settle()
        grows = [a for _, _, a in self.fixture.actions()].count("grow")
        self.assertGreaterEqual(grows, 2)  # retried every quiet spell
        self.assertFalse(self.tail.backed)
        self.assertIn('workstation_assistant_kv_tail_operations_total{op="grow",outcome="insufficient_vram"}',
                      METRICS.render())


class GatewayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "mapping.json").write_text(json.dumps({"service_key": KEY, "users": {}}))
        (root / "control").write_text("c" * 32)
        self.sent = []
        self.status, self.answer = 200, {"ok": True, "state": "unbacked"}

        async def sglang(request):
            self.sent.append((request.url.path, json.loads(request.content)))
            if self.status == 404:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(self.status, json=self.answer)

        original = boundary.sglang_client
        boundary.sglang_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(sglang))
        self.addCleanup(setattr, boundary, "sglang_client", original)

        async def litellm(scope, receive, send):
            raise AssertionError("kv_tail must not reach LiteLLM")

        self.gateway = GatewayBoundary(litellm, root / "mapping.json", root / "control")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.gateway), base_url="http://gateway")
        self.addAsyncCleanup(self.client.aclose)

    async def post(self, body, key=KEY):
        return await self.client.post("/_service/kv_tail", json=body, headers={"authorization": "Bearer " + key})

    async def test_only_the_service_key_and_three_actions_pass(self):
        self.assertEqual(401, (await self.post({"action": "status"}, key="x" * 32)).status_code)
        self.assertEqual(401, (await self.client.get("/_service/kv_tail")).status_code)
        for body in ({"action": "release_memory_occupation"}, {"action": "grow", "tail_tokens": 10},
                     {"action": "shrink", "timeout_s": 600}, {"action": "status", "timeout_s": 5}, ["shrink"]):
            self.assertEqual(400, (await self.post(body)).status_code, body)
        self.assertEqual([], self.sent)

    async def test_shrink_forwards_bounded_waits_and_returns_sglangs_answer(self):
        response = await self.post({"action": "shrink", "timeout_s": 15})
        self.assertEqual((200, {"ok": True, "state": "unbacked"}), (response.status_code, response.json()))
        self.assertEqual([("/kv_tail", {"action": "shrink", "release_timeout_s": 10})], self.sent)
        self.status, self.answer = 409, {"ok": False, "reason": "busy"}
        response = await self.post({"action": "shrink"})
        self.assertEqual((409, "busy"), (response.status_code, response.json()["reason"]))

    async def test_sglang_without_the_route_reads_as_disabled(self):
        self.status = 404
        response = await self.post({"action": "shrink"})
        self.assertEqual((200, {"ok": False, "reason": "disabled"}), (response.status_code, response.json()))

    async def test_malformed_sglang_answers_are_unavailable(self):
        self.status, self.answer = 500, {"detail": "boom"}
        self.assertEqual(503, (await self.post({"action": "grow"})).status_code)
        self.status, self.answer = 200, {"state": "backed"}  # no ok field
        self.assertEqual(503, (await self.post({"action": "grow"})).status_code)


if __name__ == "__main__":
    unittest.main()
