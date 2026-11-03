"""Stdlib-only tests of stock verify-stage metadata and the native CPU envelope."""
import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_verify as compiler
import verify_dispatch as dispatch


class VerifyCompileTests(unittest.TestCase):
    def source(self):
        return (compiler.SOURCE_ROOT / "native-attention/kernels/ops/attention/decode_attention.py").read_bytes()

    def test_actual_host_dispatch_captures_every_frozen_case(self):
        rows = dispatch.capture_cases(self.source())
        for name, prefixes in dispatch.CASES:
            row = rows[name]
            self.assertEqual(row["launch"]["grid"], [8 * len(prefixes), 2, 16])
            args, kwargs = row["launch"]["args"], row["launch"]["kwargs"]
            self.assertEqual(args[3], 0.0625)
            self.assertEqual(args[5]["dtype"], "int64")
            self.assertEqual(args[6]["shape"], [8 * len(prefixes), 12, 16, 256])
            self.assertEqual(args[9:13], [3072, 256, 512, 256])
            self.assertEqual(kwargs["BLOCK_N"], 32)
            self.assertEqual(kwargs["BLOCK_H"], 16)
            self.assertEqual(kwargs["num_stages"], 1)
            self.assertEqual(kwargs["num_warps"], 4)
            self.assertFalse(kwargs["HAS_MLA"] or kwargs["USE_FORCED"] or kwargs["IS_GFX1250"])
            self.assertFalse(row["split_values_observed"])

    def test_source_and_case_changes_refuse(self):
        for raw in (self.source() + b"\n", self.source().replace(b"BLOCK = 32", b"BLOCK = 64")):
            with self.assertRaises(ValueError):
                dispatch.capture(raw, (119990,))
        for prefixes in ((65536,), (119990, 119990), (0,)):
            with self.assertRaises(ValueError):
                dispatch.capture(self.source(), prefixes)

    def test_public_wrapper_reaches_both_frozen_stages(self):
        for _, prefixes in dispatch.CASES:
            rows = [dispatch.capture_full(self.source(), prefixes, card) for card in (0, 1)]
            self.assertEqual(rows[0], rows[1])
            row = rows[0]
            self.assertFalse(row["physical_split_values_observed"])
            self.assertEqual(row["stage1"], dispatch.capture(self.source(), prefixes)["launch"])
            self.assertEqual(row["stage2"]["grid"], [8 * len(prefixes), 12])
            self.assertEqual(row["stage2"]["args"][3], 1.0)
            self.assertEqual(row["stage2"]["kwargs"]["MAX_KV_SPLITS"], 16)
            self.assertEqual(row["stage2"]["kwargs"]["FORCED_KV_SPLITS"], 0)
            self.assertFalse(row["stage2"]["kwargs"]["HAS_SINK"] or row["stage2"]["kwargs"]["USE_PDL"])

    def test_order_is_not_misrepresented_as_a_specialization(self):
        rows = dispatch.capture_cases(self.source())
        self.assertEqual(rows["p119990-rows8-sequential"]["launch"], rows["p119990-rows8-even-odd"]["launch"])
        self.assertFalse(rows["p119990-rows8-even-odd"]["metadata_values_observed"])

    def test_original_source_nodes_and_safe_compile_call_are_used(self):
        raw = (HERE / "compile_verify_target.py").read_text()
        tree = ast.parse(raw)
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertIn("triton.compile", calls)
        self.assertIn("fn._pack_args", calls)
        self.assertNotIn("fn.run", calls)
        self.assertNotIn("torch.empty", calls)
        self.assertIn("compiled.module is None", raw)

    def test_prefetch_changes_only_the_grouped_kernel_and_masks_the_next_load(self):
        before, after = self.source(), dispatch.derive_prefetch(self.source())
        self.assertNotEqual(before, after)
        a, b = ast.parse(before), ast.parse(after)
        before_functions = {n.name: ast.dump(n) for n in a.body if isinstance(n, ast.FunctionDef)}
        after_functions = {n.name: ast.dump(n) for n in b.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual([name for name in before_functions if before_functions[name] != after_functions[name]],
                         ["_fwd_grouped_kernel_stage1"])
        self.assertIn(b"mask=next_n < split_kv_end", after)
        self.assertIn(b"kv_loc = next_kv_loc", after)
        self.assertIn(b"mask=first_n < split_kv_end", after)
        with self.assertRaises(ValueError):
            dispatch.derive_prefetch(after)

    def test_loop_carried_gather_preserves_the_consumed_index_blocks(self):
        # The final speculative block is all masked; it is never consumed.
        for length in (1, 31, 32, 33, 724, 7500, 7501):
            blocks = [list(range(start, min(start + 32, length))) for start in range(0, length, 32)]
            carried = list(range(min(32, length)))
            observed = []
            for start in range(0, length, 32):
                next_block = list(range(start + 32, min(start + 64, length)))
                observed.append(carried)
                carried = next_block
            self.assertEqual(observed, blocks)
            self.assertEqual(carried, [])

    def test_preparation_retains_exact_sandbox_and_guard(self):
        with tempfile.TemporaryDirectory(prefix="verify-census-test-") as folder:
            root = Path(folder).resolve() / "census"
            result = compiler.prepare(root)
            inputs = json.loads((compiler.compile_gemm.I / "COMPONENT-COMPILE-INPUTS-v1.json").read_bytes())
            old = ast.parse(Path(inputs["recipe"]).read_bytes())
            new = ast.parse((root / "RECIPE.py").read_bytes())
            self.assertEqual(compiler.existing._literal(old, "argv"), compiler.existing._literal(new, "argv"))
            prefix = compiler.existing._literal(old, "source").decode().split(Path(inputs["compiler"]).read_text())[0]
            self.assertTrue((root / "TARGET.py").read_text().startswith(prefix))
            self.assertFalse((root / "INVOKED.json").exists())
            self.assertFalse(result["gpu_launch_authorized"])
        self.assertFalse(any(x in sys.modules for x in ("torch", "triton", "sglang", "numpy")))


if __name__ == "__main__":
    unittest.main()
