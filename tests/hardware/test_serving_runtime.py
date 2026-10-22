"""Synthetic contracts for offline loading/queue plans and warm status.

No selected image, model, server, Pod or GPU is executed here.  These tests
only prove that source evidence gates bounded candidate JSON.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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
    def test_sdk_compiler_uses_only_selected_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            llvm = sdk / "lib/llvm/bin"
            llvm.mkdir(parents=True)
            compiler = llvm / "clang++"
            compiler.write_text("synthetic compiler; never executed")
            compiler.chmod(0o700)
            self.assertEqual(kernel_probe.sdk_compiler(sdk), str(compiler))
            compiler.chmod(0o600)
            self.assertIsNone(kernel_probe.sdk_compiler(sdk))
            compiler.unlink()
            outside = root / "foreign-clang"
            outside.write_text("synthetic foreign compiler; never executed")
            outside.chmod(0o700)
            compiler.symlink_to(outside)
            self.assertIsNone(kernel_probe.sdk_compiler(sdk))
            core = root / "_rocm_sdk_core/lib/llvm/bin/clang"
            core.parent.mkdir(parents=True)
            core.write_text("synthetic matched wheel compiler; never executed")
            core.chmod(0o700)
            devel = root / "_rocm_sdk_devel"
            exposed = devel / "lib/llvm/bin/clang++"
            exposed.parent.mkdir(parents=True)
            exposed.symlink_to(core)
            self.assertEqual(kernel_probe.sdk_compiler(devel), str(exposed))
            with self.assertRaises(ValueError):
                kernel_probe.sdk_compiler("relative-sdk")

    def setUp(self):
        self.deployment, self.observed = fixtures()

    def test_petit_option_records_request_but_never_emits_a_gfx1201_patch(self):
        self.observed[2]["flags"].append("--quantization")
        before = copy.deepcopy((self.deployment, self.observed))
        result = serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", ["petit_nvfp4"])
        self.assertEqual((self.deployment, self.observed), before)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["qualification"], "unqualified")
        self.assertEqual(result["requested"], {"--quantization": "petit_nvfp4"})
        self.assertIsNone(result["observed_quantization"])
        self.assertEqual(result["cases"], {})
        self.assertNotIn("patch", result)
        self.assertIn("gfx1201", result["reason"])
        self.assertIn("MFMA", result["reason"])
        self.assertEqual(result["identity"], serving_runtime.runtime_key(*self.observed))

    def test_petit_option_does_not_accept_cli_presence_as_hardware_support(self):
        self.observed[2]["flags"].append("--quantization")
        self.observed[2]["runtime_capabilities"]["petit_nvfp4"] = {"status": "supported-source-contract"}
        self.observed[2]["packages"]["petit-kernel"] = "synthetic"
        self.observed[1]["launch"][0]["--quantization"] = "petit_nvfp4"
        self.deployment["spec"]["template"]["spec"]["containers"][0]["args"] += ["--quantization=petit_nvfp4"]
        result = serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", ["petit_nvfp4"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["observed_quantization"], "petit_nvfp4")
        self.assertEqual(result["cases"], {})

    def test_petit_option_refuses_missing_help_ambiguous_inputs_and_other_experiments(self):
        with self.assertRaisesRegex(ValueError, "exact-image help"):
            serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", ["petit_nvfp4"])
        self.observed[2]["flags"].append("--quantization")
        for values, algorithm in ((["petit_mxfp4"], None), (["auto", "petit_nvfp4"], None),
                                  (["petit_nvfp4"], "NGRAM")):
            with self.subTest(values=values, algorithm=algorithm), self.assertRaises(ValueError):
                serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", values, algorithm)
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        for added in (["--quantization", "modelopt_fp4"], ["--quantization"],
                      ["--quantization=petit_nvfp4", "--quantization=petit_nvfp4"]):
            with self.subTest(added=added):
                args[2:] = added
                with self.assertRaises(ValueError):
                    serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", ["petit_nvfp4"])
        args[2:] = []
        self.observed[1]["experimental_kv"] = {"qualification": "unqualified"}
        with self.assertRaisesRegex(ValueError, "separate experimental KV"):
            serving_runtime.experiment_plan(self.deployment, self.observed, "weight-kernel", ["petit_nvfp4"])

    def test_actual_petit_cli_retains_private_blocked_evidence_and_failure_status(self):
        self.observed[2]["flags"].append("--quantization")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in zip(("pod", "runtime", "compiler"), self.observed):
                (root / (name + ".json")).write_text(json.dumps(value))
            deployment = root / "deployment.json"
            deployment.write_text(json.dumps(self.deployment))
            output = root / "petit"
            command = ["bash", str(ROOT / "bin/workstationctl"), "--config", str(ROOT / "config/workstation.conf.example"),
                       "rocm", "serving-experiment-plan", str(deployment), str(root), str(output),
                       "--axis", "weight-kernel", "--values", "petit_nvfp4"]
            environment = dict(os.environ, WORKSTATION_PYTHON=sys.executable, KUBECONFIG="/dev/null")
            completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertIn("gfx1201", completed.stderr)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["plan.json"])
            report = output / "plan.json"
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            original = report.read_bytes()
            self.assertEqual(json.loads(original)["status"], "blocked")
            self.assertNotEqual(subprocess.run(command, env=environment, capture_output=True, timeout=10).returncode, 0)
            self.assertEqual(report.read_bytes(), original)
            # Do not broaden the workstation's hardware policy for this option.
            self.observed[1]["devices"][0]["gfx"] = "gfx942"
            (root / "runtime.json").write_text(json.dumps(self.observed[1]))
            command[8] = str(root / "foreign")
            refused = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("distinct observed gfx1201", refused.stderr)
            self.assertFalse((root / "foreign").exists())

    def test_speculation_candidates_preserve_model_resources_and_do_not_apply(self):
        self.observed[2]["flags"] += ["--speculative-algorithm", "--speculative-num-draft-tokens"]
        before = copy.deepcopy(self.deployment)
        result = serving_runtime.experiment_plan(self.deployment, self.observed, "speculation", ["off", "2", "4"], "NGRAM")
        self.assertEqual(self.deployment, before)
        self.assertEqual(result["status"], "plan-only-unqualified")
        self.assertEqual(result["cases"]["speculation-off"]["patch"][1]["value"], before["spec"]["template"]["spec"]["containers"][0]["args"])
        self.assertEqual(result["cases"]["speculation-4"]["patch"][1]["value"][-4:],
                         ["--speculative-algorithm", "NGRAM", "--speculative-num-draft-tokens", "4"])
        for row in result["cases"].values():
            self.assertEqual(row["patch"][0]["value"], before["spec"]["template"]["spec"])
        self.assertEqual(result["unsupported"]["paroquant"]["status"], "unsupported")

    def test_experiments_refuse_missing_help_unsupported_adaptation_and_kv_combinations(self):
        for axis, values, algorithm in (("speculation", ["2"], "NGRAM"),
                                         ("speculation", ["adaptive"], "NGRAM"),
                                         ("hybrid-state", ["fp8"], None)):
            with self.subTest(axis=axis, values=values), self.assertRaises(ValueError):
                serving_runtime.experiment_plan(self.deployment, self.observed, axis, values, algorithm)
        self.observed[2]["flags"] += ["--mamba-ssm-dtype"]
        self.observed[1]["experimental_kv"] = {"qualification": "unqualified"}
        with self.assertRaises(ValueError):
            serving_runtime.experiment_plan(self.deployment, self.observed, "hybrid-state", ["float32"], None)

    def test_hybrid_precision_changes_only_its_explicit_argument(self):
        self.observed[2]["flags"] += ["--mamba-ssm-dtype"]
        result = serving_runtime.experiment_plan(self.deployment, self.observed, "hybrid-state", ["float32", "float16"], None)
        args = result["cases"]["hybrid-state-float16"]["patch"][1]["value"]
        self.assertEqual(args, self.deployment["spec"]["template"]["spec"]["containers"][0]["args"] + ["--mamba-ssm-dtype", "float16"])
        self.assertEqual(result["qualification"], "unqualified")

    def test_experiments_refuse_manifest_observation_disagreement(self):
        self.observed[2]["flags"] += ["--speculative-algorithm", "--speculative-num-draft-tokens", "--mamba-ssm-dtype"]
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        args.extend(["--speculative-algorithm", "NGRAM"])
        with self.assertRaisesRegex(ValueError, "observed launch"):
            serving_runtime.experiment_plan(self.deployment, self.observed, "speculation", ["off", "2"], "NGRAM")
        args[-2:] = ["--mamba-ssm-dtype", "float16"]
        with self.assertRaisesRegex(ValueError, "observed launch"):
            serving_runtime.experiment_plan(self.deployment, self.observed, "hybrid-state", ["float32"])

    def test_adaptive_requires_existing_eagle_topk_one_and_retains_draft_arguments(self):
        launch = self.observed[1]["launch"][0]
        launch.update({"--speculative-algorithm": "EAGLE", "--speculative-eagle-topk": "1",
                       "--speculative-draft-model-path": "/models/staged-draft",
                       "--speculative-draft-model-revision": "b" * 40})
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        for flag, value in list(launch.items())[1:]:
            args.extend([flag, value])
        self.observed[2]["flags"] += ["--speculative-algorithm", "--speculative-eagle-topk", "--speculative-num-steps",
                                       "--speculative-num-draft-tokens", "--speculative-adaptive"]
        result = serving_runtime.experiment_plan(self.deployment, self.observed, "speculation", ["off", "3", "adaptive"], "EAGLE")
        adaptive = result["cases"]["speculation-adaptive"]["patch"][1]["value"]
        self.assertEqual(adaptive.count("--speculative-algorithm"), 1)
        self.assertIn("/models/staged-draft", adaptive)
        self.assertIn("--speculative-adaptive", adaptive)
        off = result["cases"]["speculation-off"]["patch"][1]["value"]
        self.assertFalse(any(item.startswith("--speculative-") for item in off))
        launch["--speculative-eagle-topk"] = "2"
        with self.assertRaises(ValueError):
            serving_runtime.experiment_plan(self.deployment, self.observed, "speculation", ["adaptive"], "EAGLE")

    def test_actual_cli_writes_private_candidates_without_overwriting_evidence(self):
        self.observed[2]["flags"] += ["--mamba-ssm-dtype"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in zip(("pod", "runtime", "compiler"), self.observed):
                (root / (name + ".json")).write_text(json.dumps(value))
            deployment = root / "deployment.json"
            deployment.write_text(json.dumps(self.deployment))
            output = root / "candidate"
            command = ["bash", str(ROOT / "bin/workstationctl"), "--config", str(ROOT / "config/workstation.conf.example"),
                       "rocm", "serving-experiment-plan", str(deployment), str(root), str(output),
                       "--axis", "hybrid-state", "--values", "float32,float16"]
            environment = dict(os.environ, WORKSTATION_PYTHON=sys.executable, KUBECONFIG="/dev/null")
            completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            patch = json.loads((output / "hybrid-state-float16.json").read_text())
            self.assertEqual(patch[1]["value"][-2:], ["--mamba-ssm-dtype", "float16"])
            for path in output.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = (output / "plan.json").read_bytes()
            self.assertNotEqual(subprocess.run(command, env=environment, capture_output=True, timeout=10).returncode, 0)
            self.assertEqual((output / "plan.json").read_bytes(), original)


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
        resumed = copy.deepcopy(self.observed)
        resumed[0]["restart_count"] = 1
        self.assertEqual(serving_runtime.warm_status(resumed, None)["status"], "healthy")
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

    def test_current_container_failures_are_bounded_and_do_not_reuse_history(self):
        pod = copy.deepcopy(self.observed[0])
        pod.update(ready=False, container_state="crash-loop-oom-killed",
                   last_state={"terminated": {"reason": "OOMKilled", "message": "private diagnostic"}})
        result = serving_runtime.pod_status(pod)
        self.assertEqual((result["status"], result["kubernetes_readiness"]), ("unavailable", "unavailable"))
        self.assertEqual(result["reason"], "current container is restarting after an out-of-memory termination")
        self.assertNotIn("private diagnostic", json.dumps(result))
        self.assertEqual(serving_runtime.warm_status((pod, self.observed[1], self.observed[2]), None)["status"], "unavailable")
        pod["ready"] = True
        self.assertEqual(serving_runtime.pod_status(pod)["reason"], "current Pod state is inconsistent with readiness")
        self.assertEqual(serving_runtime.warm_status((pod, self.observed[1], self.observed[2]), None)["status"], "unknown")
        pod["ready"] = False
        for state, reason in (("crash-loop", "current container is restarting repeatedly; inspect the reviewed workload logs"),
                              ("terminated-oom-killed", "current container terminated after an out-of-memory termination"),
                              ("terminated", "current container terminated; inspect the reviewed workload logs"),
                              ("image-failure", "current container image cannot be started")):
            with self.subTest(state=state):
                pod["container_state"] = state
                self.assertEqual((serving_runtime.pod_status(pod)["status"], serving_runtime.pod_status(pod)["reason"]),
                                 ("unavailable", reason))
        pod.update(ready=True, container_state="running")
        healthy = serving_runtime.pod_status(pod)
        self.assertEqual((healthy["status"], healthy["kubernetes_readiness"]), ("unknown", "healthy"))
        pod["container_id"] = None
        missing = serving_runtime.pod_status(pod)
        self.assertEqual((missing["status"], missing["kubernetes_readiness"]), ("unknown", "unknown"))
        self.assertEqual(missing["reason"], "current Pod process identity is incomplete")
        pod["uid"] = ""
        self.assertEqual(serving_runtime.pod_status(pod)["reason"], "current Pod identity is incomplete")

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
