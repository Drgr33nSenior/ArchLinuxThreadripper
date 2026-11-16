"""Task acceptance contract tests.

Every workspace here is an authored fixture, so the test interpreter may stand
in for the sandbox. Nothing here is model output or model evidence.
"""
import contextlib
import copy
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "lib/workstation")]
import coding_eval
import task_acceptance

FIXTURES = ROOT / "tests/fixtures/task-acceptance"
REPAIR = FIXTURES / "sources/window-counts"
PUBLICATION = FIXTURES / "sources/record-publication"
# Pinned here, outside both acceptance sources: a changed source file fails these tests.
REPAIR_SEAL = "2f85957394a05e59a4209c7889cfd0af0fe7abebb1a7f023e7f06dbb204a2593"
PUBLICATION_SEAL = "aba69f2a29caeb2216cb3cfa0212f2f5786d4656f68c5911ca42aaf80480e4cc"
ORACLE = json.loads((REPAIR / "acceptance.json").read_text())["oracle"]
VISIBLE_TESTS = json.loads((REPAIR / "acceptance.json").read_text())["tests"]["command"]


def requires_input(available, reason):
    """Register a test only when its input exists; otherwise print the named skip."""
    def register(test):
        if available:
            return test
        print(f"SKIP: {reason}")
        return None
    return register


def host_run(fixture, files, command, seconds=120, allowed=()):
    """Authored fixtures only: the test interpreter stands in for the sandbox's /usr/bin/python3."""
    if command[0] != "/usr/bin/python3":
        raise AssertionError(command)
    return coding_eval.run_task(fixture, files, [sys.executable] + command[1:], seconds, allowed, sandbox=False)


def checked_task(source, workspace, expected_seal=None, **options):
    # Authored fixture agent has only these two scoped file operations. This
    # does not attest a deployment's command, browser or other tool routes.
    def scoped_file_route(operation, name):
        path = Path(name).resolve()
        if Path(workspace).resolve() not in path.parents:
            return "denied"
        return task_acceptance.perform_probe(operation, name)
    with tempfile.TemporaryDirectory() as temporary:
        pin = Path(temporary) / "evaluator-pin"
        pin.write_text(expected_seal or "")
        options.setdefault("agent_routes", {"fixture-file-tools": scoped_file_route})
        options.setdefault("protected_configuration", [pin])
        attempt = task_acceptance.begin_attempt(source, workspace, expected_seal,
                                                agent_routes=options.pop("agent_routes"),
                                                protected_configuration=options.pop("protected_configuration"),
                                                state=options.pop("state", None),
                                                expected_checker=task_acceptance.checker_identity())
        return task_acceptance.check_task(source, workspace, expected_seal, attempt=attempt, **options)


def statuses(result, names=task_acceptance.COMPONENTS):
    return tuple(result["components"][name]["status"] for name in names)


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.count = 0

    def fresh(self, name):
        self.count += 1
        return self.temporary / f"{name}-{self.count}"

    def repair_workspace(self, case=None):
        workspace = self.fresh("workspace")
        task_acceptance.prepare_workspace(REPAIR, workspace)
        if case:
            shutil.copytree(FIXTURES / "cases/window-counts" / case, workspace, dirs_exist_ok=True)
        return workspace

    def repair(self, case=None, **options):
        options.setdefault("run", host_run)
        result = checked_task(REPAIR, self.repair_workspace(case), REPAIR_SEAL, **options)
        return task_acceptance.validate_result(result)

    def run_cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                status = task_acceptance.main([str(argument) for argument in arguments])
            except SystemExit as error:
                status = error.code
        return status, output.getvalue().strip(), errors.getvalue().strip()


class ProtectionTests(Case):
    """The acceptance source is apart from the workspace and pinned by its seal."""

    def never(self, *arguments, **options):
        raise AssertionError("an unverified acceptance source must not run")

    def test_seal_pins_every_file_of_the_acceptance_source(self):
        self.assertEqual(REPAIR_SEAL, task_acceptance.seal(REPAIR))
        self.assertEqual(PUBLICATION_SEAL, task_acceptance.seal(PUBLICATION))
        source = self.fresh("source")
        shutil.copytree(REPAIR, source)
        self.assertEqual(REPAIR_SEAL, task_acceptance.seal(source))  # the seal does not depend on the location
        for change in ("rewrite", "add", "remove"):
            if change == "rewrite":
                (source / "oracle/test_acceptance.py").write_text("import unittest\n")
            elif change == "add":
                (source / "fixture/conftest.py").write_text("")
            else:
                (source / "mutants/closed-boundary/windows.py").unlink()
            with self.subTest(change=change):
                self.assertNotEqual(REPAIR_SEAL, task_acceptance.seal(source))

    def test_rewritten_oracle_is_never_run_or_accepted(self):
        source = self.fresh("source")
        shutil.copytree(REPAIR, source)
        # The oracle is rewritten so that the unrepaired fixture would pass it.
        (source / "oracle/test_acceptance.py").write_text("import unittest\n")
        (source / "fixture/test_windows.py").write_text("import unittest\n")
        result = checked_task(source, self.repair_workspace(), REPAIR_SEAL, claim=True, run=self.never)
        task_acceptance.validate_result(result)
        self.assertEqual(("unavailable", "protection:seal-mismatch"), (result["verdict"], result["reasons"][0]))
        self.assertEqual("failed", result["components"]["protection"]["status"])
        self.assertEqual({"unavailable"}, set(statuses(result)[1:]))
        self.assertIsNone(result["task_id"])
        self.assertTrue(result["evidence"]["claim"]["contradicted"])

    def test_source_that_holds_a_link_is_refused(self):
        source = self.fresh("source")
        shutil.copytree(REPAIR, source)
        os.symlink(REPAIR / "reference/windows.py", source / "oracle/linked.py")
        result = checked_task(source, self.repair_workspace("valid-repair"), REPAIR_SEAL, run=self.never)
        self.assertEqual(("unavailable", ["protection:source-unreadable"]), (result["verdict"], result["reasons"][:1]))

    def test_source_must_be_apart_from_the_workspace(self):
        inside = self.repair_workspace("valid-repair")
        shutil.copytree(REPAIR, inside / "acceptance")
        around = self.fresh("source")
        shutil.copytree(REPAIR, around)
        task_acceptance.prepare_workspace(REPAIR, around / "fixture/workspace")
        for source, workspace in ((inside / "acceptance", inside), (around, around / "fixture/workspace"), (inside, inside)):
            with self.subTest(source=source.name):
                result = checked_task(source, workspace, REPAIR_SEAL, run=self.never)
                self.assertEqual("unavailable", result["verdict"])
                self.assertEqual("source-overlaps-workspace", result["components"]["protection"]["reason"])

    def test_missing_pinned_seal_is_unavailable_not_accepted(self):
        result = checked_task(REPAIR, self.repair_workspace("valid-repair"), None, run=self.never)
        task_acceptance.validate_result(result)
        self.assertEqual(("unavailable", "unavailable"), (result["verdict"], result["components"]["protection"]["status"]))
        self.assertEqual("protection:no-expected-seal", result["reasons"][0])
        for options in ({"expected_seal": "not-a-digest"}, {"claim": 1}, {"run_id": "run 1"}, {"attempt_id": ""}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                checked_task(REPAIR, self.repair_workspace(), **{"expected_seal": REPAIR_SEAL, **options},
                                           run=self.never)

    def test_only_editable_files_cross_into_the_evaluation(self):
        workspace = self.repair_workspace("rewritten-test")
        shutil.copytree(FIXTURES / "cases/window-counts/patched-application", workspace, dirs_exist_ok=True)
        (workspace / "sitecustomize.py").write_text("raise SystemExit(0)\n")
        (workspace / "test_acceptance.py").write_text("import unittest\n")  # collides with a hidden path
        (workspace / "windows.py").write_text((REPAIR / "reference/windows.py").read_text())
        seen = []

        def recording(fixture, files, command, seconds=120, allowed=()):
            tree = {path.relative_to(fixture).as_posix(): path.read_bytes() for path in Path(fixture).rglob("*") if path.is_file()}
            seen.append((command, tree))
            return host_run(fixture, files, command, seconds, allowed)

        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=recording)
        self.assertEqual("accepted", result["verdict"])  # the repair is real; the agent's test files change nothing
        judged = [tree for command, tree in seen if "-I" in command]
        self.assertEqual(5, len(judged))
        for tree in judged:
            self.assertEqual({"windows.py", "test_windows.py"}, set(tree))
        self.assertEqual((workspace / "windows.py").read_bytes(), judged[-1]["windows.py"])
        agent_side = [tree for command, tree in seen if command == VISIBLE_TESTS]
        self.assertEqual(len(seen) - len(judged), len(agent_side))
        for tree in agent_side:
            self.assertEqual(b"import unittest\n", tree["test_acceptance.py"])
        self.assertEqual({"test_aaa_patch.py": "added", "sitecustomize.py": "added", "test_acceptance.py": "added",
                          "test_windows.py": "modified"},
                         {entry["path"]: entry["change"] for entry in result["evidence"]["test_diff"]["entries"]})

    def test_workspace_copy_never_exposes_the_protected_files(self):
        workspace = self.repair_workspace()
        self.assertEqual({"windows.py", "test_windows.py"}, {path.name for path in workspace.iterdir()})
        for destination in (workspace, REPAIR / "fixture/copy"):
            with self.assertRaises(ValueError):
                task_acceptance.prepare_workspace(REPAIR, destination)


class OracleTests(Case):
    """Protected tests, references, canary and the evidence about agent-written tests."""

    def test_valid_repair_is_accepted_with_reference_outcomes_and_reproducer_record(self):
        result = self.repair("valid-repair", run_id="run-1", attempt_id="attempt-2")
        self.assertEqual(("accepted", []), (result["verdict"], result["reasons"]))
        self.assertEqual(("window-counts-repair", {"run_id": "run-1", "attempt_id": "attempt-2"}),
                         (result["task_id"], result["subject"]))
        self.assertEqual([("baseline", "fail", "fail"), ("trusted", "pass", "pass"), ("closed-boundary", "fail", "fail"),
                          ("unvalidated-width", "fail", "fail")],
                         [(row["id"], row["expected"], row["observed"])
                          for row in result["components"]["references"]["outcomes"]])
        oracle = result["components"]["oracle"]
        self.assertEqual(("fail", "pass"), (oracle["baseline"], oracle["post_change"]))
        self.assertFalse(oracle["run"]["isolated"])  # authored fixture on the host; never model output
        reference = hashlib.sha256((REPAIR / "reference/windows.py").read_bytes()).hexdigest()
        self.assertEqual([{"path": "windows.py", "sha256": reference, "changed": True}], oracle["evaluated"])
        generated = result["evidence"]["generated_tests"]
        self.assertEqual(("validated", ["test_reproducer.py"]), (generated["status"], generated["files"]))
        self.assertEqual({"baseline": "fail", "post_change": "pass"}, generated["reproducer"])
        # The reproducer alone misses one mutant; it is valid because it detects a known bug and passes the reference.
        self.assertEqual({"baseline": "fail", "trusted": "pass", "closed-boundary": "fail", "unvalidated-width": "pass",
                          "post-change": "pass"}, {row["id"]: row["observed"] for row in generated["outcomes"]})
        self.assertEqual("validated", result["evidence"]["workspace_suite"]["status"])
        self.assertNotIn("def window_counts", json.dumps(result))

    def test_selective_assertion_equality_and_source_reading_bypasses_are_rejected(self):
        for case in ("selective-assertion", "always-equal", "reads-oracle"):
            with self.subTest(case=case):
                result = self.repair(case)
                self.assertEqual("rejected", result["verdict"])
                self.assertEqual("failed", result["components"]["oracle"]["status"])

    def test_untouched_fixture_is_rejected(self):
        result = self.repair()
        self.assertEqual(("rejected", ["oracle:post-change-failed"]), (result["verdict"], result["reasons"]))
        self.assertEqual(("clean", "none", "none"), tuple(result["evidence"][name]["status"] for name in
                                                          ("test_diff", "workspace_suite", "generated_tests")))

    def test_known_bad_patch_passes_the_visible_tests_and_is_rejected(self):
        workspace = self.repair_workspace("hardcoded-answers")
        self.assertEqual("tests-passed", host_run(workspace, {}, VISIBLE_TESTS)["status"])
        feedback = self.fresh("feedback.json")
        feedback.write_text(json.dumps({"text": "Project check 1: passed (exit 0)", "failures": False, "unavailable": False}))
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, claim=True, guidance=feedback, run=host_run)
        self.assertEqual(("rejected", ["oracle:post-change-failed"]), (result["verdict"], result["reasons"]))
        self.assertEqual(("fail", "fail"), (result["components"]["oracle"]["baseline"], result["components"]["oracle"]["post_change"]))
        self.assertEqual({"completed": True, "decides_verdict": False, "contradicted": True}, result["evidence"]["claim"])
        guidance = result["evidence"]["guidance"]
        self.assertEqual((False, True, False), (guidance["authoritative"], guidance["contradicted"], guidance["failures"]))
        self.assertNotIn("Project check", json.dumps(result))

    def test_manufactured_pass_cannot_change_parent_assertions(self):
        for case in ("patched-runner", "forced-exit", "rewrites-protected-test"):
            with self.subTest(case=case):
                result = self.repair(case)
                self.assertEqual(("failed", "not-applicable"), statuses(result, ("oracle", "interference")))
                self.assertEqual(("rejected", ["oracle:post-change-failed"]), (result["verdict"], result["reasons"]))

    def test_submitted_code_cannot_rewrite_the_acceptance_source_while_it_runs(self):
        before = task_acceptance.seal(REPAIR)
        result = self.repair("rewrites-protected-test")
        # The rewrite reached only the private evaluation copy, and the canary exposed it.
        self.assertEqual((REPAIR_SEAL, REPAIR_SEAL), (before, task_acceptance.seal(REPAIR)))
        self.assertEqual("rejected", result["verdict"])
        self.assertEqual("accepted", self.repair("valid-repair")["verdict"])

    def test_rewritten_test_cannot_satisfy_the_fixture_and_its_diff_is_recorded(self):
        result = self.repair("rewritten-test")
        self.assertEqual(("rejected", ["oracle:post-change-failed"]), (result["verdict"], result["reasons"]))
        entry, = result["evidence"]["test_diff"]["entries"]
        self.assertEqual(("test_windows.py", "modified"), (entry["path"], entry["change"]))
        self.assertEqual(hashlib.sha256((REPAIR / "fixture/test_windows.py").read_bytes()).hexdigest(), entry["protected_sha256"])
        self.assertNotEqual(entry["protected_sha256"], entry["workspace_sha256"])
        self.assertGreater(entry["removed_lines"], 0)
        self.assertIn("+        self.assertIsInstance(window_counts([1, 9, 10], 10), list)", entry["diff"])
        for name in ("workspace_suite", "generated_tests"):
            evidence = result["evidence"][name]
            self.assertEqual(("invalid", ["detects-no-known-bug", "baseline-reproducer-passed"]), (evidence["status"], evidence["reasons"]))
            # The rewritten tests report a pass for the unrepaired baseline: no reproducer.
            self.assertEqual("pass", evidence["reproducer"]["baseline"])

    def test_test_file_that_patches_the_application_is_detected(self):
        workspace = self.repair_workspace("patched-application")
        self.assertEqual("tests-passed", host_run(workspace, {}, VISIBLE_TESTS)["status"])  # the manufactured pass
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, claim=True, run=host_run)
        self.assertEqual(("rejected", ["oracle:post-change-failed"]), (result["verdict"], result["reasons"]))
        self.assertFalse(result["components"]["oracle"]["evaluated"][0]["changed"])
        suite = result["evidence"]["workspace_suite"]
        self.assertEqual(("invalid", ["detects-no-known-bug", "baseline-reproducer-passed"], ["test_aaa_patch.py"]),
                         (suite["status"], suite["reasons"], suite["files"]))
        self.assertEqual({"pass"}, {row["observed"] for row in suite["outcomes"]})
        self.assertEqual("invalid", result["evidence"]["generated_tests"]["status"])

    def test_deleted_visible_test_is_recorded(self):
        workspace = self.repair_workspace()
        (workspace / "test_windows.py").unlink()
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run)
        self.assertEqual("rejected", result["verdict"])
        entry, = result["evidence"]["test_diff"]["entries"]
        self.assertEqual(("test_windows.py", "deleted"), (entry["path"], entry["change"]))
        self.assertEqual(("invalid", "none"), tuple(result["evidence"][name]["status"] for name in
                                                    ("workspace_suite", "generated_tests")))

    def test_editable_file_that_is_a_link_is_not_evaluated(self):
        workspace = self.repair_workspace()
        (workspace / "windows.py").unlink()
        os.symlink(REPAIR / "reference/windows.py", workspace / "windows.py")
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run)
        self.assertEqual(("rejected", "oracle:editable-symbolic-link"), (result["verdict"], result["reasons"][0]))

    def test_references_that_do_not_behave_as_declared_leave_the_oracle_unproven(self):
        source = self.fresh("source")
        shutil.copytree(REPAIR, source)
        shutil.copy(REPAIR / "reference/windows.py", source / "mutants/closed-boundary/windows.py")
        result = checked_task(source, self.repair_workspace("valid-repair"), task_acceptance.seal(source),
                                            run=host_run)
        task_acceptance.validate_result(result)
        self.assertEqual(("failed", "unavailable", "unavailable"), statuses(result, ("references", "oracle", "interference")))
        self.assertEqual(("unavailable", "references:closed-boundary"), (result["verdict"], result["reasons"][0]))

    def test_without_the_sandbox_the_oracle_is_unavailable_never_accepted(self):
        calls = []
        workspace = self.repair_workspace("valid-repair")
        with patch("coding_eval.sandbox_available", return_value="no sandbox"), \
                patch("coding_eval.subprocess.run", side_effect=lambda *a, **k: calls.append(a)):
            result = checked_task(REPAIR, workspace, REPAIR_SEAL, claim=True)
        task_acceptance.validate_result(result)
        self.assertEqual([], calls)
        self.assertEqual(("passed", "unavailable", "unavailable", "unavailable"), statuses(result)[:4])
        self.assertEqual("unavailable", result["verdict"])
        self.assertEqual("unavailable-no-isolated-executor", result["components"]["references"]["outcomes"][0]["run"]["status"])
        self.assertEqual(("unavailable", "unavailable"), tuple(result["evidence"][name]["status"] for name in
                                                              ("workspace_suite", "generated_tests")))
        self.assertTrue(result["evidence"]["claim"]["contradicted"])

    def test_default_executor_runs_every_command_inside_the_sandbox(self):
        seen = []

        class Failed:
            returncode, stdout, stderr = 1, b"", b""

        with patch("coding_eval.sandbox_available", return_value=None), \
                patch("coding_eval.subprocess.run", side_effect=lambda argv, **options: seen.append(argv) or Failed()):
            result = checked_task(REPAIR, self.repair_workspace("valid-repair"), REPAIR_SEAL)
        # Every stand-in run fails, so the trusted reference does not pass and the oracle stays unproven.
        self.assertEqual(("unavailable", "references:trusted"), (result["verdict"], result["reasons"][0]))
        self.assertEqual(14, len(seen))  # four references, then five for each agent-side suite
        for argv in seen:
            self.assertEqual(["/usr/bin/timeout", "/usr/bin/bwrap"], [argv[0], argv[4]])
            self.assertIn("--unshare-all", argv)
            command = argv[argv.index("--") + 1:]
            self.assertTrue(command == VISIBLE_TESTS or command[:4] == ["/usr/bin/python3", "-I", "-B", "-c"])

    @requires_input(platform.system() == "Linux" and coding_eval.sandbox_available() is None,
                    "network-free bubblewrap sandbox unavailable on this host")
    def test_sandbox_grades_the_valid_and_known_bad_workspaces(self):
        self.assertEqual("accepted", self.repair("valid-repair", run=coding_eval.run_task)["verdict"])
        result = self.repair("hardcoded-answers", run=coding_eval.run_task)
        self.assertEqual("rejected", result["verdict"])
        self.assertTrue(result["components"]["oracle"]["run"]["isolated"])


class CorrectionTests(Case):
    def test_seal_without_actual_agent_routes_is_unavailable(self):
        result = task_acceptance.check_task(REPAIR, self.repair_workspace(), REPAIR_SEAL, run=host_run)
        task_acceptance.validate_result(result)
        self.assertEqual("unavailable", result["verdict"])
        self.assertEqual("attempt-lifetime-not-established", result["components"]["protection"]["reason"])

    def test_actual_unrestricted_file_route_fails_protection(self):
        def unrestricted(operation, path):
            return task_acceptance.perform_probe(operation, path)
        result = self.repair("valid-repair", agent_routes={"unrestricted-file-tool": unrestricted})
        self.assertEqual("unavailable", result["verdict"])
        self.assertEqual("failed", result["components"]["protection"]["status"])
        self.assertTrue(any(row["operation"] == "write" and row["status"] == "failed"
                            for row in result["components"]["protection"]["probes"]))

    def test_blanket_denial_is_not_boundary_proof(self):
        result = self.repair("valid-repair", agent_routes={"broken-route": lambda *args: "denied"})
        self.assertEqual("agent-route-control-failed", result["components"]["protection"]["reason"])

    def test_workspace_file_errors_preserve_rejection_and_evidence(self):
        for case in ("directory", "mode000", "extra-unreadable", "unreadable-directory", "read-error"):
            with self.subTest(case=case):
                workspace = self.repair_workspace()
                if case == "directory":
                    (workspace / "windows.py").unlink()
                    (workspace / "windows.py").mkdir()
                elif case == "mode000":
                    (workspace / "windows.py").chmod(0)
                elif case == "extra-unreadable":
                    (workspace / "extra.py").write_text("unreadable")
                    (workspace / "extra.py").chmod(0)
                elif case == "unreadable-directory":
                    (workspace / "extra").mkdir()
                    (workspace / "extra").chmod(0)
                original = Path.read_bytes
                def read(path):
                    if case == "read-error" and path == workspace / "windows.py":
                        raise PermissionError("fixture access denial")
                    return original(path)
                with patch.object(Path, "read_bytes", read):
                    result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run)
                task_acceptance.validate_result(result)
                self.assertEqual("unavailable" if case in ("mode000", "read-error") else "rejected", result["verdict"])
                self.assertEqual("unavailable" if case in ("mode000", "read-error") else "failed", result["components"]["oracle"]["status"])
                self.assertEqual("passed", result["components"]["references"]["status"])
                if case in ("extra-unreadable", "unreadable-directory"):
                    self.assertEqual("unavailable", result["evidence"]["workspace_suite"]["status"])

    def test_tests_repairing_or_ignoring_the_baseline_bug_are_invalid(self):
        for case in ("targeted-test-repair", "weakened-reproducer"):
            with self.subTest(case=case):
                result = self.repair(case)
                self.assertEqual("rejected", result["verdict"])
                for key in ("workspace_suite", "generated_tests"):
                    row = result["evidence"][key]
                    self.assertEqual("invalid", row["status"])
                    self.assertIn("baseline-reproducer-passed", row["reasons"])
                    self.assertEqual("pass", row["reproducer"]["baseline"])
                    self.assertTrue(any(outcome["expected"] == "fail" and outcome["observed"] == "fail"
                                        for outcome in row["outcomes"]))

    def test_new_receipts_join_through_existing_run_record_consumer(self):
        import run_record
        for case, extra, expected in (("valid-repair", {}, "accepted"), (None, {}, "rejected"),
                                      ("valid-repair", {"agent_routes": None}, "unavailable")):
            result = self.repair(case, run_id="compat-run", attempt_id="compat-attempt", **extra)
            joined = run_record.acceptance_from_result(result)
            self.assertEqual(expected, joined["verdict"])
            self.assertEqual("compat-run", joined["run_id"])

    def test_legacy_in_process_oracle_stays_unavailable(self):
        source = self.fresh("legacy")
        shutil.copytree(REPAIR, source)
        spec = json.loads((source / "acceptance.json").read_text())
        spec["oracle"].pop("observations")
        (source / "acceptance.json").write_text(json.dumps(spec))
        result = checked_task(source, self.repair_workspace("valid-repair"), task_acceptance.seal(source), run=host_run)
        self.assertEqual("unavailable", result["verdict"])
        self.assertIn("unavailable-unsafe-in-process-oracle", json.dumps(result))


class Records(http.server.BaseHTTPRequestHandler):
    """Stand-in for the external system's record pages; revision 3 is current."""
    seen = []

    def log_message(self, *arguments):
        pass

    def do_GET(self):
        Records.seen.append(self.path)
        match = re.fullmatch(r"/records/job-7/revisions/([23])", self.path)
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/records/job-7/revisions/3")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif not match:
            self.send_error(404)
        else:
            payload = json.dumps({"record": "job-7", "revision": int(match[1])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


class FinalCheckTests(Case):
    """External state, deliverable and linkage are opened by the checker and recorded apart."""

    FINAL = ("external_state", "deliverable", "linkage")

    def setUp(self):
        super().setUp()
        Records.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Records)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def inputs(self, case, answer=None):
        root = FIXTURES / "cases/record-publication" / case
        workspace = self.fresh("workspace")
        shutil.copytree(root / "workspace", workspace)
        text = (root / "answer.json").read_text() if answer is None else answer
        answer_file = self.fresh("answer.json")
        answer_file.write_text(text.replace("{origin}", self.origin))
        return workspace, {"answer": answer_file, "state": root / "state.json", "origins": [self.origin]}

    def publication(self, case, answer=None, **changes):
        workspace, options = self.inputs(case, answer)
        result = checked_task(PUBLICATION, workspace, PUBLICATION_SEAL, **{**options, **changes})
        return task_acceptance.validate_result(result)

    def test_fixtures_record_state_deliverable_and_linkage_separately(self):
        expected = {
            "valid": (("passed", "passed", "passed"), "accepted", []),
            "wrong-artifact": (("passed", "failed", "passed"), "rejected", ["deliverable:summary-record"]),
            "wrong-state": (("failed", "passed", "passed"), "rejected",
                            ["external_state:record-closed", "external_state:resolution-fixed"]),
            "stale-link": (("passed", "passed", "failed"), "rejected", ["linkage:link-revision-current"]),
        }
        for case, (final, verdict, reasons) in expected.items():
            with self.subTest(case=case):
                result = self.publication(case)
                self.assertEqual((final, verdict, reasons), (statuses(result, self.FINAL), result["verdict"], result["reasons"]))
                self.assertEqual(("passed", "not-applicable", "not-applicable", "not-applicable"), statuses(result)[:4])

    def test_checker_opens_the_artifact_and_link_itself(self):
        workspace, options = self.inputs("valid")
        result = checked_task(PUBLICATION, workspace, PUBLICATION_SEAL, **options)
        self.assertEqual(["/records/job-7/revisions/3"], Records.seen)  # one request, made by the checker
        deliverable = result["components"]["deliverable"]
        summary = (workspace / "out/summary.json").read_bytes()
        self.assertEqual([{"id": "summary", "status": "passed", "sha256": hashlib.sha256(summary).hexdigest(),
                           "bytes": len(summary)}], deliverable["artifacts"])
        link, = deliverable["links"]
        url = f"{self.origin}/records/job-7/revisions/3"
        self.assertEqual(("passed", hashlib.sha256(url.encode()).hexdigest()), (link["status"], link["url_sha256"]))
        self.assertEqual(hashlib.sha256(b'{"record": "job-7", "revision": 3}').hexdigest(), link["sha256"])
        state = result["components"]["external_state"]["observation"]
        self.assertEqual(hashlib.sha256(options["state"].read_bytes()).hexdigest(), state["sha256"])
        for row in result["components"]["linkage"]["checks"]:
            self.assertEqual(row["left_sha256"], row["right_sha256"])
        stale = self.publication("stale-link")["components"]["linkage"]["checks"][-1]
        self.assertEqual(("link-revision-current", "failed", "mismatch"), (stale["id"], stale["status"], stale["reason"]))
        self.assertNotEqual(stale["left_sha256"], stale["right_sha256"])

    def test_completion_claim_and_feedback_never_change_the_result(self):
        feedback = self.fresh("feedback.json")
        feedback.write_text(json.dumps({"failures": False, "unavailable": False}))
        plain, claimed = self.publication("wrong-state"), self.publication("wrong-state", claim=True, guidance=feedback)
        self.assertEqual(("rejected", "rejected"), (plain["verdict"], claimed["verdict"]))
        self.assertEqual((statuses(plain), plain["reasons"]), (statuses(claimed), claimed["reasons"]))
        self.assertTrue(claimed["evidence"]["claim"]["contradicted"] and claimed["evidence"]["guidance"]["contradicted"])
        feedback.write_text(json.dumps({"failures": True, "unavailable": False}))
        doubted = self.publication("valid", claim=False, guidance=feedback)
        self.assertEqual("accepted", doubted["verdict"])
        self.assertFalse(doubted["evidence"]["claim"]["contradicted"] or doubted["evidence"]["guidance"]["contradicted"])
        feedback.write_text(json.dumps({"failures": "no"}))
        self.assertEqual("guidance-unreadable", self.publication("valid", guidance=feedback)["evidence"]["guidance"]["reason"])

    def test_unresolved_state_and_unopened_links_stay_explicit(self):
        inside, options = self.inputs("valid")
        shutil.copy(options["state"], inside / "state.json")
        empty = self.fresh("state.json")
        empty.write_text("not JSON")
        cases = {
            "no state": ({"state": None}, ("unavailable", "passed", "unavailable"), "state-unresolved"),
            "agent-writable state": ({"state": inside / "state.json"}, ("unavailable", "passed", "unavailable"),
                                     "state-inside-workspace"),
            "unreadable state": ({"state": empty}, ("unavailable", "passed", "unavailable"), "state-unreadable"),
            "origin not allowed": ({"origins": []}, ("passed", "unavailable", "unavailable"), "link-origin-not-allowed"),
            "no answer": ({"answer": None}, ("passed", "unavailable", "unavailable"), "answer-not-supplied"),
        }
        for name, (changes, final, reason) in cases.items():
            with self.subTest(case=name):
                result = checked_task(PUBLICATION, inside, PUBLICATION_SEAL, claim=True, **{**options, **changes})
                task_acceptance.validate_result(result)
                self.assertEqual((final, "unavailable"), (statuses(result, self.FINAL), result["verdict"]))
                self.assertIn(reason, json.dumps(result["components"]))
                self.assertTrue(result["evidence"]["claim"]["contradicted"])
        refused = '{"record": "job-7", "revision": 3, "artifact": "out/summary.json", "link": "http://127.0.0.1:9/records"}'
        result = self.publication("valid", answer=refused, origins=[self.origin, "http://127.0.0.1:9"])
        self.assertEqual(("unavailable", "link-unreachable"), (result["verdict"], result["components"]["deliverable"]["links"][0]["reason"]))

    def test_dead_redirected_escaping_and_malformed_deliverables_fail(self):
        answer = '{"record": "job-7", "revision": 3, "artifact": "ARTIFACT", "link": "{origin}LINK"}'
        outside = self.fresh("outside.json")
        outside.write_text((FIXTURES / "cases/record-publication/valid/workspace/out/summary.json").read_text())
        cases = {
            "dead link": ("out/summary.json", "/records/job-7/revisions/9", "link-http-404"),
            "redirected link": ("out/summary.json", "/moved", "link-http-302"),
            "malformed link": ("out/summary.json", "/records/job 7", "link-not-a-url"),
            "path leaves the workspace": (f"../{outside.name}", "/records/job-7/revisions/3", "artifact-path-not-relative"),
            "absolute path": (str(outside), "/records/job-7/revisions/3", "artifact-path-not-relative"),
            "missing file": ("out/absent.json", "/records/job-7/revisions/3", "artifact-missing"),
            "linked file": ("out/linked.json", "/records/job-7/revisions/3", "artifact-symbolic-link"),
        }
        for name, (artifact, link, reason) in cases.items():
            with self.subTest(case=name):
                workspace, options = self.inputs("valid", answer.replace("ARTIFACT", artifact).replace("LINK", link))
                os.symlink(outside, workspace / "out/linked.json")
                result = checked_task(PUBLICATION, workspace, PUBLICATION_SEAL, claim=True, **options)
                self.assertEqual(("failed", "rejected"), (result["components"]["deliverable"]["status"], result["verdict"]))
                self.assertIn(reason, json.dumps(result["components"]["deliverable"]))
        prose = self.publication("valid", answer="Done. Record job-7 is closed and the summary is in out/summary.json.")
        self.assertEqual(("rejected", "answer-not-a-json-object"),
                         (prose["verdict"], prose["components"]["deliverable"]["answer"]["reason"]))
        fenced = (FIXTURES / "cases/record-publication/valid/answer.json").read_text()
        result = self.publication("valid", answer=f"```json\n{fenced}```\n")
        self.assertEqual(("accepted", True), (result["verdict"], result["components"]["deliverable"]["answer"]["fenced"]))

    def test_one_shot_cli_cannot_establish_attempt_lifetime(self):
        workspace, options = self.inputs("valid")
        output = self.fresh("result.json")
        status, printed, error = self.run_cli("check", PUBLICATION, output, "--workspace", workspace,
                                              "--seal", PUBLICATION_SEAL, "--state", options["state"],
                                              "--answer", options["answer"], "--allow-origin", self.origin)
        self.assertEqual(3, status)
        result = task_acceptance.load_result(output)
        self.assertEqual("unavailable", result["verdict"])
        self.assertIn("protection:attempt-lifetime-not-established", result["reasons"])
        self.assertEqual((0, "unavailable", ""), self.run_cli("validate", output))
        self.assertEqual("", error)

    def test_unreadable_artifact_preserves_external_state_and_rejection(self):
        workspace, options = self.inputs("wrong-state")
        (workspace / "out/summary.json").chmod(0)
        result = checked_task(PUBLICATION, workspace, PUBLICATION_SEAL, **options)
        task_acceptance.validate_result(result)
        self.assertEqual("rejected", result["verdict"])
        self.assertEqual(("failed", "unavailable", "unavailable"), statuses(result, self.FINAL))
        self.assertEqual("artifact-unreadable", result["components"]["deliverable"]["artifacts"][0]["reason"])

    def test_expected_link_origin_mismatch_is_rejected_without_fetch(self):
        source = self.fresh("source")
        shutil.copytree(PUBLICATION, source)
        spec = json.loads((source / "acceptance.json").read_text())
        spec["deliverable"]["links"][0]["origin"] = "https://expected.invalid"
        (source / "acceptance.json").write_text(json.dumps(spec))
        workspace, options = self.inputs("valid")
        result = checked_task(source, workspace, task_acceptance.seal(source), **options)
        self.assertEqual("rejected", result["verdict"])
        self.assertEqual("link-origin-mismatch", result["components"]["deliverable"]["links"][0]["reason"])
        self.assertEqual([], Records.seen)

    def test_answer_only_completion_cannot_establish_acceptance(self):
        source = self.fresh("source")
        source.mkdir()
        spec = json.loads((PUBLICATION / "acceptance.json").read_text())
        for name in ("external_state", "linkage"):
            spec[name] = {"applicable": False, "reason": "No state declared."}
        spec["deliverable"] = {"answer": [{"id": "done", "path": ["status"], "equals": "done"}]}
        (source / "acceptance.json").write_text(json.dumps(spec))
        workspace, options = self.inputs("valid", '{"status":"done"}')
        result = checked_task(source, workspace, task_acceptance.seal(source), claim=True, **options)
        self.assertEqual("unavailable", result["verdict"])
        self.assertEqual("independent-observation-required", result["components"]["deliverable"]["reason"])

    def test_artifact_digest_links_an_observation_to_the_evaluated_file(self):
        # The shape a hidden play-through uses: its observation names the digest of the file it ran.
        source, workspace = self.fresh("source"), self.fresh("workspace")
        source.mkdir()
        workspace.mkdir()
        (source / "acceptance.json").write_text(json.dumps({
            "schema": 1, "kind": "workstation-task-acceptance-spec", "task_id": "observed-run", "version": "1",
            "oracle": {"applicable": False, "reason": "The evaluator observes a run and supplies it as state."},
            "external_state": {"checks": [{"id": "run-completed", "path": ["completed"], "equals": True}]},
            "deliverable": {"artifacts": [{"id": "program", "path": "program.txt", "format": "bytes"}]},
            "linkage": {"checks": [{"id": "observed-this-file", "left": {"from": "artifact-sha256", "name": "program"},
                                    "right": {"from": "state", "path": ["evaluated_sha256"]}}]}}))
        (workspace / "program.txt").write_bytes(b"first version\n")
        state = self.fresh("state.json")
        state.write_text(json.dumps({"completed": True, "evaluated_sha256": hashlib.sha256(b"first version\n").hexdigest()}))
        sealed = task_acceptance.seal(source)
        self.assertEqual("accepted", checked_task(source, workspace, sealed, state=state)["verdict"])
        (workspace / "program.txt").write_bytes(b"changed after the observation\n")
        result = checked_task(source, workspace, sealed, state=state)
        self.assertEqual(("rejected", ["linkage:observed-this-file"]), (result["verdict"], result["reasons"]))


class ContractTests(Case):
    """The specification, result record and command line that other tools depend on."""

    def test_contract_names_are_stable(self):
        self.assertEqual((1, "workstation-task-acceptance-spec", "workstation-task-acceptance-result"),
                         (task_acceptance.SCHEMA, task_acceptance.SPEC_KIND, task_acceptance.RESULT_KIND))
        self.assertEqual(("protection", "references", "oracle", "interference", "external_state", "deliverable", "linkage"),
                         task_acceptance.COMPONENTS)
        self.assertEqual((("accepted", "rejected", "unavailable"), ("passed", "failed", "unavailable", "not-applicable")),
                         (task_acceptance.VERDICTS, task_acceptance.STATUSES))
        self.assertEqual({"accepted": 0, "rejected": 1, "unavailable": 3}, task_acceptance.EXIT_STATUS)

    def test_verdict_follows_only_from_the_components(self):
        def decide(**changed):
            components = {name: {"status": "passed"} for name in task_acceptance.COMPONENTS}
            components.update({name: {"status": status} for name, status in changed.items()})
            return task_acceptance.verdict(components)
        self.assertEqual("accepted", decide(external_state="not-applicable", linkage="not-applicable"))
        for name in task_acceptance.ATTEMPT_COMPONENTS:
            self.assertEqual("rejected", decide(**{name: "failed"}), name)
            self.assertEqual("unavailable", decide(**{name: "unavailable"}), name)
            self.assertEqual("rejected", decide(**{name: "failed"}, references="unavailable"), name)
        # An unverified or unproven acceptance source is never a measured failure of the attempt.
        self.assertEqual("unavailable", decide(protection="failed", oracle="failed"))
        self.assertEqual("unavailable", decide(protection="unavailable"))
        self.assertEqual("unavailable", decide(protection="not-applicable"))
        self.assertEqual("unavailable", decide(references="failed"))

    def test_result_cannot_be_edited_into_acceptance(self):
        result = self.repair()
        path = self.fresh("result.json")
        coding_eval.write(path, result)
        self.assertEqual(result, task_acceptance.load_result(path))
        edits = {
            "verdict": lambda value: value.update(verdict="accepted"),
            "reasons": lambda value: value.update(reasons=[]),
            "missing component": lambda value: value["components"].pop("interference"),
            "unknown status": lambda value: value["components"]["oracle"].update(status="skipped"),
            "claim decides": lambda value: value["evidence"]["claim"].update(decides_verdict=True),
            "guidance decides": lambda value: value["evidence"]["guidance"].update(authoritative=True),
            "missing evidence": lambda value: value["evidence"].pop("test_diff"),
            "subject": lambda value: value["subject"].update(run_id="run 1"),
            "malformed rows": lambda value: value["components"]["oracle"].update(checks=[{"status": "failed"}]),
            "malformed evidence": lambda value: value["evidence"].update(claim="completed"),
            "schema": lambda value: value.update(schema=2),
        }
        for name, edit in edits.items():
            changed = copy.deepcopy(result)
            edit(changed)
            with self.subTest(edit=name), self.assertRaises(ValueError):
                task_acceptance.validate_result(changed)

    def test_specification_declares_every_component_and_no_way_to_skip_one(self):
        repair = task_acceptance.read_tree(REPAIR)
        publication = task_acceptance.read_tree(PUBLICATION)
        task_acceptance.validate_spec(json.loads(repair["acceptance.json"]), repair)
        edits = {
            "missing component": (publication, lambda spec: spec.pop("linkage")),
            "skip member": (repair, lambda spec: spec.update(skip=["oracle"])),
            "no reason": (publication, lambda spec: spec.update(oracle={"applicable": False})),
            "applicable without checks": (publication, lambda spec: spec.update(external_state={"applicable": True})),
            "nothing declared": (publication, lambda spec: [spec.update({name: {"applicable": False, "reason": "none"}})
                                                            for name in ("external_state", "deliverable", "linkage")]),
            "linkage without state": (publication, lambda spec: spec.update(external_state={"applicable": False, "reason": "x"})),
            "two state selectors": (publication, lambda spec: spec["linkage"]["checks"][0].update(
                left={"from": "state", "path": ["records"]})),
            "unknown artifact": (publication, lambda spec: spec["linkage"]["checks"][1]["left"].update(name="absent")),
            "duplicate identifier": (publication, lambda spec: spec["external_state"]["checks"][1].update(id="record-closed")),
            "equals and type": (publication, lambda spec: spec["deliverable"]["answer"][0].update(type="string")),
            "link without answer": (publication, lambda spec: spec["deliverable"]["links"][0].update(path="out/link.json")),
            "escaping editable": (repair, lambda spec: spec.update(editable=["../windows.py"])),
            "editable without oracle": (publication, lambda spec: spec.update(editable=["windows.py"])),
            "host command": (repair, lambda spec: spec["oracle"].update(command=["python3", "-m", "unittest"])),
            "invalid legacy canary": (repair, lambda spec: spec["oracle"].update(canary="absent")),
            "canary beside the oracle": (repair, lambda spec: spec["oracle"].update(canary="reference")),
            "unbounded time": (repair, lambda spec: spec["oracle"].update(seconds=3600)),
            "no trusted reference": (repair, lambda spec: spec["oracle"].update(references=spec["oracle"]["references"][1:])),
            "no known bug": (repair, lambda spec: spec["oracle"].update(baseline="pass", references=spec["oracle"]["references"][:1])),
            "overlay outside editable": (repair, lambda spec: spec["oracle"]["references"][0].update(overlay="fixture")),
            "reserved reference name": (repair, lambda spec: spec["oracle"]["references"][0].update(id="canary")),
            "path pattern": (repair, lambda spec: spec["tests"].update(patterns=["tests/*.py"])),
        }
        for name, (files, edit) in edits.items():
            spec = json.loads(files["acceptance.json"])
            edit(spec)
            with self.subTest(edit=name), self.assertRaises(ValueError):
                task_acceptance.validate_spec(spec, files)
        shadowed = {**repair, "fixture/test_acceptance.py": b""}
        with self.assertRaises(ValueError):
            task_acceptance.validate_spec(json.loads(repair["acceptance.json"]), shadowed)

    def test_command_line_checks_the_repair_task_on_this_host(self):
        self.assertEqual((0, REPAIR_SEAL, ""), self.run_cli("seal", REPAIR))
        destination = self.fresh("workspace")
        self.assertEqual(0, self.run_cli("workspace", REPAIR, destination)[0])
        self.assertEqual(2, self.run_cli("workspace", REPAIR, destination)[0])
        shutil.copytree(FIXTURES / "cases/window-counts/valid-repair", destination, dirs_exist_ok=True)
        output = self.fresh("result.json")
        status, printed, _ = self.run_cli("check", REPAIR, output, "--workspace", destination, "--seal", REPAIR_SEAL)
        # The command line always uses the sandbox: accepted where it exists, unavailable elsewhere.
        expected = ("accepted", 0) if platform.system() == "Linux" and coding_eval.sandbox_available() is None else ("unavailable", 3)
        self.assertEqual(expected, (printed.split()[0], status))
        self.assertEqual(expected[0], task_acceptance.load_result(output)["verdict"])

    def test_repair_fixture_is_the_existing_python_bugfix_task(self):
        task = ROOT / "infrastructure/sglang/assistant-tasks/python-bugfix"
        for ours, theirs in (("fixture/windows.py", "fixture/windows.py"), ("fixture/test_windows.py", "fixture/test_windows.py"),
                             ("reference/windows.py", "reference/windows.py")):
            self.assertEqual((task / theirs).read_bytes(), (REPAIR / ours).read_bytes(), ours)


if __name__ == "__main__":
    unittest.main()
