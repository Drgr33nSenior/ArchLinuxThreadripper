"""Controller snapshot, single claim and cancellation behavior without an owner."""

import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_exchange as exchange
import hyperloom_exchange_host as host
import hyperloom_owner_backend as backend
import test_hyperloom_owner_backend as backend_tests


class ExchangeHostTests(unittest.TestCase):
    setUp = backend_tests.BackendTests.setUp

    def start(self):
        return backend.start(self.config, self.root / "output", self.binding)

    def test_terminal_record_stops_both_waiters_and_future_submissions(self):
        job, request = self.start()
        result=host.dispatch('park',self.binding,result={'reason':'CPU fixture controller stopped'})
        self.assertIn('published_sha256',result)
        with self.assertRaisesRegex(RuntimeError,'campaign parked'):
            exchange.wait_result(job,self.root/'replies',request,controller_sha256=self.binding['controller_sha256'])
        with self.assertRaisesRegex(RuntimeError,'campaign parked'):
            # A parked waiter must raise before its first suspension; this
            # also runs in the native sandbox that forbids new event-loop sockets.
            exchange.wait_result_async(job,self.root/'replies',request,
                controller_sha256=self.binding['controller_sha256']).send(None)
        output=self.root/'next';output.mkdir()
        with self.assertRaisesRegex(RuntimeError,'campaign parked'):
            backend.start(self.config,output,self.binding)
        self.assertEqual(list(output.iterdir()),[])
        with self.assertRaises(ValueError):host.dispatch('park',self.binding,result={'reason':'second attempt'})

    def test_foreign_terminal_record_fails_closed(self):
        host.dispatch('park',self.binding,result={'reason':'CPU fixture controller stopped'})
        with self.assertRaisesRegex(ValueError,'foreign'):
            exchange.require_campaign_open(self.root/'replies',campaign_sha256='0'*64,
                                           controller_sha256=self.binding['controller_sha256'])

    def test_claim_exports_exact_sources_and_snapshot_stays_independent(self):
        job, request = self.start()
        self.assertEqual(host.dispatch("list", self.binding)["jobs"], [request["id"]])
        snapshot = host.dispatch("take", self.binding, job_id=request["id"])
        self.assertEqual(host.dispatch("list", self.binding)["jobs"], [])
        target = self.root / "private-snapshot"
        host.retain_snapshot(snapshot, target, self.binding)
        (job / "sources/candidate.py").write_text("answer = 0\n")
        self.assertEqual((target / "sources/candidate.py").read_text(), "answer = 42\n")
        self.assertTrue(host.dispatch("cancelled", self.binding, job_id=request["id"])["cancelled"])
        with self.assertRaises((ValueError, FileExistsError)):
            host.dispatch("take", self.binding, job_id=request["id"])

    def test_torn_remote_snapshot_is_refused_before_local_write(self):
        _, request = self.start()
        original = host.dispatch("take", self.binding, job_id=request["id"])
        for field, value in (("sources", {"candidate.py": "aW52YWxpZA=="}),
                             ("config", {"benchmark": {}}),
                             ("claim", dict(original["claim"], controller_sha256="0" * 64))):
            snapshot = copy.deepcopy(original)
            snapshot[field] = value
            target = self.root / "rejected"
            with self.subTest(field=field), self.assertRaises(ValueError):
                host.retain_snapshot(snapshot, target, self.binding)
            self.assertFalse(target.exists())

    def test_expired_or_cancelled_job_never_claims(self):
        job, request = self.start()
        with self.assertRaises(ValueError):
            host.dispatch("take", self.binding, job_id=request["id"], now=request["expires_unix_ms"] / 1000)
        self.assertFalse((self.root / "replies" / request["id"]).exists())
        (job / "CANCELLED.json").write_text("{}")
        with self.assertRaises(ValueError):
            host.dispatch("take", self.binding, job_id=request["id"])
        self.assertFalse((self.root / "replies" / request["id"]).exists())

    def test_result_is_bound_to_claim_even_when_agent_deletes_request(self):
        job, request = self.start()
        host.dispatch("take", self.binding, job_id=request["id"])
        local = self.root / "local-replies"
        local.mkdir()
        local_reply = local / request["id"]
        local_reply.mkdir()
        remote_reply = self.root / "replies" / request["id"]
        (local_reply / "CLAIM.json").write_bytes((remote_reply / "CLAIM.json").read_bytes())
        result = exchange.publish_result(local_reply, request, status="CANCELLED",
                                         payload={"reason": "cancelled after owner release"},
                                         owner_seal_sha256="9" * 64, independent_release=True)
        (job / "REQUEST.json").unlink()
        self.assertTrue(host.dispatch("cancelled", self.binding, job_id=request["id"])["cancelled"])
        published = host.dispatch("publish", self.binding, job_id=request["id"], result=result)
        self.assertEqual(published["published_sha256"], exchange.digest(exchange.canonical(result)))
        self.assertEqual(exchange.read_result(remote_reply, request,
            controller_sha256=self.binding["controller_sha256"]), result)
        with self.assertRaises(ValueError):
            host.dispatch("publish", self.binding, job_id=request["id"], result=result)

    def test_cross_job_or_unreleased_success_cannot_publish(self):
        _, request = self.start()
        snapshot = host.dispatch("take", self.binding, job_id=request["id"])
        value = dict(id=request["id"], controller_sha256=self.binding["controller_sha256"],
                     campaign_sha256=self.binding["campaign_sha256"], request_sha256=snapshot["claim"]["request_sha256"],
                     status="SUCCEEDED", adoption_authorized=False, automatic_retry=False,
                     owner_seal_sha256="8" * 64, independent_release=False)
        with self.assertRaises(ValueError):
            host.dispatch("publish", self.binding, job_id=request["id"], result=value)
        value.update(independent_release=True, request_sha256="0" * 64)
        with self.assertRaises(ValueError):
            host.dispatch("publish", self.binding, job_id=request["id"], result=value)

    def test_source_change_between_inspection_and_export_refuses(self):
        job, request = self.start()
        real = exchange.inspect

        def inspect(*args, **kwargs):
            result = real(*args, **kwargs)
            (job / "sources/candidate.py").write_text("changed\n")
            return result

        with mock.patch.object(exchange, "inspect", side_effect=inspect), self.assertRaises(ValueError):
            host.dispatch("take", self.binding, job_id=request["id"])
        self.assertFalse((self.root / "replies" / request["id"]).exists())


if __name__ == "__main__":
    unittest.main()
