"""Offline comparison/profile contracts; no target measurement or source apply."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import performance_profiles as profiles


IDENTITY = {"model_revision": "a" * 40, "tokenizer_sha256": "b" * 64, "workload_sha256": "c" * 64,
            "hardware_sha256": "d" * 64, "software_sha256": "e" * 64, "launch_sha256": "f" * 64,
            "quantization": "fp8"}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def summary(value, n=24):
    return {"n": n, "min": value * .9, "median": value, "p95": value * 1.1, "p99": value * 1.2,
            "max": value * 1.3, "stdev": value * .02}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_serving_identity(root):
    """Keep the fixture on the same finalized producer contract as performance.sh."""
    runtime = load(root / "runtime.json")
    serving = load(root / "serving/result.json")
    serving["runtime"] = runtime
    serving["runtime_sha256"] = profiles.digest_bytes((root / "runtime.json").read_bytes())
    save(root / "serving/result.json", serving)
    runs = []
    for side in ("baseline", "candidate"):
        path = root / f"kernel-runs/{side}.json"
        run = load(path)
        run["runtime_sha256"] = serving["runtime_sha256"]
        run["pod"] = serving["pod"]
        run["workload_sha256"] = serving["workload_sha256"]
        run["identity"] = {"image": serving["pod"]["image"], "image_id": serving["pod"]["image_id"],
                           "resources": serving["pod"]["resources"], "runtime": runtime}
        save(path, run)
        runs.append(run)
    quality = profiles.model_kernels.compare_quality(runs[0], runs[1], 0, 0)
    save(root / "quality.json", quality)
    manifest = load(root / "manifest.json")
    manifest["identity"] = profiles.producer_identity(serving, runtime, serving["runtime_sha256"])
    manifest["experiment"]["profile"] = serving["profile"]
    manifest["experiment"]["prefix_state"] = serving["prefix_state"]
    save(root / "manifest.json", manifest)


def bundle(root, profile_id, throughput=100, status="measured-not-qualified", variables=None, identity=None,
           failures=0, coding="passed", context_tokens=4096, concurrency=1, coding_corpus="c" * 64,
           generation=None, latency=1, ttft=.2, startup=None, memory_status="plan-only-unqualified",
           memory_signals=None, throughput_samples=24, throughput_stdev=None, serving_status="measured-not-qualified",
           profile="interactive", prefix_state="warm-prefix"):
    throughput_summary = summary(throughput, throughput_samples)
    if throughput_stdev is not None:
        throughput_summary["stdev"] = throughput_stdev
    runtime = {"settings": {"MODEL_REVISION": "a" * 40, "MODEL_PATH": "/models/fixture", "TENSOR_PARALLEL": "1"},
               "model_files": {"model.safetensors": {"bytes": 1, "sha256": "c" * 64},
                               "tokenizer.json": {"bytes": 1, "sha256": "b" * 64}},
               "packages": {"sglang": "fixture", "torch": "fixture", "triton": None,
                            "pytorch-triton-rocm": None, "aiter": None, "transformers": None}, "hip": "fixture",
               "devices": [{"uuid": "GPU-a", "gfx": "gfx1201"}],
               "launch": [{"--model-path": "/models/fixture"}],
               "model_contract": {"quant_method": "fp8", "architectures": ["fixture"]}}
    save(root / "runtime.json", runtime)
    runtime_hash = profiles.digest_bytes((root / "runtime.json").read_bytes())
    pod = {"node": "fixture-node", "image": "example@sha256:" + "1" * 64,
           "image_id": "containerd://sha256:" + "2" * 64, "launch_spec_sha256": "f" * 64,
           "resources": {"requests": {"cpu": "8", "memory": "38Gi", "amd.com/gpu": "1"},
                         "limits": {"cpu": "8", "memory": "38Gi", "amd.com/gpu": "1"}}}
    derived_identity = profiles.producer_identity({"runtime": runtime, "runtime_sha256": runtime_hash, "pod": pod,
                                                    "workload_sha256": "c" * 64}, runtime, runtime_hash)
    if identity is not None:
        # Tests may deliberately supply a copied/incorrect manifest identity.
        derived_identity = identity
    serving = {"schema": 1, "status": serving_status, "runtime": runtime, "runtime_sha256": runtime_hash,
               "pod": pod, "workload_sha256": "c" * 64, "profile": profile, "prefix_state": prefix_state, "repeat_summary": [
        {"context_tokens": context_tokens, "concurrency": concurrency, "failures": failures, "throughput_across_repetitions": throughput_summary,
         "request_latency_seconds": summary(latency), "ttft_seconds": summary(ttft)}]}
    startup = startup if startup is not None else [
        {"schema": 1, "status": "observed-not-qualified", "cache_state": phase, "elapsed_seconds": seconds,
         "pod": dict(pod)}
        for phase, seconds in (("cold", 50), ("warm", 20))]
    startup = [dict(row, **({"pod": dict(pod)} if row.get("status") == "observed-not-qualified" and "pod" not in row else {}))
               for row in startup]
    memory = {"schema": 1, "kind": "sglang-host-memory-plan", "status": memory_status,
              "budget": {"observed_envelope_bytes": 1,
              "headroom_bytes": 2, "shm_limit_mib": 16}, "candidate_mib": 100, "baseline_mib": 110}
    memory.update(memory_signals or {})
    contract = {"repetitions": 3, "cases": [{"context_tokens": context_tokens, "output_tokens": 1}],
                "concurrency": [1]}
    kernel_runs = []
    for side in ("baseline", "candidate"):
        kernel_runs.append({"schema": 1, "status": "measured-not-qualified", "kind": "kernel-warmup", "run": side,
                            "workload_sha256": "c" * 64, "pod": dict(pod), "memory": {"status": "checked"},
                            "warmup_contract": contract,
                            "quality": [{"case": [repeat, context_tokens, 1, 0], "tokens": [1], "logprobs": [0.0]}
                                        for repeat in range(3)],
                            "identity": {"image": pod["image"], "image_id": pod["image_id"],
                                         "resources": pod["resources"], "runtime": runtime}})
    quality = profiles.model_kernels.compare_quality(kernel_runs[0], kernel_runs[1], 0, 0)
    coding_result = {"schema": 1, "kind": "workstation-coding-evaluation", "status": coding,
                     "successes": 8, "failures": 0, "unavailable": 0, "corpus_sha256": coding_corpus,
                     "template_sha256": {f"fixture-{index}": "e" * 64 for index in range(8)},
                     "generation": generation or {"model": "fixture", "temperature": 0, "max_output_tokens": 64, "seed": 1}}
    save(root / "serving/result.json", serving)
    for index, value in enumerate(startup):
        save(root / f"startup/{index}.json", value)
    save(root / "memory/plan.json", memory)
    save(root / "quality.json", quality)
    save(root / "kernel-runs/baseline.json", kernel_runs[0])
    save(root / "kernel-runs/candidate.json", kernel_runs[1])
    save(root / "coding.json", coding_result)
    manifest = {"schema": 1, "kind": "workstation-performance-evidence", "status": status,
                "profile_id": profile_id, "identity": derived_identity,
                "experiment": {"variables": variables or {}, "profile": profile, "prefix_state": prefix_state},
                "sources": {"serving": "serving/result.json", "runtime": "runtime.json",
                            "startup": ["startup/0.json", "startup/1.json"], "memory": "memory/plan.json",
                            "quality": "quality.json", "kernel_runs": ["kernel-runs/baseline.json", "kernel-runs/candidate.json"],
                            "coding": "coding.json"}}
    save(root / "manifest.json", manifest)


class ProfilesTests(unittest.TestCase):
    def test_compare_declared_differences_and_selection_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root / "baseline", "baseline")
            bundle(root / "candidate", "candidate", throughput=120, variables={"compiler_backend": "hipblaslt"})
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            result = profiles.compare(root / "baseline", root / "candidate", ["compiler_backend"], policy)
            self.assertEqual(result["recommendation"], "candidate")
            self.assertEqual(result["outcome"], "candidate-improves-throughput")
            self.assertIsNotNone(result["baseline"]["metrics"]["startup"]["cold"]["median_seconds"])
            selected = profiles.select(result, "owner-1", "candidate")
            self.assertEqual(selected["status"], "selected-unqualified")
            self.assertEqual(profiles.selection_status(selected, selected["runtime_identity"], selected["producer_conditions"])["status"], "selected-unqualified-current")
            changed = dict(selected["runtime_identity"], hardware_sha256="0" * 64)
            self.assertEqual(profiles.selection_status(selected, changed)["changed_identity_fields"], ["hardware_sha256"])
            selected_again = profiles.select(result, "owner-1", "candidate", selected)
            self.assertEqual(selected_again["previous_selection"]["profile_id"], "candidate")

    def test_refuses_undeclared_identity_and_keeps_failure_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root / "baseline", "baseline")
            changed = dict(IDENTITY, model_revision="z" * 40)
            bundle(root / "candidate", "candidate", identity=changed)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            with self.assertRaises(ValueError):
                profiles.compare(root / "baseline", root / "candidate", [], policy)
            bundle(root / "failed", "failed", status="failed", failures=1, coding="incomplete-unqualified")
            report = profiles.compare(root / "baseline", root / "failed", [], policy)
            self.assertEqual(report["recommendation"], "retain-baseline")
            self.assertEqual(report["candidate"]["metrics"]["coding"]["status"], "incomplete-unqualified")

    def test_compares_only_matching_cases_and_equivalent_quality_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "concurrency", "concurrency", throughput=130, context_tokens=8192, concurrency=2,
                   variables={"concurrency": "2"})
            report = profiles.compare(root / "baseline", root / "concurrency", ["concurrency"], policy)
            self.assertEqual((report["case_alignment"]["status"], report["recommendation"]), ("incompatible", "retain-baseline"))
            with self.assertRaises(ValueError):
                profiles.select(report, "owner-1", "concurrency")

            bundle(root / "model", "model", throughput=130)
            runtime = load(root / "model/runtime.json")
            runtime["settings"]["MODEL_REVISION"] = "z" * 40
            save(root / "model/runtime.json", runtime)
            refresh_serving_identity(root / "model")
            report = profiles.compare(root / "baseline", root / "model", ["model"], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-incomparable-conditions", "retain-baseline"))

    def test_requires_both_quality_sides_and_matching_coding_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline", coding="incomplete-unqualified")
            bundle(root / "candidate", "candidate", throughput=130)
            report = profiles.compare(root / "baseline", root / "candidate", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-quality-evidence", "retain-baseline"))
            bundle(root / "different-corpus", "different-corpus", throughput=130, coding_corpus="d" * 64)
            with self.assertRaises(ValueError):
                profiles.compare(root / "candidate", root / "different-corpus", [], policy)
            report = profiles.compare(root / "candidate", root / "different-corpus", ["coding_corpus"], policy)
            self.assertEqual((report["quality_gates"]["provenance"], report["recommendation"]),
                             ("declared-variant", "retain-baseline"))

    def test_selection_refuses_forged_recommendation_or_evidence_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "candidate", "candidate", throughput=130)
            report = profiles.compare(root / "baseline", root / "candidate", [], policy)
            forged = copy.deepcopy(report)
            forged["candidate"]["identity"]["software_sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                profiles.select(forged, "owner-1", "candidate")
            forged = copy.deepcopy(report)
            forged["recommendation"] = "retain-baseline"
            with self.assertRaises(ValueError):
                profiles.select(forged, "owner-1", "candidate")

    def test_rejects_escape_and_marks_short_tail_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root / "baseline", "baseline")
            manifest = json.loads((root / "baseline/manifest.json").read_text())
            manifest["sources"]["serving"] = "../outside.json"
            save(root / "baseline/manifest.json", manifest)
            with self.assertRaises(ValueError):
                profiles.inspect(root / "baseline")
            bundle(root / "candidate", "candidate")
            result = json.loads((root / "candidate/serving/result.json").read_text())
            result["repeat_summary"][0]["ttft_seconds"] = summary(.2, n=3)
            save(root / "candidate/serving/result.json", result)
            inspected = profiles.inspect(root / "candidate")
            self.assertEqual(inspected["metrics"]["serving"]["cases"][0]["ttft"]["tail_status"], "unknown-insufficient-samples")

    def test_throughput_cannot_mask_latency_ttft_or_startup_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "slow-tail", "slow-tail", throughput=130)
            serving = json.loads((root / "slow-tail/serving/result.json").read_text())
            serving["repeat_summary"][0]["request_latency_seconds"]["p95"] = 2
            save(root / "slow-tail/serving/result.json", serving)
            report = profiles.compare(root / "baseline", root / "slow-tail", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("candidate-regresses-serving-latency-or-ttft", "retain-baseline"))
            self.assertEqual(report["performance_gates"]["serving_latency"]["status"], "regressed")

            failed_start = [{"schema": 1, "status": "failed", "cache_state": "cold", "elapsed_seconds": 50},
                            {"schema": 1, "status": "observed-not-qualified", "cache_state": "warm", "elapsed_seconds": 20}]
            bundle(root / "failed-start", "failed-start", throughput=130, startup=failed_start)
            report = profiles.compare(root / "baseline", root / "failed-start", [], policy)
            cold = report["candidate"]["metrics"]["startup"]["cold"]
            self.assertEqual((cold["attempts"], cold["failed"], cold["unknown"], cold["samples"]), (1, 1, 0, 0))
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-incomplete-startup-evidence", "retain-baseline"))

            bundle(root / "startup-regression", "startup-regression", throughput=130,
                   startup=[{"schema": 1, "status": "observed-not-qualified", "cache_state": "cold", "elapsed_seconds": 60},
                            {"schema": 1, "status": "observed-not-qualified", "cache_state": "warm", "elapsed_seconds": 20}])
            report = profiles.compare(root / "baseline", root / "startup-regression", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("candidate-regresses-startup", "retain-baseline"))

    def test_refuses_incomplete_latency_known_memory_failure_and_noisy_throughput(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "incomplete-latency", "incomplete-latency", throughput=130)
            serving = json.loads((root / "incomplete-latency/serving/result.json").read_text())
            serving["repeat_summary"][0]["ttft_seconds"] = None
            save(root / "incomplete-latency/serving/result.json", serving)
            report = profiles.compare(root / "baseline", root / "incomplete-latency", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-incomplete-serving-latency-evidence", "retain-baseline"))

            bundle(root / "memory-refusal", "memory-refusal", throughput=130, memory_status="refused-capacity",
                   memory_signals={"pressure_status": "memory-pressure-observed"})
            report = profiles.compare(root / "baseline", root / "memory-refusal", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-adverse-memory-evidence", "retain-baseline"))
            memory = report["candidate"]["metrics"]["memory"]
            self.assertEqual((memory["source_status"], memory["pressure_status"]),
                             ("refused-capacity", "memory-pressure-observed"))

            bundle(root / "one-repeat", "one-repeat", throughput=130, throughput_samples=1)
            report = profiles.compare(root / "baseline", root / "one-repeat", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-incomplete-serving-throughput-evidence", "retain-baseline"))

            bundle(root / "noisy", "noisy", throughput=106, throughput_stdev=20)
            report = profiles.compare(root / "baseline", root / "noisy", [], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-noisy", "retain-baseline"))

    def test_rejects_symlinked_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root / "baseline", "baseline")
            target = root / "baseline/serving/result.json"
            moved = root / "baseline/serving/retained.json"
            target.rename(moved)
            os.symlink("retained.json", target)
            with self.assertRaises(ValueError):
                profiles.inspect(root / "baseline")

    def test_reports_available_sensor_power_without_claiming_wall_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root / "candidate", "candidate")
            save(root / "candidate/power.json", {"hwmon": {"/sys/hwmon/hwmon0": {"values": {"power1_average": "250000000"}}}})
            manifest = json.loads((root / "candidate/manifest.json").read_text())
            manifest["sources"]["power"] = "power.json"
            save(root / "candidate/manifest.json", manifest)
            power = profiles.inspect(root / "candidate")["metrics"]["device_power"]
            self.assertEqual(power["status"], "observed")
            self.assertEqual(power["sensors"]["/sys/hwmon/hwmon0:power1_average"]["median"], 250)
            self.assertIn("not wall power", power["scope"])

    def test_binds_finalized_serving_producer_and_checked_kernel_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            bundle(root, "profile")
            inspected = profiles.inspect(root)
            self.assertEqual(inspected["metrics"]["serving"]["source_status"], "measured-not-qualified")

            serving = load(root / "serving/result.json")
            serving["pod"]["node"] = None
            save(root / "serving/result.json", serving)
            with self.assertRaisesRegex(ValueError, "identity is incomplete"):
                profiles.inspect(root)
            serving["pod"]["node"] = "fixture-node"
            save(root / "serving/result.json", serving)

            serving["status"] = "measured-awaiting-provenance"
            save(root / "serving/result.json", serving)
            with self.assertRaisesRegex(ValueError, "provenance verification"):
                profiles.inspect(root)
            serving["status"] = "measured-not-qualified"
            save(root / "serving/result.json", serving)

            manifest = load(root / "manifest.json")
            for field, value in (("model_revision", "z" * 40), ("tokenizer_sha256", "0" * 64),
                                 ("workload_sha256", "0" * 64), ("hardware_sha256", "0" * 64),
                                 ("software_sha256", "0" * 64), ("launch_sha256", "0" * 64),
                                 ("quantization", "different")):
                with self.subTest(field=field):
                    manifest["identity"] = dict(inspected["identity"], **{field: value})
                    save(root / "manifest.json", manifest)
                    with self.assertRaisesRegex(ValueError, "manifest identity"):
                        profiles.inspect(root)
            manifest["identity"] = inspected["identity"]
            save(root / "manifest.json", manifest)

            quality = load(root / "quality.json")
            quality["baseline_sha256"] = "0" * 64
            save(root / "quality.json", quality)
            with self.assertRaisesRegex(ValueError, "checked kernel runs"):
                profiles.inspect(root)

            # Canonical model_kernels comparison binds named baseline/candidate
            # positions. Reversing otherwise valid digests is not interchangeable.
            quality = profiles.model_kernels.compare_quality(load(root / "kernel-runs/baseline.json"),
                                                              load(root / "kernel-runs/candidate.json"), 0, 0)
            quality["baseline_sha256"], quality["candidate_sha256"] = (
                quality["candidate_sha256"], quality["baseline_sha256"])
            save(root / "quality.json", quality)
            with self.assertRaisesRegex(ValueError, "checked kernel runs"):
                profiles.inspect(root)

            quality = profiles.model_kernels.compare_quality(load(root / "kernel-runs/baseline.json"),
                                                              load(root / "kernel-runs/candidate.json"), 0, 0)
            save(root / "quality.json", quality)
            start = load(root / "startup/0.json")
            start["pod"]["resources"]["limits"]["memory"] = "32Gi"
            save(root / "startup/0.json", start)
            with self.assertRaisesRegex(ValueError, "startup observation"):
                profiles.inspect(root)
            start["pod"]["resources"]["limits"]["memory"] = "38Gi"
            save(root / "startup/0.json", start)

            for name in ("baseline", "candidate"):
                path = root / f"kernel-runs/{name}.json"
                run = load(path)
                run["schema"] = 2
                save(path, run)
            quality = profiles.model_kernels.compare_quality(load(root / "kernel-runs/baseline.json"),
                                                              load(root / "kernel-runs/candidate.json"), 0, 0)
            save(root / "quality.json", quality)
            with self.assertRaisesRegex(ValueError, "checked kernel runs"):
                profiles.inspect(root)
            for name in ("baseline", "candidate"):
                path = root / f"kernel-runs/{name}.json"
                run = load(path)
                run["schema"] = 1
                run["memory"] = {"status": "unchecked"}
                save(path, run)
            # The canonical comparator itself rejects unverified cgroup memory
            # evidence; profile inspection must retain that refusal.
            with self.assertRaises(ValueError):
                profiles.model_kernels.compare_quality(load(root / "kernel-runs/baseline.json"),
                                                       load(root / "kernel-runs/candidate.json"), 0, 0)
            with self.assertRaisesRegex(ValueError, "checked kernel runs"):
                profiles.inspect(root)

            runtime = load(root / "runtime.json")
            runtime["settings"]["MODEL_REVISION"] = "z" * 40
            save(root / "runtime.json", runtime)
            with self.assertRaisesRegex(ValueError, "runtime hash"):
                profiles.inspect(root)

    def test_detects_changed_model_files_when_model_revision_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "candidate", "candidate", throughput=130)
            runtime = load(root / "candidate/runtime.json")
            runtime["model_files"]["model.safetensors"]["sha256"] = "d" * 64
            save(root / "candidate/runtime.json", runtime)
            refresh_serving_identity(root / "candidate")
            with self.assertRaisesRegex(ValueError, "undeclared"):
                profiles.compare(root / "baseline", root / "candidate", [], policy)
            result = profiles.compare(root / "baseline", root / "candidate", ["model"], policy)
            self.assertEqual((result["outcome"], result["recommendation"]),
                             ("inconclusive-incomparable-conditions", "retain-baseline"))
            self.assertIn("producer.model_files_sha256", [item["field"] for item in result["differences"]])

    def test_quality_runs_must_bind_the_serving_model_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profile"
            bundle(root, "profile")
            for side in ("baseline", "candidate"):
                path = root / f"kernel-runs/{side}.json"
                run = load(path)
                run["identity"]["runtime"]["model_files"]["model.safetensors"]["sha256"] = "d" * 64
                save(path, run)
            quality = profiles.model_kernels.compare_quality(load(root / "kernel-runs/baseline.json"),
                                                              load(root / "kernel-runs/candidate.json"), 0, 0)
            save(root / "quality.json", quality)
            with self.assertRaisesRegex(ValueError, "checked kernel runs"):
                profiles.inspect(root)

    def test_retains_incomplete_serving_evidence_without_case_key_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            for name, change in (
                    ("missing", lambda value: value.pop("repeat_summary")),
                    ("malformed", lambda value: value.update(repeat_summary=[{"context_tokens": True}])),
                    ("aborted", lambda value: value.update(status="failed-or-cache-unverified"))):
                with self.subTest(name=name):
                    bundle(root / name, name, throughput=130)
                    serving = load(root / name / "serving/result.json")
                    change(serving)
                    save(root / name / "serving/result.json", serving)
                    inspected = profiles.inspect(root / name)
                    self.assertEqual(inspected["metrics"]["serving"]["status"], "incomplete")
                    self.assertIn("serving:serving/result.json", inspected["artifact_sha256"])
                    report = profiles.compare(root / "baseline", root / name, [], policy)
                    self.assertEqual((report["outcome"], report["recommendation"]),
                                     ("inconclusive-incomplete-serving-evidence", "retain-baseline"))

    def test_profile_and_prefix_bind_to_producer_and_remain_declared_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "candidate", "candidate", throughput=130, profile="batch", prefix_state="new-prefix")
            with self.assertRaisesRegex(ValueError, "undeclared"):
                profiles.compare(root / "baseline", root / "candidate", [], policy)
            report = profiles.compare(root / "baseline", root / "candidate", ["profile", "prefix_state"], policy)
            self.assertEqual((report["outcome"], report["recommendation"]),
                             ("inconclusive-incomparable-conditions", "retain-baseline"))
            self.assertEqual([item["field"] for item in report["differences"]], ["profile", "prefix_state"])
            manifest = load(root / "candidate/manifest.json")
            manifest["experiment"]["prefix_state"] = "warm-prefix"
            save(root / "candidate/manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "experiment labels"):
                profiles.inspect(root / "candidate")

    def test_selection_status_has_bounded_current_and_stale_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = {"schema": 1, "minimum_practical_gain_percent": 5, "maximum_regression_percent": 5, "noise_percent": 2}
            bundle(root / "baseline", "baseline")
            bundle(root / "candidate", "candidate", throughput=130)
            selection = profiles.select(profiles.compare(root / "baseline", root / "candidate", [], policy), "owner-1", "candidate")
            current = profiles.selection_status(selection, selection["runtime_identity"], selection["producer_conditions"])
            stale = profiles.selection_status(selection, dict(selection["runtime_identity"], launch_sha256="0" * 64))
            self.assertEqual(current["status"], "selected-unqualified-current")
            self.assertEqual(stale["status"], "stale")
            self.assertTrue(current["reason"])
            self.assertIn("launch_sha256", stale["reason"])
            self.assertLessEqual(len(stale["reason"]), 256)
            changed_conditions = dict(selection["producer_conditions"], model_files_sha256="0" * 64)
            stale_conditions = profiles.selection_status(selection, selection["runtime_identity"], changed_conditions)
            self.assertEqual((stale_conditions["status"], stale_conditions["changed_condition_fields"]),
                             ("stale", ["model_files_sha256"]))
            self.assertEqual(profiles.selection_status(selection, selection["runtime_identity"])["status"], "unknown")
            legacy = dict(selection)
            legacy.pop("producer_conditions")
            self.assertEqual(profiles.selection_status(legacy, selection["runtime_identity"], selection["producer_conditions"])["status"],
                             "unknown")


if __name__ == "__main__":
    unittest.main()
