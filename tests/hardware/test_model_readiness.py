"""Synthetic metadata/byte fixtures, not model or numerical GPU qualification."""
import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import model_readiness as readiness
import model_kernels
import serving_memory


def config():
    # Deliberately tiny invented geometry, never a release specification.
    return {"architectures": ["Qwen4ExpForCausalLM"], "model_type": "qwen4_exp_text",
            "hidden_size": 16, "num_hidden_layers": 2, "num_attention_heads": 4,
            "num_key_value_heads": 2, "head_dim": 4, "vocab_size": 32,
            "hc_count": 4, "hc_lowrank": 3, "layer_types": ["linear_attention", "full_attention"],
            "linear_num_key_heads": 2, "linear_num_value_heads": 4,
            "linear_key_head_dim": 2, "linear_value_head_dim": 4, "linear_conv_kernel_dim": 4}


def qsa(c):
    c.update(indexer_n_heads=4, indexer_kv_heads=1, indexer_head_dim=4,
             indexer_budget=2048, indexer_compress_ratio=4, partial_rotary_factor=1.0)
    return c


def ple(c):
    c.update(ple_layer_ids=[1], ple_embed_dim=16, ple_conv_kernel_size=4,
             ngram_size=3, heads_per_ngram=2, ngram_vocab_size_base=64,
             make_ngram_vocab_size_divisible_by=8, eos_token_id=31)
    return c


def stage(directory, c=None):
    (directory / "config.json").write_text(json.dumps(c or config()))
    for name in ("LICENSE", "tokenizer.json", "generation_config.json", "chat_template.jinja"):
        (directory / name).write_text("synthetic-only")
    (directory / "tokenizer_config.json").write_text("{}")
    tensors = {"model.weight": ("F8_E4M3", [2, 4]), "model.weight_scale_inv": ("F32", [1]),
               "model.norm.weight": ("BF16", [4]), "model.ple.ngram_embedding.weight": ("BF16", [4, 4]),
               "mtp.weight": ("BF16", [2, 2])}
    end, header = 0, {}
    for name, (dtype, shape) in tensors.items():
        count = readiness.DTYPE_BYTES[dtype]
        for dim in shape:
            count *= dim
        header[name] = {"shape": shape, "dtype": dtype, "data_offsets": [end, end+count]}
        end += count
    raw = json.dumps(header).encode()
    (directory / "model.safetensors").write_bytes(struct.pack("<Q", len(raw)) + raw + bytes(end))
    return header


class ReadinessTests(unittest.TestCase):
    def test_optional_features_come_from_config_not_name(self):
        c = config()
        r = readiness.configuration(c, 2)
        self.assertEqual(r["errors"], [])
        self.assertFalse(r["qsa"])
        self.assertFalse(r["ple"])
        self.assertTrue(readiness.configuration(qsa(c), 2)["qsa"])
        c.pop("indexer_budget")
        self.assertIn("QSA: missing/invalid indexer_budget", readiness.configuration(c, 2)["errors"])

    def test_tp_replicated_kv_and_replicated_low_rank(self):
        c = config()
        c["num_key_value_heads"] = 1
        self.assertEqual(readiness.configuration(c, 2)["errors"], [])
        c["num_attention_heads"] = 3
        self.assertTrue(any("tensor-parallel" in s for s in readiness.configuration(c, 2)["errors"]))

    def test_bad_shapes_and_selector_boundaries(self):
        for key, value in [("hidden_size", True), ("hc_count", 1), ("linear_num_value_heads", 3),
                           ("num_hidden_layers", 3)]:
            c = config(); c[key] = value
            self.assertTrue(readiness.configuration(c, 2)["errors"], key)

    def test_qsa_replicated_indexer_and_rope(self):
        c = qsa(config()); c["indexer_n_heads"] = 3
        self.assertEqual(readiness.configuration(c, 2)["errors"], [])
        c["indexer_head_dim"] = 2
        self.assertIn("QSA: explicit attention rotary dimension must fit the indexer head", readiness.configuration(c, 2)["errors"])
        c["rope_parameters"] = {"partial_rotary_factor": 0.5}
        self.assertEqual(readiness.configuration(c, 2)["errors"], [])
        c["rope_parameters"]["partial_rotary_factor"] = float("nan")
        self.assertTrue(readiness.configuration(c, 2)["errors"])
        for key, value in [("indexer_budget", 2047), ("indexer_compress_ratio", 1), ("indexer_kv_heads", 2)]:
            c = qsa(config()); c[key] = value
            self.assertTrue(readiness.configuration(c, 2)["errors"], key)

    def test_ple_history_config_is_not_old_model_state(self):
        self.assertEqual(readiness.configuration(ple(config()), 2)["errors"], [])
        for key, value in [("ple_layer_ids", [0]), ("ple_layer_ids", [2]), ("ple_layer_ids", [1, 1]),
                           ("eos_token_id", None), ("eos_token_id", "31"), ("eos_token_id", [31, 30]),
                           ("eos_token_id", 32), ("ple_embed_dim", 15), ("ngram_size", 1)]:
            c = ple(config()); c[key] = value
            self.assertTrue(readiness.configuration(c, 2)["errors"], key)

    def test_moe_and_quantization_are_not_substitutes(self):
        c = config(); c["num_experts"] = 512
        self.assertTrue(readiness.configuration(c, 2)["errors"])
        c = config(); c["quantization_config"] = {"quant_method": "awq"}
        self.assertTrue(readiness.configuration(c, 2)["errors"])

    def test_tensor_bytes_and_memory_are_complete_but_not_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); stage(directory)
            inventory = readiness.tensor_inventory(directory)
            report = serving_memory.model_memory_estimate(config(), inventory)
        self.assertEqual(report["tensor_storage_bytes"], {"main_weights": 8, "scales": 4,
                         "unquantized_tensors": 8, "embedding_tables": 32, "mtp_weights": 8})
        self.assertEqual(report["total_tensor_storage_bytes"], 60)
        self.assertEqual(report["checkpoint_storage_half_bytes"], 30)
        self.assertEqual(report["per_rank_weight_bytes"], [None, None])
        self.assertEqual(report["kv_bf16_full_context_per_rank_bytes"], 2 * 4 * 2 * 65536)
        self.assertEqual(report["active_recurrent_fp32_per_rank_bytes"], 2 * 2 * 2 * 4 * 4)
        self.assertEqual(report["active_gdn_convolution_bf16_per_rank_bytes"], 144)
        self.assertIsNone(report["temporary_workspace_bytes"])
        self.assertIsNone(report["host_loading_peak_bytes"])
        sparse = serving_memory.model_memory_estimate(qsa(config()), inventory)
        self.assertEqual(report["kv_bf16_full_context_per_rank_bytes"], sparse["kv_bf16_full_context_per_rank_bytes"])

    def test_truncated_unindexed_and_traversal_shards_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); stage(d)
            p = d / "model.safetensors"
            p.write_bytes(p.read_bytes()[:-1])
            with self.assertRaises(ValueError): readiness.tensor_inventory(d)
            stage(d)
            (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"model.weight": "../outside.safetensors"}}))
            with self.assertRaises(ValueError): readiness.tensor_inventory(d)
            (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"wrong": "model.safetensors"}}))
            with self.assertRaises(ValueError): readiness.tensor_inventory(d)

    def test_tensor_config_and_fp8_scale_shapes(self):
        c = config(); c["quantization_config"] = {"quant_method": "fp8", "fmt": "e4m3", "weight_block_size": [2, 2]}
        details = readiness.configuration(c, 2)
        inv = {"tensors": {"model.q_proj.weight": {"dtype": "F8_E4M3", "shape": [3, 4]},
                           "model.q_proj.weight_scale_inv": {"dtype": "F32", "shape": [2, 2]}}}
        self.assertEqual(readiness.tensor_contract_errors(details, inv), [])
        bad = copy.deepcopy(inv); bad["tensors"]["model.q_proj.weight_scale_inv"]["shape"] = [1, 2]
        self.assertTrue(readiness.tensor_contract_errors(details, bad))
        bad = copy.deepcopy(inv); bad["tensors"]["model.q_proj.weight"]["dtype"] = "F8_E4M3FNUZ"
        self.assertTrue(readiness.tensor_contract_errors(details, bad))
        ple_inv = {"tensors": {"model.ple.ngram_embedding.shard_0.weight": {"dtype": "F8_E4M3", "shape": [4, 4]}}}
        self.assertEqual(readiness.tensor_contract_errors(details, ple_inv),
                         ["FP8: scale mapping unreviewed for model.ple.ngram_embedding.shard_0.weight"])
        bad = copy.deepcopy(inv); bad["tensors"]["lm_head.weight"] = {"dtype": "BF16", "shape": [31, 16]}
        self.assertTrue(readiness.tensor_contract_errors(details, bad))

    def test_even_valid_synthetic_assets_cannot_authorize_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); stage(d, ple(qsa(config())))
            r = readiness.preflight(d)
            self.assertEqual(r["status"], "blocked")
            self.assertFalse(r["automatic_apply"])
            self.assertIsNone(r["model"]["repository"])
            for fragment in ("rollback", "graph", "cancellation", "prefix-cache", "EOS", "tools", "QSA", "PLE"):
                self.assertIn(fragment, r["reason"])
            (d / "LICENSE").unlink()
            self.assertIn("missing/unsafe asset LICENSE", readiness.preflight(d)["reason"])

    def test_existing_runtime_contract_cannot_be_reused(self):
        for scope in ("legacy", "single-bf16"):
            with self.assertRaisesRegex(ValueError, "separately reviewed"):
                model_kernels.validate_runtime_contract({"scope": scope}, {"model_contract": {
                    "architectures": ["Qwen4ExpForConditionalGeneration"], "model_type": "qwen4_exp"}})

    def test_cli_writes_refusal_without_overwriting_or_launching(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preflight.json"
            cmd = ["bash", str(ROOT / "bin/workstationctl"), "rocm", "model-preflight", str(output)]
            env = {**os.environ, "WORKSTATION_PYTHON": sys.executable}
            run = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertIn("official dense checkpoint", json.loads(output.read_text())["reason"])
            before = output.read_bytes()
            self.assertNotEqual(subprocess.run(cmd, capture_output=True, env=env).returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_nonfinite_config_becomes_serializable_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); stage(d)
            for value in ("NaN", "Infinity", "1e999"):
                (d / "config.json").write_text('{"rms_norm_eps": ' + value + '}')
                result = readiness.preflight(d)
                self.assertEqual(result["status"], "blocked")
                json.dumps(result, allow_nan=False)
                self.assertIn("checkpoint:", result["reason"])

    def test_profile_and_offline_export_remain_disabled(self):
        profile, _ = readiness.read_json(readiness.PROFILE)
        self.assertFalse(profile["automatic_apply"])
        self.assertIsNone(profile["image"])
        self.assertTrue(all(v is None for v in profile["model"].values()))
        for name in ("source.files", "bridge-runtime.files"):
            lines = (ROOT / "infrastructure/packages/bootstrap" / name).read_text().splitlines()
            self.assertIn("lib/workstation/model_readiness.py", lines)
            self.assertIn("infrastructure/sglang/qwen4-readiness/profile.json", lines)


if __name__ == "__main__":
    unittest.main()
