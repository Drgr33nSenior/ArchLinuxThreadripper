"""Sealed typed performance bundle integration tests; no model or host actions."""
import json
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import coding_eval
import performance_bundle


REVISION = "a" * 64


def private(path, mode=0o600):
    os.chmod(path, mode)


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(path.parent, 0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    private(path)


def coding_bundle(path):
    corpus, corpus_hash, _ = coding_eval.load_corpus()
    source = ROOT / "lib/workstation/coding_tasks.json"
    shutil.copyfile(source, path / "coding_tasks.json")
    private(path / "coding_tasks.json")
    responses = []
    for task in corpus["tasks"]:
        if task["kind"] == "code":
            source = "def add(a, b):\n return a + b\n" if task["id"] == "python-syntax-fix" else "def is_even(value):\n return value % 2 == 0\n"
            answer = {"language": "python", "source": source}
        else:
            answer = task["expected"]
        responses.append({"task_id": task["id"], "attempts": 1, "latency_ms": None,
                          "input_tokens": 1, "output_tokens": 1, "response": json.dumps(answer)})
    save(path / "responses.json", {"schema": 1, "kind": "workstation-coding-evaluation-responses",
                                     "corpus_sha256": corpus_hash,
                                     "generation": {"model": "fixture", "temperature": 0, "max_output_tokens": 10, "seed": 1},
                                     "responses": responses})
    save(path / "spec.json", {"schema": 1, "kind": "coding-eval", "responses": "responses.json", "corpus": "coding_tasks.json"})


class BundleTests(unittest.TestCase):
    def profile_selection(self):
        return {"schema": 1, "kind": "workstation-measured-profile-selection", "selected_profile_id": "candidate",
                "runtime_identity": {"model_revision": "a" * 40, "tokenizer_sha256": "b" * 64,
                                     "workload_sha256": "c" * 64, "hardware_sha256": "d" * 64,
                                     "software_sha256": "e" * 64, "launch_sha256": "f" * 64,
                                     "quantization": "fp8"}}

    def current_identity(self, **changes):
        identity = dict(self.profile_selection()["runtime_identity"], **changes)
        return {"schema": 1, "kind": "workstation-performance-profile-identity-observation",
                "status": "observed-not-qualified",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "boot_id": "11111111-2222-3333-4444-555555555555", "identity": identity}

    def test_seal_inspect_and_run_actual_coding_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            manifest = performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            inspected, spec, actual = performance_bundle.inspect(root, digest)
            self.assertEqual((inspected, spec, actual), (manifest, json.loads((root / "spec.json").read_text()), digest))
            output = Path(tmp) / "output"
            summary = performance_bundle.run(root, digest, output, "fixture-target", REVISION, "owner-main")
            self.assertEqual(summary["kind"], "coding-eval")
            self.assertEqual(summary["status"], "incomplete-unqualified")
            self.assertEqual(summary["evidence_id"], "")
            self.assertTrue(summary["artifacts"])
            self.assertNotIn("def add", json.dumps(summary))
            self.assertEqual((output / "summary.json").stat().st_mode & 0o077, 0)
            self.assertTrue(all(item["qualification"] == "analysis-output-not-qualified" for item in summary["artifacts"]))

    def test_cli_inspect_emits_unanalysed_bridge_summary_after_full_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["performance_bundle.py", "inspect", str(root), digest]), mock.patch("sys.stdout", output):
                performance_bundle.main()
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["schema"], 1)
            self.assertEqual(summary["kind"], "coding-eval")
            self.assertEqual(summary["sha256"], digest)
            self.assertEqual(summary["status"], "sealed-unanalysed")
            self.assertEqual(summary["reason"], "analysis_not_executed")
            self.assertFalse(summary["artifacts"])
            self.assertEqual(summary["preconditions"]["target"], "fixture-target")

    def test_rejects_unsealed_changes_nonprivate_files_and_output_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            digest, _ = performance_bundle.sha(root / "spec.json")
            private(root / "responses.json", 0o644)
            with self.assertRaises(ValueError):
                performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            private(root / "responses.json")
            performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            manifest_hash, _ = performance_bundle.sha(root / "manifest.json")
            (root / "responses.json").write_text("{}")
            private(root / "responses.json")
            with self.assertRaises(ValueError):
                performance_bundle.inspect(root, manifest_hash)
            output = Path(tmp) / "exists"
            output.mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                performance_bundle.run(root, manifest_hash, output, "fixture-target", REVISION, "owner-main")

    def test_rejects_extra_empty_directory_after_sealing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            (root / "unexpected").mkdir(mode=0o700)
            with self.assertRaises(ValueError):
                performance_bundle.inspect(root, digest)

    def test_refuses_client_like_path_and_unknown_spec_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            spec = json.loads((root / "spec.json").read_text())
            spec["responses"] = "../responses.json"
            save(root / "spec.json", spec)
            with self.assertRaises(ValueError):
                performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)

    def test_failed_analysis_exports_only_bounded_failure_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            save(root / "spec.json", {"schema": 1, "kind": "loading", "deployment": "missing-deployment.json",
                                      "evidence": "missing-evidence", "threads": [1], "reserve_mib": 1024,
                                      "per_thread_mib": 64})
            performance_bundle.seal(root, "loading", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            output = Path(tmp) / "output"
            summary = performance_bundle.run(root, digest, output, "fixture-target", REVISION, "owner-main")
            self.assertEqual(summary["status"], "failed")
            self.assertEqual([item["name"] for item in summary["artifacts"]], ["failure.json"])
            self.assertTrue((output / "failure.json").is_file())
            self.assertFalse((output / "loading").exists())

    def test_partial_candidate_artifact_is_removed_after_analysis_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            output = Path(tmp) / "output"

            def partial_then_fail(_root, _kind, _spec, destination, _owner, _cache_root, _reserve):
                partial = destination / "candidate"
                partial.mkdir(mode=0o700)
                (partial / "patch.json").write_text('{"unsafe":"partial"}')
                os.chmod(partial / "patch.json", 0o600)
                raise ValueError("simulated refused analysis")

            with mock.patch.object(performance_bundle, "run_kind", side_effect=partial_then_fail):
                summary = performance_bundle.run(root, digest, output, "fixture-target", REVISION, "owner-main")
            self.assertEqual(summary["status"], "failed")
            self.assertEqual([item["name"] for item in summary["artifacts"]], ["failure.json"])
            self.assertFalse((output / "candidate").exists())

    def test_historical_warm_status_never_claims_live_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir(mode=0o700)
            spec = {"schema": 1, "kind": "warm-status", "evidence": "evidence", "warmup": None, "lifecycle": None}
            with mock.patch.object(performance_bundle, "kernel_evidence", return_value=None):
                status, reason, fields = performance_bundle.run_kind(Path(tmp), "warm-status", spec, output, "owner-main", None, None)
            result = json.loads((output / "warm-status/status.json").read_text())
            self.assertEqual(status, "unknown")
            self.assertTrue(reason.startswith("historical:"))
            self.assertIn("historical sealed-evidence", result["scope"])
            self.assertEqual(fields[0][1], "unknown")

    def test_profile_status_is_boot_bound_fresh_and_marks_identity_drift_stale(self):
        selection = self.profile_selection()
        current = self.current_identity()
        current_status = performance_bundle.profile_status(selection, current)
        self.assertEqual(current_status["status"], "selected-unqualified-current")
        for field, value in (("model_revision", "z" * 40), ("software_sha256", "0" * 64),
                             ("hardware_sha256", "1" * 64), ("launch_sha256", "2" * 64)):
            with self.subTest(field=field):
                stale = performance_bundle.profile_status(selection, self.current_identity(**{field: value}))
                self.assertEqual((stale["status"], stale["changed_identity_fields"]), ("stale", [field]))
        self.assertEqual(performance_bundle.profile_status(selection, {})["status"], "unknown")
        stale_time = self.current_identity()
        stale_time["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat().replace("+00:00", "Z")
        self.assertEqual(performance_bundle.profile_status(selection, stale_time)["status"], "unknown")

    def test_profile_status_bundle_maps_current_to_summary_without_live_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            save(root / "selection.json", self.profile_selection())
            save(root / "current.json", self.current_identity())
            save(root / "spec.json", {"schema": 1, "kind": "profile-status", "selection": "selection.json",
                                      "current_identity": "current.json"})
            performance_bundle.seal(root, "profile-status", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            output = Path(tmp) / "output"
            summary = performance_bundle.run(root, digest, output, "fixture-target", REVISION, "owner-main")
            stored = json.loads((output / "profile-status/status.json").read_text())
            self.assertEqual((summary["status"], stored["status"]), ("current-unqualified", "selected-unqualified-current"))
            self.assertIn("as of observed_at", stored["scope"])


if __name__ == "__main__":
    unittest.main()
