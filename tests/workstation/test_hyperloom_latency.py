"""CPU checks for balanced latency decisions; no runtime or device imports."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_latency as latency


POLICY = {"minimum_samples": 3, "noise_floor_pct": 1,
          "maximum_spread_pct": 5, "minimum_gain_pct": 2, "maximum_output_token_delta_pct": 1}


def record(arm, scales=(1, 1), *, case_counts=(1, 1)):
    value = {"schema": 1, "kind": "serving", "profiled": False, "complete": True,
             "quality_passed": True, "independent_release": True,
             "selection_sha256": "a" * 64, "runtime_sha256": "b" * 64,
             "workload_sha256": "c" * 64, "candidate_sha256": arm * 64,
             "observation_id": arm * 16, "arm": "mtp",
             "owner_seal_sha256": arm * 64, "quality_sha256": arm * 64, "cases": []}
    for concurrency, count in enumerate(case_counts, 1):
        for case in range(count):
            samples = []
            for repeat in range(3):
                duration = (100 + repeat * 0.1) * scales[concurrency - 1]
                group = f"c{concurrency}-{case}-r{repeat}"
                samples.append({"id": f"{arm * 16}:mtp:{group}", "group_id": group, "warmup": False,
                                "completed": concurrency, "request_e2e_ms": [duration] * concurrency,
                                "group_wall_ms": duration + 0.1, "prompt_tokens": [100] * concurrency,
                                "completion_tokens": [1000] * concurrency, "reasoning_tokens": [400] * concurrency,
                                "final_tokens": [599] * concurrency, "response_sha256": [arm * 64] * concurrency,
                                "task_quality_passed": True})
            value["cases"].append({"id": f"c{concurrency}-{case}", "concurrency": concurrency,
                                   "request_contract_sha256": str(concurrency) * 64, "samples": samples})
    return value


def paired_record(arm, scales=(1, 1), control_scales=(1, 1)):
    candidate, control = record(arm, scales), record(arm, control_scales)
    control["arm"], control["candidate_sha256"] = "control", "f" * 64
    for case in control["cases"]:
        for sample in case["samples"]:
            sample["id"] = arm * 16 + ":control:" + sample["group_id"]
    return {"workstation_latency": candidate, "workstation_latency_control": control}


class LatencyTests(unittest.TestCase):
    def test_keep_is_dimensionless_and_never_adopts(self):
        result = latency.compare(record("d"), record("e", (.9, .9)), POLICY)
        self.assertEqual(result["verdict"], "KEEP")
        self.assertAlmostEqual(result["candidate_score"], 1 / .9)
        self.assertEqual(result["score_unit"], "dimensionless_latency_ratio")
        self.assertNotIn("output_throughput", result)
        self.assertFalse(result["adoption_authorized"])

    def test_c2_regression_vetoes_large_c1_win(self):
        result = latency.compare(record("d"), record("e", (.5, 1.05)), POLICY)
        self.assertGreater(result["gain_pct"], 2)
        self.assertEqual(result["verdict"], "REVERT")
        self.assertIn("c2-0:e2e_ms:regression", result["reasons"])

    def test_group_tail_cannot_hide_behind_average_latency(self):
        candidate = record("e", (.9, .9))
        for sample in candidate["cases"][1]["samples"]:
            sample["request_e2e_ms"] = [50, 110]
            sample["group_wall_ms"] = 110.1
        result = latency.compare(record("d"), candidate, POLICY)
        self.assertEqual(result["verdict"], "REVERT")
        self.assertIn("c2-0:group_wall_ms:regression", result["reasons"])

    def test_concurrency_weights_do_not_depend_on_case_count(self):
        first = latency.compare(record("d"), record("e", (.9, .95)), POLICY)
        second = latency.compare(record("d", case_counts=(4, 1)),
                                 record("e", (.9, .95), case_counts=(4, 1)), POLICY)
        self.assertAlmostEqual(first["candidate_score"], second["candidate_score"])

    def test_small_unresolved_gain_is_not_keep(self):
        result = latency.compare(record("d"), record("e", (.995, .995)), POLICY)
        self.assertEqual(result["verdict"], "REVERT")
        self.assertIn("balanced_gain_not_resolved", result["reasons"])

    def test_noisy_candidate_cannot_inflate_acceptance_band(self):
        candidate = record("e", (.9, .9))
        sample = candidate["cases"][1]["samples"][0]
        sample["request_e2e_ms"] = [150, 150]
        sample["group_wall_ms"] = 151
        result = latency.compare(record("d"), candidate, POLICY)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")

    def test_quality_release_completion_profile_and_component_refused(self):
        for key, value in (("quality_passed", False), ("independent_release", False),
                           ("complete", False), ("profiled", True), ("kind", "component")):
            candidate = record("e")
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)

    def test_fixed_identity_mismatch_refused(self):
        for name in latency.FIXED_IDENTITIES:
            candidate = record("e")
            candidate[name] = "f" * 64
            with self.subTest(name=name), self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)

    def test_case_contract_and_coverage_mismatch_refused(self):
        for mutation in (lambda c: c["cases"].pop(),
                         lambda c: c["cases"][1].update(request_contract_sha256="f" * 64),
                         lambda c: c["cases"][1].update(id="different")):
            candidate = record("e")
            mutation(candidate)
            with self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)

    def test_replayed_sample_and_duplicate_id_refused(self):
        for duplicate in ("d" * 16 + ":mtp:c1-0-r0", "e" * 16 + ":mtp:c2-0-r0"):
            candidate = record("e")
            candidate["cases"][0]["samples"][0]["id"] = duplicate
            with self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)

    def test_bad_numeric_and_incomplete_pair_refused(self):
        for value in (0, -1, float("nan"), float("inf"), True, "100"):
            candidate = record("e")
            candidate["cases"][0]["samples"][0]["request_e2e_ms"] = [value]
            with self.subTest(value=value), self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)
        candidate = record("e")
        candidate["cases"][1]["samples"][0]["completed"] = 1
        with self.assertRaises(ValueError):
            latency.compare(record("d"), candidate, POLICY)

    def test_insufficient_repeats_or_warmup_refused(self):
        candidate = record("e")
        candidate["cases"][0]["samples"].pop()
        with self.assertRaises(ValueError):
            latency.compare(record("d"), candidate, POLICY)

    def test_unequal_repeat_counts_are_not_matched(self):
        candidate = record("e")
        extra = copy.deepcopy(candidate["cases"][0]["samples"][-1])
        extra["id"] += "-extra"
        candidate["cases"][0]["samples"].append(extra)
        with self.assertRaises(ValueError):
            latency.compare(record("d"), candidate, POLICY)
        candidate = record("e")
        candidate["cases"][0]["samples"][0]["warmup"] = True
        with self.assertRaises(ValueError):
            latency.compare(record("d"), candidate, POLICY)

    def test_policy_must_be_explicit_and_finite(self):
        policies = [{}, dict(POLICY, minimum_samples=2), dict(POLICY, noise_floor_pct=True),
                    dict(POLICY, maximum_spread_pct=float("nan")), dict(POLICY, minimum_gain_pct=0)]
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                latency.compare(record("d"), record("e"), policy)

    def test_inputs_are_unchanged(self):
        before, after = record("d"), record("e", (.9, .9))
        saved = copy.deepcopy((before, after, POLICY))
        latency.compare(before, after, POLICY)
        self.assertEqual((before, after, POLICY), saved)

    def test_generated_length_difference_is_inconclusive_even_when_faster(self):
        candidate = record("e", (.8, .8))
        for sample in candidate["cases"][1]["samples"]:
            sample["completion_tokens"][1] += 57
            sample["reasoning_tokens"][1] += 57
        result = latency.compare(record("d"), candidate, POLICY)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertIn("c2-0:unequal_generated_work", result["reasons"])

    def test_missing_token_or_response_evidence_refused(self):
        for key in ("completion_tokens", "reasoning_tokens", "final_tokens", "response_sha256"):
            candidate = record("e")
            del candidate["cases"][0]["samples"][0][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                latency.compare(record("d"), candidate, POLICY)

    def test_sample_group_names_repeat_but_owner_observations_do_not(self):
        before, after = paired_record("d"), paired_record("e", (.9, .9))
        result = latency.paired_comparison(before["workstation_latency"], after["workstation_latency"],
            before["workstation_latency_control"], after["workstation_latency_control"], POLICY)
        self.assertEqual(result["verdict"], "KEEP")
        after["workstation_latency"]["observation_id"] = "d" * 16
        with self.assertRaises(ValueError):
            latency.paired_comparison(before["workstation_latency"], after["workstation_latency"],
                before["workstation_latency_control"], after["workstation_latency_control"], POLICY)

    def test_faster_environment_cannot_hide_paired_regression(self):
        before, after = paired_record("d"), paired_record("e", (.9, .9), (.85, .85))
        result = latency.paired_comparison(before["workstation_latency"], after["workstation_latency"],
            before["workstation_latency_control"], after["workstation_latency_control"], POLICY)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertTrue(any("environment_drift" in r for r in result["reasons"]))
        self.assertEqual(result["paired_control_comparison"]["verdict"], "REVERT")

    def test_slower_environment_is_drift_not_kernel_regression(self):
        before, after = paired_record("d"), paired_record("e", (1.05, 1.05), (1.08, 1.08))
        result = latency.paired_comparison(before["workstation_latency"], after["workstation_latency"],
            before["workstation_latency_control"], after["workstation_latency_control"], POLICY)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertTrue(all("environment_drift" in r for r in result["reasons"]))

    def test_same_job_control_cannot_be_replaced_with_another_job(self):
        before, after = paired_record("d"), paired_record("e", (.9, .9))
        with self.assertRaises(ValueError):
            latency.paired_comparison(before["workstation_latency"], after["workstation_latency"],
                before["workstation_latency_control"], paired_record("a")["workstation_latency_control"], POLICY)


if __name__ == "__main__":
    unittest.main()
