"""Offline run-record-v1 fixtures: identity, plan binding, failures, acceptance and resource joins.

Synthetic records only; no model, gateway, PSU or GPU is contacted. The request
path through the gateway and service-tools is tested in test_run_trace.py.
"""

import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
sys.path.insert(0, str(ROOT / "apps/base/shared-api"))
import assistant_metrics
import measurement
import run_record as rr
import telemetry_power

FIXTURES = ROOT / "tests/fixtures/run-record"
PUBLICATION = FIXTURES / "publication"
SECOND = 10**9
CLI = [sys.executable, "-B", str(ROOT / "lib/workstation/run_record.py")]


def jsonl(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


class RunRecordTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((FIXTURES / "plan.json").read_text())
        self.attempts = jsonl("attempts.jsonl")
        self.acceptance = jsonl("acceptance.jsonl")
        self.psu = jsonl("psu.jsonl")
        self.gpu = jsonl("gpu.jsonl")
        self.traces = jsonl("traces.jsonl")

    def report(self, **changes):
        values = dict(plan=self.plan, attempts=self.attempts, acceptances=self.acceptance,
                      psu_samples=self.psu, gpu_samples=self.gpu, traces=self.traces)
        values.update(changes)
        return rr.report(values["plan"], values["attempts"], values["acceptances"],
                         values["psu_samples"], values["gpu_samples"], values["traces"])

    def bound(self, plan, records=None):
        """The fixture records as they would be if their run had started under ``plan``."""
        digest = rr.plan_digest(plan)
        return [dict(record, plan_sha256=digest) for record in (self.attempts if records is None else records)]

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
            self.report(plan=late, attempts=self.bound(late), traces=self.bound(late, self.traces))
        for targets in ({}, {"task_wall_seconds": 0}, {"task_wall_seconds": 60, "throughput": 5}):
            with self.subTest(targets), self.assertRaises(ValueError):
                self.report(plan=dict(self.plan, latency_targets=targets))
        report = self.report()
        self.assertEqual(report["latency_targets"], {"task_wall_seconds": 150})
        self.assertEqual(report["latency_targets_declared_unix_ns"], self.plan["declared_unix_ns"])
        self.assertRegex(report["plan_sha256"], r"\A[a-f0-9]{64}\Z")

    def test_declared_but_unattempted_task_is_reported_not_dropped(self):
        plan = dict(self.plan, tasks=self.plan["tasks"] + [{"task_id": "fixture-skipped", "task_version": "v1"}])
        report = self.report(plan=plan, attempts=self.bound(plan), traces=self.bound(plan, self.traces))
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
        self.assertEqual(gpu["devices"], {"0000:03:00.0": {"samples": 2, "mean_watts": 200.0, "max_watts": 220,
                                                           "sensors": ["power1_average"]}})
        # GPU board samples are never added to the PSU DC estimate.
        self.assertEqual(self.report(gpu_samples=[])["energy"]["psu_dc_run_window"]["covered_joules"],
                         report["energy"]["psu_dc_run_window"]["covered_joules"])

    def test_backwards_psu_wall_clock_is_a_gap_and_never_destroys_the_report(self):
        baseline = self.report()
        backwards = copy.deepcopy(self.psu)
        backwards[5]["unix_ns"], backwards[6]["unix_ns"] = backwards[6]["unix_ns"], backwards[5]["unix_ns"]
        report = self.report(psu_samples=backwards)
        # Acceptance, attempts, tokens and GPU observations are unaffected by the PSU clock.
        for key in ("summary", "deployments"):
            self.assertEqual(report[key], baseline[key])
        self.assertEqual(report["energy"]["gpu_component_run_window"], baseline["energy"]["gpu_component_run_window"])
        self.assertEqual(report["coverage"]["psu_clock_steps_back"], 1)
        self.assertEqual(baseline["coverage"]["psu_clock_steps_back"], 0)
        run = report["energy"]["psu_dc_run_window"]
        self.assertIn("clock_discontinuity", [gap["reason"] for gap in run["gaps"]])
        self.assertLess(run["covered_seconds"], baseline["energy"]["psu_dc_run_window"]["covered_seconds"])
        # Covered time and gaps still partition the window: replayed wall time is not counted twice.
        gaps = sum(gap["end_unix_ns"] - gap["start_unix_ns"] for gap in run["gaps"]) / 1e9
        self.assertAlmostEqual(run["covered_seconds"] + gaps, run["window_seconds"])
        # A clock that steps back before the window and then replays it adds no coverage.
        replay = [dict(sample, unix_ns=sample["unix_ns"] + 500 * SECOND) for sample in self.psu[:1]] + self.psu
        replayed = self.report(psu_samples=replay)["energy"]["psu_dc_run_window"]
        self.assertEqual((replayed["covered_seconds"], replayed["covered_joules"]), (0.0, 0.0))
        self.assertEqual(self.report(psu_samples=replay)["coverage"]["psu_clock_steps_back"], len(self.psu) - 1)

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

    def test_measurement_receipt_binds_identity_and_plan_at_start_and_becomes_an_attempt(self):
        identity = dict(self.attempts[0]["identity"])
        deployment = self.attempts[0]["deployment"]
        digest = rr.plan_digest(self.plan)
        show = [sys.executable, "-c", f"import os; print(os.environ.get({rr.CONTEXT_ENV!r}))"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, bad, message in (
                    ("bad-id", dict(identity, run_id="bad id"), "bounded identifier"),
                    ("other-run", dict(identity, run_id="fixture-run-2"), "another run"),
                    ("other-arm", dict(identity, arm_id="other-arm"), "arm differs"),
                    ("undeclared", dict(identity, task_id="fixture-undeclared"), "undeclared task")):
                with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                    measurement.run_command(show, root / name, 5, identity=bad, plan=self.plan)
                self.assertFalse((root / name).exists())
            # An identity without its plan (or the reverse) is refused before anything runs.
            for kwargs in ({"identity": identity}, {"plan": self.plan}):
                with self.assertRaises(ValueError):
                    measurement.run_command(show, root / "half", 5, **kwargs)
            self.assertFalse((root / "half").exists())
            future = dict(self.plan, declared_unix_ns=2**62)
            with self.assertRaisesRegex(ValueError, "declared before the first attempt"):
                measurement.run_command(show, root / "early", 5, identity=identity, plan=future)
            self.assertEqual(measurement.run_command(show, root / "ok", 5, identity=identity, plan=self.plan), 0)
            receipt = json.loads((root / "ok/run.json").read_text())
            self.assertEqual((receipt["identity"], receipt["plan_sha256"]), (identity, digest))
            # The binding exists from the start and the workload receives the same context.
            started = json.loads((root / "ok/run-start.json").read_text())
            self.assertEqual((started["kind"], started["identity"], started["plan_sha256"], started["started_unix_ns"]),
                             ("attempt-start", identity, digest, receipt["started_unix_ns"]))
            context = (root / "ok/stdout.txt").read_text().strip()
            self.assertEqual(rr.parse_context(context), (identity, digest))
            attempt = rr.attempt_from_receipt(receipt, deployment, tokens={"prompt": 1, "completion": 1})
            self.assertEqual((attempt["outcome"], attempt["failure"], attempt["identity"], attempt["plan_sha256"]),
                             ("completed", None, identity, digest))
            self.assertGreaterEqual(attempt["ended_unix_ns"], attempt["started_unix_ns"])
            # A deployment that was not captured stays null.
            self.assertEqual(rr.attempt_from_receipt(receipt)["deployment"], dict.fromkeys(rr.DEPLOYMENT_KEYS))
            retry = dict(identity, attempt_id="fixture-completed-2", parent_id=identity["attempt_id"], cause="retry")
            self.assertEqual(measurement.run_command([sys.executable, "-c", "raise SystemExit(7)"], root / "exit", 5,
                                                     identity=retry, plan=self.plan), 7)
            failed = rr.attempt_from_receipt(json.loads((root / "exit/run.json").read_text()), deployment)
            self.assertEqual(failed["failure"], {"category": "unknown", "source": "measurement:workload-exit"})
            with self.assertRaises(subprocess.TimeoutExpired):
                measurement.run_command([sys.executable, "-c", "import time; time.sleep(5)"], root / "slow", .1,
                                        identity=retry, plan=self.plan)
            slow = rr.attempt_from_receipt(json.loads((root / "slow/run.json").read_text()), deployment)
            self.assertEqual(slow["failure"], {"category": "timeout-cancellation", "source": "measurement:timeout"})
            # Receipts without an identity keep working, export no context and stay unjoinable.
            with mock.patch.dict(os.environ):
                os.environ.pop(rr.CONTEXT_ENV, None)
                measurement.run_command(show, root / "plain", 5)
            plain = json.loads((root / "plain/run.json").read_text())
            self.assertEqual((plain["identity"], plain["plan_sha256"]), (None, None))
            self.assertEqual((root / "plain/stdout.txt").read_text().strip(), "None")
            self.assertFalse((root / "plain/run-start.json").exists())
            with self.assertRaisesRegex(ValueError, "identity must be an object"):
                rr.attempt_from_receipt(plain, deployment)
            # The receipt of a run under this plan joins; the same receipt is refused under an edited plan.
            edited = dict(self.plan, latency_targets={"task_wall_seconds": 9999})
            self.assertEqual(rr.report(self.plan, [attempt], [])["summary"]["attempts"], 1)
            with self.assertRaisesRegex(ValueError, "different plan"):
                rr.report(edited, [attempt], [])

    def test_cli_reports_fixtures_and_rejects_invalid_input(self):
        command = CLI + ["report", "--plan", str(FIXTURES / "plan.json"), "--attempts", str(FIXTURES / "attempts.jsonl"),
                         "--acceptance", str(FIXTURES / "acceptance.jsonl"), "--psu", str(FIXTURES / "psu.jsonl"),
                         "--gpu", str(FIXTURES / "gpu.jsonl"), "--traces", str(FIXTURES / "traces.jsonl")]
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

    def test_plan_digest_is_bound_at_attempt_start_and_retrospective_edits_are_rejected(self):
        digest = rr.plan_digest(self.plan)
        report = self.report()
        self.assertEqual(report["plan_sha256"], digest)
        self.assertTrue(all(attempt["plan_sha256"] == digest for attempt in self.attempts))
        # The round-1 gap: a target edited after the results while keeping the declared time.
        edits = {
            "target": dict(self.plan, latency_targets={"task_wall_seconds": 300}),
            "tasks": dict(self.plan, tasks=self.plan["tasks"][:2] + [{"task_id": "fixture-retried", "task_version": "v2"}]),
            "added task": dict(self.plan, tasks=self.plan["tasks"] + [{"task_id": "fixture-late", "task_version": "v1"}]),
            "declared time": dict(self.plan, declared_unix_ns=self.plan["declared_unix_ns"] - 1),
        }
        for name, edited in edits.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, "different plan"):
                self.report(plan=edited)
        # Rewriting the attempts does not help while the services' request traces hold the original digest.
        edited = edits["target"]
        with self.assertRaisesRegex(ValueError, "request trace was recorded under a different plan"):
            self.report(plan=edited, attempts=self.bound(edited))
        # One attempt that started under another plan is enough to refuse the report.
        attempts = copy.deepcopy(self.attempts)
        attempts[4]["plan_sha256"] = rr.plan_digest(edited)
        with self.assertRaisesRegex(ValueError, "different plan"):
            self.report(attempts=attempts)
        for missing in (None, "", "0" * 63):
            attempts[4]["plan_sha256"] = missing
            with self.subTest(missing), self.assertRaisesRegex(ValueError, "plan_sha256 must be a sha256"):
                self.report(attempts=attempts)
        identity = self.attempts[0]["identity"]
        self.assertEqual(rr.bind(self.plan, identity, self.plan["declared_unix_ns"]), digest)
        with self.assertRaisesRegex(ValueError, "declared before the first attempt"):
            rr.bind(self.plan, identity, self.plan["declared_unix_ns"] - 1)

    def test_run_context_round_trips_and_rejects_everything_else(self):
        digest = rr.plan_digest(self.plan)
        for attempt in self.attempts:
            value = rr.context_value(attempt["identity"], digest)
            self.assertLessEqual(len(value), rr.MAX_CONTEXT)
            self.assertEqual(rr.parse_context(value), (attempt["identity"], digest))
        longest = dict.fromkeys(rr.IDENTITY_KEYS[:6], "a" * 128)
        longest.update(attempt_id="b" * 128, cause="retry")
        self.assertLessEqual(len(rr.context_value(longest, digest)), rr.MAX_CONTEXT)
        value = rr.context_value(self.attempts[0]["identity"], digest)
        for bad in (value.replace("v1;", "v2;"), value.replace(";plan=", ";plan=x"), value + ";extra=1",
                    value.replace("task=", "task=a b"), value.replace("cause=initial", "cause=retry"),
                    value.replace("run=", "arm=", 1), value.replace("parent=-", "parent=fixture-completed-1"),
                    value.rsplit(";", 1)[0], "", None, value.encode(), "x" * (rr.MAX_CONTEXT + 1)):
            with self.subTest(bad), self.assertRaises(ValueError):
                rr.parse_context(bad)

    def test_request_traces_join_attempts_and_stay_apart_from_acceptance(self):
        report = self.report()
        completed = self.task(report, "fixture-completed")["attempt_records"][0]["request_trace"]
        self.assertEqual(completed, {"requests": 4, "outside_attempt_window": 0, "services": {
            "gateway": {"requests": 2, "by_result": {"ok": 2}, "by_operation": {"chat": 2}},
            "service-tools": {"requests": 2, "by_result": {"ok": 1, "tool_error": 1},
                              "by_operation": {"tool:search_web": 1, "tool:read_web_page": 1}}}})
        failed = self.task(report, "fixture-failed")["attempt_records"][0]["request_trace"]
        self.assertEqual(failed["services"]["gateway"]["by_result"], {"backend_error": 1})
        self.assertEqual((report["coverage"]["attempts_with_request_trace"], report["coverage"]["request_trace_events"],
                          report["coverage"]["unmatched_request_trace_events"]), (5, 8, 0))
        # Request and tool outcomes never change a task status.
        without = self.report(traces=[])
        self.assertEqual([task["status"] for task in without["tasks"]], [task["status"] for task in report["tasks"]])
        self.assertIsNone(self.task(without, "fixture-completed")["attempt_records"][0]["request_trace"])
        self.assertEqual(without["coverage"]["attempts_with_request_trace"], 0)
        # An event for an attempt that left no record is counted, not dropped and not fatal.
        stray = copy.deepcopy(self.traces[0])
        stray["identity"].update(attempt_id="fixture-unrecorded")
        self.assertEqual(self.report(traces=self.traces + [stray])["coverage"]["unmatched_request_trace_events"], 1)
        late = dict(self.traces[0], ended_unix_ns=self.attempts[0]["ended_unix_ns"] + 1)
        self.assertEqual(self.task(self.report(traces=[late]), "fixture-completed")["attempt_records"][0]
                         ["request_trace"]["outside_attempt_window"], 1)
        cases = {
            "another run": lambda t: t["identity"].update(run_id="fixture-run-2"),
            "identity differs": lambda t: t["identity"].update(arm_id="other-arm"),
            "unknown service": lambda t: t.update(service="sglang"),
            "fixed name": lambda t: t.update(operation="GET /v1/models?key=DUMMY"),
            "unknown request trace result": lambda t: t.update(result="success"),
            "code must be a fixed name": lambda t: t.update(code="Traceback (most recent call last)"),
            "HTTP status": lambda t: t.update(status="200"),
            "unexpected or missing keys": lambda t: t.update(path="/v1/chat/completions"),
            "ends before it starts": lambda t: t.update(ended_unix_ns=t["started_unix_ns"] - 1),
        }
        for message, change in cases.items():
            trace = copy.deepcopy(self.traces[0])
            change(trace)
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                self.report(traces=[trace])

    def test_benchmark_telemetry_becomes_psu_and_gpu_samples_without_inventing_readings(self):
        rows = jsonl("telemetry.jsonl")
        samples = [telemetry_power.sample_from_snapshot(row) for row in rows]
        self.assertEqual([sample["reason"] for sample in samples],
                         ["available", "available", "invalid_sensor", "available", "available"])
        self.assertEqual([sample["watts"] for sample in samples], [300.0, 320.0, None, 340.0, 360.0])
        self.assertEqual({key: samples[0][key] for key in ("boot_id", "unix_ns", "monotonic_ns")},
                         {key: rows[0][key] for key in ("boot_id", "unix_ns", "monotonic_ns")})
        gpu = [sample for row in rows for sample in rr.gpu_samples_from_snapshot(row)]
        self.assertEqual([(sample["device"], sample["sensor"], sample["watts"]) for sample in gpu],
                         [("0000:03:00.0", "power1_average", watts) for watts in (180.0, 200.0, 210.0, 220.0)])
        for sample in gpu:
            rr.validate_gpu_sample(sample)
        # A missing PSU reading is a gap in the join; a card without a reading has no sample.
        window = rr.psu_window(rr.psu_timeline(samples), rows[0]["unix_ns"], rows[-1]["unix_ns"])
        self.assertEqual((window["covered_seconds"], window["window_seconds"]), (2.0, 4.0))
        self.assertAlmostEqual(window["covered_joules"], 310 + 350)
        self.assertEqual([gap["reason"] for gap in window["gaps"]], ["sample_unavailable"])
        # Other sensors, an instantaneous-only card, and unusable rows.
        row = copy.deepcopy(rows[0])
        gpu_path = next(path for path, entry in row["hwmon"].items() if entry["name"] == "amdgpu")
        row["hwmon"][gpu_path]["values"] = {"power1_input": "150000000"}
        self.assertEqual([(s["sensor"], s["watts"]) for s in rr.gpu_samples_from_snapshot(row)], [("power1_input", 150.0)])
        row["hwmon"][gpu_path]["values"] = {"power1_average": "not-a-number"}
        self.assertEqual(rr.gpu_samples_from_snapshot(row), [])
        row["gpus_by_bdf"] = {}
        row["hwmon"][gpu_path]["values"] = {"power1_average": "150000000"}
        self.assertEqual(rr.gpu_samples_from_snapshot(row), [])
        psu_path = next(path for path, entry in rows[0]["hwmon"].items() if entry["name"] == "corsairpsu")
        variants = {
            "not_detected": lambda r: r["hwmon"].pop(psu_path),
            "ambiguous_devices": lambda r: r["hwmon"].update({psu_path.replace("hwmon5", "hwmon9"): r["hwmon"][psu_path]}),
            "invalid_sensor": lambda r: r["hwmon"][psu_path]["labels"].update(power1="power +12v"),
        }
        for reason, change in variants.items():
            row = copy.deepcopy(rows[0])
            change(row)
            self.assertEqual(telemetry_power.sample_from_snapshot(row), {"reason": reason, "watts": None, "rails": {}})
        for row in (None, [], {"hwmon": []}):
            self.assertEqual(telemetry_power.sample_from_snapshot(row)["reason"], "invalid_sensor")
            self.assertEqual(rr.gpu_samples_from_snapshot(row), [])
        # A row from a runner without a boot ID is never treated as a covered sample.
        old = dict(rows[0], boot_id=None)
        timeline = rr.psu_timeline([telemetry_power.sample_from_snapshot(old), samples[1]])
        self.assertEqual(rr.psu_window(timeline, rows[0]["unix_ns"], rows[1]["unix_ns"])["covered_seconds"], 0.0)

    def test_snapshot_psu_sample_names_the_same_device_as_the_psu_sampler(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs, proc = Path(tmp) / "sys", Path(tmp) / "proc"
            attachment = "pci0000:00/0000:00:08.1/usb3/3-2/0003:1B1C:1C1F.0002"
            device = sysfs / "devices" / attachment / "hwmon/hwmon5"
            device.mkdir(parents=True)
            for name, value in {"name": "corsairpsu", "power1_label": "power total", "power1_input": "300000000"}.items():
                (device / name).write_text(value + "\n")
            (sysfs / "class/hwmon").mkdir(parents=True)
            (sysfs / "class/hwmon/hwmon5").symlink_to(device)
            (proc / "sys/kernel/random").mkdir(parents=True)
            (proc / "sys/kernel/random/boot_id").write_text("11111111-1111-1111-1111-111111111111\n")
            collected = telemetry_power.collect(sysfs, proc, lambda: (1700000010000000000, 5010000000000))
            row = {"unix_ns": 1700000010000000000, "monotonic_ns": 5010000000000,
                   "boot_id": "11111111-1111-1111-1111-111111111111",
                   "hwmon": {"/sys/devices/" + attachment + "/hwmon/hwmon5": {
                       "name": "corsairpsu", "labels": {"power1": "power total"}, "values": {"power1_input": "300000000"}}}}
            self.assertEqual(collected, telemetry_power.sample_from_snapshot(row))
            # The benchmark runner's own snapshot records the boot ID the PSU sampler uses.
            self.assertEqual(measurement.snapshot(sysfs, proc, Path(tmp) / "cgroup")["boot_id"], collected["boot_id"])

    def test_measurement_run_produces_joinable_receipt_and_resource_records(self):
        identity = dict(self.attempts[0]["identity"])
        plan = dict(self.plan, declared_unix_ns=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "plan.json").write_text(json.dumps(plan))
            (root / "identity.json").write_text(json.dumps(identity))
            runner = [sys.executable, "-B", str(ROOT / "lib/workstation/measurement.py"), "--output", str(root / "run"),
                      "--timeout", "20", "--run-plan", str(root / "plan.json"), "--run-identity", str(root / "identity.json"),
                      "run", "--", sys.executable, "-c", "import time; time.sleep(1.2)"]
            self.assertEqual(subprocess.run(runner, capture_output=True, text=True, timeout=60).returncode, 0)
            attempt = json.loads(subprocess.run(CLI + ["attempt", str(root / "run"), "--model", "fixture-model",
                                                       "--tokens", "12", "5"], capture_output=True, text=True,
                                                check=True, timeout=30).stdout)
            self.assertEqual((attempt["identity"], attempt["plan_sha256"], attempt["tokens"], attempt["outcome"]),
                             (identity, rr.plan_digest(plan), {"prompt": 12, "completion": 5}, "completed"))
            self.assertEqual(attempt["deployment"], {"serving_contract": None, "model": "fixture-model",
                                                     "tokenizer_sha256": None, "template_sha256": None})
            summary = json.loads(subprocess.run(
                CLI + ["resources", str(root / "run/telemetry.jsonl"), "--psu", str(root / "psu.jsonl"),
                       "--gpu", str(root / "gpu.jsonl")], capture_output=True, text=True, check=True, timeout=30).stdout)
            self.assertGreaterEqual(summary["telemetry_rows"], 2)
            self.assertEqual(summary["psu_samples"], summary["telemetry_rows"])
            psu, gpu = ([json.loads(line) for line in (root / name).read_text().splitlines()]
                        for name in ("psu.jsonl", "gpu.jsonl"))
            # This host has no Corsair PSU or Radeon card: the samples say so and the join reports a gap, not zero watts.
            self.assertEqual((summary["psu_samples_available"], summary["gpu_samples"], gpu), (0, 0, []))
            self.assertTrue(all(sample["watts"] is None for sample in psu))
            report = rr.report(plan, [attempt], [], psu, gpu)
            window = report["tasks"][0]["attempt_records"][0]["psu_dc"]
            self.assertEqual((window["covered_joules"], window["coverage"], [g["reason"] for g in window["gaps"]]),
                             (0.0, 0.0, ["no_samples"]))
            self.assertEqual(report["tasks"][0]["status"], "unverified")
            # Outputs are never replaced, and half a binding or an edited plan is refused before the workload runs.
            again = subprocess.run(CLI + ["resources", str(root / "run/telemetry.jsonl"), "--psu", str(root / "psu.jsonl"),
                                          "--gpu", str(root / "gpu2.jsonl")], capture_output=True, text=True, timeout=30)
            self.assertEqual((again.returncode, again.stdout), (1, ""))
            half = runner[:runner.index("--run-identity")] + runner[runner.index("run"):]
            half[half.index(str(root / "run"))] = str(root / "half")
            self.assertEqual(subprocess.run(half, capture_output=True, text=True, timeout=30).returncode, 2)
            (root / "identity.json").write_text(json.dumps(dict(identity, arm_id="other-arm")))
            other = list(runner)
            other[other.index(str(root / "run"))] = str(root / "other")
            refused = subprocess.run(other, capture_output=True, text=True, timeout=30)
            self.assertEqual((refused.returncode, refused.stderr.strip()),
                             (1, "run record rejected: identity arm differs from the declared arm"))
            self.assertFalse((root / "half").exists() or (root / "other").exists())

    def test_cli_context_binds_an_identity_to_its_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = Path(tmp) / "identity.json"
            identity.write_text(json.dumps(self.attempts[3]["identity"]))
            command = CLI + ["context", "--plan", str(FIXTURES / "plan.json"), "--identity", str(identity)]
            result = json.loads(subprocess.run(command, capture_output=True, text=True, check=True, timeout=30).stdout)
            self.assertEqual((result["header"], result["environment"], result["plan_sha256"]),
                             ("X-Workstation-Run-Context", "WORKSTATION_RUN_CONTEXT", rr.plan_digest(self.plan)))
            self.assertEqual(rr.parse_context(result["value"]), (self.attempts[3]["identity"], result["plan_sha256"]))
            identity.write_text(json.dumps(dict(self.attempts[3]["identity"], task_version="v9")))
            refused = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual((refused.returncode, refused.stdout), (1, ""))
            self.assertIn("undeclared task version", refused.stderr)

    def test_request_traces_are_extracted_from_service_logs_without_copying_other_lines(self):
        sentinel = "DUMMY_SECRET_SENTINEL"
        other = copy.deepcopy(self.traces[0])
        other["identity"].update(run_id="fixture-run-2")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gateway.log").write_text("".join(json.dumps(event) + "\n" for event in self.traces[:4] + [other]))
            (root / "tools.log").write_text(
                "ERROR:    Exception in ASGI application " + sentinel + "\n"
                + json.dumps({"kind": "request-trace", "note": sentinel}) + "\n\n"
                + "".join(json.dumps(event) + "\n" for event in self.traces[4:]) + "[1, 2]\n")
            command = CLI + ["traces", "--run-id", "fixture-run-1", "--output", str(root / "traces.jsonl"),
                             str(root / "gateway.log"), str(root / "tools.log")]
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual(json.loads(result.stdout), {"kept": 8, "other_runs": 1, "ignored_lines": 3})
            kept = (root / "traces.jsonl").read_text()
            self.assertEqual([json.loads(line) for line in kept.splitlines()], self.traces)
            self.assertNotIn(sentinel, kept + result.stdout + result.stderr)
            self.assertEqual(subprocess.run(command, capture_output=True, text=True, timeout=30).returncode, 1)

    def test_real_checker_receipts_feed_the_join_through_the_adapter(self):
        plan = json.loads((PUBLICATION / "plan.json").read_text())
        attempts = [json.loads(line) for line in (PUBLICATION / "attempts.jsonl").read_text().splitlines()]
        results = {path.stem: json.loads(path.read_text()) for path in sorted((PUBLICATION / "results").glob("*.json"))}
        self.assertEqual(sorted(results), ["accepted", "rejected", "unavailable-unresolved", "unavailable-unverified"])
        verdicts = {name: rr.acceptance_from_result(result) for name, result in results.items()}
        self.assertEqual({name: (v["verdict"], v["task_id"], v["task_version"], v["attempt_id"])
                          for name, v in verdicts.items()}, {
            "rejected": ("rejected", "record-publication", "2026-11-15.1", "fixture-publication-1"),
            "unavailable-unresolved": ("unavailable", "record-publication", "2026-11-15.1", "fixture-publication-2"),
            "unavailable-unverified": ("unavailable", None, None, "fixture-publication-3"),
            "accepted": ("accepted", "record-publication", "2026-11-15.1", "fixture-publication-4")})
        for name, verdict in verdicts.items():
            result = results[name]
            self.assertEqual((verdict["run_id"], verdict["checker"], verdict["seal_sha256"]),
                             ("fixture-publication-run", {"id": "workstation-task-acceptance", "sha256": result["checker_sha256"]},
                              result["components"]["protection"]["seal_sha256"]))
            self.assertEqual(verdict["spec_sha256"], result["spec_sha256"])
        # The checker result records feed the join directly, and so do their converted verdicts.
        report = rr.report(plan, attempts, list(results.values()))
        self.assertEqual(report, rr.report(plan, attempts, list(verdicts.values())))
        task = report["tasks"][0]
        self.assertEqual([row["acceptance"] for row in task["attempt_records"]],
                         ["rejected", "unavailable", "unavailable", "accepted"])
        self.assertEqual((task["status"], task["accepted_attempt_id"], task["task_wall_seconds"], task["meets_latency_target"]),
                         ("accepted", "fixture-publication-4", 360.0, True))
        self.assertEqual((report["summary"]["attempts"], report["summary"]["retries"],
                          report["coverage"]["attempts_with_acceptance"]), (4, 3, 4))
        self.assertEqual(task["attempt_records"][0]["acceptance_spec_sha256"], results["rejected"]["spec_sha256"])
        # No self-report override: the rejected attempt claimed completion, reported itself completed and
        # had only successful tool calls. The verdict is the checker's alone.
        self.assertTrue(results["rejected"]["evidence"]["claim"]["completed"])
        self.assertEqual((attempts[0]["outcome"], attempts[0]["tool_calls"]), ("completed", {"succeeded": 2, "failed": 0}))
        rejected_only = rr.report(plan, attempts[:1], [results["rejected"]])
        self.assertEqual((rejected_only["tasks"][0]["status"], rejected_only["summary"]["tasks_accepted"]), ("rejected", 0))
        for name in ("unavailable-unresolved", "unavailable-unverified"):
            attempt = copy.deepcopy(attempts[0])
            attempt["identity"]["attempt_id"] = results[name]["subject"]["attempt_id"]
            unavailable = rr.report(plan, [attempt], [results[name]])
            self.assertEqual((unavailable["tasks"][0]["status"], unavailable["summary"]["tasks_unverified"]),
                             ("unverified", 1), name)

    def test_checker_result_adapter_refuses_edited_incomplete_or_unjoinable_results(self):
        results = {path.stem: json.loads(path.read_text()) for path in sorted((PUBLICATION / "results").glob("*.json"))}

        def changed(name, change):
            result = copy.deepcopy(results[name])
            change(result)
            return result
        cases = {
            # A verdict edited to "accepted" without the components to support it.
            "does not follow from the recorded components": changed("rejected", lambda r: r.update(verdict="accepted")),
            "does not follow from the recorded components ": changed(
                "unavailable-unverified", lambda r: r.update(verdict="accepted")),
            "does not follow from the recorded components  ": changed(
                "accepted", lambda r: r["components"]["linkage"].update(status="failed")),
            # The model's completion claim or the repair-loop guidance marked as deciding.
            "not deciding the verdict": changed("accepted", lambda r: r["evidence"]["claim"].update(decides_verdict=True)),
            "not deciding the verdict ": changed("accepted", lambda r: r["evidence"]["guidance"].update(authoritative=True)),
            "not deciding the verdict  ": changed("accepted", lambda r: r["evidence"].pop("claim")),
            "every component with a known status": changed("accepted", lambda r: r["components"].pop("oracle")),
            "every component with a known status ": changed(
                "accepted", lambda r: r["components"]["oracle"].update(status="skipped")),
            "every component with a known status  ": changed(
                "accepted", lambda r: r["components"]["protection"].update(status="not-applicable")),
            # Nothing to join on, and no way to supply it from outside the result.
            "names no run and attempt": changed("accepted", lambda r: r["subject"].update(attempt_id=None)),
            "names no run and attempt ": changed("accepted", lambda r: r["subject"].update(run_id=None)),
            "task acceptance subject has unexpected or missing keys": changed(
                "accepted", lambda r: r["subject"].update(task_id="record-publication")),
            "exactly when the acceptance source was verified": changed("accepted", lambda r: r.update(task_id=None)),
            "exactly when the acceptance source was verified ": changed(
                "unavailable-unverified", lambda r: r.update(task_id="record-publication")),
            "task_version must be a bounded identifier": changed("accepted", lambda r: r.update(spec_version="release 1")),
            "attempt_id must be a bounded identifier": changed("accepted", lambda r: r["subject"].update(attempt_id="a b")),
            "checker digest": changed("accepted", lambda r: r.update(checker_sha256=None)),
            "unsupported task acceptance result": changed("accepted", lambda r: r.update(schema=2)),
            "task acceptance result has unexpected or missing keys": changed("accepted", lambda r: r.update(override="accepted")),
            "task acceptance result has unexpected or missing keys ": changed("accepted", lambda r: r.pop("evidence")),
            "no time zone": changed("accepted", lambda r: r.update(created_utc="2026-11-15T13:00:00")),
            "Invalid isoformat": changed("accepted", lambda r: r.update(created_utc="yesterday")),
        }
        for message, result in cases.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message.strip()):
                rr.acceptance_from_result(result)
        with self.assertRaisesRegex(ValueError, "task acceptance result must be an object"):
            rr.acceptance_from_result([results["accepted"]])
        # A verdict record cannot claim an unverified task as accepted or rejected either.
        verdict = rr.acceptance_from_result(results["unavailable-unverified"])
        for forged in ("accepted", "rejected"):
            with self.subTest(forged), self.assertRaisesRegex(ValueError, "task_id must be a bounded identifier"):
                rr.validate_acceptance(dict(verdict, verdict=forged))
        # A result for another task or run never joins this attempt.
        plan = json.loads((PUBLICATION / "plan.json").read_text())
        attempts = [json.loads(line) for line in (PUBLICATION / "attempts.jsonl").read_text().splitlines()]
        other = dict(rr.acceptance_from_result(results["accepted"]), task_id="another-task")
        with self.assertRaisesRegex(ValueError, "does not match a recorded attempt"):
            rr.report(plan, attempts, [other])

    def test_checker_validation_also_runs_when_the_checker_is_installed(self):
        result = json.loads((PUBLICATION / "results/accepted.json").read_text())
        seen = []

        def refuse(value):
            seen.append(value)
            raise ValueError("refused by the checker")
        with mock.patch.dict(sys.modules, {"task_acceptance": types.SimpleNamespace(validate_result=refuse)}):
            with self.assertRaisesRegex(ValueError, "refused by the checker"):
                rr.acceptance_from_result(result)
        self.assertEqual(seen, [result])
        with mock.patch.dict(sys.modules, {"task_acceptance": types.SimpleNamespace(validate_result=seen.append)}):
            self.assertEqual(rr.acceptance_from_result(result)["verdict"], "accepted")

    def test_cli_converts_a_checker_result_and_reports_from_result_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            lines = []
            for name in ("rejected", "unavailable-unresolved", "unavailable-unverified", "accepted"):
                result = subprocess.run(CLI + ["acceptance", str(PUBLICATION / "results" / (name + ".json"))],
                                        capture_output=True, text=True, check=True, timeout=30)
                self.assertEqual(result.stdout.count("\n"), 1)
                lines.append(result.stdout)
            verdicts = Path(tmp) / "acceptance.jsonl"
            verdicts.write_text("".join(lines))
            report = json.loads(subprocess.run(
                CLI + ["report", "--plan", str(PUBLICATION / "plan.json"), "--attempts", str(PUBLICATION / "attempts.jsonl"),
                       "--acceptance", str(verdicts)], capture_output=True, text=True, check=True, timeout=30).stdout)
            self.assertEqual((report["summary"]["tasks_accepted"], report["summary"]["attempts"]), (1, 4))
            edited = json.loads((PUBLICATION / "results/rejected.json").read_text())
            edited["verdict"] = "accepted"
            (Path(tmp) / "edited.json").write_text(json.dumps(edited))
            refused = subprocess.run(CLI + ["acceptance", str(Path(tmp) / "edited.json")],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual((refused.returncode, refused.stdout), (1, ""))
            self.assertIn("does not follow from the recorded components", refused.stderr)

    def test_overlap_counts_match_the_pairwise_definition(self):
        def pairwise(attempts):
            return {a["identity"]["attempt_id"]: sum(
                1 for other in attempts if other is not a and other["started_unix_ns"] < a["ended_unix_ns"]
                and other["ended_unix_ns"] > a["started_unix_ns"]) for a in attempts}
        generator = random.Random(20261115)
        for size in (0, 1, 2, 5, 40):
            for _ in range(60):
                attempts = []
                for index in range(size):
                    start = generator.randint(1, 12)
                    attempts.append({"identity": {"attempt_id": f"a{index}"}, "started_unix_ns": start,
                                     "ended_unix_ns": start + generator.choice((0, 0, 1, 2, 5))})
                self.assertEqual(rr.overlaps(attempts), pairwise(attempts))

    def test_input_bounds_are_enforced(self):
        self.assertEqual((rr.MAX_RECORDS, rr.MAX_SAMPLES), (10000, 100000))
        with self.assertRaisesRegex(ValueError, "too many samples or trace events"):
            self.report(psu_samples=[{}] * (rr.MAX_SAMPLES + 1))
        with self.assertRaisesRegex(ValueError, "too many records"):
            self.report(acceptances=[{}] * (rr.MAX_RECORDS + 1))
        with tempfile.TemporaryDirectory() as tmp:
            long = Path(tmp) / "long.jsonl"
            long.write_bytes(b"{" + b" " * rr.MAX_LINE + b"}\n")
            with self.assertRaisesRegex(ValueError, "line is too long"):
                rr._jsonl(long)
            many = Path(tmp) / "many.jsonl"
            many.write_text("{}\n" * 4)
            with self.assertRaisesRegex(ValueError, "too many attempt records"):
                rr._jsonl(many, 3, "attempt")


if __name__ == "__main__":
    unittest.main()
