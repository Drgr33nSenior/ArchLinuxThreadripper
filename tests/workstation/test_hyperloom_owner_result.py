"""HTTP/native joins and latency projections with inert retained-format fixtures."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
import hyperloom_owner_result as projection
import hyperloom_workload as menu
from test_hyperloom_latency import POLICY
from test_hyperloom_workload import donor


def fixture():
    derived = menu.derive(donor())
    workload = dict(schema=1, cases=derived["cases"], groups=derived["groups"], requests=30,
                    samples_per_case=3, diagnostic_only=False)
    bound = dict(campaign_sha256="a" * 64, selection_sha256="b" * 64, runtime_sha256="c" * 64,
                 workload_sha256=exchange.digest(exchange.canonical(workload)),
                 selected_source_set_sha256="d" * 64, benchmark={"model": "fixture"}, latency_policy=POLICY)
    request = dict(id="0" * 32, kind="serving", campaign_sha256=bound["campaign_sha256"],
                   selection_sha256=bound["selection_sha256"], source_set_sha256="e" * 64)
    decoded = {"answers": []}
    quality = dict(status="NATIVE_CPU_FINAL_QUALITY_AND_RELEASE_VALIDATED", independent_release_verified=True,
                   quality_pass=True, long_answer_coverage_pass=True, decoded_rows=60, natural_rows=60,
                   ungraded_cap_work_rows=0, state_only_cancellations=0, qualification=False, selected=False,
                   grades=[])
    summaries = {}
    for physical, logical in projection.ARMS:
        rows = []
        for i, source in enumerate(derived["rows"]):
            raw_response = exchange.digest((physical + str(i)).encode())
            canonical_response = exchange.digest(("canonical" + physical + str(i)).encode())
            ids_sha = exchange.digest(("ids" + physical + str(i)).encode())
            segments = dict(reasoning_token_count=20, final_answer_token_count=79, closing_marker_index=20)
            contract = dict(case=source["case"], request_sha256=source["sha256"],
                raw_http_response_sha256=raw_response, raw_response_sha256=canonical_response,
                response_token_ids_sha256=ids_sha, status="NORMAL_STOP_COMPLETE", finish_reason="stop",
                metric_eligible=True, response_id_count=100, reported_completion_tokens=100,
                client_elapsed_seconds=(10 if logical == "control" else 9), prompt_token_count=4,
                thinking_segments=segments)
            rows.append(dict(ordinal=i, case=source["case"], body_sha256=source["sha256"], contract=contract))
            decoded["answers"].append(dict(arm=physical, ordinal=i, case=source["case"],
                request_body_sha256=source["sha256"], response_raw_sha256=raw_response,
                response_canonical_sha256=canonical_response, output_ids_sha256=ids_sha, output_tokens=100,
                final_status="FINAL_SEGMENT_DECODED", finish_reason="stop", wire_decode_matches=True,
                error=None, **segments))
            quality["grades"].append(dict(arm=physical, ordinal=i, case=source["case"], status="QUALITY_PASS",
                quality_passed=True, long_answer_coverage_passed=True, final_answer_token_count=79))
        groups = []
        for group in workload["groups"]:
            times = [rows[i]["contract"]["client_elapsed_seconds"] for i in group["ordinals"]]
            groups.append(dict(group_id=group["id"], ordinals=list(group["ordinals"]), kind=group["kind"],
                concurrency=group["concurrency"], measured=group["kind"] == "timing",
                excluded_warmup=group["kind"] == "warmup", per_request_latency_seconds=times,
                completion_tokens=100 * group["concurrency"], makespan_seconds=max(times) + .01))
        summaries[physical] = dict(stopped_early=False, paired_error=None, executed_case_count=30,
                                   frozen_case_count=30, rows=rows, groups=groups)
    return dict(summaries=summaries, decoded=decoded, quality=quality, quality_sha256="f" * 64,
                workload=workload, request=request, binding=bound, owner_seal_sha256="1" * 64,
                observation_id="2" * 16)


class OwnerResultTests(unittest.TestCase):
    def setUp(self):
        self.value = fixture()

    def report(self):
        return projection.serving_report(**self.value)

    def test_real_menu_projects_paired_native_counts_and_physical_throughput(self):
        report = self.report()
        observed, control = report["workstation_latency"], report["workstation_latency_control"]
        self.assertEqual(len(observed["cases"]), 6)
        self.assertEqual(observed["arm"], "mtp")
        self.assertEqual(control["arm"], "control")
        self.assertEqual(report["paired_comparison"]["verdict"], "KEEP")
        self.assertAlmostEqual(report["throughput"]["output_throughput"], 2700 / (18 * 9.01))
        self.assertEqual(report["throughput"]["completed_requests"], 27)
        sample = observed["cases"][2]["samples"][0]
        self.assertEqual(sample["request_e2e_ms"], [9000, 9000])
        self.assertEqual(sample["reasoning_tokens"], [20, 20])
        self.assertEqual(sample["final_tokens"], [79, 79])
        self.assertFalse(report["adoption_authorized"])
        envelope = dict(status="SUCCEEDED", independent_release=True, payload={"benchmark_report": report},
            owner_seal_sha256=self.value["owner_seal_sha256"],
            request_sha256=exchange.digest(exchange.canonical(self.value["request"])))
        self.assertEqual(backend.validate_report(envelope, self.value["request"], self.value["binding"]), report)

    def test_profile_partial_wrong_workload_or_foreign_campaign_refused(self):
        mutations = (
            lambda v: v["request"].update(kind="profile"),
            lambda v: v["request"].update(campaign_sha256="9" * 64),
            lambda v: v["workload"].update(diagnostic_only=True),
            lambda v: v["workload"].update(samples_per_case=2),
            lambda v: v["summaries"]["bf16"].update(stopped_early=True),
            lambda v: v["summaries"]["kv4"]["rows"].pop(),
        )
        for change in mutations:
            self.value = fixture()
            change(self.value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.report()

    def test_quality_failure_or_ungraded_answer_never_becomes_a_timing(self):
        for field in ("quality_pass", "long_answer_coverage_pass", "independent_release_verified"):
            self.value = fixture()
            self.value["quality"][field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report()
        for field, value in (("status", "QUALITY_REFUSAL"), ("quality_passed", False),
                             ("long_answer_coverage_passed", False), ("arm", "kv4")):
            self.value = fixture()
            self.value["quality"]["grades"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report()

    def test_native_answer_from_another_response_request_or_arm_refused(self):
        for field in ("request_body_sha256", "response_raw_sha256", "response_canonical_sha256",
                      "output_ids_sha256", "arm"):
            self.value = fixture()
            self.value["decoded"]["answers"][33][field] = "0" * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report()

    def test_server_metadata_cannot_replace_client_wall_time(self):
        self.value["summaries"]["kv4"]["rows"][3]["contract"]["terminal_meta_info"] = {
            "e2e_latency": .000001, "decode_throughput": 999999999, "reasoning_tokens": 0}
        report = self.report()
        sample = report["workstation_latency"]["cases"][0]["samples"][0]
        self.assertEqual(sample["request_e2e_ms"], [9000])
        self.assertEqual(sample["reasoning_tokens"], [20])

    def test_pair_order_group_coverage_and_token_totals_are_bound(self):
        mutations = (
            lambda v: v["summaries"]["kv4"]["groups"][4]["ordinals"].reverse(),
            lambda v: v["summaries"]["kv4"]["groups"][4].update(concurrency=1),
            lambda v: v["summaries"]["kv4"]["groups"][4].update(measured=False),
            lambda v: v["summaries"]["kv4"]["groups"][4].update(completion_tokens=10),
            lambda v: v["summaries"]["kv4"]["groups"][4].update(per_request_latency_seconds=[.1, .1]),
            lambda v: v["summaries"]["kv4"]["groups"].pop(),
            lambda v: v["decoded"]["answers"][33].update(output_tokens=1),
        )
        for change in mutations:
            self.value = copy.deepcopy(fixture())
            change(self.value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.report()


if __name__ == "__main__":
    unittest.main()
