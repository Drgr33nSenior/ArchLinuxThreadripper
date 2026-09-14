"""Receipt descriptor tests; no container, model, server, or GPU is started."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sglang_evidence", ROOT / "tests/hardware/sglang-evidence.py")
evidence = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evidence
spec.loader.exec_module(evidence)


def descriptor():
    configuration = {"gpu_arch": "gfx1201", "tensor_parallel": 1, "compute_dtype": "float16",
                     "attention": "causal-full", "head_dimension": 128, "kv_heads": 8,
                     "query_heads": 32, "page_size": 1, "rotation": "post-rope-qk"}
    return {"schema": 1, "runtime": "sglang", "source_repository": "sgl-project/sglang",
            "source_revision": "a" * 40, "source_tree_sha256": "b" * 64, "patch_sha256": "c" * 64,
            "binary_sha256": "d" * 64, "compiler_revision": "clang 20.1.0 sha256:deadbeef",
            "dependencies_sha256": "e" * 64, "codec": "ultraquant-rdna4-v1",
            "configuration_sha256": evidence.canonical_digest(configuration), "configuration": configuration,
            "requested_mode": "ultraquant-rdna4-v1", "observed_mode": "compressed-gpu",
            "qualification": "unqualified", "restart_required": True}


MODEL = {"architectures": ["LlamaForCausalLM"], "num_attention_heads": 32,
         "num_key_value_heads": 8, "head_dim": 128}


class SGLangExperimentalKVReceiptTests(unittest.TestCase):
    def test_mapped_binary_deduplicates_normal_library_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libuq_hip.so"
            library.write_bytes(b"candidate")
            real_path = evidence.Path

            class Maps:
                def read_text(self):
                    prefix = "0000-1000 r-xp 00000000 00:00 1 "
                    return prefix + str(library) + "\n" + prefix.replace("r-xp", "r--p") + str(library) + "\n"

            def path(name):
                return Maps() if str(name) == "/proc/77/maps" else real_path(name)

            with mock.patch.object(evidence, "Path", side_effect=path):
                self.assertEqual(evidence.mapped_uq_binary(77), evidence.digest(library))

    def test_descriptor_requires_actual_candidate_configuration(self):
        evidence.validate_descriptor(descriptor(), MODEL)
        for change in (
                lambda value: value.__setitem__("binary_sha256", None),
                lambda value: value["configuration"].__setitem__("page_size", 2),
                lambda value: value.__setitem__("observed_mode", "not-run"),
                lambda value: value.__setitem__("requested_mode", "ordinary-kv")):
            value = copy.deepcopy(descriptor())
            change(value)
            with self.assertRaises(RuntimeError):
                evidence.validate_descriptor(value, MODEL)

    def test_descriptor_refuses_hybrid_or_mismatched_model(self):
        hybrid = dict(MODEL, architectures=["QwenHybridForCausalLM"])
        with self.assertRaises(RuntimeError):
            evidence.validate_descriptor(descriptor(), hybrid)
        wrong_heads = dict(MODEL, num_key_value_heads=4)
        with self.assertRaises(RuntimeError):
            evidence.validate_descriptor(descriptor(), wrong_heads)

    def test_receipt_ignores_a_prior_launcher_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt_dir = Path(directory) / "sglang-experimental-kv"
            receipt_dir.mkdir(mode=0o700)
            os.chmod(receipt_dir, 0o700)
            for pid, launcher, launcher_ticks, worker_ticks in ((31, 1, 10, 310), (41, 2, 20, 410)):
                value = {"schema": 1, "pid": pid, "start_ticks": worker_ticks,
                         "launcher_pid": launcher, "launcher_start_ticks": launcher_ticks,
                         "descriptor": descriptor()}
                path = receipt_dir / f"{pid}.json"
                path.write_text(json.dumps(value))
                os.chmod(path, 0o600)
            real_path = evidence.Path

            class Proc:
                def stat(self):
                    return type("Stat", (), {"st_uid": os.getuid()})()

            def path(value):
                text = str(value)
                if text == "/tmp/sglang-experimental-kv":
                    return receipt_dir
                if text.startswith("/proc/"):
                    return Proc()
                return real_path(value)

            with mock.patch.object(evidence, "Path", side_effect=path), \
                    mock.patch.object(evidence, "process_start_ticks", side_effect=lambda pid: {2: 20, 41: 410}[pid]), \
                    mock.patch.object(evidence, "launcher_owns_worker", return_value=True), \
                    mock.patch.object(evidence, "mapped_uq_binary", return_value="d" * 64):
                self.assertEqual(evidence.experimental_kv_receipt(
                    [(2, {"--attention-backend": "ultraquant-rdna4-v1"})], MODEL), descriptor())


if __name__ == "__main__":
    unittest.main()
