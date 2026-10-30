"""Exercise the controller with inert, hash-pinned CPU owner programs.

These fixtures establish delegation and failure handling, not GPU admission,
remote cleanup, model correctness or the protected owner's supervision.
"""
import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import hyperloom as integration
import hyperloom_owner as adapter


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OwnerControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.session = self.root / "controller"
        self.session.mkdir()
        self.source = self.root / "author"
        self.source.mkdir()
        self.selection = self.root / "selection.json"
        self.selection.write_text('{"selected":"unchanged"}\n')
        self.boot = "00000000-0000-0000-0000-000000000001"
        self.output = self.root / "reports"

    def fixture(self, fail=None, requests_per_start=30):
        # The real adapter imports this fixture supervisor and invokes real
        # child entry points. It implements no GPU/SSH/privileged operation.
        supervisor = '''import json, subprocess
from pathlib import Path
def run(name, argv, seconds, *, cwd):
    with (Path(cwd)/'attempts.jsonl').open('a') as stream:
        stream.write(json.dumps({'name':name,'argv':argv,'seconds':seconds})+'\\n')
    subprocess.run(argv, cwd=cwd, check=True, timeout=5)
'''
        self.binding = {"nonce": "a" * 16, "boot_id": self.boot,
            "target_root": "/home/developer/fp8-kv-benchmark." + "a" * 16,
            "remote_upload_root": "/home/developer/fp8-benchmark-upload." + "a" * 16,
            "protected_root": "/var/tmp/fp8-benchmark-source." + "a" * 16,
            "manifest_sha256": "b" * 64, "archive_sha256": "c" * 64}
        self.put(self.session / "LAUNCHER-BINDINGS.json", self.binding)
        self.put(self.source / "LAUNCHER-BINDINGS.json", self.binding)
        self.put(self.session / "PACKAGE-IDENTITIES.json", self.binding)
        self.put(self.source / "PACKAGE-IDENTITIES.json", self.binding)
        (self.source / "review").mkdir()
        self.put(self.source / "review/FINAL-SETTINGS.json", {"mode": "native-http-fp8-sprint01-v1",
                 "requests": [{"ordinal": i} for i in range(requests_per_start)]})
        (self.source / "FINAL-ARTIFACTS.sha256").write_text(
            sha(self.source / "review/FINAL-SETTINGS.json") + "  review/FINAL-SETTINGS.json\n")
        for name in adapter.CONTROLLER_FILES:
            (self.session / name).write_text("# Inert CPU helper\n")
        (self.session / "local_commands.py").write_text(supervisor)
        for filename, stage in (("run_native.py", None), ("collect_results.py", "collect"),
                                ("observe_closeout.py", "closeout"),
                                ("observe_failed_closeout.py", "failed-closeout")):
            code = "import sys\nfrom pathlib import Path\nroot=Path(__file__).resolve().parent\n"
            code += "stage=sys.argv[1]\n" if stage is None else f"stage={stage!r}\n"
            code += "with (root/'executed.txt').open('a') as stream: stream.write(stage+'\\n')\n"
            code += f"if stage == {fail!r}: raise RuntimeError('retained CPU fixture failure')\n"
            if stage == "collect":
                code += (f"p=root/'collected/workload/{Path(self.binding['target_root']).name}'\n"
                         "p.mkdir(parents=True)\n(p/'FINAL-ARTIFACTS.sha256').write_text('fixture seal\\n')\n")
            (self.session / filename).write_text(code)
        self.auth = {"status": "REGISTERED_CPU_FIXTURE", "native_root": str(self.session), "nonce": self.binding["nonce"],
            "source_root": str(self.source), "source_seal": {"root": str(self.source),
                "manifest_sha256": sha(self.source / "FINAL-ARTIFACTS.sha256")},
            "selection_sha256": sha(self.selection), "no_automatic_retry": True,
            "owner_invocations_allowed": 1, "deployment_authorized": False, "promotion_authorized": False,
            "manifest_sha256": self.binding["manifest_sha256"], "archive_sha256": self.binding["archive_sha256"],
            "owner_seconds": 5400, "model_starts_allowed": 2, "generations": 2 * requests_per_start,
            "hyperloom_controller": {"files": {name: sha(self.session/name) for name in adapter.CONTROLLER_FILES},
                "binding_sha256": sha(self.session / "LAUNCHER-BINDINGS.json"),
                "identities_sha256": sha(self.session / "PACKAGE-IDENTITIES.json"),
                "phase_seconds": {"upload": 80, "launch": 5500, "collect": 120, "closeout": 100},
                "campaign_seconds": 5840}}
        self.authorize()
        return self.prepare()

    @staticmethod
    def put(path, value):
        path.write_text(json.dumps(value))

    def authorize(self):
        self.put(self.session / "RUN-AUTHORIZATION.json", self.auth)
        self.auth_sha = sha(self.session / "RUN-AUTHORIZATION.json")

    def prepare(self):
        return adapter.prepare(self.session, self.auth_sha, self.selection, self.boot)

    def run_fixture(self, plan):
        # Mac 3.11.1 lacks the real collector ABI. Stage fixtures contain no
        # archive extraction. Native 3.14 exercises this gate without a stub.
        runtime = nullcontext() if adapter.controller_runtime_ready() else patch.object(adapter, "controller_runtime_ready", return_value=True)
        with runtime, patch.object(adapter, "export_bound") as exporter:
            result = adapter.run(plan, self.output, {"fixture": True}, object())
        return result, exporter

    def test_prepare_is_cpu_only_and_covers_whole_controller_budget(self):
        plan = self.fixture()
        self.assertEqual(plan["maximum_controller_seconds"], 5840)
        self.assertEqual(plan["model_starts_allowed"], 2)
        self.assertFalse((self.session / "executed.txt").exists())
        self.assertFalse((self.session / "HYPERLOOM-STARTED.json").exists())
        self.assertFalse(plan["automatic_optimization"])

    def test_benchmark_budget_comes_from_sealed_consumed_schedule(self):
        plan = self.fixture(requests_per_start=4)
        self.assertEqual(plan["external_requests_allowed"], 8)
        self.assertEqual(plan["requests_per_start"], 4)
        self.auth["generations"] = 6
        self.authorize()
        with self.assertRaisesRegex(ValueError, "sealed external request count"):
            self.prepare()
        path = self.source / "review/FINAL-SETTINGS.json"
        path.write_text('{"mode":"native-http-fp8-sprint01-v1","requests":[]}')
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.prepare()

    def test_public_cli_checks_without_installed_prefix_or_execution(self):
        self.fixture()
        command = [sys.executable, "-B", str(ROOT / "lib/workstation/hyperloom.py"),
            "--root", str(ROOT), "sglang-owner", str(self.session),
            "--authorization-sha256", self.auth_sha, "--selection", str(self.selection),
            "--boot-id", self.boot]
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout)["gpu_execution"], "NOT RUN")
        self.assertEqual(json.loads(result.stdout)["controller_runtime_ready"], adapter.controller_runtime_ready())
        refused = subprocess.run([*command, "--execute-owner"], capture_output=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.session / "executed.txt").exists())
        self.assertFalse((self.session / "HYPERLOOM-STARTED.json").exists())

    def test_success_delegates_in_order_imports_both_arms_and_refuses_retry(self):
        result, exporter = self.run_fixture(self.fixture())
        self.assertEqual(result["status"], "OWNER_COMPLETED_REPORTS_IMPORTED")
        self.assertTrue(result["closeout_observed"])
        self.assertEqual((self.session / "executed.txt").read_text().splitlines(),
                         ["upload", "launch", "collect", "closeout"])
        self.assertEqual([c.args[3] for c in exporter.call_args_list], ["bf16", "kv4"])
        self.assertFalse(result["qualification"])
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.prepare()
        self.assertTrue((self.output / "OWNER-RESULT.json").is_file())

    def test_launch_failure_collects_and_uses_failure_observer_without_retry(self):
        result, exporter = self.run_fixture(self.fixture("launch"))
        self.assertEqual(result["status"], "OWNER_FAILED_RETAINED")
        self.assertEqual(result["failures"], ["launch"])
        self.assertTrue(result["closeout_observed"])
        self.assertEqual((self.session / "executed.txt").read_text().splitlines(),
                         ["upload", "launch", "collect", "failed-closeout"])
        exporter.assert_not_called()

    def test_collection_failure_still_attempts_successful_launch_observer(self):
        result, exporter = self.run_fixture(self.fixture("collect"))
        self.assertEqual(result["status"], "OWNER_FAILED_RETAINED")
        self.assertEqual(result["failures"], ["collect"])
        self.assertEqual((self.session / "executed.txt").read_text().splitlines()[-1], "closeout")
        exporter.assert_not_called()

    def test_closeout_failure_cannot_import_or_claim_release(self):
        result, exporter = self.run_fixture(self.fixture("closeout"))
        self.assertEqual(result["status"], "OWNER_FAILED_RETAINED")
        self.assertFalse(result["closeout_observed"])
        exporter.assert_not_called()

    def test_report_failure_retains_successful_closeout_and_never_relaunches(self):
        plan = self.fixture()
        runtime = nullcontext() if adapter.controller_runtime_ready() else patch.object(adapter, "controller_runtime_ready", return_value=True)
        with runtime, patch.object(adapter, "export_bound", side_effect=ValueError("retained import refusal")):
            result = adapter.run(plan, self.output, {}, None)
        self.assertEqual(result["status"], "ADAPTER_FAILED_RETAINED")
        self.assertTrue(result["closeout_observed"])
        self.assertEqual((self.session / "executed.txt").read_text().splitlines().count("launch"), 1)

    def test_incompatible_controller_refuses_before_consumption_or_owner_import(self):
        plan = self.fixture()
        # Exercise the actual configured runtime first. Then retain the same
        # negative contract on a newer runtime that has the required API.
        if not adapter.controller_runtime_ready():
            with self.assertRaisesRegex(ValueError, "tar data filter"):
                adapter.run(plan, self.output, {}, None)
        with patch.object(adapter, "controller_runtime_ready", return_value=False):
            with self.assertRaisesRegex(ValueError, "tar data filter"):
                adapter.run(plan, self.output, {}, None)
        self.assertFalse((self.session / "HYPERLOOM-STARTED.json").exists())
        self.assertFalse((self.session / "executed.txt").exists())
        self.assertFalse(self.output.exists())

    def test_upload_failure_never_launches_or_imports(self):
        result, exporter = self.run_fixture(self.fixture("upload"))
        self.assertEqual(result["status"], "UPLOAD_FAILED_NOT_LAUNCHED")
        self.assertEqual((self.session / "executed.txt").read_text(), "upload\n")
        self.assertFalse(result["closeout_observed"])
        exporter.assert_not_called()

    def test_substituted_helper_fails_before_reserving_or_loading(self):
        plan = self.fixture()
        (self.session / "connection.py").write_text("raise RuntimeError('must not execute')\n")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            adapter.run(plan, self.output, {}, None)
        self.assertFalse((self.session / "HYPERLOOM-STARTED.json").exists())
        self.assertFalse(self.output.exists())

    def test_stale_boot_selection_and_unaccounted_import_refuse(self):
        self.fixture()
        with self.assertRaisesRegex(ValueError, "nonce/boot"):
            adapter.prepare(self.session, self.auth_sha, self.selection, "different-boot")
        self.selection.write_text('{"selected":"other"}')
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.prepare()
        self.selection.write_text('{"selected":"unchanged"}\n')
        (self.session / "json.py").write_text("raise RuntimeError('shadow')\n")
        with self.assertRaisesRegex(ValueError, "import source"):
            self.prepare()

    def test_collector_metadata_and_nested_import_substitution_refuse(self):
        self.fixture()
        (self.session / "PACKAGE-IDENTITIES.json").write_text('{"nonce":"different"}')
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.prepare()
        self.put(self.session / "PACKAGE-IDENTITIES.json", self.binding)
        nested = self.session / "socket"
        nested.mkdir()
        (nested / "__init__.py").write_text("raise RuntimeError('unreviewed module')")
        with self.assertRaisesRegex(ValueError, "import source"):
            self.prepare()

    def test_invalid_budgets_and_authority_refuse(self):
        self.fixture()
        mutations = [("owner_invocations_allowed", 2), ("model_starts_allowed", 3),
                     ("model_starts_allowed", 1), ("generations", 59), ("generations", 61),
                     ("owner_seconds", 20), ("deployment_authorized", True), ("promotion_authorized", True)]
        for key, value in mutations:
            original = self.auth[key]
            self.auth[key] = value
            self.authorize()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.prepare()
            self.auth[key] = original
        self.auth["hyperloom_controller"]["phase_seconds"]["launch"] = 20
        self.authorize()
        with self.assertRaisesRegex(ValueError, "headroom"):
            self.prepare()

    def test_sealed_owner_refuses_without_import(self):
        self.fixture()
        (self.session / "FINAL-ARTIFACTS.sha256").write_text("historical seal")
        with self.assertRaisesRegex(ValueError, "sealed or already consumed"):
            self.prepare()
        self.assertFalse((self.session / "executed.txt").exists())

    def test_pure_writer_does_not_import_executor_package_and_rejects_substitution(self):
        wheel = self.root / "release.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("hyperloom/__init__.py", "raise RuntimeError('package must not load')")
            archive.writestr("hyperloom/orchestrator/actions/executors/__init__.py",
                             "raise RuntimeError('executor must not load')")
            archive.writestr("hyperloom/orchestrator/actions/executors/bypass_report.py", "CPU_FIXTURE = True\n")
        contract = {"HYPERLOOM_WHEEL_SHA256": sha(wheel)}
        self.assertTrue(integration.report_writer(wheel, contract).CPU_FIXTURE)
        with wheel.open("ab") as stream:
            stream.write(b"substitution")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            integration.report_writer(wheel, contract)


if __name__ == "__main__":
    unittest.main()
