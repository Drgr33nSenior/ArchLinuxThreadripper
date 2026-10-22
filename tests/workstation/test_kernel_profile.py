#!/usr/bin/env python3
"""Offline kernel profile and build-closure fixtures."""

import gzip
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))

import kernel_profile


class KernelProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.sys = self.temp / "sys"
        self.proc = self.temp / "proc"
        self.boot = self.temp / "boot"
        self.modules = self.temp / "modules"
        self.policy_path = ROOT / "infrastructure/kernel/threadripper-9960x-dual-r9700.json"
        self.policy = kernel_profile.load_policy(self.policy_path)
        for path in (self.sys / "class/dmi/id", self.sys / "bus/pci/devices", self.sys / "bus/pci/drivers",
                     self.sys / "module", self.proc, self.boot, self.modules / "fixture/build"):
            path.mkdir(parents=True, exist_ok=True)
        for name, value in self.policy["platform"].items():
            (self.sys / "class/dmi/id" / name).write_text(value + "\n")
        cpu = self.policy["cpu"]
        blocks = []
        for index in range(cpu["logical_cpus"]):
            blocks.append("\n".join((f"processor : {index}", f"vendor_id : {cpu['vendor_id']}",
                                     f"cpu family : {cpu['family']}", f"model : {cpu['model']}",
                                     f"stepping : {cpu['stepping']}", f"model name : {cpu['model_name']}")))
        (self.proc / "cpuinfo").write_text("\n\n".join(blocks) + "\n")
        (self.proc / "modules").write_text("amdgpu 1 0 - Live 0x0\nnvme 1 0 - Live 0x0\n")
        self._make_pci()
        config = "\n".join(f"{name}={accepted[0]}" for name, accepted in sorted(self.policy["required_config"].items())) + "\n"
        with gzip.open(self.proc / "config.gz", "wb") as stream:
            stream.write(config.encode())

    def _make_pci(self):
        index = 0
        for requirement in self.policy["pci"]:
            for _ in range(requirement["count"]):
                path = self.sys / "bus/pci/devices" / f"0000:{index:02x}:00.0"
                path.mkdir()
                (path / "vendor").write_text(requirement["vendor"] + "\n")
                (path / "device").write_text(requirement["device"] + "\n")
                (path / "class").write_text(requirement["class_prefix"] + "00\n")
                if not requirement.get("allow_unbound"):
                    driver_name = requirement["drivers"][0]
                    driver = self.sys / "bus/pci/drivers" / driver_name
                    driver.mkdir(exist_ok=True)
                    if requirement["modules"][0] is not None:
                        module_name = requirement["modules"][0]
                        module = self.sys / "module" / module_name
                        module.mkdir(exist_ok=True)
                        link = driver / "module"
                        if not link.exists():
                            link.symlink_to(module)
                    (path / "driver").symlink_to(driver)
                index += 1

    def capture(self, name="profile"):
        output = self.temp / name
        result = kernel_profile.capture(self.policy_path, output, sys_root=self.sys, proc_root=self.proc,
                                        boot_root=self.boot, modules_root=self.modules, release="fixture")
        return output, result

    def test_exact_hardware_profile_and_build_closure(self):
        output, result = self.capture()
        self.assertEqual("observed", result["status"])
        self.assertEqual(self.policy["required_modules"], result["required_modules"])
        self.assertNotIn("serial", json.dumps(result).lower())
        kernel_profile.validate_profile(self.policy_path, output)
        inventory = self.temp / "module-inventory.txt"
        inventory.write_text("\n".join(self.policy["required_modules"]) + "\n")
        validation = self.temp / "validation.json"
        checked = kernel_profile.validate_build(self.policy_path, output, output / "current.config", inventory, validation)
        self.assertEqual("pass", checked["status"])
        self.assertEqual([], checked["missing_modules"])

        wifi = next(item for item in self.policy["pci"] if item["name"] == "wifi")
        self.assertEqual(["ath12k_wifi7_pci"], wifi["drivers"])
        self.assertEqual(["ath12k"], wifi["modules"])
        self.assertIn("ath12k", self.policy["required_modules"])
        self.assertIn("ath12k_wifi7", self.policy["required_modules"])

    def test_identity_and_driver_mismatches_fail_closed(self):
        cases = []
        board = self.sys / "class/dmi/id/board_name"
        board.write_text("different\n")
        cases.append(self.capture("wrong-board")[1])
        board.write_text(self.policy["platform"]["board_name"] + "\n")
        gpu = self.sys / "bus/pci/devices/0000:00:00.0"
        gpu.joinpath("driver").unlink()
        cases.append(self.capture("unbound-gpu")[1])
        gpu.joinpath("driver").symlink_to(self.sys / "bus/pci/drivers/amdgpu")
        for result in cases:
            self.assertEqual("failed", result["status"])
            self.assertTrue(result["failures"])

    def test_required_workload_features_cannot_be_omitted(self):
        output, _ = self.capture("workload-features")
        good = kernel_profile.parse_config((output / "current.config").read_text())
        # Independent regression list: generic HSA/P2PDMA is not sufficient.
        required = ("CONFIG_HSA_AMD_P2P", "CONFIG_PCI_P2PDMA", "CONFIG_HSA_AMD_SVM",
                    "CONFIG_VXLAN", "CONFIG_NF_CONNTRACK", "CONFIG_CFS_BANDWIDTH",
                    "CONFIG_SECCOMP_FILTER", "CONFIG_DEBUG_INFO_BTF", "CONFIG_TCG_TPM",
                    "CONFIG_INPUT_UINPUT", "CONFIG_IA32_EMULATION", "CONFIG_NTSYNC")
        for name in required:
            with self.subTest(name=name):
                self.assertIn(name, self.policy["required_config"])
                config = dict(good)
                config.pop(name)
                self.assertTrue(any(name in error for error in kernel_profile.check_config(self.policy, config)))

    def test_only_explicit_other_gpu_families_are_pruned(self):
        removed = {name for name, values in self.policy["required_config"].items() if values == ["n"]}
        self.assertEqual({"CONFIG_DRM_I915", "CONFIG_DRM_XE", "CONFIG_DRM_NOUVEAU", "CONFIG_DRM_RADEON"}, removed)
        output, _ = self.capture("pruned")
        config = kernel_profile.parse_config((output / "current.config").read_text())
        self.assertEqual([], kernel_profile.check_config(self.policy, config))
        for name in removed:
            changed = dict(config, **{name: "m"})
            self.assertTrue(any(name in error for error in kernel_profile.check_config(self.policy, changed)))
        self.assertEqual(["m"], self.policy["required_config"]["CONFIG_DRM_AMDGPU"])
        self.assertEqual(["y"], self.policy["required_config"]["CONFIG_CPU_MITIGATIONS"])

    def test_observed_extra_module_is_part_of_build_closure(self):
        (self.proc / "modules").write_text("amdgpu 1 0 - Live 0x0\nusbnet 1 0 - Live 0x0\n")
        output, report = self.capture("extra-loaded")
        self.assertIn("usbnet", report["required_modules"])
        kernel_profile.validate_profile(self.policy_path, output)
        inventory = self.temp / "without-usbnet.txt"
        inventory.write_text("\n".join(self.policy["required_modules"]) + "\n")
        with self.assertRaisesRegex(ValueError, "does not satisfy"):
            kernel_profile.validate_build(self.policy_path, output, output / "current.config", inventory,
                                          self.temp / "missing-observed.json")

    def test_mixed_case_netfilter_modules_preserve_identity(self):
        (self.proc / "modules").write_text(
            "xt_NFLOG 1 0 - Live 0x0\nipt_REJECT 1 0 - Live 0x0\n"
            "xt_MASQUERADE 1 0 - Live 0x0\namdgpu 1 0 - Live 0x0\n")
        output, result = self.capture("netfilter")
        self.assertEqual("observed", result["status"])
        self.assertEqual(["amdgpu", "ipt_REJECT", "xt_MASQUERADE", "xt_NFLOG"],
                         result["loaded_modules"])
        kernel_profile.validate_profile(self.policy_path, output)
        inventory = self.temp / "modules-mixed.txt"
        inventory.write_text("amdgpu\nxt_NFLOG\n")
        self.assertEqual({"amdgpu", "xt_NFLOG"}, kernel_profile.module_inventory(inventory))

    def test_invalid_module_path_characters_remain_rejected(self):
        for index, invalid in enumerate(("../amdgpu", "xt/NFLOG", "xt;NFLOG", "xt$NFLOG")):
            (self.proc / "modules").write_text(f"{invalid} 1 0 - Live 0x0\n")
            _, result = self.capture(f"bad-module-{index}")
            self.assertEqual("failed", result["status"])
            self.assertIn("invalid loaded module inventory", result["failures"])

    def test_generic_current_config_records_candidate_gaps(self):
        with gzip.open(self.proc / "config.gz", "wb") as stream:
            stream.write(b"CONFIG_X86_64=y\n")
        output, result = self.capture("generic-config")
        self.assertEqual("observed", result["status"])
        self.assertEqual([], result["failures"])
        self.assertIn("CONFIG_X86_NATIVE_CPU=n (requires y)", result["candidate_config_failures"])
        kernel_profile.validate_profile(self.policy_path, output)

        with gzip.open(self.proc / "config.gz", "wb") as stream:
            stream.write(b"not a kernel configuration\n")
        _, malformed = self.capture("malformed-config")
        self.assertEqual("failed", malformed["status"])
        self.assertTrue(malformed["failures"])

    def test_generic_sata_and_xhci_module_bindings_are_accepted(self):
        for driver, module_name in (("ahci", "ahci"), ("xhci_hcd", "xhci_pci")):
            module = self.sys / "module" / module_name
            module.mkdir()
            (self.sys / "bus/pci/drivers" / driver / "module").symlink_to(module)
        _, result = self.capture("generic-bindings")
        self.assertEqual("observed", result["status"])
        self.assertEqual([], result["failures"])
        self.assertIn("ahci", result["required_modules"])
        self.assertIn("xhci_pci", result["required_modules"])

    def test_profile_and_module_tampering_is_rejected(self):
        output, _ = self.capture()
        profile = output / "profile.json"
        raw = json.loads(profile.read_text())
        raw["required_modules"] = raw["required_modules"][:-1]
        profile.write_text(json.dumps(raw) + "\n")
        with self.assertRaisesRegex(ValueError, "manifest"):
            kernel_profile.validate_profile(self.policy_path, output)

        output, _ = self.capture("fresh")
        inventory = self.temp / "incomplete-modules.txt"
        inventory.write_text("amdgpu\n")
        with self.assertRaisesRegex(ValueError, "does not satisfy"):
            kernel_profile.validate_build(self.policy_path, output, output / "current.config", inventory,
                                          self.temp / "failed-validation.json")
        failed = json.loads((self.temp / "failed-validation.json").read_text())
        self.assertIn("ath12k_wifi7", failed["missing_modules"])

        output, _ = self.capture("edited-hardware")
        profile = output / "profile.json"
        raw = json.loads(profile.read_text())
        raw["platform"]["board_name"] = "different"
        profile.write_text(json.dumps(raw, sort_keys=True, indent=2) + "\n")
        (output / "MANIFEST.json").write_text(json.dumps({
            "current.config": kernel_profile.file_digest(output / "current.config"),
            "profile.json": kernel_profile.file_digest(profile),
        }, sort_keys=True, indent=2) + "\n")
        with self.assertRaisesRegex(ValueError, "hardware identity"):
            kernel_profile.validate_profile(self.policy_path, output)

        output, _ = self.capture("edited-config-gaps")
        profile = output / "profile.json"
        raw = json.loads(profile.read_text())
        raw["candidate_config_failures"] = ["invented"]
        profile.write_text(json.dumps(raw, sort_keys=True, indent=2) + "\n")
        (output / "MANIFEST.json").write_text(json.dumps({
            "current.config": kernel_profile.file_digest(output / "current.config"),
            "profile.json": kernel_profile.file_digest(profile),
        }, sort_keys=True, indent=2) + "\n")
        with self.assertRaisesRegex(ValueError, "gap evidence"):
            kernel_profile.validate_profile(self.policy_path, output)

    def test_profile_paths_and_config_duplicates_are_rejected(self):
        output, _ = self.capture()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.capture()
        manifest = output / "MANIFEST.json"
        manifest.unlink()
        manifest.symlink_to(output / "profile.json")
        with self.assertRaisesRegex(ValueError, "regular file"):
            kernel_profile.validate_profile(self.policy_path, output)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            kernel_profile.parse_config("CONFIG_SMP=y\nCONFIG_SMP=y\n")

    def test_malformed_policy_is_rejected(self):
        malformed = json.loads(self.policy_path.read_text())
        malformed["pci"][0]["modules"] = ["missing_from_required_inventory"]
        path = self.temp / "malformed-policy.json"
        path.write_text(json.dumps(malformed) + "\n")
        with self.assertRaisesRegex(ValueError, "missing from"):
            kernel_profile.load_policy(path)


if __name__ == "__main__":
    unittest.main()
