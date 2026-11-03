"""Offline Qwen4 diagnostics. No imports of model code, GPU access or activation.

The reviewed profile is deliberately unresolved. Even valid synthetic metadata
cannot grant model identity, kernel qualification or permission to serve.
"""
import hashlib
import json
import math
from pathlib import Path
import struct


PROFILE = Path(__file__).resolve().parents[2] / "infrastructure/sglang/qwen4-readiness/profile.json"
QSA_FIELDS = ("indexer_n_heads", "indexer_kv_heads", "indexer_head_dim",
              "indexer_budget", "indexer_compress_ratio")
DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
               "I16": 2, "I8": 1, "U8": 1, "BOOL": 1, "F8_E4M3": 1,
               "F8_E5M2": 1, "F8_E4M3FNUZ": 1, "F8_E5M2FNUZ": 1}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024**2:
        raise ValueError("require a bounded regular JSON file: " + path.name)
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=unique_object)
    # Also catches overflow from a syntactically ordinary number such as 1e999.
    json.dumps(value, allow_nan=False)
    return value, hashlib.sha256(raw).hexdigest()


def positive(value):
    return type(value) is int and 0 < value <= 2**40


def configuration(config, tp):
    """Inspect explicit config values; never fill dense release values from clues."""
    if not isinstance(config, dict) or not positive(tp):
        raise ValueError("require a config object and positive tensor parallel size")
    text = config.get("text_config", config)
    if not isinstance(text, dict):
        raise ValueError("text_config must be an object")
    errors = []
    architecture = config.get("architectures")
    if architecture not in (["Qwen4ExpForConditionalGeneration"], ["Qwen4ExpForCausalLM"]):
        errors.append("architecture: no reviewed dense release loader for " + str(architecture))
    if text.get("model_type") not in ("qwen4_exp_text", "qwen4_exp"):
        errors.append("architecture: unrecognized Qwen4 text model_type")
    for key in ("hidden_size", "num_hidden_layers", "num_attention_heads", "num_key_value_heads",
                "head_dim", "vocab_size", "hc_count", "hc_lowrank"):
        if not positive(text.get(key)):
            errors.append("configuration: missing/invalid " + key)
    for key in ("hidden_size", "num_attention_heads", "linear_num_key_heads", "linear_num_value_heads",
                "intermediate_size", "moe_intermediate_size", "shared_expert_intermediate_size"):
        value = text.get(key)
        if value is not None and (not positive(value) or value % tp):
            errors.append("tensor-parallel: " + key + " must divide across ranks")
    q, kv = text.get("num_attention_heads"), text.get("num_key_value_heads")
    if positive(q) and positive(kv) and (q % kv or (kv % tp if kv >= tp else tp % kv)):
        errors.append("tensor-parallel: incompatible Q/KV heads (including KV replication)")
    hc, hidden = text.get("hc_count"), text.get("hidden_size")
    if positive(hc) and (hc <= 1 or (positive(hidden) and hidden * hc % tp)):
        errors.append("tensor-parallel: invalid hyperconnection stream width")
    layers = text.get("layer_types")
    if (not isinstance(layers, list) or len(layers) != text.get("num_hidden_layers")
            or any(v not in ("linear_attention", "full_attention", "indexed_attention", "qwen_sparse_attention") for v in layers)):
        errors.append("configuration: explicit complete layer_types required")
        layers = []
    if "linear_attention" in layers:
        for key in ("linear_key_head_dim", "linear_value_head_dim", "linear_num_key_heads",
                    "linear_num_value_heads", "linear_conv_kernel_dim"):
            if not positive(text.get(key)):
                errors.append("DeltaNet: missing/invalid " + key)
        k, v = text.get("linear_num_key_heads"), text.get("linear_num_value_heads")
        if positive(k) and positive(v) and v % k:
            errors.append("DeltaNet: value heads must be divisible by key heads")
    present = [key for key in QSA_FIELDS if text.get(key) is not None]
    qsa = bool(present)
    if qsa:
        for key in QSA_FIELDS:
            if not positive(text.get(key)):
                errors.append("QSA: missing/invalid " + key)
        if all(positive(text.get(k)) for k in QSA_FIELDS):
            if text["indexer_kv_heads"] != 1:
                errors.append("QSA: reviewed indexer requires one shared KV head")
            if text["indexer_budget"] % text["indexer_compress_ratio"]:
                errors.append("QSA: budget must be divisible by compression ratio")
            if text["indexer_compress_ratio"] < 2 or text["indexer_budget"] // text["indexer_compress_ratio"] not in (512, 2048):
                errors.append("QSA: reviewed SGLang selector requires ratio >=2 and block top-k 512 or 2048")
            # The reviewed SGLang indexer uses ReplicatedLinear, unlike the
            # Transformers TP plan. Do not require indexer heads / TP here.
            rope = text.get("rope_parameters", text.get("rope_scaling", {})) or {}
            factor = rope.get("partial_rotary_factor", text.get("partial_rotary_factor")) if isinstance(rope, dict) else None
            if (type(factor) not in (int, float) or not math.isfinite(factor)
                    or not 0 < factor <= 1 or not positive(text.get("head_dim"))
                    or not 0 < int(text["head_dim"] * factor) <= text["indexer_head_dim"]):
                errors.append("QSA: explicit attention rotary dimension must fit the indexer head")
    ple = text.get("ple_layer_ids", [])
    if not isinstance(ple, list) or any(type(i) is not int for i in ple):
        errors.append("PLE: ple_layer_ids must be one-based integer indices")
        ple = []
    if ple:
        if len(set(ple)) != len(ple) or any(not 1 <= i <= len(layers) or layers[i-1] != "linear_attention" for i in ple):
            errors.append("PLE: layers must be unique one-based DeltaNet layer indices")
        for key in ("ple_embed_dim", "ple_conv_kernel_size", "ngram_size", "heads_per_ngram",
                    "ngram_vocab_size_base", "make_ngram_vocab_size_divisible_by"):
            if not positive(text.get(key)):
                errors.append("PLE: missing/invalid " + key)
        size, heads, dim = (text.get(k) for k in ("ngram_size", "heads_per_ngram", "ple_embed_dim"))
        if all(positive(v) for v in (size, heads, dim)) and (size < 2 or dim % ((size-1)*heads)):
            errors.append("PLE: embedding dimension must divide by total n-gram heads")
        eos, vocab = text.get("eos_token_id"), text.get("vocab_size")
        if type(eos) is not int or not positive(vocab) or not 0 <= eos < vocab:
            errors.append("PLE: reviewed SGLang history requires a scalar in-vocabulary EOS token")
    experts = text.get("num_experts")
    if experts is not None and (not positive(experts) or experts > 1):
        errors.append("model-identity: MoE/unknown expert configuration is not an official dense checkpoint")
    quant = config.get("quantization_config", text.get("quantization_config"))
    if quant is not None and (not isinstance(quant, dict) or quant.get("quant_method") != "fp8"):
        errors.append("quantization: unsupported method; no automatic conversion")
    if isinstance(quant, dict) and quant.get("quant_method") == "fp8":
        block = quant.get("weight_block_size")
        if block is not None and (not isinstance(block, list) or len(block) != 2 or not all(positive(n) for n in block)):
            errors.append("FP8: invalid weight_block_size; scale layout unresolved")
    return {"architectures": architecture, "model_type": text.get("model_type"),
            "qsa": qsa, "ple": bool(ple), "hyperconnections": hc,
            "native_mtp_layers": text.get("mtp_num_hidden_layers"),
            "quantization": quant, "errors": errors, "text_config": text}


def tensor_inventory(directory):
    """Read safetensors headers only; validate byte extents, never deserialize code.

    Header hashes identify this estimate, not weight authenticity. Official
    snapshot verification must independently hash and match the weight bodies.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("checkpoint directory must be a regular directory")
    index_path = directory / "model.safetensors.index.json"
    weight_map = None
    if index_path.exists():
        index, _ = read_json(index_path)
        weight_map = index.get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            raise ValueError("missing safetensors weight_map")
        shards = sorted(set(weight_map.values()))
    else:
        shards = ["model.safetensors"]
    if any(not isinstance(n, str) or Path(n).name != n or not n.endswith(".safetensors") for n in shards):
        raise ValueError("checkpoint shard must be a local safetensors filename")
    if set(p.name for p in directory.glob("*.safetensors")) != set(shards):
        raise ValueError("checkpoint shards missing or unindexed")
    records, files = {}, {}
    for shard in shards:
        path = directory / shard
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing regular checkpoint shard: " + shard)
        size = path.stat().st_size
        with path.open("rb") as stream:
            prefix = stream.read(8)
            if len(prefix) != 8:
                raise ValueError("truncated safetensors header")
            length = struct.unpack("<Q", prefix)[0]
            if not 2 <= length <= min(16 * 1024**2, size-8):
                raise ValueError("invalid safetensors header length")
            raw = stream.read(length)
        header = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(header, dict):
            raise ValueError("safetensors header must be an object")
        extents = []
        for name, entry in header.items():
            if name == "__metadata__":
                continue
            if name in records or not isinstance(entry, dict):
                raise ValueError("duplicate/malformed checkpoint tensor")
            shape, offsets, dtype = (entry.get(k) for k in ("shape", "data_offsets", "dtype"))
            if (not isinstance(shape, list) or any(type(n) is not int or n < 0 for n in shape)
                    or not isinstance(offsets, list) or len(offsets) != 2
                    or any(type(n) is not int or n < 0 for n in offsets) or dtype not in DTYPE_BYTES):
                raise ValueError("unsupported tensor shape, offsets or dtype: " + name)
            count = math.prod(shape) * DTYPE_BYTES[dtype]
            if offsets[1]-offsets[0] != count or offsets[1] > size-8-length:
                raise ValueError("tensor byte extent differs from shape: " + name)
            extents.append(tuple(offsets))
            records[name] = {"shape": shape, "dtype": dtype, "bytes": count, "shard": shard}
        end = 0
        for start, stop in sorted(extents):
            if start != end:
                raise ValueError("safetensors overlapping or unaccounted bytes")
            end = stop
        if end != size-8-length:
            raise ValueError("safetensors trailing unaccounted bytes")
        files[shard] = {"bytes": size, "header_sha256": hashlib.sha256(raw).hexdigest()}
    if not records or (weight_map is not None and weight_map != {n: r["shard"] for n, r in records.items()}):
        raise ValueError("weight_map differs from tensor inventory")
    return {"tensors": records, "files": files, "weight_body_verification": "NOT RUN"}


def tensor_contract_errors(details, inventory):
    """Check known unpartitioned embedding/head and block-scale storage shapes.

    This is not a full release-specific loader mapping. In particular, do not
    divide every tensor by TP or guess fused QKV/GR/MTP tensor layouts.
    """
    errors, config = [], details["text_config"]
    tensors = inventory["tensors"]
    for name, row in tensors.items():
        if name.endswith(("embed_tokens.weight", "lm_head.weight")):
            expected = [config.get("vocab_size"), config.get("hidden_size")]
            if all(positive(v) for v in expected) and row["shape"] != expected:
                errors.append("tensor-shape: " + name + " differs from vocab_size/hidden_size")
    quant = details.get("quantization")
    if isinstance(quant, dict) and quant.get("quant_method") == "fp8":
        block = quant.get("weight_block_size")
        if isinstance(block, list) and len(block) == 2 and all(positive(v) for v in block):
            for name, row in tensors.items():
                if not row["dtype"].startswith("F8_") or not name.endswith(".weight"):
                    continue
                if not name.endswith(tuple("." + layer + ".weight" for layer in
                                           ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"))):
                    # PLE has its own scalar scale/storage contract. Other
                    # fused/optional tensors also need a release loader map.
                    errors.append("FP8: scale mapping unreviewed for " + name)
                    continue
                scale = tensors.get(name[:-len("weight")] + "weight_scale_inv")
                if len(row["shape"]) != 2:
                    errors.append("FP8: no reviewed block-scale mapping for " + name)
                    continue
                expected = [(dim + size-1)//size for dim, size in zip(row["shape"], block)]
                if scale is None or scale["shape"] != expected or scale["dtype"] != "F32":
                    errors.append("FP8: missing/incompatible FP32 block inverse scales for " + name)
                if quant.get("fmt") == "e4m3" and row["dtype"] != "F8_E4M3":
                    errors.append("FP8: serialized format differs from E4M3FN config for " + name)
    return errors


def preflight(model_dir=None, resource_path=None):
    from serving_memory import model_memory_estimate

    profile, profile_hash = read_json(PROFILE)
    # This profile cannot become launchable by filling in a local JSON field.
    # A future release needs a separate reviewed runtime/identity integration.
    blockers = ["release: official dense checkpoint repository/revision/licence/tokenizer unresolved",
                "runtime: no qualified dense Qwen4 ROCm image contract; pinned installation is unchanged",
                "kernels: gfx1201 FP8 dispatch, gated residual and DeltaNet execution unqualified"]
    details, inventory, assets = None, None, {}
    if model_dir is None:
        blockers.append("checkpoint: no staged config, tokenizer, chat template or safetensors supplied")
    else:
        directory = Path(model_dir)
        try:
            config, digest = read_json(directory / "config.json")
            assets["config.json"] = digest
            details = configuration(config, 2)
            blockers.extend(details["errors"])
            tokenizer, digest = read_json(directory / "tokenizer_config.json")
            assets["tokenizer_config.json"] = digest
            required = ["LICENSE", "tokenizer.json", "generation_config.json"]
            if not tokenizer.get("chat_template"):
                required.append("chat_template.jinja")
            for name in required:
                path = directory / name
                if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024**2:
                    blockers.append("checkpoint: missing/unsafe asset " + name)
                else:
                    assets[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory = tensor_inventory(directory)
            blockers.extend(tensor_contract_errors(details, inventory))
            blockers.append("checkpoint: weight bodies must match the official immutable snapshot; headers are not authentication")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            blockers.append("checkpoint: " + str(error))
    if details:
        if details["qsa"]:
            blockers.append("QSA: selection/causal/partial-block/cache-update kernels on gfx1201 unqualified")
        if details["ple"]:
            blockers.append("PLE: n-gram/convolution request isolation and host-offload loading peaks unqualified")
        if details["qsa"] or details["ple"]:
            blockers.append("rollback: Transformers cache cropping unsupported with QSA or PLE; SGLang acceptance/rollback must be tested independently")
    resource = None
    if resource_path:
        try:
            resource, assets["resource-plan.json"] = read_json(resource_path)
        except (OSError, ValueError) as error:
            blockers.append("resources: " + str(error))
    memory = model_memory_estimate(details["text_config"] if details else None, inventory, resource)
    blockers.extend(memory["blockers"])
    blockers.extend(["lifecycle: graph live-input/ownership/cancellation and prefix-cache parity NOT RUN",
                     "client: dense EOS/stops/thinking/tools/structured-output and gateway acceptance NOT RUN"])
    return {"schema": 1, "status": "blocked", "reason": "; ".join(blockers), "blockers": blockers,
            "profile": profile["profile"], "profile_sha256": profile_hash, "automatic_apply": False,
            "model": profile["model"], "image": profile["image"], "configuration": details,
            "assets_sha256": assets, "tensor_inventory": inventory, "memory": memory,
            "hardware_validation": "NOT RUN", "deployment": "NOT RUN"}
