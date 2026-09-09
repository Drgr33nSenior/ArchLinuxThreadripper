"""Synthetic SGLang contracts; no model, compiler cache or GPU is qualified."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"lib/workstation"))
import model_kernels as kernels
import kernel_run


def fixtures():
    locks = dict(line.split("=", 1) for line in (ROOT/"versions.lock").read_text().splitlines()
                 if line and not line.startswith("#"))
    resources = {name: {"cpu": "24", "memory": "38Gi", "amd.com/gpu": "2"} for name in ("requests", "limits")}
    pod = {"uid": "fixture-a", "started_at": "2026-09-09T00:00:00Z", "image": locks["SGLANG_ROCM_IMAGE"],
           "image_id": "containerd://" + locks["SGLANG_ROCM_IMAGE"].split("@")[-1], "resources": resources}
    runtime = {"settings": {"MODEL_REVISION": "a"*40, "MODEL_PATH": "/models/fixture", "CONTEXT_LENGTH": "32768",
                            "TENSOR_PARALLEL": "2", "MAX_RUNNING_REQUESTS": "2"},
               "model_files": {"model.safetensors": {"sha256": "c"*64}}, "packages": {"torch": "fixture"}, "hip": "fixture",
               "devices": [{"uuid": "GPU-a", "gfx": "gfx1201"}, {"uuid": "GPU-b", "gfx": "gfx1201"}],
               "launch": [{"--model-path": "/models/fixture"}],
               "model_contract": {"architectures": ["Qwen3_5ForConditionalGeneration"]}}
    compiler = {"schema": 1, "status": "observed-not-qualified", "python": "fixture", "host_kernel": "fixture", "amdgpu_srcversion": None,
                "packages": {"torch": "fixture"},
                "hip": "fixture", "compiler": {"sha256": "d"*64}, "loaded_libraries": {"libhip.so": "e"*64},
                "settings": {}, "sources": {"srt/server_args.py": locks["SGLANG_SERVER_ARGS_SHA256"],
                    "srt/compilation/torch_compile_decoration.py": locks["SGLANG_COMPILE_DECORATION_SHA256"],
                    "srt/models/qwen3_5.py": locks["SGLANG_QWEN35_SOURCE_SHA256"]},
                "compile_threads_supported": True, "affinity_cpus": 24,
                "flags": ["--cuda-graph-bs-decode", "--enable-torch-compile", "--torch-compile-max-bs"],
                "cgroup": {"cpu.max": "2400000 100000", "memory.max": str(38*1024**3),
                           "memory.current": str(8*1024**3), "memory.peak": str(10*1024**3),
                           "memory.events": "max 0\noom 0\noom_kill 0", "cpuset.cpus.effective": "0-23"},
                "caches": {"TRITON_CACHE_DIR": {"root": "/cache/triton/workstation/fixture", "files": {"kernel.hsaco": {"sha256": "a"*64}}}}}
    container = {"name": "sglang", "image": pod["image"], "resources": resources,
                 "args": ["--model-path", "$(MODEL_PATH)"], "env": [{"name": "HF_HUB_OFFLINE", "value": "1"}],
                 "volumeMounts": [{"name": "cache", "mountPath": "/cache"}]}
    deployment = {"spec": {"template": {"spec": {"containers": [container], "volumes": [
        {"name": "cache", "persistentVolumeClaim": {"claimName": "sglang-hf-cache"}},
        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}}]}}}}
    workload = {"schema": 1, "profile": "interactive", "model_revision": "a"*40, "repetitions": 3,
                "requests_per_worker": 1, "concurrency": [1, 2], "prefix_state": "warm-prefix",
                "cases": [{"context_tokens": 4096, "input_ids": [1]*4094, "output_tokens": 2, "source_sha256": "f"*64}]}
    return deployment, workload, (pod, runtime, compiler)


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.deployment, self.workload, self.observed = fixtures()

    def plan(self, **kwargs):
        return kernels.plan(self.deployment, self.workload, self.observed,
                            kwargs.pop("profile", "capture"), kwargs.pop("workers", [1, 2]),
                            kwargs.pop("reserve_mib", 32768), kwargs.pop("worker_mib", 1024),
                            kwargs.pop("reserve_cpus", 2), **kwargs)

    def test_bounded_independent_profiles(self):
        before = copy.deepcopy(self.deployment)
        result = self.plan()
        self.assertEqual(result["maximum_workers_per_rank"], 3)
        self.assertEqual(result["cases"]["workers-2"]["total_workers"], 4)
        self.assertEqual(before, self.deployment)
        patch_rows = result["cases"]["workers-2"]["patch"]
        self.assertEqual(patch_rows[0]["op"], "test")
        self.assertEqual(patch_rows[1]["value"][-3:], ["--cuda-graph-bs-decode", "1", "2"])
        env = {e["name"]: e["value"] for e in patch_rows[2]["value"]}
        self.assertTrue(env["TRITON_CACHE_DIR"].startswith("/cache/triton/workstation/"))
        self.assertEqual(env["TORCHINDUCTOR_COMPILE_THREADS"], "2")
        self.assertNotEqual(result["cases"]["workers-1"]["cache_identity"], result["cases"]["workers-2"]["cache_identity"])

    def test_insufficient_memory_and_overrides(self):
        for options in ({"workers": [4]}, {"reserve_mib": 38912}, {"reserve_mib": 1},
                        {"workers": [0]}, {"workers": [1, 1]}, {"reserve_cpus": float("nan")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(**options)

    def test_tp_and_effective_cpu_limits(self):
        self.observed[2]["cgroup"]["cpu.max"] = "400000 100000"
        with self.assertRaises(ValueError):
            self.plan(workers=[2])
        self.assertEqual(self.plan(workers=[1])["maximum_workers_per_rank"], 1)
        self.observed[1]["settings"]["TENSOR_PARALLEL"] = "1"
        with self.assertRaises(ValueError):
            self.plan(workers=[1])

    def test_unknown_limits_and_new_memory_inventory(self):
        self.observed[2]["cgroup"]["memory.max"] = "max"
        with self.assertRaises(ValueError):
            self.plan()
        self.observed[2]["cgroup"]["memory.max"] = str(128*1024**3)
        # More host memory alone never expands the unchanged Pod envelope.
        self.assertEqual(self.plan()["maximum_workers_per_rank"], 3)

    def test_legacy_requires_ack_and_exact_help(self):
        with self.assertRaises(ValueError):
            self.plan(profile="legacy-compile")
        result = self.plan(profile="legacy-compile", experimental=True)
        self.assertEqual(result["cases"]["workers-1"]["patch"][1]["value"][-3:],
                         ["--enable-torch-compile", "--torch-compile-max-bs", "2"])
        self.observed[2]["flags"] = []
        with self.assertRaises(ValueError):
            self.plan(profile="legacy-compile", experimental=True)

    def test_provenance_source_and_duplicate_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, data in zip(("pod.json", "runtime.json", "compiler.json"), self.observed):
                kernels.write(Path(directory)/name, data)
            self.assertEqual(kernels.evidence(directory), self.observed)
            runtime = self.observed[1]
            runtime["devices"][1]["uuid"] = runtime["devices"][0]["uuid"]
            (Path(directory)/"runtime.json").write_text(json.dumps(runtime))
            with self.assertRaises(ValueError):
                kernels.evidence(directory)

    def test_numerical_contract(self):
        valid = {"meta_info": {"finish_reason": {"type": "length"}, "completion_tokens": 2,
                               "output_token_logprobs": [[-.5, 1, None], [-.2, 2, None]]}}
        row = kernel_run.numerical_response(valid, 2)
        row["case"] = [0, 4096, 1, 0]
        kernels.quality_compare([row], [row], .001, .001)
        for bad in ("abort", "error", None):
            data = copy.deepcopy(valid)
            data["meta_info"]["finish_reason"] = bad
            with self.assertRaises(ValueError):
                kernel_run.numerical_response(data, 2)
        other = copy.deepcopy(row)
        other["logprobs"][0] = float("nan")
        with self.assertRaises(ValueError):
            kernels.quality_compare([row], [other], .001, .001)
        with self.assertRaises(ValueError):
            kernels.quality_compare([], [], .001, .001)

    def test_memory_failure_retained(self):
        pod, runtime, compiler = self.observed
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            report = {"status": "measured-awaiting-provenance", "pod": pod,
                      "identity": kernels.runtime_key(pod, runtime, compiler)}
            kernels.write(path/"result.json", report)
            kernels.write(path/"compiler-before.json", compiler)
            changed = copy.deepcopy(compiler)
            changed["cgroup"]["memory.events"] = "max 1\noom 0\noom_kill 0"
            kernels.write(path/"compiler-after.json", changed)
            with self.assertRaises(ValueError):
                kernel_run.finish(path)
            self.assertEqual(kernels.load(path/"result.json")["status"], "failed-memory-or-provenance")

    def test_full_warmup_path_and_telemetry_failure(self):
        pod, runtime, compiler = self.observed
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ev = path/"evidence"
            ev.mkdir()
            for name, data in zip(("pod.json", "runtime.json", "compiler.json"), self.observed):
                kernels.write(ev/name, data)
            kernels.write(path/"workload.json", self.workload)
            calls = []
            def request(_base, route, payload=None, **_kwargs):
                calls.append(route)
                if route == "/model_info":
                    return io.BytesIO(json.dumps({"model_path": runtime["settings"]["MODEL_PATH"]}).encode())
                if route == "/generate":
                    self.assertTrue(payload["return_logprob"])
                    return io.BytesIO(json.dumps({"meta_info": {"completion_tokens": 2, "finish_reason": {"type": "length"},
                        "output_token_logprobs": [[-.2, 1, None], [-.3, 2, None]]}}).encode())
                return io.BytesIO(b"{}")
            def steady(_base, _workload, _evidence, output):
                output.mkdir()
                kernels.write(output/"result.json", {"runs": [{"fixture": True}], "profile": "interactive", "prefix_state": "warm-prefix"})
                (output/"host-telemetry.jsonl").write_text(json.dumps(sample) + "\n")
                return 0
            sample = {"gpus_by_bdf": {bdf: {"mem_info_vram_used": "1", "mem_info_vram_total": "2"}
                                     for bdf in ("0000:01:00.0", "0000:02:00.0")}}
            with patch.dict(os.environ, {"AGENT_BASE_URL": "http://127.0.0.1:18000/v1"}), \
                    patch.object(kernel_run.serving, "request", side_effect=request), \
                    patch.object(kernel_run.serving, "run", side_effect=steady), \
                    patch.object(kernel_run, "snapshot", return_value=sample):
                kernel_run.run("kernel-warmup", path/"workload.json", ev, ev/"compiler.json", path/"run")
                result = kernels.load(path/"run/result.json")
                self.assertEqual(len(result["quality"]), 9)
                self.assertEqual(calls.count("/generate"), 9)
                self.assertIsNotNone(result["steady_state"])
                kernels.write(path/"run/compiler-after.json", compiler)
                kernel_run.finish(path/"run")
                self.assertEqual(kernels.load(path/"run/result.json")["memory"]["status"], "checked")
                with patch.object(kernel_run, "snapshot", side_effect=RuntimeError("fixture sampler failure")), self.assertRaises(RuntimeError):
                    kernel_run.run("kernel-warmup", path/"workload.json", ev, ev/"compiler.json", path/"failed")
                self.assertEqual(kernels.load(path/"failed/result.json")["status"], "failed")

    def test_source_drift_and_workload_bounds(self):
        self.workload["concurrency"] = [1, 4]
        with self.assertRaises(ValueError):
            self.plan()
        self.workload["concurrency"] = [1, 2]
        self.deployment["spec"]["template"]["spec"]["containers"][0]["args"] += ["--cuda-graph-config", "{}"]
        with self.assertRaises(ValueError):
            self.plan()

    def test_profile_stop_failure_and_start_rejection(self):
        pod, runtime, compiler = self.observed
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            ev = path/"evidence"
            ev.mkdir()
            for name, data in zip(("pod.json", "runtime.json", "compiler.json"), self.observed):
                kernels.write(ev/name, data)
            kernels.write(path/"workload.json", self.workload)
            routes = []
            def request(_base, route, payload=None, **_kwargs):
                routes.append(route)
                if route == "/model_info":
                    return io.BytesIO(json.dumps({"model_path": runtime["settings"]["MODEL_PATH"]}).encode())
                if route == "/stop_profile":
                    raise RuntimeError("fixture stop failure")
                return io.BytesIO(b"{}")
            with patch.dict(os.environ, {"AGENT_BASE_URL": "http://127.0.0.1:18000/v1"}), \
                    patch.object(kernel_run.serving, "request", side_effect=request), \
                    patch.object(kernel_run, "generate", return_value={"tokens": [1, 2], "logprobs": [-.2, -.3]}), \
                    patch.object(kernel_run, "snapshot", return_value={}):
                with self.assertRaises(ValueError):
                    kernel_run.run("kernel-profile", path/"workload.json", ev, ev/"compiler.json", path/"profile")
                self.assertIn("/stop_profile", routes)
                self.assertEqual(kernels.load(path/"profile/result.json")["status"], "failed-profile-cleanup")
                routes.clear()
                def rejected(_base, route, payload=None, **_kwargs):
                    routes.append(route)
                    if route == "/model_info":
                        return io.BytesIO(json.dumps({"model_path": runtime["settings"]["MODEL_PATH"]}).encode())
                    raise RuntimeError("start rejected")
                with patch.object(kernel_run.serving, "request", side_effect=rejected), self.assertRaises(RuntimeError):
                    kernel_run.run("kernel-profile", path/"workload.json", ev, ev/"compiler.json", path/"rejected")
                self.assertNotIn("/stop_profile", routes)

    def test_dispatch_requires_gpu_events_and_matching_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            trace = path/"trace.json"
            kernels.write(trace, {"traceEvents": [{"cat": "cpu_op", "name": "aten::mm"}]})
            run = {"status": "measured-not-qualified", "kind": "kernel-profile", "identity": {"fixture": True}, "workload_sha256": "a"*64}
            with self.assertRaises(ValueError):
                kernels.dispatch(trace, run)
            trace.write_text(json.dumps({"traceEvents": [{"cat": "kernel", "name": "fixture_kernel", "ph": "X", "dur": 10}]}))
            observed = kernels.dispatch(trace, run)
            kernels.write(path/"review.json", {"backend": "hipblaslt", "kernel": "wrong"})
            with self.assertRaises(ValueError):
                kernels.seal_tuning(run, observed, path/"review.json", path/"missing", "hipblaslt", path/"out")
            self.assertEqual(observed["kernels_us"], {"fixture_kernel": 10})
            (path/"library.log").write_text("hipblasLtMatmul fixture operation\n")
            review = {"backend": "hipblaslt", "kernel": "fixture_kernel", "shape": [2, 4, 8], "dtype": "bf16",
                      "library_log": "library.log", "library_log_sha256": kernels.file_hash(path/"library.log")}
            (path/"review.json").write_text(json.dumps(review))
            (path/"tuning.txt").write_text("fixture,not-a-real-tuning-result\n")
            kernels.seal_tuning(run, observed, path/"review.json", path/"tuning.txt", "hipblaslt", path/"sealed")
            manifest = kernels.load(path/"sealed/manifest.json")
            self.assertEqual(manifest["artifact_sha256"], kernels.file_hash(path/"sealed/tuning-result"))
            (path/"library.log").write_text("changed")
            with self.assertRaises(ValueError):
                kernels.seal_tuning(run, observed, path/"review.json", path/"tuning.txt", "hipblaslt", path/"changed")

    def test_cache_empty_is_not_reuse(self):
        # Complete run records with the same software but a real process boundary.
        pod, runtime, compiler = self.observed
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for index, name in enumerate(("cold", "warm")):
                output = path/name
                output.mkdir()
                p = {**pod, "uid": name}
                result = {"status": "measured-not-qualified", "kind": "kernel-warmup", "pod": p,
                    "identity": kernels.runtime_key(pod, runtime, compiler), "workload_sha256": "a"*64,
                    "quality": [{"case": [i, 4096, 1, 0], "tokens": [1], "logprobs": [-.2]} for i in range(3)],
                    "warmup_contract": {"repetitions": 3, "concurrency": [1], "cases": [{"context_tokens": 4096, "output_tokens": 1}]},
                    "memory": {"status": "checked"}, "warmup_seconds": 1, "steady_state": {"fixture": True}}
                kernels.write(output/"result.json", result)
                kernels.write(path/(name+"-startup.json"), {"status": "observed-not-qualified", "pod": p, "elapsed_seconds": 10-index})
            kernels.write(path/"cold/compiler-after.json", compiler)
            kernels.write(path/"warm/compiler-before.json", compiler)
            for rank in range(2):
                (path/f"rank{rank}.log").write_text(f"rank {rank}: fx graph cache hit for key fixture\n")
            logs = [path/"rank0.log", path/"rank1.log"]
            args = [path/"cold", path/"warm", path/"cold-startup.json", path/"warm-startup.json", logs, .001, .001]
            result = kernels.compare(*args)
            self.assertEqual(result["status"], "reuse-observed-not-qualified")
            empty = copy.deepcopy(compiler)
            empty["caches"]["TRITON_CACHE_DIR"]["files"] = {}
            (path/"warm/compiler-before.json").write_text(json.dumps(empty))
            with self.assertRaises(ValueError):
                kernels.compare(*args)
            (path/"warm/compiler-before.json").write_text(json.dumps(compiler))
            logs[1].write_text("compilation occurred, no hit\n")
            with self.assertRaises(ValueError):
                kernels.compare(*args)

    def test_cli_generation_and_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, data in zip(("pod.json", "runtime.json", "compiler.json"), self.observed):
                kernels.write(path/name, data)
            kernels.write(path/"deployment.json", self.deployment)
            kernels.write(path/"workload.json", self.workload)
            args = ["model_kernels.py", "plan", str(path/"deployment.json"), str(path/"workload.json"), str(path), str(path/"plan"),
                    "--profile", "capture", "--workers", "1,2", "--reserve-mib", "32768", "--worker-mib", "1024"]
            with patch.object(sys, "argv", args):
                kernels.main()
            self.assertEqual(kernels.load(path/"plan/plan.json")["status"], "plan-only-unqualified")
            compiler = copy.deepcopy(self.observed[2])
            compiler["sources"]["srt/server_args.py"] = "0"*64
            (path/"compiler.json").write_text(json.dumps(compiler))
            with self.assertRaises(ValueError):
                kernels.evidence(path)


if __name__ == "__main__":
    unittest.main()
