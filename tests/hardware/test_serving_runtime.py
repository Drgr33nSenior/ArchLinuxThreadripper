"""Synthetic contracts for offline loading/queue plans and warm status.

No selected image, model, server, Pod or GPU is executed here.  These tests
only prove that source evidence gates bounded candidate JSON.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import serving_runtime

probe_spec = importlib.util.spec_from_file_location("kernel_probe", ROOT / "tests/hardware/sglang-kernel-evidence.py")
kernel_probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(kernel_probe)


def fixtures():
    lock = dict(line.split("=", 1) for line in (ROOT / "versions.lock").read_text().splitlines()
                if line and not line.startswith("#"))
    resources = {kind: {"cpu": "24", "memory": "38Gi", "amd.com/gpu": "2"}
                 for kind in ("requests", "limits")}
    pod = {"uid": "fixture-a", "started_at": "2026-09-11T00:00:00Z", "container_id": "containerd://fixture",
           "restart_count": 0, "ready": True, "image": lock["SGLANG_ROCM_IMAGE"],
           "image_id": "containerd://" + lock["SGLANG_ROCM_IMAGE"].split("@")[-1], "resources": resources}
    runtime = {"settings": {"MODEL_REVISION": "a" * 40, "MODEL_PATH": "/models/fixture", "CONTEXT_LENGTH": "4096",
                            "TENSOR_PARALLEL": "2", "MAX_RUNNING_REQUESTS": "2",
                            "TRITON_CACHE_DIR": "/cache/triton/workstation/fixture",
                            "TORCHINDUCTOR_CACHE_DIR": "/cache/torchinductor/workstation/fixture"},
               "model_files": {"model.safetensors": {"sha256": "c" * 64}}, "packages": {"torch": "fixture"},
               "hip": "fixture", "devices": [{"uuid": "GPU-a", "gfx": "gfx1201"}, {"uuid": "GPU-b", "gfx": "gfx1201"}],
               "launch": [{"--model-path": "/models/fixture"}],
               "model_contract": {"architectures": ["Qwen3_5ForConditionalGeneration"]}}
    cap_sources = {"srt/model_loader/default_loader.py": "d" * 64}
    compiler = {"schema": 1, "status": "observed-not-qualified", "python": "fixture", "host_kernel": "fixture",
                "amdgpu_srcversion": None, "packages": {"torch": "fixture"}, "hip": "fixture",
                "compiler": {"sha256": "d" * 64}, "loaded_libraries": {"libhip.so": "e" * 64}, "settings": {},
                "sources": {"srt/server_args.py": lock["SGLANG_SERVER_ARGS_SHA256"],
                            "srt/compilation/torch_compile_decoration.py": lock["SGLANG_COMPILE_DECORATION_SHA256"],
                            "srt/models/qwen3_5.py": lock["SGLANG_QWEN35_SOURCE_SHA256"]},
                "compile_threads_supported": True, "affinity_cpus": 24,
                "flags": ["--model-loader-extra-config", "--max-queued-requests"],
                "runtime_capabilities": {
                    "model_loader_threads": {"status": "supported-source-contract", "flag": "--model-loader-extra-config",
                                             "key": "num_threads", "enable_key": "enable_multithread_load", "sources": cap_sources},
                    "bounded_queue": {"status": "supported-source-contract", "flag": "--max-queued-requests",
                                      "key": "maximum_queued_requests", "sources": cap_sources}},
                "cgroup": {"cpu.max": "2400000 100000", "memory.max": str(38 * 1024**3),
                            "memory.current": str(8 * 1024**3), "memory.peak": str(10 * 1024**3),
                            "memory.events": "max 0\noom 0\noom_kill 0", "cpuset.cpus.effective": "0-23"},
                "caches": {"TRITON_CACHE_DIR": {"root": "/cache/triton/workstation/fixture", "files": {}},
                           "TORCHINDUCTOR_CACHE_DIR": {"root": "/cache/torchinductor/workstation/fixture", "files": {}}},
                "storage": {"schema": 1, "observed_at": "2026-09-11T00:00:00Z", "roots": {
                    "model": {"status": "observed", "path": "/models/fixture", "filesystem_device": 1,
                              "root_inode": 2, "total_bytes": 1000, "available_bytes": 500,
                              "observed_at": "2026-09-11T00:00:00Z"},
                    "triton": {"status": "observed", "path": "/cache/triton/workstation/fixture", "filesystem_device": 1,
                               "root_inode": 3, "total_bytes": 1000, "available_bytes": 400,
                               "observed_at": "2026-09-11T00:00:00Z"},
                    "torchinductor": {"status": "observed", "path": "/cache/torchinductor/workstation/fixture", "filesystem_device": 1,
                                      "root_inode": 4, "total_bytes": 1000, "available_bytes": 300,
                                      "observed_at": "2026-09-11T00:00:00Z"}}}}
    container = {"name": "sglang", "image": pod["image"], "resources": resources,
                 "args": ["--model-path", "$(MODEL_PATH)"], "env": [],
                 "volumeMounts": [{"name": "cache", "mountPath": "/cache"}]}
    deployment = {"spec": {"template": {"spec": {"containers": [container], "volumes": [
        {"name": "cache", "persistentVolumeClaim": {"claimName": "sglang-hf-cache"}},
        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}}]}}}}
    return deployment, (pod, runtime, compiler)


class ServingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.deployment, self.observed = fixtures()

    def test_loading_plan_is_independent_and_exact_image_gated(self):
        before = copy.deepcopy(self.deployment)
        result = serving_runtime.loading_plan(self.deployment, self.observed, [1, 2, 4], 32768, 256)
        self.assertEqual(self.deployment, before)
        self.assertEqual(result["status"], "plan-only-unqualified")
        self.assertEqual(result["cases"]["loader-threads-2"]["total_threads"], 4)
        self.assertEqual(result["disk_headroom"]["roots"]["model"]["available_bytes"], 500)
        args = result["cases"]["loader-threads-2"]["patch"][1]["value"]
        self.assertEqual(args[-2:], ["--model-loader-extra-config", '{"enable_multithread_load":true,"num_threads":2}'])
        self.assertEqual(result["unsupported"]["pre_sharded_checkpoints"].split()[0], "unsupported")
        self.observed[2]["runtime_capabilities"]["model_loader_threads"]["status"] = "unsupported"
        with self.assertRaises(ValueError):
            serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)

    def test_loading_refuses_unknown_config_and_unbounded_budget(self):
        self.observed[1]["launch"][0]["--model-loader-extra-config"] = '{"unrelated":true}'
        with self.assertRaises(ValueError):
            serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)
        self.observed[1]["launch"][0].pop("--model-loader-extra-config")
        for count, reserve, thread_mib in ((65, 32768, 256), (1, 38912, 256), (1, 32768, 0)):
            with self.subTest(count=count, reserve=reserve, thread_mib=thread_mib), self.assertRaises(ValueError):
                serving_runtime.loading_plan(self.deployment, self.observed, [count], reserve, thread_mib)

    def test_disk_headroom_is_bound_when_observed_and_unknown_for_old_or_unsafe_evidence(self):
        self.observed[2].pop("storage")
        result = serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)
        self.assertEqual(result["disk_headroom"]["status"], "unknown")

        for change in ("zero-total", "bad-timestamp", "parent-escape"):
            with self.subTest(change=change):
                deployment, observed = fixtures()
                storage = observed[2]["storage"]
                if change == "zero-total":
                    storage["roots"]["model"].update(total_bytes=0, available_bytes=0)
                elif change == "bad-timestamp":
                    storage["observed_at"] = "not-a-timestamp"
                else:
                    observed[1]["settings"]["MODEL_PATH"] = "/models/../foreign"
                    storage["roots"]["model"]["path"] = "/models/../foreign"
                self.assertEqual(serving_runtime.loading_plan(deployment, observed, [1], 32768, 256)
                                 ["disk_headroom"]["status"], "unknown")
        self.deployment, self.observed = fixtures()
        self.observed[2]["storage"]["roots"]["model"]["available_bytes"] = 1001
        result = serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)
        self.assertEqual(result["disk_headroom"]["status"], "unknown")
        self.deployment, self.observed = fixtures()
        self.observed[2]["storage"]["roots"]["model"]["path"] = "/models/other"
        result = serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)
        self.assertEqual(result["disk_headroom"]["status"], "unknown")
        self.deployment, self.observed = fixtures()
        self.observed[2]["storage"]["roots"]["triton"]["path"] = "/cache/triton/other"
        result = serving_runtime.loading_plan(self.deployment, self.observed, [1], 32768, 256)
        self.assertEqual(result["disk_headroom"]["status"], "unknown")

    def test_probe_storage_observation_uses_only_exact_safe_mount_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            model_base = root / "models"; model = model_base / "candidate"
            model.mkdir(parents=True)
            observed = kernel_probe.storage_observation(str(model), (model_base,), "2026-09-11T00:00:00Z")
            self.assertEqual(observed["status"], "observed")
            self.assertGreaterEqual(observed["available_bytes"], 0)
            self.assertGreaterEqual(observed["total_bytes"], observed["available_bytes"])
            self.assertEqual(kernel_probe.storage_observation(None, (model_base,), "at")["status"], "unknown")
            self.assertEqual(kernel_probe.storage_observation(str(root), (model_base,), "at")["status"], "unknown")
            link = root / "link"
            os.symlink(model, link)
            self.assertEqual(kernel_probe.storage_observation(str(link), (root,), "at")["status"], "unknown")
            linked_parent = root / "linked-parent"
            os.symlink(model_base, linked_parent)
            self.assertEqual(kernel_probe.storage_observation(str(linked_parent / "candidate"), (root,), "at")["status"], "unknown")
            linked_base = root / "linked-base"
            os.symlink(model_base, linked_base)
            self.assertEqual(kernel_probe.storage_observation(str(model), (linked_base,), "at")["status"], "unknown")

    def test_loading_and_queue_normalize_one_equals_form_but_refuse_duplicates(self):
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        args.extend(["--model-loader-extra-config={\"num_threads\":1}"])
        result = serving_runtime.loading_plan(self.deployment, self.observed, [2], 32768, 256)
        patched = result["cases"]["loader-threads-2"]["patch"][1]["value"]
        self.assertEqual(patched[-2:], ["--model-loader-extra-config", '{"enable_multithread_load":true,"num_threads":2}'])
        args.extend(["--model-loader-extra-config", "ignored"])
        with self.assertRaises(ValueError):
            serving_runtime.loading_plan(self.deployment, self.observed, [2], 32768, 256)

        self.deployment, self.observed = fixtures()
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        args.extend(["--max-queued-requests=4", "--max-queued-requests", "8"])
        with self.assertRaises(ValueError):
            serving_runtime.queue_plan(self.deployment, self.observed, 8)

    def test_queue_plan_is_bounded_and_priority_is_explicitly_unsupported(self):
        result = serving_runtime.queue_plan(self.deployment, self.observed, 8)
        self.assertEqual(result["maximum_queued_requests"], 8)
        self.assertEqual(result["patch"][1]["value"][-2:], ["--max-queued-requests", "8"])
        self.assertEqual(result["priority"]["status"], "unsupported-current-client-path")
        for value in (0, 1, 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                serving_runtime.queue_plan(self.deployment, self.observed, value)
        self.observed[2]["runtime_capabilities"]["bounded_queue"]["sources"] = {"broken": "not-a-hash"}
        with self.assertRaises(ValueError):
            serving_runtime.queue_plan(self.deployment, self.observed, 8)

    def test_warm_status_requires_current_process_and_runtime_identity(self):
        pod, runtime, compiler = self.observed
        status = serving_runtime.warm_status(self.observed, None)
        self.assertEqual(status["status"], "healthy")
        self.assertEqual(status["representative_warmup"], "not-applicable")
        result = {"kind": "kernel-warmup", "status": "measured-not-qualified", "pod": copy.deepcopy(pod),
                  "identity": serving_runtime.runtime_key(pod, runtime, compiler), "memory": {"status": "checked"}}
        self.assertEqual(serving_runtime.warm_status(self.observed, result)["status"], "ready")
        result["pod"]["restart_count"] = 1
        self.assertEqual(serving_runtime.warm_status(self.observed, result)["status"], "stale")
        result["pod"] = copy.deepcopy(pod); result["status"] = "incomplete"
        self.assertEqual(serving_runtime.warm_status(self.observed, result)["status"], "unknown")
        activity = {"state": "running", "pod": copy.deepcopy(pod)}
        self.assertEqual(serving_runtime.warm_status(self.observed, result, activity)["status"], "warming")
        activity["pod"]["container_id"] = "containerd://other"
        self.assertEqual(serving_runtime.warm_status(self.observed, result, activity)["status"], "unknown")
        result["status"] = "failed"
        failed = serving_runtime.warm_status(self.observed, result)
        self.assertEqual((failed["status"], failed["warmup_status"]), ("healthy", "failed"))
        result["status"] = "failed-interrupted"
        self.assertEqual(serving_runtime.warm_status(self.observed, result)["warmup_status"], "failed")
        self.observed[0]["ready"] = False
        self.assertEqual(serving_runtime.warm_status(self.observed, None)["status"], "model-loading")
        result["status"] = "measured-not-qualified"; result["pod"] = copy.deepcopy(self.observed[0])
        self.assertEqual(serving_runtime.warm_status(self.observed, result)["status"], "model-loading")
        self.observed[1]["devices"][0]["uuid"] = "unknown"
        self.assertEqual(serving_runtime.warm_status(self.observed, None)["status"], "unknown")
        self.assertEqual(serving_runtime.warm_status(self.observed, {"not": "a warmup"})["status"], "unknown")

    def test_fresh_pod_status_never_reuses_old_runtime_evidence(self):
        pod = copy.deepcopy(self.observed[0])
        pod["ready"] = False
        self.assertEqual(serving_runtime.pod_status(pod)["status"], "model-loading")
        pod["ready"] = True
        current = serving_runtime.pod_status(pod)
        self.assertEqual((current["status"], current["kubernetes_readiness"]), ("unknown", "healthy"))
        self.assertIsNone(current["identity"])

    def test_exact_image_capabilities_require_help_and_hashed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sglang"; (source / "srt/model_loader").mkdir(parents=True)
            (source / "srt/managers").mkdir(parents=True)
            (source / "srt/server_args.py").write_text("model_loader_extra_config max_queued_requests")
            (source / "srt/model_loader/default.py").write_text("num_threads enable_multithread_load")
            (source / "srt/managers/scheduler.py").write_text("max_queued_requests")
            caps = kernel_probe.runtime_capabilities(source, ["--model-loader-extra-config", "--max-queued-requests"], {})
            self.assertEqual(caps["model_loader_threads"]["status"], "supported-source-contract")
            self.assertEqual(caps["bounded_queue"]["status"], "supported-source-contract")
            missing = kernel_probe.runtime_capabilities(source, [], {})
            self.assertEqual(missing["model_loader_threads"]["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
