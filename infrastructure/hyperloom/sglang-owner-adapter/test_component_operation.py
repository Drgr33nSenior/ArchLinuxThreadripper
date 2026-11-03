"""Component controller boundary tests with a genuinely CPU-tested preparation.

The preparation is read-only. Tests replace the GPU caller and replay boundary
with explicit fixtures and use temporary checkpoints. No GPU command is run.
"""

import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import component_operation as component
import owner_operation as op
from hyperloom_exchange import canonical, digest

FIXTURE = op.R2 / "hyperloom-sglang-implementation-20261102-v1/component-operation-cpu-v1"


class CallerFixture:
    def __init__(self, root, prop, *, failure=False):
        self.root, self.prop, self.failure = root, prop, failure
        self.calls = []
        self.summary = dict(component_complete=True, independent_release_verified=True,
            fixture=True, usefulness=[dict(card=card, candidate_status="CORRECT",
                timing=dict(useful=False, zero_prefix_adoption_gate=False)) for card in (0, 1)])

    def run(self, name, argv, seconds, **kwargs):
        self.calls.append((name, argv, seconds, kwargs))
        if self.failure:
            raise RuntimeError("synthetic owner transport failure")
        assert name == "caller-once"
        assert seconds == 2180
        native = Path(self.prop["native_root"])
        (native / "results").mkdir(parents=True)
        verified = component.verified(self.prop["preparation"], json.loads((FIXTURE / "BINDING.json").read_bytes()))
        package = verified["context"]["package"]
        op._write(native / "OPERATOR-RESULT.json", dict(accepted=True, owner_exit_code=0, command_failures=[], fixture=True))
        op._write(native / "physical-owner.json", dict(elapsed_seconds=.25, fixture=True))
        op._write(native / "results/RESULT-SUMMARY.json", self.summary)
        op._write(native / "results/RETAINED-SEALS.json", {label: dict(root=package[key], fixture=True)
                  for label, key in (("workload", "remote_root"), ("protected", "protected_root"))})
        return dict(elapsed_seconds=.5, exit_code=0, child_reaped=True, owned_group_absent=True, fixture=True)

    def replay(self, campaign, runner, verified, native, deadline):
        assert self.calls and self.calls[-1][0] == "caller-once"
        return self.summary, dict(fixture=True, external_execution=False)


class ComponentOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hyperloom-component-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bound = json.loads((FIXTURE / "BINDING.json").read_bytes())
        result = json.loads((FIXTURE / "PREPARATION-RESULT.json").read_bytes())
        self.preparation = result["record"]
        request = json.loads((FIXTURE / "snapshot/REQUEST.json").read_bytes())
        self.now = request["created_unix_ms"] / 1000 + 120
        for name in ("checkpoints", "runs", "campaigns"):
            (self.root / name).mkdir()
        op._write(self.root / "checkpoints/CHECKPOINT-1.json", dict(schema=1, active_owner=None, active_caller=None,
            current_global_attempt_index=None, independent_release_verified=True, last_completed_global_attempt_index=190,
            cumulative_owner_seconds=1.0, cumulative_caller_seconds=2.0))

    def propose(self):
        self.prop = component.propose(self.preparation, self.bound, campaign_parent=self.root / "campaigns",
            checkpoint_root=self.root / "checkpoints", mx_root=self.root / "runs")
        return self.prop

    def authority(self):
        prop = self.prop
        binding = component.verified(self.preparation, self.bound)["binding"]
        approval = op._write(self.root / "APPROVAL.json", dict(fixture=True, GPU=False))
        return op._write(self.root / "AUTHORITY.json", dict(schema=1, kind="component", campaign=prop["campaign_root"],
            proposal_sha256=prop["proposal"]["sha256"], execute_once_sha256=prop["execute_once"]["sha256"],
            nonce=prop["nonce"], global_gpu_attempt_index=prop["index"], request_sha256=binding["request_sha256"],
            source_set_sha256=binding["source_set_sha256"], candidate_sha256=binding["candidate_sha256"],
            campaign_sha256=self.bound["campaign_sha256"], controller_sha256=self.bound["controller_sha256"],
            owner="CPU test fixture", owner_answer="Synthetic authority only; the GPU boundary is substituted.", approval=approval,
            no_retry=True, one_invocation=True, deployment=False, promotion=False, publication=False,
            adoption_authorized=False, qualification=False, utc=op._utc(self.now)))

    def execute(self, caller=None, *, cancelled=lambda: False, replay=None, authorization=None):
        caller = caller or CallerFixture(self.root, self.prop)
        self.caller = caller
        with patch.object(component.time, "time", return_value=self.now):
            return component.execute(self.prop["record"], authorization or self.authority(), self.bound,
                checkpoint_root=self.root / "checkpoints", cancelled=cancelled, commands_module=caller,
                replay=replay or caller.replay)

    def test_verified_native_preparation_and_proposal_are_inert(self):
        verified = component.verified(self.preparation, self.bound)
        context = verified["context"]
        self.assertEqual(context["native_component_source_gate"]["component_tests"], 37)
        self.assertEqual(context["native_component_source_gate"]["owner_tests"], 10)
        result = self.propose()
        self.assertEqual(result["index"], 191)
        self.assertFalse(Path(result["native_root"]).exists())
        self.assertFalse((Path(result["campaign_root"]) / "REGISTRATION.json").exists())
        self.assertEqual(len(list((self.root / "checkpoints").iterdir())), 1)
        caller = Path(result["execute_once"]["path"]).read_text()
        self.assertIn("--registration-sha256", caller)
        self.assertIn(verified["record"]["context"]["sha256"], caller)

    def test_negative_measured_candidate_is_completed_diagnostic_without_serving_admission(self):
        self.propose()
        result = self.execute()
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertTrue(result["independent_release"])
        self.assertFalse(result["payload"]["component_report"]["eligible_for_serving"])
        self.assertNotIn("benchmark_report", result["payload"])
        self.assertEqual(len(self.caller.calls), 1)
        state = op._checkpoint(self.root / "checkpoints")["state"]
        self.assertIsNone(state["active_owner"])
        self.assertEqual(state["last_completed_global_attempt_index"], 191)
        self.assertEqual(state["cumulative_caller_seconds"], 2.5)

    def test_busy_changed_or_unreleased_checkpoint_prevents_the_owner(self):
        self.propose()
        state = op._checkpoint(self.root / "checkpoints")["state"]
        state["active_owner"] = "another fixture owner"
        op._write(self.root / "checkpoints/CHECKPOINT-2.json", state)
        result = self.execute()
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(self.caller.calls, [])
        self.assertFalse(result["registered"])

    def test_cancellation_after_registration_consumes_no_gpu_invocation(self):
        self.propose()
        calls = []
        def cancelled():
            calls.append(True)
            return len(calls) == 2
        result = self.execute(cancelled=cancelled)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.caller.calls, [])
        self.assertFalse(result["owner_started"])
        self.assertEqual(op._checkpoint(self.root / "checkpoints")["state"]["last_completed_global_attempt_index"],190)

    def test_failed_caller_parks_with_unresolved_owner_and_no_replay(self):
        self.propose()
        caller = CallerFixture(self.root, self.prop, failure=True)
        result = self.execute(caller)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["park_campaign"])
        self.assertFalse(result["independent_release"])
        self.assertEqual(op._checkpoint(self.root / "checkpoints")["state"]["current_global_attempt_index"],191)

    def test_replay_failure_cannot_accept_caller_release_strings(self):
        self.propose()
        def replay(*args):raise ValueError("synthetic retained source mismatch")
        result = self.execute(replay=replay)
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["independent_release"])
        self.assertEqual(result["payload"], {})

    def test_timeout_is_retained_and_closes_the_registered_index_as_unresolved(self):
        self.propose()
        def expired(*args):raise subprocess.TimeoutExpired(['fixture-replay'],1)
        result=self.execute(replay=expired)
        self.assertEqual(result['status'],'FAILED')
        self.assertTrue(result['park_campaign'])
        self.assertFalse(result['independent_release'])
        self.assertIn('TimeoutExpired',result['reason'])
        self.assertTrue(Path(result['record']['path']).is_file())

    def test_changed_authority_is_refused_before_registration(self):
        self.propose()
        authority = self.authority()
        value = json.loads(Path(authority["path"]).read_bytes())
        value["candidate_sha256"] = "0" * 64
        bad = op._write(self.root / "BAD-AUTHORITY.json", value)
        result = self.execute(authorization=bad)
        self.assertEqual(result["status"], "REFUSED")
        self.assertFalse(result["registered"])
        self.assertEqual(self.caller.calls, [])

    def test_completed_operation_cannot_run_again(self):
        self.propose()
        authority = self.authority()
        self.assertEqual(self.execute(authorization=authority)["status"], "SUCCEEDED")
        result = self.execute(authorization=authority)
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(self.caller.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
