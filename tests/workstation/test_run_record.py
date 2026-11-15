"""Offline run-record-v1 fixtures: identity, failures, acceptance and resource joins.

Synthetic records only; no model, gateway, PSU or GPU is contacted.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
sys.path.insert(0, str(ROOT / "apps/base/shared-api"))
import assistant_metrics
import measurement
import run_record as rr

FIXTURES = ROOT / "tests/fixtures/run-record"
SECOND = 10**9


def jsonl(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


class RunRecordTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((FIXTURES / "plan.json").read_text())
        self.attempts = jsonl("attempts.jsonl")
        self.acceptance = jsonl("acceptance.jsonl")
        self.psu = jsonl("psu.jsonl")
        self.gpu = jsonl("gpu.jsonl")

    def report(self, **changes):
        values = dict(plan=self.plan, attempts=self.attempts, acceptances=self.acceptance,
                      psu_samples=self.psu, gpu_samples=self.gpu)
        values.update(changes)
        return rr.report(values["plan"], values["attempts"], values["acceptances"],
                         values["psu_samples"], values["gpu_samples"])

    def task(self, report, task_id):
        return next(task for task in report["tasks"] if task["task_id"] == task_id)

    def attempt(self, attempt_id):
        return next(a for a in self.attempts if a["identity"]["attempt_id"] == attempt_id)

    def test_completed_failed_and_retried_fixtures_trace_end_to_end(self):
        report = self.report()
        self.assertEqual((report["schema"], report["kind"], report["interface"], report["acceptance_interface"]),
                         (1, "run-record-report", "run-record-v1", "task-acceptance-v1"))
        completed = self.task(report, "fixture-completed")
        self.assertEqual((completed["status"], completed["accepted_attempt_id"], completed["task_wall_seconds"],
                          completed["meets_latency_target"]), ("accepted", "fixture-completed-1", 60.0, True))
        failed = self.task(report, "fixture-failed")
        self.assertEqual((failed["status"], failed["failed_attempts"], failed["meets_latency_target"]),
                         ("rejected", 1, None))
        self.assertEqual(failed["attempt_records"][0]["failure_category"], "timeout-cancellation")
        retried = self.task(report, "fixture-retried")
        chain = [(r["attempt_id"], r["parent_id"], r["cause"], r["acceptance"], r["failure_category"])
                 for r in retried["attempt_records"]]
        self.assertEqual(chain, [
            ("fixture-retried-1", None, "initial", "rejected", None),
            ("fixture-retried-2", "fixture-retried-1", "retry", "missing", "edit-schema"),
            ("fixture-retried-3", "fixture-retried-2", "retry", "accepted", None)])
        # Latency spans every attempt from the first start, not only the accepted one.
        self.assertEqual((retried["status"], retried["task_wall_seconds"], retried["meets_latency_target"]),
                         ("accepted", 200.0, False))

    def test_tool_success_never_decides_task_acceptance(self):
        report = self.report()
        retried = self.task(report, "fixture-retried")["attempt_records"]
        # Every tool call succeeded yet the checker rejected; failed tool calls yet accepted.
        self.assertEqual((retried[0]["tool_calls"], retried[0]["acceptance"]), ({"succeeded": 4, "failed": 0}, "rejected"))
        self.assertEqual((retried[2]["tool_calls"], retried[2]["acceptance"]), ({"succeeded": 2, "failed": 2}, "accepted"))
        statuses = {t["task_id"]: t["status"] for t in report["tasks"]}
        for tool_calls in ({"succeeded": 99, "failed": 0}, {"succeeded": 0, "failed": 99}, None):
            attempts = copy.deepcopy(self.attempts)
            for attempt in attempts:
                attempt["tool_calls"] = tool_calls
            self.assertEqual({t["task_id"]: t["status"] for t in self.report(attempts=attempts)["tasks"]}, statuses)
        self.assertIn("never task acceptance", report["summary"]["tool_calls"]["note"])

    def test_totals_include_failed_attempts_retries_and_all_consumed_work(self):
        summary = self.report()["summary"]
        self.assertEqual({k: summary[k] for k in ("tasks_declared", "tasks_accepted", "tasks_accepted_within_target",
                                                  "tasks_rejected", "tasks_unverified", "tasks_not_attempted",
                                                  "attempts", "failed_attempts", "retries", "attempt_wall_seconds")},
                         {"tasks_declared": 3, "tasks_accepted": 2, "tasks_accepted_within_target": 1,
                          "tasks_rejected": 1, "tasks_unverified": 0, "tasks_not_attempted": 0,
                          "attempts": 5, "failed_attempts": 2, "retries": 2, "attempt_wall_seconds": 300.0})
        self.assertEqual(summary["tokens"], {"prompt": 6300, "completion": 1290, "attempts_with_tokens": 4})
        self.assertEqual(summary["failures_by_category"], {
            "retrieval": 0, "reasoning-answer": 0, "edit-schema": 1, "timeout-cancellation": 1,
            "environment": 0, "verification": 0, "unknown": 0})

    def test_failure_taxonomy_is_closed_and_includes_unknown(self):
        self.assertEqual(rr.FAILURE_CATEGORIES, ("retrieval", "reasoning-answer", "edit-schema",
                                                 "timeout-cancellation", "environment", "verification", "unknown"))
        self.assertTrue(set(rr.MEASUREMENT_FAILURES.values()) <= set(rr.FAILURE_CATEGORIES))
        attempts = copy.deepcopy(self.attempts)
        attempts[1]["failure"]["category"] = "unknown"
        self.assertEqual(self.report(attempts=attempts)["summary"]["failures_by_category"]["unknown"], 1)
        attempts[1]["failure"]["category"] = "network"
        with self.assertRaisesRegex(ValueError, "use 'unknown'"):
            self.report(attempts=attempts)
        attempts[1]["failure"] = None
        with self.assertRaisesRegex(ValueError, "failure must be an object"):
            self.report(attempts=attempts)
        attempts = copy.deepcopy(self.attempts)
        attempts[0]["failure"] = {"category": "unknown", "source": "fixture"}
        with self.assertRaisesRegex(ValueError, "completed attempt has no failure"):
            self.report(attempts=attempts)

    def test_missing_or_unavailable_final_verdict_is_unverified_not_accepted(self):
        for verdicts in ([a for a in self.acceptance if a["attempt_id"] != "fixture-failed-1"],
                         [dict(a, verdict="unavailable") if a["attempt_id"] == "fixture-failed-1" else a
                          for a in self.acceptance]):
            report = self.report(acceptances=verdicts)
            self.assertEqual(self.task(report, "fixture-failed")["status"], "unverified")
            self.assertEqual(report["summary"]["tasks_accepted"], 2)
        report = self.report(acceptances=[])
        self.assertEqual(report["summary"]["tasks_accepted"], 0)
        self.assertEqual(report["coverage"]["attempts_with_acceptance"], 0)

    def test_acceptance_join_rejects_mismatched_or_duplicate_verdicts(self):
        cases = {
            "does not match": dict(self.acceptance[0], attempt_id="fixture-missing"),
            "does not match ": dict(self.acceptance[0], task_id="fixture-failed"),
            "does not match  ": dict(self.acceptance[0], run_id="fixture-run-2"),
            "predates": dict(self.acceptance[0], evaluated_unix_ns=self.attempts[0]["ended_unix_ns"] - 1),
            "checker digest": dict(self.acceptance[0], checker={"id": "fixture-checker", "sha256": None}),
            "unsupported task-acceptance": dict(self.acceptance[0], interface="task-acceptance-v0"),
            "verdict must be": dict(self.acceptance[0], verdict="self-reported-done"),
        }
        for message, verdict in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message.strip()):
                self.report(acceptances=[verdict] + self.acceptance[1:])
        with self.assertRaisesRegex(ValueError, "more than one acceptance"):
            self.report(acceptances=self.acceptance + [dict(self.acceptance[0], verdict="rejected")])

    def test_identity_and_causal_parent_chain_are_enforced(self):
        def broken(change):
            attempts = copy.deepcopy(self.attempts)
            change({a["identity"]["attempt_id"]: a for a in attempts})
            return attempts
        cases = {
            "initial attempt has no parent": lambda a: a["fixture-retried-2"]["identity"].update(cause="initial"),
            "a retry names": lambda a: a["fixture-retried-2"]["identity"].update(parent_id=None),
            "unknown parent": lambda a: a["fixture-retried-2"]["identity"].update(parent_id="fixture-nope"),
            "parent is another task": lambda a: a["fixture-retried-2"]["identity"].update(parent_id="fixture-failed-1"),
            "more than one retry": lambda a: a["fixture-retried-3"]["identity"].update(parent_id="fixture-retried-1"),
            "before its parent ends": lambda a: a["fixture-retried-2"].update(started_unix_ns=a["fixture-retried-1"]["ended_unix_ns"] - 1),
            "another run": lambda a: a["fixture-failed-1"]["identity"].update(run_id="fixture-run-2"),
            "arm differs": lambda a: a["fixture-failed-1"]["identity"].update(arm_id="other-arm"),
            "undeclared task": lambda a: a["fixture-failed-1"]["identity"].update(task_version="v2"),
            "own parent": lambda a: a["fixture-retried-2"]["identity"].update(parent_id="fixture-retried-2"),
            "bounded identifier": lambda a: a["fixture-failed-1"]["identity"].update(task_id="fix task"),
        }
        for message, change in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.report(attempts=broken(change))
        with self.assertRaisesRegex(ValueError, "duplicate attempt_id"):
            self.report(attempts=self.attempts + [copy.deepcopy(self.attempts[0])])
        two_roots = copy.deepcopy(self.attempts)
        two_roots[3]["identity"].update(parent_id=None, cause="initial")
        with self.assertRaisesRegex(ValueError, "more than one initial attempt"):
            self.report(attempts=two_roots)

    def test_latency_targets_must_be_declared_before_the_first_attempt(self):
        late = dict(self.plan, declared_unix_ns=self.attempts[0]["started_unix_ns"] + 1)
        with self.assertRaisesRegex(ValueError, "declared before the first attempt"):
            self.report(plan=late)
        for targets in ({}, {"task_wall_seconds": 0}, {"task_wall_seconds": 60, "throughput": 5}):
            with self.subTest(targets), self.assertRaises(ValueError):
                self.report(plan=dict(self.plan, latency_targets=targets))
        report = self.report()
        self.assertEqual(report["latency_targets"], {"task_wall_seconds": 150})
        self.assertEqual(report["latency_targets_declared_unix_ns"], self.plan["declared_unix_ns"])
        self.assertRegex(report["plan_sha256"], r"\A[a-f0-9]{64}\Z")

    def test_declared_but_unattempted_task_is_reported_not_dropped(self):
        plan = dict(self.plan, tasks=self.plan["tasks"] + [{"task_id": "fixture-skipped", "task_version": "v1"}])
        report = self.report(plan=plan)
        self.assertEqual(self.task(report, "fixture-skipped")["status"], "not-attempted")
        self.assertEqual(report["summary"]["tasks_not_attempted"], 1)

    def test_psu_energy_joins_only_covered_intervals_with_gaps_and_resets(self):
        report = self.report()
        completed = self.task(report, "fixture-completed")["attempt_records"][0]["psu_dc"]
        # 10-30 s interpolated (333.33 -> 400 W) plus 30-60 s (400 -> 420 W); 60-70 s unavailable.
        self.assertAlmostEqual(completed["covered_joules"], 7333.333333 + 12300, places=3)
        self.assertEqual((completed["covered_seconds"], completed["window_seconds"]), (50.0, 60.0))
        self.assertAlmostEqual(completed["coverage"], 50 / 60)
        self.assertEqual([g["reason"] for g in completed["gaps"]], ["sample_unavailable"])
        retried = self.task(report, "fixture-retried")["attempt_records"]
        self.assertEqual([g["reason"] for g in retried[0]["psu_dc"]["gaps"]], ["sample_gap"])
        self.assertEqual((retried[2]["psu_dc"]["resets"], [g["reason"] for g in retried[2]["psu_dc"]["gaps"]]),
                         (1, ["boot_changed"]))
        run = report["energy"]["psu_dc_run_window"]
        self.assertEqual((run["covered_seconds"], run["window_seconds"], run["resets"]), (210.0, 390.0, 1))
        self.assertEqual([g["reason"] for g in run["gaps"]], ["sample_unavailable", "sample_gap", "boot_changed"])
        self.assertEqual(report["coverage"]["psu_dc_run_coverage"], run["coverage"])
        trailing = self.report(psu_samples=self.psu[:3] + [{"reason": "sensor_unavailable", "watts": None, "rails": {}}])
        self.assertEqual([g["reason"] for g in trailing["energy"]["psu_dc_run_window"]["gaps"]], ["sample_unavailable"])
        no_samples = self.report(psu_samples=[])
        window = no_samples["energy"]["psu_dc_run_window"]
        self.assertEqual((window["covered_joules"], window["coverage"], [g["reason"] for g in window["gaps"]]),
                         (0.0, 0.0, ["no_samples"]))

    def test_attribution_levels_stay_separate_and_task_energy_is_not_invented(self):
        report = self.report()
        self.assertEqual(report["energy"]["psu_dc_run_window"]["attribution"], "system-psu-dc-output")
        self.assertEqual(report["energy"]["gpu_component_run_window"]["attribution"], "gpu-board-component")
        self.assertEqual(report["energy"]["task_attributable"]["joules"], None)
        self.assertIn("independent per-task meter", report["energy"]["task_attributable"]["reason"])
        for task in report["tasks"]:
            for attempt in task["attempt_records"]:
                self.assertIsNone(attempt["task_energy_joules"])
        gpu = self.task(report, "fixture-completed")["attempt_records"][0]["gpu_component"]
        self.assertEqual(gpu["devices"], {"0000:03:00.0": {"samples": 2, "mean_watts": 200.0, "max_watts": 220}})
        # GPU board samples are never added to the PSU DC estimate.
        self.assertEqual(self.report(gpu_samples=[])["energy"]["psu_dc_run_window"]["covered_joules"],
                         report["energy"]["psu_dc_run_window"]["covered_joules"])
        backwards = copy.deepcopy(self.psu)
        backwards[5]["unix_ns"], backwards[6]["unix_ns"] = backwards[6]["unix_ns"], backwards[5]["unix_ns"]
        with self.assertRaisesRegex(ValueError, "wall clock is not increasing"):
            self.report(psu_samples=backwards)

    def test_overlapping_attempts_and_deployment_changes_are_visible(self):
        attempts = copy.deepcopy(self.attempts)
        attempts[1]["started_unix_ns"] = attempts[0]["started_unix_ns"] + 30 * SECOND
        attempts[1]["deployment"] = dict(attempts[1]["deployment"], serving_contract="fixture-contract-2")
        report = self.report(attempts=attempts)
        self.assertEqual(self.task(report, "fixture-completed")["attempt_records"][0]["overlapping_attempts"], 1)
        self.assertTrue(report["deployment_changed"])
        self.assertEqual(len(report["deployments"]), 2)
        self.assertFalse(self.report()["deployment_changed"])

    def test_records_are_closed_so_free_text_and_secrets_cannot_enter(self):
        sentinel = "DUMMY_SECRET_SENTINEL"
        changes = [
            lambda a: a.update(prompt=sentinel),
            lambda a: a["identity"].update(note=sentinel),
            lambda a: a["deployment"].update(model="https://example.invalid/" + sentinel),
            lambda a: a["failure"].update(source="exit with " + sentinel),
            lambda a: a["tokens"].update(prompt=-1),
        ]
        for change in changes:
            attempts = copy.deepcopy(self.attempts)
            change(attempts[3])
            with self.assertRaises(ValueError):
                self.report(attempts=attempts)
        self.assertNotIn(sentinel, json.dumps(self.report()))

    def test_identities_are_never_prometheus_labels(self):
        identity_labels = {"task", "task_id", "run", "run_id", "attempt", "attempt_id", "arm", "arm_id",
                           "parent", "parent_id", "trace_id", "session", "user"}
        for name, (_, _, labels) in assistant_metrics.FAMILIES.items():
            self.assertFalse(identity_labels & set(labels), name)
            # Free-form label values are bounded by format and never come from requests.
            for label, values in labels.items():
                self.assertTrue(values is None and label == "contract" or isinstance(values, tuple), name)

    def test_measurement_receipt_carries_identity_and_becomes_an_attempt(self):
        identity = dict(self.attempts[0]["identity"])
        deployment = self.attempts[0]["deployment"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "bounded identifier"):
                measurement.run_command([sys.executable, "-c", "pass"], root / "bad", 5,
                                        identity=dict(identity, run_id="bad id"))
            self.assertFalse((root / "bad").exists())
            self.assertEqual(measurement.run_command([sys.executable, "-c", "pass"], root / "ok", 5,
                                                     identity=identity), 0)
            receipt = json.loads((root / "ok/run.json").read_text())
            self.assertEqual(receipt["identity"], identity)
            attempt = rr.attempt_from_receipt(receipt, deployment, tokens={"prompt": 1, "completion": 1})
            self.assertEqual((attempt["outcome"], attempt["failure"], attempt["identity"]), ("completed", None, identity))
            self.assertGreaterEqual(attempt["ended_unix_ns"], attempt["started_unix_ns"])
            retry = dict(identity, attempt_id="fixture-completed-2", parent_id=identity["attempt_id"], cause="retry")
            self.assertEqual(measurement.run_command([sys.executable, "-c", "raise SystemExit(7)"], root / "exit", 5,
                                                     identity=retry), 7)
            failed = rr.attempt_from_receipt(json.loads((root / "exit/run.json").read_text()), deployment)
            self.assertEqual(failed["failure"], {"category": "unknown", "source": "measurement:workload-exit"})
            with self.assertRaises(subprocess.TimeoutExpired):
                measurement.run_command([sys.executable, "-c", "import time; time.sleep(5)"], root / "slow", .1,
                                        identity=retry)
            slow = rr.attempt_from_receipt(json.loads((root / "slow/run.json").read_text()), deployment)
            self.assertEqual(slow["failure"], {"category": "timeout-cancellation", "source": "measurement:timeout"})
            # Receipts without an identity keep working and stay unjoinable.
            measurement.run_command([sys.executable, "-c", "pass"], root / "plain", 5)
            plain = json.loads((root / "plain/run.json").read_text())
            self.assertIsNone(plain["identity"])
            with self.assertRaisesRegex(ValueError, "identity must be an object"):
                rr.attempt_from_receipt(plain, deployment)

    def test_cli_reports_fixtures_and_rejects_invalid_input(self):
        command = [sys.executable, "-B", str(ROOT / "lib/workstation/run_record.py"), "report",
                   "--plan", str(FIXTURES / "plan.json"), "--attempts", str(FIXTURES / "attempts.jsonl"),
                   "--acceptance", str(FIXTURES / "acceptance.jsonl"), "--psu", str(FIXTURES / "psu.jsonl"),
                   "--gpu", str(FIXTURES / "gpu.jsonl")]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        self.assertEqual(json.loads(result.stdout), json.loads(json.dumps(self.report())))
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "acceptance.jsonl"
            bad.write_text(json.dumps(dict(self.acceptance[0], verdict="done")) + "\n")
            command[command.index("--acceptance") + 1] = str(bad)
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("run record rejected", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
