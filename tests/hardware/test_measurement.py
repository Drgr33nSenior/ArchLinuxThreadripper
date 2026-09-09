"""Offline fixtures: no ROCm, cluster, disk workload or target qualification."""
import json
import io
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import measurement
import serving
import serving_sweep
import storage_benchmark
import quality_metrics
import frame_metrics
import stack_inventory


class Handler(BaseHTTPRequestHandler):
    mode = "valid"

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        if self.path == "/model_info":
            self.wfile.write(b'{"model_path":"/models/fixture"}')
        elif self.path == "/get_load":
            self.wfile.write(b'[{"dp_rank":0,"num_waiting_reqs":0}]')
        else:
            self.wfile.write(b'# no metrics in fixture\n')

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        if self.mode == "malformed":
            self.wfile.write(b'data: {bad}\n\n')
            return
        for count in (1, 2, 3):
            self.wfile.write(b'data: ' + json.dumps({"meta_info": {"completion_tokens": count, "cached_tokens": 1}}).encode() + b'\n\n')
        if self.mode != "truncated":
            self.wfile.write(b'data: [DONE]\n\n')


class Tests(unittest.TestCase):
    def test_pod_cgroup_identity_and_unknown_layout(self):
        uid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(measurement.pod_cgroup(uid, root))
            directory = root / ("kubepods-pod" + uid.replace("-", "_") + ".slice")
            directory.mkdir()
            (directory / "memory.current").write_text("123")
            self.assertEqual(measurement.pod_cgroup(uid, root), directory)
            self.assertEqual(measurement.cgroup_values(directory)["values"]["memory.current"], "123")
            self.assertEqual(measurement.cgroup_values(None)["status"], "unavailable")

    def test_arch_mirror_classification_and_redaction(self):
        result = stack_inventory.classify(["https://archive.archlinux.org/repos/2026/09/04/extra/os/x86_64",
                                           "https://user:secret@mirror.example/arch?token=secret"])
        self.assertEqual(result[0]["classification"], "dated-archive")
        self.assertEqual(result[1]["classification"], "rolling-candidate-unverified")
        self.assertNotIn("secret", json.dumps(result))

    def test_distinct_frames_not_client_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "frames.md5"
            file.write_text("#tb 0: 1/60\n" + "".join(f"0, {i}, {i}, 1, 8, {i:032x}\n" for i in range(601)))
            self.assertEqual(frame_metrics.analyze(file, 60)["status"], "frame-cadence-passed-not-hardware-qualified")
            self.assertEqual(frame_metrics.analyze(file, 120)["status"], "failed")
            file.write_text("#tb 0: 1/60\n" + "".join(f"0, {i}, {i}, 1, 8, {'0'*32}\n" for i in range(601)))
            self.assertEqual(frame_metrics.analyze(file, 60)["status"], "failed")

    def test_offline_sweep_preserves_memory_and_gpu(self):
        deployment = {"spec": {"template": {"spec": {"containers": [{"name": "sglang", "args": [],
            "resources": {"requests": {"cpu": "20", "memory": "32Gi", "amd.com/gpu": "1"},
                          "limits": {"cpu": "20", "memory": "32Gi", "amd.com/gpu": "1"}}}]}}}}
        cases = serving_sweep.plan(deployment)
        for name, changes in cases.items():
            self.assertEqual(len(changes), 1)
            if name.startswith("cpu-"):
                self.assertEqual(changes[0]["value"]["limits"]["memory"], "32Gi")
                self.assertEqual(changes[0]["value"]["limits"]["amd.com/gpu"], "1")

    def test_scratch_safety_and_result_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch("storage_benchmark.shutil.disk_usage") as usage:
                usage.return_value.free = 1024 ** 3
                with self.assertRaises(ValueError):
                    storage_benchmark.validate_directory(root, 1)
            link = root / "link"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                storage_benchmark.validate_directory(link, 1)
        for bad in ({}, {"jobs": [{"error": 1}]}, {"jobs": [{"error": 0}]}):
            with self.assertRaises(ValueError):
                storage_benchmark.validate_result(bad)
        for mode in ("qd1", "parallel", "warm-load", "advisory-evicted-load"):
            args = storage_benchmark.fio_args("/scratch/workstation-fio.bin", 1024, mode)
            self.assertIn("--readonly", args)
            self.assertIn("--allow_file_create=0", args)

    def test_quality_rejects_skipped_and_missing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ops.csv"
            fixture = (Path(__file__).resolve().parents[1] / "fixtures/llama-quality/ops.csv").read_text()
            record = Path(tmp) / "run.json"
            good_record = {"status": "measured-not-qualified", "returncode": 0}
            record.write_text(json.dumps(good_record))
            path.write_text(fixture)
            self.assertEqual(quality_metrics.ops(path, "Vulkan0", record)["supported_passes"], 3)
            path.write_text(fixture + fixture.splitlines()[1].replace('"1",""', '"0","not supported"') + "\n")
            self.assertEqual(quality_metrics.ops(path, "Vulkan0", record)["unsupported"], 1)
            for bad in ("", '"broken\n', fixture.splitlines()[0] + "\n",
                        fixture.replace('"1"', '"0"'),
                        "\n".join(line for line in fixture.splitlines() if "SOFT_MAX" not in line),
                        fixture.replace('"1",""', '"1","comparison failed"', 1),
                        fixture.replace('"test"', '"support"'), fixture.replace("Vulkan0", "Vulkan1"),
                        fixture + '"Vulkan0","MUL_MAT"\n'):
                path.write_text(bad)
                with self.subTest(csv=bad[:40]), self.assertRaises((ValueError, quality_metrics.csv.Error)):
                    quality_metrics.ops(path, "Vulkan0", record)
            path.write_text(fixture)
            for bad in ({"status": "failed", "returncode": 0}, {"status": "measured-not-qualified", "returncode": 9}, {},
                        dict(good_record, error_category="telemetry")):
                record.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    quality_metrics.ops(path, "Vulkan0", record)
            with self.assertRaises(ValueError):
                quality_metrics.perplexity(path)
            path.write_text('Final estimate: PPL = 3.0 +/- 0.1\n')
            self.assertEqual(quality_metrics.perplexity(path)["perplexity"], 3)

    def test_stream_chunk_timing_and_abort_metadata(self):
        def check(counts, ticks, finish=None):
            rows = [{"meta_info": {"completion_tokens": count}} for count in counts]
            rows[-1]["meta_info"]["finish_reason"] = finish
            body = b"".join(b"data: " + json.dumps(row).encode() + b"\n\n" for row in rows) + b"data: [DONE]\n\n"
            with patch("serving.request", return_value=io.BytesIO(body)), patch("serving.time.monotonic", side_effect=ticks):
                return serving.stream("http://127.0.0.1", {"input_ids": [1], "output_tokens": counts[-1]})
        single = check([4], [0, 2, 3])
        self.assertTrue(single["ok"])
        self.assertIsNone(single["tpot_seconds"])
        self.assertEqual(single["first_chunk_tokens"], 4)
        self.assertEqual(single["ttft_seconds"], 2)
        self.assertEqual(single["latency_seconds"], 3)
        self.assertAlmostEqual(single["output_tokens_per_second"], 4 / 3)
        multi = check([3, 5], [0, 2, 6, 7])
        self.assertEqual(multi["tpot_seconds"], 2)  # (6 - 2) / (5 - 3), not /4.
        self.assertEqual(multi["itl_seconds"], [2])
        self.assertEqual(multi["ttft_seconds"], 2)
        normal = check([1, 2, 3], [0, 1, 3, 5, 6], {"type": "length", "length": 3})
        self.assertEqual(normal["tpot_seconds"], 2)
        for finish in ({"type": "abort", "message": "private-error", "status_code": 500, "err_type": "InternalError"},
                       {"type": "error"}, "abort", "error"):
            result = check([3], [0, 1], finish)
            self.assertFalse(result["ok"])
            self.assertNotIn("private-error", json.dumps(result))

    def test_sampler_failure_cannot_mark_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result"
            previous = signal.getsignal(signal.SIGTERM)
            with patch.object(measurement.Sampler, "__exit__", side_effect=RuntimeError("fixture sampler failure")), \
                    patch.object(measurement.Sampler, "__enter__"), self.assertRaises(RuntimeError):
                measurement.run_command([sys.executable, "-c", "pass"], out, 5)
            result = json.loads((out / "run.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["error_category"], "telemetry")
            self.assertEqual(result["error_class"], "RuntimeError")
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_storage_sigterm_reaps_owned_workload_and_preserves_exit(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/performance/storage-cancel.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            runner = subprocess.Popen([sys.executable, str(fixture), "runner", str(root)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            pids = []
            try:
                deadline = time.monotonic() + 10
                while not (root / "workload-ready").exists():
                    if runner.poll() is not None or time.monotonic() > deadline:
                        self.fail("harmless workload did not start")
                    time.sleep(.02)
                pids = json.loads((root / "workload-ready").read_text())
                runner.send_signal(signal.SIGTERM)
                runner.communicate(timeout=15)
                self.assertEqual(runner.returncode, 143)
                run = json.loads((root / "result/0-prepare/run.json").read_text())
                report = json.loads((root / "result/result.json").read_text())
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["error_category"], "interrupted")
                self.assertEqual(run["interrupted_signal"], signal.SIGTERM)
                self.assertEqual(run["returncode"], -signal.SIGKILL)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error_category"], "interrupted")
                for pid in pids:
                    # An orphan zombie awaiting init's reaper has exited; it
                    # is not a surviving workload. Inspect only fixture PIDs.
                    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                    self.assertTrue(not status or status.startswith("Z"), (pid, status))
                self.assertIsNone(unrelated.poll())
            finally:
                if runner.poll() is None:
                    runner.kill()
                runner.communicate(timeout=5)
                if pids:
                    try:
                        os.killpg(pids[0], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                unrelated.terminate()
                unrelated.wait(timeout=5)
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(fixture), "exit7", tmp], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(json.loads((Path(tmp) / "result/result.json").read_text())["returncode"], 7)

    def test_serving_complete_and_failure_records(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                evidence = root / "evidence"
                evidence.mkdir()
                (evidence / "runtime.json").write_text(json.dumps({"settings": {"MODEL_REVISION": "revision", "CONTEXT_LENGTH": "4096", "MODEL_PATH": "/models/fixture"}, "model_files": {"model": "hash"}, "launch": ["fixture"]}))
                (evidence / "pod.json").write_text(json.dumps({"uid": "fixture", "image": "image@sha256:" + "a" * 64, "image_id": "actual"}))
                workload = {"schema": 1, "profile": "interactive", "model_revision": "revision", "prefix_state": "warm-prefix",
                    "concurrency": [1, 2], "repetitions": 3, "requests_per_worker": 1,
                    "cases": [{"context_tokens": 4096, "output_tokens": 3, "input_ids": [1] * 4093, "source_sha256": "a" * 64}]}
                file = root / "workload.json"
                file.write_text(json.dumps(workload))
                base = f"http://127.0.0.1:{server.server_port}"
                Handler.mode = "valid"
                self.assertEqual(serving.run(base, file, evidence, root / "good"), 0)
                good = json.loads((root / "good/result.json").read_text())
                self.assertEqual(len(good["runs"]), 6)
                self.assertEqual(good["repeat_summary"][0]["throughput_across_repetitions"]["n"], 3)
                self.assertEqual(good["status"], "measured-awaiting-provenance")
                Handler.mode = "truncated"
                with self.assertRaises(ValueError):
                    serving.run(base, file, evidence, root / "bad")
                self.assertEqual(json.loads((root / "bad/result.json").read_text())["status"], "incomplete")
                workload["concurrency"] = [48]
                with self.assertRaises(ValueError):
                    serving.validate(workload, json.loads((evidence / "runtime.json").read_text()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cpu_unknown_and_effective_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(measurement.cpu_power(root)["amd_pstate_status"])
            policy = root / "devices/system/cpu/cpufreq/policy0"
            policy.mkdir(parents=True)
            (policy / "scaling_driver").write_text("amd-pstate-epp\n")
            (policy / "energy_performance_preference").write_text("performance\n")
            result = measurement.cpu_power(root)
            self.assertEqual(result["policies"]["policy0"]["scaling_driver"], "amd-pstate-epp")
            self.assertIsNone(result["policies"]["policy0"]["boost"])

    def test_summaries_and_endpoint(self):
        self.assertEqual(serving.summary([1, 2, 3])["median"], 2)
        for bad in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                serving.summary([bad])
        for url in ("http://public.example/v1", "http://user:password@localhost/v1",
                    "http://localhost/v1?secret=yes", "http://localhost/v1#fragment"):
            with self.assertRaises(ValueError):
                serving.endpoint(url)
        self.assertEqual(serving.endpoint("http://127.0.0.1:18000/v1"), "http://127.0.0.1:18000")

    def test_stream_cases(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            for mode in ("valid", "malformed", "truncated"):
                Handler.mode = mode
                result = serving.stream(base, {"input_ids": [1], "output_tokens": 3})
                self.assertEqual(result["ok"], mode == "valid")
                self.assertNotIn("text", result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_run_failure_timeout_and_output_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = measurement.run_command([sys.executable, "-c", "raise SystemExit(7)"], root / "failed", 5)
            self.assertEqual(status, 7)
            self.assertEqual(json.loads((root / "failed/run.json").read_text())["status"], "failed")
            with self.assertRaises(FileExistsError):
                measurement.run_command(["false"], root / "failed", 1)
            with self.assertRaises(subprocess.TimeoutExpired):
                measurement.run_command([sys.executable, "-c", "import time; time.sleep(5)"], root / "timeout", .1)
            self.assertEqual(json.loads((root / "timeout/run.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
