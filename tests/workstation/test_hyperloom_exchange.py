"""File-exchange failure tests with an inert controller; no GPU imports."""

import asyncio
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_exchange as exchange


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.requests, self.replies = self.root / "requests", self.root / "replies"
        self.requests.mkdir()
        self.replies.mkdir()
        self.source = self.root / "candidate.py"
        self.source.write_text("answer = 42\n")
        self.identities = {"campaign_sha256": "a" * 64, "selection_sha256": "b" * 64}
        self.controller = "c" * 64

    def submit(self, **kwargs):
        args = dict(self.identities, kind="serving", config={"framework": "sglang"},
                    sources={"candidate.py": self.source}, caller_seconds=180,
                    campaign_deadline_unix_ms=2000000, now=1000)
        args.update(kwargs)
        return exchange.submit(self.requests, **args)

    def claim(self, job, **kwargs):
        args = dict(self.identities, controller_sha256=self.controller, now=1001)
        args.update(kwargs)
        return exchange.claim(job, self.replies, **args)

    def succeed(self, job):
        request, config, reply = self.claim(job)
        result = exchange.publish_result(reply, request, status="SUCCEEDED",
            payload={"fixture": True}, owner_seal_sha256="d" * 64, independent_release=True)
        return request, reply, result

    def test_request_claim_result_roundtrip(self):
        job, request = self.submit()
        checked, config = exchange.inspect(job, **self.identities)
        self.assertEqual(checked, request)
        self.assertEqual(config, {"framework": "sglang"})
        _, reply, expected = self.succeed(job)
        self.assertEqual(exchange.read_result(reply, request, controller_sha256=self.controller), expected)
        self.assertFalse(expected["adoption_authorized"])
        self.assertFalse(expected["automatic_retry"])
        self.assertFalse((job / "RESULT.json").exists())

    def test_claim_is_single_use(self):
        job, _ = self.submit()
        self.claim(job)
        with self.assertRaises(FileExistsError):
            self.claim(job)

    def test_source_change_and_extra_member_refused(self):
        job, _ = self.submit()
        (job / "sources/candidate.py").write_text("changed = True\n")
        with self.assertRaises(ValueError):
            self.claim(job)
        job, _ = self.submit()
        (job / "sources/extra.py").write_text("pass\n")
        with self.assertRaises(ValueError):
            self.claim(job)

    def test_configuration_change_refused(self):
        job, _ = self.submit()
        (job / "CONFIG.json").write_text('{"framework":"different"}\n')
        with self.assertRaises(ValueError):
            self.claim(job)

    def test_wrong_campaign_or_selection_refused(self):
        job, _ = self.submit()
        for key in self.identities:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.claim(job, **{key: "f" * 64})

    def test_cancellation_before_claim(self):
        job, _ = self.submit()
        (job / "CANCELLED.json").write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.claim(job)
        self.assertFalse((self.replies / job.name).exists())

    def test_late_admission_or_backward_clock_refused(self):
        for now in (999, 1061):
            job, _ = self.submit()
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.claim(job, now=now)

    def test_full_envelope_must_fit_campaign(self):
        for ceiling in (1239000, 1240000, 1560000, 1679000):
            with self.subTest(ceiling=ceiling), self.assertRaises(ValueError):
                self.submit(campaign_deadline_unix_ms=ceiling)
        self.assertEqual(list(self.requests.iterdir()), [])
        _, request = self.submit(campaign_deadline_unix_ms=1680000)
        self.assertEqual(request["post_release_seconds"], 440)

    def test_component_reserves_cpu_preparation_and_retained_replay(self):
        with self.assertRaises(ValueError):
            self.submit(kind="component", campaign_deadline_unix_ms=1240000)
        _, request = self.submit(kind="component", campaign_deadline_unix_ms=1900000)
        self.assertEqual(request["post_release_seconds"], 120)
        self.assertEqual((request["expires_unix_ms"]-request["created_unix_ms"])//1000,600+180+120)

    def test_unreleased_success_refused_but_retained_failure_allowed(self):
        job, request = self.submit()
        _, _, reply = self.claim(job)
        with self.assertRaises(ValueError):
            exchange.publish_result(reply, request, status="SUCCEEDED", payload={},
                                    owner_seal_sha256="d" * 64, independent_release=False)
        result = exchange.publish_result(reply, request, status="FAILED", payload={"reason": "fixture"})
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["independent_release"])

    def test_foreign_or_modified_result_refused(self):
        job, request = self.submit()
        _, reply, result = self.succeed(job)
        path = reply / "RESULT.json"
        for key in ("request_sha256", "source_set_sha256", "controller_sha256", "campaign_sha256"):
            changed = dict(result, **{key: "f" * 64})
            path.write_bytes(exchange.canonical(changed))
            with self.subTest(key=key), self.assertRaises(ValueError):
                exchange.read_result(reply, request, controller_sha256=self.controller)

    def test_result_cannot_be_overwritten(self):
        job, request = self.submit()
        _, reply, _ = self.succeed(job)
        before = (reply / "RESULT.json").read_bytes()
        with self.assertRaises(ValueError):
            exchange.publish_result(reply, request, status="FAILED", payload={})
        self.assertEqual((reply / "RESULT.json").read_bytes(), before)

    def test_agent_written_result_in_outbox_is_ignored_and_timeout_retained(self):
        job, request = self.submit()
        (job / "RESULT.json").write_text('{"status":"SUCCEEDED"}\n')
        with self.assertRaises(TimeoutError):
            exchange.wait_result(job, self.replies, request, controller_sha256=self.controller)
        marker = exchange.decoded_json((job / "CANCELLED.json").read_bytes())
        self.assertFalse(marker["owner_release_claimed"])
        with self.assertRaises(ValueError):
            self.claim(job)

    def test_published_result_wait_uses_real_reply_tree(self):
        now = time.time()
        job, request = self.submit(now=now, campaign_deadline_unix_ms=int((now + 1000) * 1000))
        _, _, reply = self.claim(job, now=now + .1)
        exchange.publish_result(reply, request, status="UNSUPPORTED", payload={"reason": "inert fixture"})
        result = exchange.wait_result(job, self.replies, request, controller_sha256=self.controller)
        self.assertEqual(result["status"], "UNSUPPORTED")
        self.assertFalse((job / "CANCELLED.json").exists())

    def test_request_and_reply_trees_cannot_alias(self):
        job, request = self.submit()
        with self.assertRaises(ValueError):
            exchange.claim(job, self.requests, **self.identities, controller_sha256=self.controller, now=1001)
        with self.assertRaises(ValueError):
            exchange.wait_result(job, self.requests, request, controller_sha256=self.controller)

    def test_symlink_source_and_reply_are_refused(self):
        link = self.root / "link.py"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            self.submit(sources={"candidate.py": link})
        job, request = self.submit()
        _, reply, _ = self.succeed(job)
        link = self.root / "reply-link"
        link.symlink_to(reply, target_is_directory=True)
        with self.assertRaises(ValueError):
            exchange.read_result(link, request, controller_sha256=self.controller)

    def test_interrupt_retains_cancel_without_claiming_release(self):
        now = time.time()
        job, request = self.submit(now=now, campaign_deadline_unix_ms=int((now + 1000) * 1000))
        with mock.patch.object(exchange.time, "sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            exchange.wait_result(job, self.replies, request, controller_sha256=self.controller)
        self.assertTrue((job / "CANCELLED.json").exists())

    def test_async_result_uses_the_authenticated_reply_tree(self):
        now = time.time()
        job, request = self.submit(now=now, campaign_deadline_unix_ms=int((now + 1000) * 1000))
        _, _, reply = self.claim(job, now=now + .1)
        expected = exchange.publish_result(reply, request, status="UNSUPPORTED", payload={"fixture": True})
        result = asyncio.run(exchange.wait_result_async(job, self.replies, request,
                                                       controller_sha256=self.controller))
        self.assertEqual(result, expected)
        self.assertFalse((job / "CANCELLED.json").exists())

    def test_async_cancel_joins_wait_and_never_claims_owner_release(self):
        now = time.time()
        job, request = self.submit(now=now, campaign_deadline_unix_ms=int((now + 1000) * 1000))

        async def cancel():
            task = asyncio.create_task(exchange.wait_result_async(job, self.replies, request,
                                                                 controller_sha256=self.controller))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.done())

        asyncio.run(cancel())
        marker = exchange.decoded_json((job / "CANCELLED.json").read_bytes())
        self.assertEqual(marker["reason"], "CancelledError")
        self.assertFalse(marker["owner_release_claimed"])
        self.assertFalse((self.replies / job.name).exists())
        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.claim(job, now=now + .1)

    def test_async_expired_request_keeps_failure_without_retry(self):
        job, request = self.submit()
        with self.assertRaises(TimeoutError):
            asyncio.run(exchange.wait_result_async(job, self.replies, request,
                                                  controller_sha256=self.controller))
        self.assertEqual(len(list(self.requests.iterdir())), 1)
        self.assertFalse(exchange.decoded_json((job / "CANCELLED.json").read_bytes())["owner_release_claimed"])


if __name__ == "__main__":
    unittest.main()
