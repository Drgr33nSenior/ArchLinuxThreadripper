"""CPU-only compiler bridge tests; native execution is never called here."""

import ast
import base64
import copy
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_component as compiler
import component_dispatch as dispatch
import component_scope

R2 = HERE.parents[2] / "scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2"
DONOR = R2 / "phase2-attn-c4-accumulator-compile-v1"
SOURCE = R2 / "phase2-attn-c4-accumulator-preparation-v2/source"


def inputs():
    result = dict(recipe=str(DONOR / "native-compile-v3/RECIPE.py"), compiler=str(DONOR / "compile_cpu.py"),
                  launch=str(DONOR / "LAUNCH.json"), fixture=str(SOURCE / "fake_source.py"),
                  gate=str(SOURCE / "compiler_gate.py"), support=str(SOURCE / "support.py"),
                  baseline=str(SOURCE / "extend_attention_fwd_baseline.py"),
                  candidate=str(SOURCE / "extend_attention_fwd_candidate.py"),
                  candidate_sha256="e945ad72296777556ed93859263442c077e0f1d1d7082268c1f669e157a324e9")
    for name in ("connection", "commands"):
        result[name] = str(R2 / "hyperloom-sglang-implementation-20261102-v1/native-metadata" /
                           ("connection.py" if name == "connection" else "local_commands.py"))
    return result


class CompileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="component-bridge-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs = inputs()

    def fixture(self):
        return compiler.owner._source(Path(self.inputs["fixture"]), compiler.PINS["fixture"], "component_test_fixture")

    def capture(self, raw, role="candidate", name="capture"):
        return dispatch.capture(self.fixture(), raw, role, json.loads(Path(self.inputs["launch"]).read_bytes()),
                                root=self.root / name)

    def test_retained_both_arms_match_real_frozen_dispatch(self):
        for arm in ("baseline", "candidate"):
            raw = Path(self.inputs[arm]).read_text()
            captured = self.capture(raw, arm, arm)
            self.assertEqual(captured["launch"], json.loads(Path(self.inputs["launch"]).read_bytes()))
            self.assertEqual([(r["card"], r["prefix"]) for r in captured["witnesses"]],
                             [(0, 0), (0, 16384), (1, 0), (1, 16384)])

    def test_index_width_comes_from_the_host_wrapper(self):
        raw = Path(self.inputs["candidate"]).read_text()
        old = "    sm_scale = sm_scale or 1.0 / (Lq**0.5)"
        changed = raw.replace(old, "    kv_indices = kv_indices.to(torch.int32)\n" + old)
        self.assertNotEqual(raw, changed)
        captured = self.capture(changed)
        self.assertEqual(captured["launch"]["args"][9]["dtype"], "int32")
        with self.assertRaisesRegex(ValueError, "index pointer"):
            self.capture(changed, "baseline", "baseline")

    def test_geometry_abi_and_prefix_dependent_dispatch_are_refused(self):
        raw = Path(self.inputs["candidate"]).read_text()
        old = "    sm_scale = sm_scale or 1.0 / (Lq**0.5)"
        for index, line in enumerate(("    k_scale = True\n", "    v_scale = 2.0\n",
                                      "    if kv_indices.numel(): kv_indices = kv_indices.to(torch.int32)\n")):
            with self.subTest(index), self.assertRaises(ValueError):
                self.capture(raw.replace(old, line + old), name="bad" + str(index))

    def test_prepare_reuses_sandbox_prefix_and_compiler_function_exactly(self):
        result = compiler.prepare(self.inputs, self.root / "prepared")
        tree = ast.parse((self.root / "prepared/RECIPE.py").read_bytes())
        donor = ast.parse(Path(self.inputs["recipe"]).read_bytes())
        self.assertEqual(compiler._literal(tree, "argv"), compiler._literal(donor, "argv"))
        inner = compiler._literal(tree, "source").decode()
        prefix = compiler._literal(donor, "source").decode().split(Path(self.inputs["compiler"]).read_text())[0]
        self.assertTrue(inner.startswith(prefix))
        original_function = next(n for n in ast.parse(Path(self.inputs["compiler"]).read_bytes()).body
                                 if isinstance(n, ast.FunctionDef) and n.name == "compile_arm")
        derived_function = next(n for n in ast.parse(inner).body if isinstance(n, ast.FunctionDef) and n.name == "compile_arm")
        self.assertEqual(ast.dump(original_function), ast.dump(derived_function))
        self.assertEqual(result["work_seconds"], 240)
        self.assertEqual(result["changed_functions"], ["_fwd_kernel"])
        self.assertFalse((self.root / "prepared/INVOKED.json").exists())
        self.assertFalse(any(name in sys.modules for name in ("torch", "triton", "sglang", "numpy")))

    def test_source_pin_scope_and_destination_are_checked_before_preparation(self):
        for mode in ("pin", "scope"):
            bad = copy.deepcopy(self.inputs)
            if mode == "pin":
                bad["candidate_sha256"] = "0" * 64
            else:
                path = self.root / "outside.py"
                path.write_text(Path(bad["candidate"]).read_text() + "\nimport os\n")
                bad.update(candidate=str(path), candidate_sha256=compiler.digest(path.read_bytes()))
            with self.subTest(mode), self.assertRaises(ValueError):
                compiler.prepare(bad, self.root / "prepared")
            self.assertFalse((self.root / "prepared").exists())
        with self.assertRaises(ValueError):
            compiler.prepare(self.inputs, self.root)

    def test_automated_source_surface_has_no_python_escape_or_unbounded_host_edit(self):
        baseline = Path(self.inputs["baseline"]).read_text()
        candidate = Path(self.inputs["candidate"]).read_text()
        self.assertFalse(component_scope.validate(baseline, candidate)["host_index_cast"])
        changed = candidate.replace(component_scope.HOST_ANCHOR,
                                    component_scope.INDEX_CAST + component_scope.HOST_ANCHOR)
        self.assertTrue(component_scope.validate(baseline, changed)["host_index_cast"])
        self.assertEqual(self.capture(changed)["launch"]["args"][9]["dtype"], "int32")
        for revision in (candidate.replace(component_scope.HOST_ANCHOR, "    print('outside')\n" + component_scope.HOST_ANCHOR),
                         candidate.replace("tl.program_id(0)", "__import__('os').getpid()"),
                         candidate.replace("tl.program_id(0)", "tl.__dict__['program_id'](0)")):
            self.assertNotEqual(revision, candidate)
            with self.assertRaises(ValueError):
                component_scope.validate(baseline, revision)

    def retained_report(self):
        report = json.loads((DONOR / "COMPILE-RESULT.json").read_bytes())
        report["status"] = "CPU_COMPILED"
        report["dispatch"] = {arm: self.capture(Path(self.inputs[arm]).read_text(), arm, arm)
                              for arm in ("baseline", "candidate")}
        for arm, row in report["arms"].items():
            for part, info in row["artifacts"].items():
                info["gzip_base64"] = base64.b64encode(gzip.compress((DONOR / "artifacts" / (arm + "." + part)).read_bytes(), mtime=0)).decode()
        return report

    def receipt(self, metadata, report):
        raw = compiler.canonical(dict(returncode=0, stdout=json.dumps(report), stderr="", elapsed_seconds=1))
        (self.root / "target-compile.stdout").write_bytes(raw)
        (self.root / "target-compile.stderr").write_bytes(b"")
        return dict(exit_code=0, child_reaped=True, owned_group_absent=True, failure=None, signals=[],
                    stdin_sha256=metadata["recipe_sha256"], work_seconds=240, term_seconds=5, reap_seconds=5,
                    stdout=dict(bytes=len(raw), sha256=compiler.digest(raw)),
                    stderr=dict(bytes=0, sha256=compiler.digest(b"")))

    def test_retained_artifacts_recompute_frozen_resource_gate(self):
        metadata = compiler.prepare(self.inputs, self.root / "preparation")
        result = compiler.readout(self.root, metadata, self.receipt(metadata, self.retained_report()))
        self.assertEqual(result["compiler_gate"], "DISTINCT_CODE")
        self.assertTrue(result["gpu_eligible"])
        self.assertFalse(result["gpu_launch_authorized"])
        self.assertEqual(result["compiler_pins"]["B"], compiler.BASELINE_PARTS)
        self.assertEqual(len(list((self.root / "artifacts").iterdir())), 10)

    def test_invalid_baseline_receipt_or_candidate_identity_never_yields_result(self):
        metadata = compiler.prepare(self.inputs, self.root / "preparation")
        base = self.retained_report()
        for change in ("receipt", "baseline", "candidate"):
            report = copy.deepcopy(base)
            if change == "baseline":
                report["baseline_reproduced"] = False
            if change == "candidate":
                report["arms"]["candidate"]["source_sha256"] = "0" * 64
            receipt = self.receipt(metadata, report)
            if change == "receipt":
                receipt["child_reaped"] = False
            with self.subTest(change), self.assertRaises(ValueError):
                compiler.readout(self.root, metadata, receipt)
            self.assertFalse((self.root / "COMPILE-RESULT.json").exists())

    def test_artifact_trailer_and_decompression_bomb_are_rejected(self):
        expected = dict(bytes=2, sha256=compiler.digest(b"ok"))
        for value in (gzip.compress(b"ok") + b"extra", gzip.compress(b"x" * ((8 << 20) + 1))):
            with self.assertRaises(ValueError):
                compiler._decompress(base64.b64encode(value).decode(), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
