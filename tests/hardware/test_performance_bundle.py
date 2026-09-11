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
# Candidate fixtures intentionally reuse the ordinary producer fixture rather
# than reproducing its JSON by hand.  unittest discovery imports this module as
# ``tests.hardware.test_performance_bundle``, so include the sibling test
# directory explicitly instead of relying on the process working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def candidate_bundle(path, kind, variant):
    """Tiny actual producer inputs for the selected Python/Go export contract.

    No summary is mocked. The caller seals and exports through workstationctl.
    Cache inventory touches only a new sibling fixture directory, never a host cache.
    """
    import math
    import inference_cache
    import performance_profiles
    from test_performance_profiles import bundle, load, refresh_serving_identity
    from test_serving_runtime import fixtures

    policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
    spec = {"schema": 1, "kind": kind}
    reserve = 1
    if kind == "coding-eval":
        coding_bundle(path)
        if variant == "refused":
            save(path / "responses.json", {})
        return reserve
    if kind in ("comparison", "profile-selection"):
        bundle(path / "baseline", "baseline")
        bundle(path / "candidate", "candidate", throughput=120)
        declared = []
        if variant == "declared-launch":
            runtime = load(path / "candidate/runtime.json")
            runtime["launch"][0]["--attention-backend"] = "triton"
            save(path / "candidate/runtime.json", runtime)
            refresh_serving_identity(path / "candidate")
            declared = ["launch"]
        elif variant in ("cross-quality-mismatch", "cross-logprob-mismatch"):
            for side in ("baseline", "candidate"):
                result_path = path / f"candidate/kernel-runs/{side}.json"
                result = json.loads(result_path.read_text())
                for row in result["quality"]:
                    if variant == "cross-quality-mismatch":
                        row["tokens"] = [2]
                    else:
                        row["logprobs"] = [0.02]
                save(result_path, result)
            import model_kernels
            save(path / "candidate/quality.json", model_kernels.compare_quality(
                json.loads((path / "candidate/kernel-runs/baseline.json").read_text()),
                json.loads((path / "candidate/kernel-runs/candidate.json").read_text()), 0, 0))
        elif variant == "unrelated-quality":
            runtime = load(path / "candidate/runtime.json")
            runtime["packages"]["torch"] = "different"
            save(path / "candidate/runtime.json", runtime)
            refresh_serving_identity(path / "candidate")
            declared = ["software"]
        elif variant == "missing-cross-evidence":
            manifest = json.loads((path / "candidate/manifest.json").read_text())
            manifest["sources"].pop("quality")
            manifest["sources"].pop("kernel_runs")
            save(path / "candidate/manifest.json", manifest)
        elif variant == "checked-launch-mismatch":
            result_path = path / "candidate/kernel-runs/candidate.json"
            result = json.loads(result_path.read_text())
            result["identity"]["runtime"]["launch"][0]["--attention-backend"] = "triton"
            save(result_path, result)
            import model_kernels
            save(path / "candidate/quality.json", model_kernels.compare_quality(
                json.loads((path / "candidate/kernel-runs/baseline.json").read_text()), result, 0, 0))
        if kind == "comparison":
            result_path = path / "candidate/serving/result.json"
            result = json.loads(result_path.read_text())
            if variant == "incomplete":
                result.pop("repeat_summary", None)
            elif variant == "refused":
                result["workload_sha256"] = "0" * 64
            save(result_path, result)
            save(path / "policy.json", policy)
            spec.update(baseline_bundle="baseline", candidate_bundle="candidate", policy_json="policy.json", declared_variables=declared)
        else:
            comparison = performance_profiles.compare(path / "baseline", path / "candidate", declared, policy)
            save(path / "comparison.json", comparison)
            spec.update(comparison="comparison.json", candidate_id="absent" if variant == "refused" else "candidate", previous_selection=None)
    elif kind in ("loading", "queue", "warm-status"):
        deployment, (pod, runtime, compiler) = fixtures()
        if variant == "refused":
            compiler["sources"]["srt/server_args.py"] = "0" * 64
        if variant == "unsupported":
            compiler["runtime_capabilities"] = {}
        for name, value in (("pod", pod), ("runtime", runtime), ("compiler", compiler)):
            save(path / f"evidence/{name}.json", value)
        spec["evidence"] = "evidence"
        if kind == "loading":
            save(path / "deployment.json", deployment)
            spec.update(deployment="deployment.json", threads=[1, 2], reserve_mib=32768, per_thread_mib=256)
        elif kind == "queue":
            save(path / "deployment.json", deployment)
            spec.update(deployment="deployment.json", maximum_queued=8)
        else:
            spec.update(warmup=None, lifecycle=None)
            if variant == "unknown":
                save(path / "warmup.json", {})
                spec["warmup"] = "warmup.json"
    elif kind == "cache":
        cache_root = path.parent / "disposable-cache"
        for backend in inference_cache.BACKENDS:
            (cache_root / backend / "workstation").mkdir(parents=True, mode=0o700)
        registry = path.parent / "registry.json"
        save(registry, {"schema": 1, "kind": inference_cache.KIND, "namespaces": []})
        observed = inference_cache.inventory(cache_root, registry, path / "inventory")
        if variant == "incomplete":
            reserve = math.ceil(observed["filesystem"]["available_bytes"] / 1024**2) + 1
        elif variant == "refused":
            save(path / "inventory/inventory.json", {})  # inner receipt must refuse
        spec.update(mode="plan", inventory="inventory")
    elif kind == "profile-status":
        fixture = BundleTests()
        current = fixture.current_identity()
        selection = fixture.profile_selection()
        if variant == "stale":
            current["identity"]["software_sha256"] = "0" * 64
        elif variant in ("stale-weights", "stale-resources"):
            key = "model_files_sha256" if variant == "stale-weights" else "resources_sha256"
            current["producer_conditions"][key] = "0" * 64
        elif variant in ("stale-runtime-launch", "stale-dtype"):
            key = "observed_launch_sha256" if variant == "stale-runtime-launch" else "model_settings_sha256"
            current["producer_conditions"][key] = "0" * 64
        elif variant == "legacy-selection":
            selection.pop("producer_conditions")
        elif variant == "legacy-observation":
            current.pop("producer_conditions")
        elif variant == "malformed-conditions":
            current["producer_conditions"] = {"runtime_sha256": True}
        elif variant == "legacy-condition-schema":
            current["producer_conditions"]["schema"] = 1
        elif variant == "unknown":
            current = {}
        save(path / "current.json", current)
        save(path / "selection.json", {} if variant == "refused" else selection)
        spec.update(selection="selection.json", current_identity="current.json")
    else:
        raise ValueError("unknown contract fixture kind")
    save(path / "spec.json", spec)
    # The comparison fixture helpers use the ambient umask; explicitly keep
    # all retained input directories and files private even when invoked alone.
    for directory, _, files in os.walk(path):
        private(directory, 0o700)
        for name in files:
            private(Path(directory) / name)
    return reserve


class BundleTests(unittest.TestCase):
    def test_inspect_rejects_post_seal_directory_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            coding_bundle(root)
            performance_bundle.seal(root, "coding-eval", "fixture-target", REVISION)
            digest, _ = performance_bundle.sha(root / "manifest.json")
            performance_bundle.inspect(root, digest)
            empty = root / "empty"
            (Path(tmp) / "retained-empty").mkdir(mode=0o700)
            empty.symlink_to(Path(tmp) / "retained-empty", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "directory.*unsafe"):
                performance_bundle.inspect(root, digest)

    def profile_selection(self):
        return {"schema": 1, "kind": "workstation-measured-profile-selection", "selected_profile_id": "candidate",
                "producer_conditions": {"schema": 2, **{key: "9" * 64 for key in
                                        ("runtime_sha256", "model_files_sha256", "model_settings_sha256",
                                         "observed_launch_sha256", "launch_settings_sha256", "resources_sha256")}},
                "runtime_identity": {"model_revision": "a" * 40, "tokenizer_sha256": "b" * 64,
                                     "workload_sha256": "c" * 64, "hardware_sha256": "d" * 64,
                                     "software_sha256": "e" * 64, "launch_sha256": "f" * 64,
                                     "quantization": "fp8"}}

    def current_identity(self, **changes):
        identity = dict(self.profile_selection()["runtime_identity"], **changes)
        return {"schema": 1, "kind": "workstation-performance-profile-identity-observation",
                "status": "observed-not-qualified",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "boot_id": "11111111-2222-3333-4444-555555555555", "identity": identity,
                "producer_conditions": self.profile_selection()["producer_conditions"]}

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

    def test_candidate_bundle_exercises_cross_profile_quality_variants(self):
        expected = {"declared-launch": "candidate-improves-throughput",
                    "cross-quality-mismatch": "inconclusive-quality-evidence",
                    "cross-logprob-mismatch": "inconclusive-quality-evidence",
                    "unrelated-quality": "inconclusive-quality-evidence",
                    "missing-cross-evidence": "inconclusive-quality-evidence"}
        for variant, reason in expected.items():
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "bundle"
                root.mkdir(mode=0o700)
                candidate_bundle(root, "comparison", variant)
                manifest = performance_bundle.seal(root, "comparison", "fixture-target", REVISION)
                output = Path(tmp) / "output"
                summary = performance_bundle.run(root, performance_bundle.sha(root / "manifest.json")[0], output,
                                                 manifest["target"], manifest["source_revision"], "owner-main")
                self.assertEqual((summary["status"], summary["reason"]), ("comparison-not-qualified", reason))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            root.mkdir(mode=0o700)
            candidate_bundle(root, "comparison", "checked-launch-mismatch")
            performance_bundle.seal(root, "comparison", "fixture-target", REVISION)
            summary = performance_bundle.run(root, performance_bundle.sha(root / "manifest.json")[0], Path(tmp) / "output",
                                             "fixture-target", REVISION, "owner-main")
            self.assertEqual(summary["status"], "failed")

    def test_candidate_bundle_sealed_profile_selection_rechecks_quality(self):
        # The direct selector is covered by the profile tests. Exercise the
        # sealed dispatcher separately so a report that is not selection-safe
        # cannot become a selected profile after artifact exchange.
        expected = {"declared-launch": "selected-unqualified",
                    "cross-quality-mismatch": "failed",
                    "cross-logprob-mismatch": "failed",
                    "unrelated-quality": "failed",
                    "missing-cross-evidence": "failed"}
        for variant, status in expected.items():
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "bundle"
                root.mkdir(mode=0o700)
                candidate_bundle(root, "profile-selection", variant)
                manifest = performance_bundle.seal(root, "profile-selection", "fixture-target", REVISION)
                summary = performance_bundle.run(root, performance_bundle.sha(root / "manifest.json")[0], Path(tmp) / "output",
                                                 manifest["target"], manifest["source_revision"], "owner-main")
                self.assertEqual(summary["status"], status)


if __name__ == "__main__":
    unittest.main()
