"""CPU-only registrar tests. Every operation execution below is an inert fixture."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify_serving_run as cq
import build_owner
from test_owner_operation import idle_checkpoint
from test_hyperloom_latency import paired_record

op = cq.op
I = op.R2 / "hyperloom-sglang-implementation-20261102-v1"


class FixtureOperation:
    """No subprocess, SSH, GPU, real registration or shared checkpoint write."""
    _idle, _checkpoint, _bound = staticmethod(op._idle), staticmethod(op._checkpoint), staticmethod(op._bound)

    def __init__(self, plan, *, wrong_packet=False, passed=False):
        self.plan, self.wrong_packet, self.passed, self.calls = plan, wrong_packet, passed, []

    def prepare(self, snapshot, bound, **kwargs):
        self.calls.append("prepare")
        self.bound = bound
        self.request = op._json(snapshot / "REQUEST.json")
        pins = dict(self.plan["expected_package_pins"])
        if self.wrong_packet:
            pins["package_sha256"] = "0" * 64
        return dict(status="PREPARED_NOT_REGISTERED_OR_RUN", nonce=self.plan["nonce"], package=pins,
                    caller=dict(sha256=self.plan["expected_caller_sha256"]),
                    caller_plan=dict(sha256=self.plan["expected_caller_plan_sha256"]), record={"fixture": True})

    def propose(self, preparation, bound, **kwargs):
        self.calls.append("propose")
        return dict(status="PROPOSED_NOT_REGISTERED_OR_RUN", campaign_root=str(kwargs["campaign_parent"] / "fixture"),
                    proposal=dict(sha256="a" * 64), execute_once=dict(sha256=self.plan["expected_caller_sha256"]),
                    nonce=self.plan["nonce"], index=191, record={"fixture": True})

    def execute(self, proposal, authorization, bound, **kwargs):
        self.calls.append("execute")
        self.authorization = op._json(Path(authorization["path"]))
        return dict(status="SUCCEEDED" if self.passed else "FAILED", owner_started=True,
                    independent_release=True, grading_performed=self.passed, payload={}, fixture=True)


class RegistrarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.plan = op._json(I / "optimization-continuation-v1/verify-serving-registration-v2/PLAN.json")
        self.plan["execution_root"] = str(self.root / "run")
        self.plan["binding"].update(requests_root=str(self.root / "run/requests"), replies_root=str(self.root / "run/replies"))
        self.plan["checkpoint_root"] = str(self.root / "checkpoints")
        (self.root / "checkpoints").mkdir()
        op._write(self.root / "checkpoints/CHECKPOINT-1.json", idle_checkpoint())
        decision = self.root / "decision.md"
        decision.write_text("CPU TEST FIXTURE ONLY. No GPU authority.\n")
        self.plan["decision_path"] = str(decision)
        self.plan_ref = op._write(self.root / "plan.json", self.plan)
        self.approval = dict(schema=1, kind=cq.KIND, plan_sha256=self.plan_ref["sha256"],
            runtime_sha256=self.plan["binding"]["runtime_sha256"], owner_inputs_sha256=self.plan["owner_inputs"]["sha256"],
            packet_pins=self.plan["expected_package_pins"], seconds=7660, one_invocation=True,
            automatic_retry=False, adoption_authorized=False, optimizer_start_authorized=False,
            owner="CPU fixture", owner_answer="Fixture only; no GPU authority", decision=op._pin(decision))
        self.approval_ref = op._write(self.root / "approval.json", self.approval)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_approval_validation_is_read_only(self):
        inputs = cq.validate(self.plan, self.plan_ref, self.approval)
        self.assertEqual(inputs["frozen_packet"]["pins"], self.plan["expected_package_pins"])
        self.assertFalse((self.root / "run").exists())

    def test_foreign_or_missing_approval_refuses_before_retention(self):
        for key, value in (("runtime_sha256", "0" * 64), ("owner_inputs_sha256", "0" * 64),
                           ("plan_sha256", "0" * 64), ("seconds", 14400), ("one_invocation", False),
                           ("optimizer_start_authorized", True), ("owner_answer", "")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                cq.validate(self.plan, self.plan_ref, dict(self.approval, **{key: value}))
        with self.assertRaises((ValueError, FileNotFoundError)):
            cq.run(self.plan_ref, dict(path=str(self.root / "missing.json"), sha256="0" * 64))
        self.assertFalse((self.root / "run").exists())

    def test_busy_owner_does_not_start_the_clock_or_create_a_run(self):
        p = self.root / "checkpoints/CHECKPOINT-1.json"
        value = op._json(p);value["active_owner"] = "fixture-existing-owner"
        p.write_bytes(op.exchange.canonical(value))
        operation = FixtureOperation(self.plan)
        with self.assertRaises(ValueError):
            cq.run(self.plan_ref, self.approval_ref, operation=operation)
        self.assertEqual(operation.calls, [])
        self.assertFalse((self.root / "run").exists())

    def test_reproduction_mismatch_cannot_propose_or_execute(self):
        operation = FixtureOperation(self.plan, wrong_packet=True)
        result = cq.run(self.plan_ref, self.approval_ref, operation=operation)
        self.assertEqual(result["status"], "FAILED_PARKED")
        self.assertFalse(result["gpu_owner_started"])
        self.assertEqual(operation.calls, ["prepare"])
        self.assertIn("reproduce", result["reason"])

    def test_failure_runs_owner_seam_once_and_parks(self):
        operation = FixtureOperation(self.plan)
        result = cq.run(self.plan_ref, self.approval_ref, operation=operation)
        self.assertEqual(result["status"], "FAILED_PARKED")
        self.assertEqual(operation.calls, ["prepare", "propose", "execute"])
        self.assertTrue(result["gpu_owner_started"])
        self.assertFalse(result["optimizer_started"] or result["original_campaign_extended"])
        self.assertEqual(result["deadline_unix_ms"] - result["started_unix_ms"], 7660000)
        self.assertEqual(operation.request["expires_unix_ms"], result["deadline_unix_ms"])
        self.assertEqual(operation.authorization["approval"], self.approval_ref)
        with self.assertRaises(ValueError):
            cq.run(self.plan_ref, self.approval_ref, operation=operation)
        self.assertEqual(operation.calls.count("execute"), 1)

    def test_success_is_still_parked_and_not_hyperloom_qualification(self):
        operation = FixtureOperation(self.plan, passed=True)
        with patch.object(cq, "ab_report", return_value={"fixture": True}) as compare:
            result = cq.run(self.plan_ref, self.approval_ref, operation=operation)
        self.assertEqual(result["status"], "VERIFY_SERVING_MEASURED_PARKED")
        self.assertEqual(compare.call_count, 1)
        self.assertFalse(result["hyperloom_campaign_qualified"] or result["optimizer_started"])

    def test_negative_and_positive_results_are_retained_comparisons(self):
        policy = self.plan["binding"]["latency_policy"]
        for scales, verdict in (((1, 1), "REVERT"), ((.8, .8), "KEEP"), ((1, 1.1), "REVERT")):
            with self.subTest(scales=scales), patch.object(cq.backend, "validate_report", return_value=paired_record("d", scales)):
                result = cq.ab_report({}, {}, {"latency_policy": policy})
                self.assertEqual(result['verdict'], verdict)
                self.assertFalse(result['adoption_authorized'])

    def test_source_family_and_both_source_sets_are_explicit(self):
        for field, value in (('serving_source_family', 'extend_attention'),
                             ('verify_serving_sha256', '0' * 64),
                             ('selected_source_set_sha256', '0' * 64),
                             ('observed_power_cap_microwatts', 235000000)):
            plan = copy.deepcopy(self.plan)
            plan['binding'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                cq.validate(plan, self.plan_ref, self.approval)
        plan = copy.deepcopy(self.plan)
        plan['candidate_source_set_sha256'] = plan['binding']['selected_source_set_sha256']
        with self.assertRaises(ValueError):
            cq.validate(plan, self.plan_ref, self.approval)

    def test_incomplete_frozen_identity_refuses_before_build(self):
        inputs = op._json(Path(self.plan["owner_inputs"]["path"]))
        for frozen in (None, {}, {"nonce": self.plan["nonce"]}, dict(inputs["frozen_packet"], nonce="bad")):
            with self.subTest(frozen=frozen), self.assertRaises(ValueError):
                build_owner.build(dict(inputs, frozen_packet=frozen), self.root / "invalid")
            self.assertFalse((self.root / "invalid").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
