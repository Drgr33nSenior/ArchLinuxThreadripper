"""Local stdlib checks for the closed N32 experiment; never invoke native work."""
import ast
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_gemm as compiler
import gemm_dispatch as dispatch

REVIEW = compiler.R2 / "phase2-128k-c4ab-ff396f8f-preparation-v2/author/package-v2/review"


class GemmCompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = (REVIEW / "ops.py").read_bytes()
        cls.candidate = dispatch.derive(cls.baseline)

    def capture(self, arm, m, n=8240, k=5120):
        return dispatch.capture(self.baseline, self.candidate, arm=arm, m=m, n=n, k=k, card=0)

    def test_declared_cases_capture_real_host_dispatch_and_abi(self):
        captured = dispatch.capture_cases(self.baseline, self.candidate)
        self.assertEqual(len(captured), 10)
        self.assertEqual(captured["qkvz_ba-m8"]["baseline"]["grid"], [129])
        self.assertEqual(captured["qkvz_ba-m8"]["candidate"]["grid"], [258])
        for name, m, n, k in dispatch.CASES:
            for arm, bn in (("baseline", 64), ("candidate", 32)):
                row = captured[name][arm]
                self.assertEqual(row["grid"], [(n + bn - 1) // bn])
                self.assertEqual(row["args"][5:8], [m, n, k // 2])
                self.assertEqual(row["args"][3], dict(dtype="uint8", shape=[m, k // 32], stride=[1, m]))
                self.assertEqual(row["kwargs"]["BLOCK_SIZE_N"], bn)
                self.assertEqual(row["kwargs"]["num_warps"], 4)
                self.assertEqual(row["kwargs"]["SPLITK_BLOCK_SIZE"], k)

    def test_unchanged_large_row_dispatch_and_small_row_scope(self):
        for m in (1, 2, 7, 8, 15, 16, 17, 1023, 1024, 2048):
            b, c = self.capture("baseline", m), self.capture("candidate", m)
            if m > 16:
                self.assertEqual(b, c)
            else:
                self.assertEqual(c["kwargs"], dict(b["kwargs"], BLOCK_SIZE_N=32))
                self.assertEqual(c["args"], b["args"])

    def test_scope_refuses_any_extra_source_change(self):
        for extra in (b"\nimport os\n", b"\nCONFIG['num_stages']=2\n", b"\n# extra edit\n"):
            with self.subTest(extra), self.assertRaises(ValueError):
                dispatch.validate(self.baseline, self.candidate + extra)
        with self.assertRaises(ValueError):
            dispatch.derive(self.baseline + b"\n")
        with self.assertRaises(ValueError):
            dispatch.validate(self.baseline, self.candidate.replace(b"(16, 32, 4)", b"(16, 32, 8)"))

    def test_original_shape_and_identity_refusals_remain(self):
        for values in ((0, 8240, 5120), (2049, 8240, 5120), (8, 8241, 5120), (True, 8240, 5120)):
            with self.subTest(values), self.assertRaises(ValueError):
                self.capture("candidate", *values)
        with self.assertRaises(ValueError):
            dispatch.capture(self.baseline, self.candidate, arm="candidate", m=8, n=8240, k=5120, card=True)

    def test_historical_gpu_pins_cover_every_case_and_both_row_specializations(self):
        pins = compiler.gpu_pins()
        self.assertEqual(set(pins), {r[0] for r in dispatch.CASES})
        self.assertEqual(pins["gate_up-m8"]["hsaco"], "98410c916be3f38162c4298e6e5266165ca92b90a7ee87dee2d90abc6a833b26")
        self.assertEqual(pins["gate_up-m8"], pins["gate_up-m16"])
        self.assertEqual(pins["qkvz_ba-m8"], pins["gate_up-m8"])

    def test_isa_resource_extent_and_workgroup_refusals(self):
        values = dict(zip(compiler.FIELDS, (0, 0, 80, 0, 128, 0, 32, 128)))
        raw = lambda x: "\n".join(f".{k}: {v}" for k, v in x.items()).encode()
        self.assertEqual(compiler.inspect_isa(raw(values)), values)
        for key, value in (("wavefront_size", 64), ("max_flat_workgroup_size", 256), ("group_segment_fixed_size", 65537)):
            with self.subTest(key), self.assertRaises(ValueError):
                compiler.inspect_isa(raw(dict(values, **{key: value})))
        with self.assertRaises(ValueError):
            compiler.inspect_isa(raw(values) + b"\n.vgpr_count: 128")

    def test_native_target_has_no_launch_or_driver_initialization(self):
        raw = (HERE / "compile_gemm_target.py").read_text()
        tree = ast.parse(raw)
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertNotIn("fn.run", calls)
        self.assertNotIn("fn.warmup", calls)
        self.assertNotIn("torch.empty", calls)
        self.assertIn("triton.compile", calls)
        self.assertIn("create_function_from_signature", calls)
        self.assertIn("fn._pack_args", calls)
        self.assertIn("compiled.module is None", raw)

    def test_prepare_preserves_sandbox_and_cpu_guard(self):
        with tempfile.TemporaryDirectory(prefix="gemm-prepare-test-") as folder:
            root = Path(folder).resolve() / "compile"
            result = compiler.prepare(root)
            donor = json.loads((compiler.I / "COMPONENT-COMPILE-INPUTS-v1.json").read_bytes())
            old = ast.parse(Path(donor["recipe"]).read_bytes())
            new = ast.parse((root / "RECIPE.py").read_bytes())
            self.assertEqual(compiler.existing._literal(old, "argv"), compiler.existing._literal(new, "argv"))
            inner = compiler.existing._literal(old, "source").decode()
            prefix = inner.split(Path(donor["compiler"]).read_text())[0]
            self.assertTrue((root / "TARGET.py").read_text().startswith(prefix))
            self.assertFalse((root / "INVOKED.json").exists())
            self.assertEqual(result["work_seconds"], 240)
            self.assertEqual(result["scope"]["affected_rows"], "1..16")
            self.assertFalse(result["gpu_launch_authorized"])
            with self.assertRaises(ValueError):
                compiler.prepare(root)
        self.assertFalse(any(x in sys.modules for x in ("torch", "triton", "sglang", "numpy")))


if __name__ == "__main__":
    unittest.main()
