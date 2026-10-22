"""Offline GEMM package/profile contracts; no GPU or image build is implied."""

import base64
import copy
import csv
import functools
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import gemm_profile as gemm


class GemmProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.profile = gemm.load(gemm.PROFILE_DIR / "profile.json")
        self.observation = {name: copy.deepcopy(self.profile[name]) for name in ("sources", "runtime", "devices", "settings", "model_files", "environment")}
        self.observation.update(visibility_overrides=[], communication_overrides=[], host={"kernel_release": "fixture", "boot_id": "d819d208-9753-464e-b20a-a78b2fff9234"})
        args = []
        for option, value in self.profile["settings"].items():
            args += [option] if value is True else [option, value]
        resources = {"requests": {"amd.com/gpu": "2", "cpu": "8", "memory": "40Gi"},
                     "limits": {"amd.com/gpu": "2", "cpu": "8", "memory": "40Gi"}}
        self.deployment = {"spec": {"replicas": 0, "template": {"spec": {
            "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "sglang", "image": self.profile["parent_image"], "resources": resources,
                            "args": args, "command": ["python3", "-m", "sglang.launch_server"],
                            "env": [{"name": "HF_HUB_OFFLINE", "value": "1"}],
                            "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [{"name": "cache", "mountPath": "/cache"}]}],
            "volumes": [{"name": "cache", "persistentVolumeClaim": {"claimName": "sglang-hf-cache"}}]}}}}
        self.built = {"profile": "gemm-m1", "parent_image": self.profile["parent_image"],
                      "profile_sha256": gemm.sha(gemm.canonical(self.profile)),
                      "image": "localhost/workstation/sglang@sha256:" + "a" * 64,
                      "config_digest": "sha256:" + "b" * 64}

    def test_exact_tables_and_boundary_selection(self):
        tables = gemm.validate_tables(self.profile, gemm.PROFILE_DIR)
        for raw in tables.values():
            values = {int(key): row for key, row in json.loads(raw).items()}
            for m in (1, 2, 3, 4, 5, 8, 31, 128, 511, 2048, 4096):
                expected = self.profile["single_token"] if m == 1 else self.profile["incumbent"]
                self.assertEqual(expected, values[min(values, key=lambda row: abs(row - m))])
        for changed in ("m_grid", "tables", "moe", "communication"):
            profile = copy.deepcopy(self.profile)
            profile[changed] = None
            with self.assertRaises((ValueError, TypeError)):
                gemm.validate_tables(profile, gemm.PROFILE_DIR)

    def test_reproducible_oci_delta_and_record_preservation(self):
        profile_dir = self.temp / "profile"
        shutil.copytree(gemm.PROFILE_DIR, profile_dir)
        checksum = base64.urlsafe_b64encode(bytes.fromhex(self.profile["sources"]["srt/layers/sampler.py"])).decode().rstrip("=")
        raw_record = ("unrelated.py,sha256=unchanged,3\r\nsglang/srt/layers/sampler.py,sha256=" + checksum + ",123\r\n").encode()
        profile = copy.deepcopy(self.profile)
        profile["parent_record_sha256"] = gemm.sha(raw_record)
        (profile_dir / "profile.json").write_bytes(gemm.canonical(profile))
        record = self.temp / "RECORD"
        record.write_bytes(raw_record)
        one = gemm.package(self.temp / "one", record_path=record, profile_dir=profile_dir)
        two = gemm.package(self.temp / "two", record_path=record, profile_dir=profile_dir)
        self.assertEqual(one, two)
        self.assertEqual((self.temp / "one/gemm-layer.tar").read_bytes(), (self.temp / "two/gemm-layer.tar").read_bytes())
        with tarfile.open(self.temp / "one/gemm-layer.tar") as archive:
            self.assertEqual(5, len(archive.getmembers()))
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((0, 0, 0o644, 0), (member.uid, member.gid, member.mode, member.mtime))
                self.assertNotIn("sampler.py", member.name)
                self.assertNotIn("fp8_kernel.py", member.name)
                self.assertEqual(one["members"][member.name], gemm.sha(archive.extractfile(member).read()))
            revised = archive.extractfile(gemm.SITE + gemm.RECORD).read()
            self.assertTrue(revised.startswith(raw_record))
            rows = list(csv.reader(io.StringIO(revised.decode())))
            self.assertEqual({gemm.TABLE_DIR + row["name"] for row in profile["tables"]}, {row[0] for row in rows[2:]})
        with self.assertRaises(FileExistsError):
            gemm.package(self.temp / "one", record_path=record, profile_dir=profile_dir)
        record.write_bytes(raw_record + b"changed")
        with self.assertRaisesRegex(ValueError, "RECORD differs"):
            gemm.package(self.temp / "bad", record_path=record, profile_dir=profile_dir)

    def test_effective_profile_and_exact_rollback(self):
        before = copy.deepcopy(self.deployment)
        result = gemm.plan(self.deployment, self.observation, self.built)
        candidate = result["patch"][1]["value"]
        original = self.deployment["spec"]["template"]["spec"]
        self.assertEqual("gemm-m1", result["selected"])
        self.assertEqual(original, result["patch"][0]["value"])
        self.assertEqual(original, result["rollback"][1]["value"])
        self.assertEqual(candidate, result["rollback"][0]["value"])
        self.assertEqual(before, self.deployment)
        container = candidate["containers"][0]
        self.assertEqual(self.built["image"], container["image"])
        self.assertEqual("launch", container["command"][-1])
        self.assertEqual(original["containers"][0]["args"], container["args"])
        self.assertEqual(original["containers"][0]["resources"], container["resources"])
        self.assertEqual(original["securityContext"], candidate["securityContext"])
        env = {row["name"]: row["value"] for row in container["env"]}
        self.assertEqual("1", env["HF_HUB_OFFLINE"])
        self.assertEqual("0", env["SGLANG_USE_AITER"])
        self.assertEqual("gemm-m1", env["WORKSTATION_GEMM_PROFILE"])
        self.assertIn(result["cache_identity"], env["TRITON_CACHE_DIR"])
        changed_boot = copy.deepcopy(self.observation)
        changed_boot["host"]["boot_id"] = "00000000-0000-0000-0000-000000000000"
        self.assertNotEqual(result["cache_identity"], gemm.plan(self.deployment, changed_boot, self.built)["cache_identity"])

    def test_launch_restarts_with_the_same_receipt_volume(self):
        # Fresh launcher processes share one directory, just like a surviving
        # /tmp emptyDir. Only hardware verification and the SGLang executable
        # are stubbed; receipt creation uses real files and permissions.
        harness = r'''
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gemm_profile as gemm
volume = Path(sys.argv[2])
mode = sys.argv[3]
profile = gemm.load(gemm.PROFILE_DIR / "profile.json")
arguments = []
for option, value in profile["settings"].items():
    arguments += [option] if value is True else [option, value]
gemm.load = lambda path: profile
def verify(*args):
    if mode == "fail":
        raise RuntimeError("verification fixture failure")
    if mode == "mismatch":
        return {"schema": 1, "status": "incumbent-required", "failures": ["identity mismatch fixture"]}
    return {"schema": 1, "status": "effective-not-model-qualified", "failures": []}
gemm.verify = verify
original_path = gemm.Path
gemm.Path = lambda path: volume / "gemm-profile-receipt.json" if str(path) == "/tmp/gemm-profile-receipt.json" else original_path(path)
original_temporary_file = tempfile.NamedTemporaryFile
def temporary_file(*args, **kwargs):
    kwargs["dir"] = volume
    return original_temporary_file(*args, **kwargs)
gemm.tempfile = type("TemporaryFiles", (), {"NamedTemporaryFile": staticmethod(temporary_file)})
original_exec = gemm.os.execv
def exec_stub(executable, argv):
    assert executable == sys.executable
    assert argv == [sys.executable, "-m", "sglang.launch_server", *arguments]
    original_exec(executable, [executable, "-c", "print('SGLANG_EXEC_STUB')"])
gemm.os.execv = exec_stub
sys.argv = ["gemm_profile.py", "launch", *arguments]
gemm.main()
'''
        command = [sys.executable, "-I", "-B", "-c", harness,
                   str(ROOT / "lib/workstation"), str(self.temp), "pass"]
        retained = {}
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("SGLANG_EXEC_STUB\n", result.stdout)
            for path, raw in retained.items():
                self.assertEqual(raw, path.read_bytes(), "restart overwrote a previous receipt")
            receipts = sorted(self.temp.glob("gemm-profile-receipt*.json"))
            self.assertEqual(len(retained) + 1, len(receipts))
            for path in receipts:
                self.assertFalse(path.is_symlink())
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                self.assertEqual([], gemm.load(path)["failures"])
            self.assertTrue(any("GEMM profile receipt: " + str(path) in result.stderr
                                for path in set(receipts) - set(retained)))
            retained = {path: path.read_bytes() for path in receipts}
        # An old fixed-name symlink must neither block launch nor be followed.
        victim = self.temp / "unrelated.json"
        victim.write_text("preserve unrelated data")
        legacy = self.temp / "gemm-profile-receipt.json"
        legacy.symlink_to(victim)
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("SGLANG_EXEC_STUB\n", result.stdout)
        self.assertTrue(legacy.is_symlink())
        self.assertEqual("preserve unrelated data", victim.read_text())
        command[-1] = "fail"
        failed = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual("", failed.stdout)
        self.assertIn("GEMM identity changed; select incumbent image", failed.stderr)
        failures = [gemm.load(path) for path in self.temp.glob("gemm-profile-receipt*.json") if path != legacy]
        self.assertEqual(4, len(failures))
        failure = next(row for row in failures if row["failures"])
        self.assertEqual("incumbent-required", failure["status"])
        self.assertEqual("RuntimeError", failure["error_class"])
        command[-1] = "mismatch"
        mismatch = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, mismatch.returncode)
        self.assertEqual("", mismatch.stdout)
        self.assertIn("GEMM identity changed; select incumbent image", mismatch.stderr)
        receipts = [path for path in self.temp.glob("gemm-profile-receipt*.json") if path != legacy]
        self.assertEqual(5, len(receipts))
        self.assertTrue(any(gemm.load(path)["failures"] == ["identity mismatch fixture"] for path in receipts))
        self.assertTrue(legacy.is_symlink())
        self.assertEqual("preserve unrelated data", victim.read_text())
        for path, raw in retained.items():
            self.assertEqual(raw, path.read_bytes())

    def test_unsupported_and_stale_fallback_remove_tuning_mounts(self):
        spec = self.deployment["spec"]["template"]["spec"]
        for index, row in enumerate(self.profile["tables"]):
            spec["containers"][0]["volumeMounts"].append({"name": f"table{index}", "mountPath": "/" + gemm.SITE + gemm.TABLE_DIR + row["name"], "readOnly": True})
            spec["volumes"].append({"name": f"table{index}", "hostPath": {"path": "/fixture"}})
        for key in ("runtime", "sources", "devices", "settings", "model_files", "host", "environment", "visibility_overrides", "communication_overrides"):
            observation = copy.deepcopy(self.observation)
            observation[key] = {} if key not in ("devices", "visibility_overrides") else ["bad"]
            result = gemm.plan(self.deployment, observation, self.built)
            self.assertEqual("incumbent", result["selected"])
            self.assertTrue(result["fallback_reasons"])
            container = result["patch"][1]["value"]["containers"][0]
            self.assertEqual(self.profile["parent_image"], container["image"])
            self.assertEqual(["cache"], [row["name"] for row in container["volumeMounts"]])
            self.assertEqual("sglang.launch_server", container["command"][-1])
        stale = copy.deepcopy(self.built)
        stale["profile_sha256"] = "0" * 64
        self.assertEqual("incumbent", gemm.plan(self.deployment, self.observation, stale)["selected"])
        self.assertEqual("incumbent", gemm.plan(self.deployment, self.observation, self.built, selection="incumbent")["selected"])
        spec["containers"][0]["args"] += ["--max-running-requests", "4"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            gemm.plan(self.deployment, self.observation, self.built)

    def test_scope_controls_aliases_and_unrelated_model_are_rejected(self):
        args = self.deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        for extra in (["--kv-cache-dtype", "fp8"], ["--tp-size", "1"], ["--tensor-parallel-size", "1"],
                      ["--disable-cuda-graph"], ["--enable-torch-compile"], ["--speculative-algorithm", "EAGLE"],
                      ["--cpu-offload-gb", "4"], ["--max-running-requests", "4"]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                gemm.parse_args(args + extra)
        changed = copy.deepcopy(self.deployment)
        changed_args = changed["spec"]["template"]["spec"]["containers"][0]["args"]
        changed_args[changed_args.index("--model-path") + 1] = "/models/another-checkpoint"
        self.assertEqual("incumbent", gemm.plan(changed, self.observation, self.built)["selected"])

    def test_manifest_environment_overrides_are_not_inherited(self):
        for override in ({"name": "SGLANG_USE_AITER", "value": "1"}, {"name": "HF_HUB_OFFLINE", "value": "0"},
                         {"name": "NCCL_CUMEM_ENABLE", "value": "1"}, {"name": "HSA_OVERRIDE_GFX_VERSION", "value": "9.0.0"}):
            changed = copy.deepcopy(self.deployment)
            container = changed["spec"]["template"]["spec"]["containers"][0]
            container["env"] = [override]
            result = gemm.plan(changed, self.observation, self.built)
            self.assertEqual("incumbent", result["selected"])
            env = {row["name"]: row["value"] for row in result["patch"][1]["value"]["containers"][0]["env"]}
            self.assertEqual("0", env["SGLANG_USE_AITER"])
            self.assertEqual("1", env["HF_HUB_OFFLINE"])
            self.assertNotIn("NCCL_CUMEM_ENABLE", env)
            self.assertNotIn("HSA_OVERRIDE_GFX_VERSION", env)
        changed = copy.deepcopy(self.deployment)
        changed["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "unresolved"}}]
        self.assertEqual("incumbent", gemm.plan(changed, self.observation, self.built)["selected"])
        changed["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "HF_HUB_OFFLINE", "value": "0"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            gemm.plan(changed, self.observation, self.built)

    def test_loader_checked_on_each_device_and_failures_retained(self):
        directory = self.temp / "installed"
        shutil.copytree(gemm.PROFILE_DIR, directory / "configs")
        events = []
        @functools.lru_cache
        def loader(n, k, bn, bk):
            events.append((n, k, bn, bk))
            row = next(row for row in self.profile["tables"] if row["n"] == n)
            return {int(key): value for key, value in gemm.load(directory / "configs" / row["name"]).items()}
        module = SimpleNamespace(__file__=str(directory / "fp8_kernel.py"), get_w8a8_block_fp8_configs=loader)
        class Device:
            def __init__(self, index): self.index = index
            def __enter__(self): events.append(self.index)
            def __exit__(self, *args): pass
        torch = SimpleNamespace(cuda=SimpleNamespace(device=Device))
        with patch.dict(sys.modules, {"torch": torch}):
            result = gemm.verify(self.profile, self.observation["settings"], observation=self.observation, module=module)
            self.assertEqual("effective-not-model-qualified", result["status"])
            self.assertEqual(4, len(result["loaded"]))
            self.assertEqual(4, len([item for item in events if isinstance(item, tuple)]))
            table = directory / "configs" / self.profile["tables"][0]["name"]
            table.write_text("{}")
            failed = gemm.verify(self.profile, self.observation["settings"], observation=self.observation, module=module)
            self.assertEqual("incumbent-required", failed["status"])
            self.assertEqual("effective-table-loading", failed["failure_stage"])
            self.assertTrue(failed["failures"])
            self.assertEqual([], failed["loaded"])
        wrong = copy.deepcopy(self.observation)
        wrong["sources"] = {}
        failed = gemm.verify(self.profile, wrong["settings"], observation=wrong, module=module)
        self.assertEqual("incumbent-required", failed["status"])
        self.assertEqual([], failed["loaded"])

    def test_existing_kernel_plan_cli_accepts_retained_token_workloads(self):
        workload = {"schema": 1, "profile": "interactive", "model_revision": self.profile["model"]["revision"],
                    "repetitions": 3, "requests_per_worker": 1, "concurrency": [1], "prefix_state": "new-prefix",
                    "cases": [{"context_tokens": 4096, "input_ids": [17] * count, "output_tokens": 128,
                               "source_sha256": "c" * 64} for count in (512, 2048)]}
        for name, value in (("deployment.json", self.deployment), ("workload.json", workload),
                            ("gemm-observation.json", self.observation), ("built.json", self.built)):
            (self.temp / name).write_bytes(gemm.canonical(value))
        command = [sys.executable, "-B", str(ROOT / "lib/workstation/model_kernels.py"), "plan",
                   str(self.temp / "deployment.json"), str(self.temp / "workload.json"), str(self.temp),
                   str(self.temp / "plan"), "--profile", "gemm-m1", "--gemm-image", str(self.temp / "built.json")]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        planned = gemm.load(self.temp / "plan/plan.json")
        self.assertEqual("gemm-m1", planned["selected"])
        self.assertTrue((self.temp / "plan/gemm-m1.json").is_file())
        self.assertTrue((self.temp / "plan/incumbent-rollback.json").is_file())
        self.assertEqual(planned["patch"], gemm.load(self.temp / "plan/gemm-m1.json"))
        table_path = "/" + gemm.SITE + gemm.TABLE_DIR + self.profile["tables"][0]["name"]
        self.deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"].append(
            {"name": "shared-tuning", "mountPath": table_path})
        for group in ("containers", "initContainers", "ephemeralContainers"):
            deployment = copy.deepcopy(self.deployment)
            deployment["spec"]["template"]["spec"].setdefault(group, []).append(
                {"name": "diagnostic", "volumeMounts": [{"name": "shared-tuning", "mountPath": "/unrelated"}]})
            (self.temp / "deployment.json").write_bytes(gemm.canonical(deployment))
            command[7] = str(self.temp / ("shared-" + group))
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("dedicated", result.stderr)
            self.assertFalse(Path(command[7]).exists())
        self.deployment["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "SGLANG_MOE_CONFIG_DIR", "value": "/unreviewed-moe"})
        (self.temp / "deployment.json").write_bytes(gemm.canonical(self.deployment))
        command[7] = str(self.temp / "mixed-moe")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("MoE config-directory", result.stderr)
        self.assertFalse(Path(command[7]).exists())
        self.deployment["spec"]["template"]["spec"]["containers"][0]["env"].pop()
        (self.temp / "deployment.json").write_bytes(gemm.canonical(self.deployment))
        workload["cases"][0]["input_ids"][0] = -1
        (self.temp / "workload.json").write_bytes(gemm.canonical(workload))
        command[7] = str(self.temp / "bad-plan")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.temp / "bad-plan").exists())


if __name__ == "__main__":
    unittest.main()
