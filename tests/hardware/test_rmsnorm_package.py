"""Offline guarded RMSNorm packaging; no Torch or GPU execution implied."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import model_kernels
import gemm_profile

DIRECTORY = ROOT / "infrastructure/sglang/rmsnorm-no-residual"


class RMSNormPackageTests(unittest.TestCase):
    def test_reproducible_package_and_fresh_output(self):
        # Bind an explicit synthetic source preimage in this fixture only.
        source = ("class RMSNorm(BaseFusedOp):\n"
                  "    def forward_hip(self, x, residual=None, post_residual_addition=None, quant_linear=None):\n"
                  "        # Fallback to native implementation if vllm is not available\n"
                  "        return self.forward_native(x, residual, post_residual_addition)\n").encode()
        profile = json.loads((DIRECTORY / "profile.json").read_text())
        profile["source_sha256"] = hashlib.sha256(source).hexdigest()
        original_load = model_kernels.load
        def load(path):
            return copy.deepcopy(profile) if Path(path) == DIRECTORY / "profile.json" else original_load(path)
        with tempfile.TemporaryDirectory() as directory, patch.object(model_kernels, "load", side_effect=load):
            root = Path(directory)
            path = root / "source.py"
            path.write_bytes(source)
            first = model_kernels.rmsnorm_package(root / "a", path)
            second = model_kernels.rmsnorm_package(root / "b", path)
            self.assertEqual(first, second)
            self.assertEqual((root / "a/rmsnorm-source.tar").read_bytes(), (root / "b/rmsnorm-source.tar").read_bytes())
            self.assertFalse(first["automatic_apply"])
            rewritten = (root / "a/payload/layernorm.py").read_text()
            ast.parse(rewritten)
            self.assertEqual(1, rewritten.count("return F.rms_norm(x.contiguous()"))
            with tarfile.open(root / "a/rmsnorm-source.tar") as archive:
                self.assertEqual(3, len(archive.getmembers()))
                for row in archive.getmembers():
                    self.assertTrue(row.isfile())
                    self.assertEqual((0, 0, 0, 0o644), (row.uid, row.gid, row.mtime, row.mode))
            with self.assertRaisesRegex(ValueError, "already exists"):
                model_kernels.rmsnorm_package(root / "a", path)
            path.write_bytes(source + b"# stale\n")
            with self.assertRaisesRegex(ValueError, "preimage changed"):
                model_kernels.rmsnorm_package(root / "stale", path)
            self.assertFalse((root / "stale").exists())
            link = root / "symlink.py"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "regular file"):
                model_kernels.rmsnorm_package(root / "link", link)

    def test_guard_refuses_unsupported_semantics(self):
        class RMSNorm:
            pass
        class Tensor:
            dtype = "bf16"
            device = "cuda:0"
            is_cuda = True
            shape = (1, 2048)
            def dim(self): return len(self.shape)
            def numel(self):
                result = 1
                for value in self.shape: result *= value
                return result
            def is_contiguous(self): return True
        weight = Tensor()
        weight.shape = (2048,)
        layer = RMSNorm()
        layer.weight = weight
        layer.hidden_size = 2048
        layer.variance_epsilon = 1e-6
        layer.cast_x_before_out_mul = layer.fp32_residual = False
        layer.override_orig_dtype = layer.variance_size_override = None
        x = Tensor()
        namespace = dict(RMSNorm=RMSNorm, torch=SimpleNamespace(bfloat16="bf16", is_grad_enabled=lambda: False),
                         _workstation_no_residual_rms_enabled=True, _is_hip=True, _use_aiter=False,
                         _has_vllm_rms_norm=False, is_batch_invariant_mode_enabled=lambda: False)
        exec(compile((DIRECTORY / "guard.py").read_text(), "guard.py", "exec"), namespace)
        guard = namespace["_workstation_no_residual_rms_supported"]
        self.assertTrue(guard(layer, x, None, None, None))
        for arguments in ((object(), None, None), (None, object(), None), (None, None, object())):
            self.assertFalse(guard(layer, x, *arguments))
        for attribute, value in (("cast_x_before_out_mul", True), ("fp32_residual", True),
                                 ("override_orig_dtype", "fp32"), ("variance_size_override", 1024),
                                 ("x_pad_to_multiple", 128), ("variance_epsilon", 1e-5), ("hidden_size", 129)):
            candidate = copy.copy(layer)
            setattr(candidate, attribute, value)
            self.assertFalse(guard(candidate, x, None, None, None), attribute)
        for attribute, value in (("dtype", "fp32"), ("device", "cuda:1"), ("shape", (2047,))):
            candidate = copy.copy(layer)
            candidate.weight = copy.copy(weight)
            setattr(candidate.weight, attribute, value)
            self.assertFalse(guard(candidate, x, None, None, None), attribute)
        for shape in ((0, 2048), (513, 2048), (1, 2049), (2048,), (1, 1, 1, 2048)):
            candidate = Tensor()
            candidate.shape = shape
            self.assertFalse(guard(layer, candidate, None, None, None), shape)
        for key, value in (("_workstation_no_residual_rms_enabled", False), ("_is_hip", False),
                           ("_use_aiter", True), ("_has_vllm_rms_norm", True),
                           ("is_batch_invariant_mode_enabled", lambda: True)):
            prior = namespace[key]
            namespace[key] = value
            self.assertFalse(guard(layer, x, None, None, None), key)
            namespace[key] = prior
        namespace["torch"].is_grad_enabled = lambda: True
        self.assertFalse(guard(layer, x, None, None, None))

    def test_stale_profile_and_guard_refused_before_output(self):
        original = gemm_profile.bounded_bytes
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package"
            source = Path(directory) / "source.py"
            source.write_bytes(b"explicit source identity fixture\n")
            profile = json.loads((DIRECTORY / "profile.json").read_text())
            profile["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            original_load = model_kernels.load
            def load(path):
                return copy.deepcopy(profile) if Path(path) == DIRECTORY / "profile.json" else original_load(path)
            for name in ("profile.json", "guard.py"):
                def read(path, *args):
                    raw = original(path, *args)
                    return raw + b" " if Path(path) == DIRECTORY / name else raw
                expected = "profile changed" if name == "profile.json" else "semantic guard changed"
                with patch.object(model_kernels, "load", side_effect=load), \
                     patch.object(gemm_profile, "bounded_bytes", side_effect=read), self.assertRaisesRegex(ValueError, expected):
                    model_kernels.rmsnorm_package(output, source)
                self.assertFalse(output.exists())

    def test_epilogue_selection_is_reproducible_and_does_not_change_old_profile(self):
        source = ("class RMSNorm(BaseFusedOp):\n"
                  "    def forward_hip(self, x, residual=None, post_residual_addition=None, quant_linear=None):\n"
                  "        # Fallback to native implementation if vllm is not available\n"
                  "        return self.forward_native(x, residual, post_residual_addition)\n").encode()
        directory = DIRECTORY.parent / "rmsnorm-epilogue"
        profile = json.loads((directory / "profile.json").read_text())
        profile["source_sha256"] = hashlib.sha256(source).hexdigest()
        original_load = model_kernels.load
        def load(path):
            return copy.deepcopy(profile) if Path(path) == directory / "profile.json" else original_load(path)
        with tempfile.TemporaryDirectory() as temporary, patch.object(model_kernels, "load", side_effect=load):
            root = Path(temporary)
            path = root / "source.py"
            path.write_bytes(source)
            a = model_kernels.rmsnorm_package(root / "a", path, "rmsnorm-epilogue")
            b = model_kernels.rmsnorm_package(root / "b", path, "rmsnorm-epilogue")
            self.assertEqual(a, b)
            self.assertEqual((root / "a/rmsnorm-source.tar").read_bytes(), (root / "b/rmsnorm-source.tar").read_bytes())
            text = (root / "a/payload/layernorm.py").read_text()
            ast.parse(text)
            self.assertEqual(text.count("return _workstation_rms_epilogue(x, self.weight.data)"), 1)
            self.assertIn('get_bool_env_var("WORKSTATION_RMSNORM_EPILOGUE")', text)
            self.assertNotIn('get_bool_env_var("WORKSTATION_RMSNORM_NO_RESIDUAL")', text)
            self.assertNotIn("return F.rms_norm", text)
            self.assertIn("enable_fp_fusion=False", text)
            self.assertIn("scale = torch.rsqrt(variance + 1e-6)", text)
            self.assertEqual(json.loads((root / "a/payload/profile.json").read_text())["environment"],
                             {"WORKSTATION_RMSNORM_EPILOGUE": "1"})
            with self.assertRaisesRegex(ValueError, "unsupported RMSNorm profile"):
                model_kernels.rmsnorm_package(root / "unknown", path, "../arbitrary")
            self.assertFalse((root / "unknown").exists())
            original = gemm_profile.bounded_bytes
            for name, message in (("kernel.py", "kernel changed"), ("profile.json", "profile changed")):
                def stale(file, *args):
                    raw = original(file, *args)
                    return raw + b" " if Path(file) == directory / name else raw
                with patch.object(gemm_profile, "bounded_bytes", side_effect=stale), self.assertRaisesRegex(ValueError, message):
                    model_kernels.rmsnorm_package(root / "stale", path, "rmsnorm-epilogue")
                self.assertFalse((root / "stale").exists())


if __name__ == "__main__":
    unittest.main()
