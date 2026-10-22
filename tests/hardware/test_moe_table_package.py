"""Offline experimental MoE packaging contracts; no Torch, GPU or deployment."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import model_kernels
import gemm_profile

SOURCE = ROOT / "infrastructure/sglang/moe-m1"
TABLE_SHA = "c59324d8bd2254b7171c8a74187a90f13776958ce02e620757be04acd0a5afbf"


class MoETablePackageTests(unittest.TestCase):
    def test_exact_normal_down_layout_and_prefill_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "package"
            record = model_kernels.moe_table_package(output)
            self.assertFalse(record["automatic_apply"])
            self.assertIn("experimental", record["qualification"])
            with tarfile.open(output / "moe-tables.tar") as archive:
                self.assertEqual(len(archive.getmembers()), 6)
                tables = [row for row in archive.getmembers() if row.name.startswith("configs/triton_3_8_0/")]
                self.assertEqual(len(tables), 2)
                self.assertEqual(sum(row.name.endswith("_down.json") for row in tables), 1)
                for row in archive.getmembers():
                    self.assertTrue(row.isfile())
                    self.assertEqual((row.uid, row.gid, row.mtime, row.mode), (0, 0, 0, 0o644))
                    data = archive.extractfile(row).read()
                    self.assertEqual(data, (output / "payload" / row.name).read_bytes())
                    self.assertEqual(hashlib.sha256(data).hexdigest(), record["members_sha256"][row.name])
                for row in tables:
                    data = archive.extractfile(row).read()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), TABLE_SHA)
                    value = json.loads(data)
                    self.assertEqual(value["1"]["BLOCK_SIZE_M"], 32)
                    self.assertEqual(value["2"]["BLOCK_SIZE_M"], 64)
                    self.assertEqual(set(value), {str(2 ** n) for n in range(13)})
                    self.assertTrue(all(row == value["2"] for m, row in value.items() if int(m) >= 2))
            selection = json.loads((output / "payload/SELECT-ENV.json").read_text())
            self.assertEqual(selection["default_selection"], "incumbent")
            self.assertFalse(selection["automatic_apply"])
            self.assertEqual(selection["environment_if_explicitly_selected_and_verified"],
                             {"SGLANG_MOE_CONFIG_DIR": "/work/source/moe-config"})
            self.assertIn("omit SGLANG_MOE_CONFIG_DIR", selection["unsupported_or_stale_fallback"])
            self.assertNotIn("cache_identity_sha256", selection)
            self.assertIn("fresh owner boot/runtime", selection["cache_reuse"])
            profile = json.loads((output / "payload/profile.json").read_text())
            self.assertIn("not independently inspected", profile["precision"]["kv_evidence"])
            dependency = json.loads((output / "payload/gemm-dependency.json").read_text())
            self.assertEqual(len(dependency["model_files"]), 14)
            self.assertEqual(gemm_profile.sha(gemm_profile.canonical(dependency)), record["gemm_dependency_sha256"])

    def test_reproducible_tar_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / "a", Path(temporary) / "b"
            left = model_kernels.moe_table_package(first)
            right = model_kernels.moe_table_package(second)
            self.assertEqual(left, right)
            self.assertEqual((first / "moe-tables.tar").read_bytes(), (second / "moe-tables.tar").read_bytes())
            with self.assertRaisesRegex(ValueError, "already exists"):
                model_kernels.moe_table_package(first)

    def test_changed_tables_profile_and_symlink_refused_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ("normal.json", "down.json", "profile.json"):
                directory = base / name
                shutil.copytree(SOURCE, directory)
                path = directory / name
                path.write_bytes(path.read_bytes() + b" ")
                output = base / (name + "-output")
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    model_kernels.moe_table_package(output, directory)
                self.assertFalse(output.exists())
            directory = base / "symlink"
            shutil.copytree(SOURCE, directory)
            (directory / "down.json").unlink()
            (directory / "down.json").symlink_to(SOURCE / "down.json")
            with self.assertRaisesRegex(ValueError, "not symlinks"):
                model_kernels.moe_table_package(base / "symlink-output", directory)

    def test_stale_gemm_dependency_refused(self):
        changed = copy.deepcopy(gemm_profile.load(gemm_profile.PROFILE_DIR / "profile.json"))
        changed["runtime"]["torch"] = "unreviewed"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            with patch.object(gemm_profile, "bounded_bytes", return_value=json.dumps(changed).encode()):
                with self.assertRaisesRegex(ValueError, "stale or unsupported"):
                    model_kernels.moe_table_package(output)
            self.assertFalse(output.exists())

    def test_public_build_cli_propagates_and_does_not_apply(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "package"
            environment = dict(os.environ, HOME_LAB_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(["bash", str(ROOT / "bin/workstationctl"), "build", "sglang-moe-tables", str(output)],
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["tar_sha256"], model_kernels.file_hash(output / "moe-tables.tar"))
            self.assertFalse(json.loads(result.stdout)["automatic_apply"])
            bad = subprocess.run(["bash", str(ROOT / "bin/workstationctl"), "build", "sglang-moe-tables"],
                                 env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
