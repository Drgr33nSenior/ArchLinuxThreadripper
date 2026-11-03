"""CPU contract tests. Native upstream KEEP-path qualification is separate."""

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_sdk_adapter as adapter
import hyperloom_latency as latency
from test_hyperloom_latency import POLICY, record, paired_record


@dataclass
class ComparisonFixture:
    objective: str
    candidate: float
    reference: float
    verdict: str
    refused_by: str = ""
    degrade_reason: str = ""
    veto_reason: str = ""


class AdapterTests(unittest.TestCase):
    def setUp(self):
        fixture = ModuleType("hyperloom.common.perf_metric")
        fixture.GradedComparison = ComparisonFixture
        fixture.constraint_veto_reason = lambda measurement, state: getattr(state, "fixture_veto", "")
        self.patch = mock.patch.dict(sys.modules, {"hyperloom.common.perf_metric": fixture})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.baseline = dict(paired_record("d"), output_throughput=100)
        self.state = SimpleNamespace(grading={"objective": latency.OBJECTIVE,
                                              "policy": dict(POLICY), "selection_sha256": "a" * 64},
                                     baseline_perf=self.baseline, current_best=None)

    def test_balanced_grade_keeps_units_separate(self):
        candidate = dict(paired_record("e", (.9, .9)), output_throughput=100)
        graded = adapter.grade(self.state, candidate)
        self.assertEqual(graded.verdict, "KEEP")
        self.assertEqual(graded.objective, latency.OBJECTIVE)
        self.assertAlmostEqual(graded.candidate, 1 / .9)
        self.assertEqual(candidate["output_throughput"], 100)

    def test_high_throughput_cannot_rescue_c2_regression(self):
        candidate = dict(paired_record("e", (.5, 1.1)), output_throughput=1000000)
        graded = adapter.grade(self.state, candidate)
        self.assertEqual(graded.verdict, "REVERT")
        self.assertIn("regression", graded.refused_by)

    def test_missing_or_invalid_evidence_never_falls_back(self):
        for candidate in ({"output_throughput": 1000000}, None, {"workstation_latency": {}}):
            graded = adapter.grade(self.state, candidate)
            self.assertEqual(graded.verdict, "REVERT")
            self.assertTrue(graded.degrade_reason)

    def test_broken_current_anchor_does_not_fall_back_to_baseline(self):
        self.state.current_best = {"output_throughput": 999}
        graded = adapter.grade(self.state, paired_record("e", (.8, .8)))
        self.assertEqual(graded.verdict, "REVERT")
        self.assertTrue(graded.degrade_reason)

    def test_identical_measurement_cannot_be_a_new_gain(self):
        graded = adapter.grade(self.state, self.baseline)
        self.assertEqual(graded.verdict, "REVERT")
        self.assertEqual(graded.candidate, 1)
        self.assertEqual(graded.reference, 1)
        self.assertEqual(graded.refused_by, "same_measurement_not_a_new_gain")

    def test_wrong_seeded_selection_refused(self):
        self.state.grading["selection_sha256"] = "f" * 64
        graded = adapter.grade(self.state, paired_record("e", (.8, .8)))
        self.assertEqual(graded.verdict, "REVERT")
        self.assertTrue(graded.degrade_reason)

    def test_upstream_threshold_can_tighten_but_not_lower_policy(self):
        candidate = paired_record("e", (.97, .97))
        self.assertEqual(adapter.grade(self.state, candidate, keep_threshold_pct=0).verdict, "KEEP")
        self.assertEqual(adapter.grade(self.state, candidate, keep_threshold_pct=5).verdict, "REVERT")
        self.assertTrue(adapter.grade(self.state, candidate, keep_threshold_pct=float("nan")).degrade_reason)

    def test_snapshot_carries_evidence_without_aliasing(self):
        saved = adapter.snapshot(self.baseline)
        self.assertEqual(saved, self.baseline)
        saved["workstation_latency"]["cases"].clear()
        self.assertTrue(self.baseline["workstation_latency"]["cases"])
        self.assertIsNone(adapter.snapshot({"output_throughput": 999}))

    def test_existing_constraint_veto_is_preserved(self):
        self.state.fixture_veto = "power_budget_exceeded"
        graded = adapter.grade(self.state, paired_record("e", (.8, .8)))
        self.assertEqual(graded.verdict, "REVERT")
        self.assertEqual(graded.veto_reason, "power_budget_exceeded")

    def test_missing_paired_control_cannot_keep(self):
        graded = adapter.grade(self.state, {"workstation_latency": record("e", (.8, .8))})
        self.assertEqual(graded.verdict, "REVERT")
        self.assertTrue(graded.degrade_reason)

    def test_environment_drift_is_reported_separately(self):
        graded = adapter.grade(self.state, paired_record("e", (1.05, 1.05), (1.08, 1.08)))
        self.assertEqual(graded.verdict, "REVERT")
        self.assertEqual(graded.degrade_reason, "environment_drift")

    def test_length_confounded_gain_is_not_kept(self):
        candidate = paired_record("e", (.9, .9))
        for sample in candidate["workstation_latency"]["cases"][0]["samples"]:
            sample["completion_tokens"][0] += 57
            sample["reasoning_tokens"][0] += 57
        graded = adapter.grade(self.state, candidate)
        self.assertEqual(graded.verdict, "REVERT")
        self.assertEqual(graded.degrade_reason, "unequal_generated_work")


if __name__ == "__main__":
    unittest.main()
