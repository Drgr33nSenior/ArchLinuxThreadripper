"""Opt-in offline kernel plans and evidence checks. Never apply or restart a Pod."""
import argparse
import copy
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tarfile

from serving import validate, finite, summary
import gemm_profile
import graph_profile


def load(path):
    return json.loads(Path(path).read_text())


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rmsnorm_package(output, source_path, variant="rmsnorm-no-residual"):
    """Package one guarded source override; no Torch import, image build or apply."""
    profiles = Path(__file__).resolve().parents[2] / "infrastructure/sglang"
    pins = {"rmsnorm-no-residual": "1ac6e7ed4ce725402caff0ead7d1979d46c55c7a08d79c30111d3cef839904b6",
            "rmsnorm-epilogue": "df3e3f5bf776934eda3f9d6f074b6e98225c2c66f22d83842a894c7847c2c626"}
    if variant not in pins:
        raise ValueError("unsupported RMSNorm profile")
    directory = profiles / variant
    if gemm_profile.sha(gemm_profile.bounded_bytes(directory / "profile.json")) != pins[variant]:
        raise ValueError("RMSNorm profile changed; review before repackaging")
    profile = load(directory / "profile.json")
    dependency = load(gemm_profile.PROFILE_DIR / "profile.json")
    if gemm_profile.sha(gemm_profile.canonical(dependency)) != profile["gemm_dependency_sha256"]:
        raise ValueError("accepted GEMM dependency changed")
    gemm_profile.validate_tables(dependency, gemm_profile.PROFILE_DIR)
    original = gemm_profile.bounded_bytes(source_path)
    if gemm_profile.sha(original) != profile["source_sha256"]:
        raise ValueError("RMSNorm source preimage changed; omit override")
    guard_bytes = gemm_profile.bounded_bytes(profiles / "rmsnorm-no-residual/guard.py")
    if gemm_profile.sha(guard_bytes) != "854611d20ccffbee5790171b5b1e383dcbe2af6481a68d5aac81634e3f84c1f0":
        raise ValueError("RMSNorm semantic guard changed; review before repackaging")
    guard = guard_bytes.decode()
    environment = "WORKSTATION_RMSNORM_NO_RESIDUAL"
    operation = "F.rms_norm(x.contiguous(), (self.hidden_size,), self.weight.data, self.variance_epsilon)"
    kernel = ""
    if variant == "rmsnorm-epilogue":
        kernel_bytes = gemm_profile.bounded_bytes(directory / "kernel.py")
        if gemm_profile.sha(kernel_bytes) != "c0c2d15cffd5ea0312e64a6f2aac6d248217f7aa146ca05c9b4e73d93b42d90b":
            raise ValueError("RMSNorm epilogue kernel changed; review before repackaging")
        kernel = kernel_bytes.decode() + "\n\n"
        environment = "WORKSTATION_RMSNORM_EPILOGUE"
        operation = "_workstation_rms_epilogue(x, self.weight.data)"
        profile["kernel_sha256"] = gemm_profile.sha(kernel_bytes)
    text = original.decode()
    anchor = "class RMSNorm(BaseFusedOp):\n"
    branch = "        # Fallback to native implementation if vllm is not available\n"
    if text.count(anchor) != 1 or text.count(branch) != 1:
        raise ValueError("RMSNorm source anchors changed")
    text = text.replace(anchor, f"_workstation_no_residual_rms_enabled = get_bool_env_var(\"{environment}\")\n\n\n" + guard + "\n\n" + kernel + anchor)
    text = text.replace(branch, "        if _workstation_no_residual_rms_supported(self, x, residual, post_residual_addition, quant_linear):\n"
                        f"            return {operation}\n" + branch)
    compile(text, "layernorm.py", "exec")
    source = text.encode()
    output = Path(output)
    if output.exists():
        raise ValueError("RMSNorm output already exists")
    output.mkdir(mode=0o700)
    payload = output / "payload"
    payload.mkdir(mode=0o700)
    profile.update(source_override_sha256=gemm_profile.sha(source), guard_sha256=gemm_profile.sha(guard.encode()))
    members = {"layernorm.py": source, "profile.json": gemm_profile.canonical(profile),
               "gemm-dependency.json": gemm_profile.canonical(dependency)}
    for name, raw in members.items():
        with (payload / name).open("xb") as stream:
            stream.write(raw)
    manifest = "".join(gemm_profile.sha(raw) + "  " + name + "\n" for name, raw in sorted(members.items()))
    (payload / "SOURCE.sha256").write_text(manifest)
    with tarfile.open(output / "rmsnorm-source.tar", "x", format=tarfile.USTAR_FORMAT) as archive:
        for name, raw in sorted(members.items()):
            row = tarfile.TarInfo(name)
            row.size, row.mode = len(raw), 0o644
            row.uid = row.gid = row.mtime = 0
            archive.addfile(row, io.BytesIO(raw))
    result = {"profile": profile["profile"], "automatic_apply": False,
              "source_sha256": profile["source_sha256"], "source_override_sha256": profile["source_override_sha256"],
              "tar_sha256": file_hash(output / "rmsnorm-source.tar"),
              "parent_image": dependency["parent_image"], "runtime": dependency["runtime"],
              "model": dependency["model"], "devices": dependency["devices"]}
    (output / "package-identities.json").write_bytes(gemm_profile.canonical(result))
    return result


def moe_table_package(output, profile_dir=None):
    """Retain the measured tables only; no launch, image mutation or Torch import.

    These exact bytes are an experimental configuration artifact. A fresh
    owner verifier must bind effective loading and runtime identities before
    selection; emitting this package cannot establish a live GPU observation.
    """
    directory = Path(profile_dir) if profile_dir is not None else Path(__file__).resolve().parents[2] / "infrastructure/sglang/moe-m1"
    profile_pin = "8e30e782250bf300b507370cb1d5f5038ce6e9891d4166f14fe64cc1fcd06c24"
    table_pin = "c59324d8bd2254b7171c8a74187a90f13776958ce02e620757be04acd0a5afbf"
    dependency_pin = "19c0abed0d30b526c9f4f04cb2ada6158607c4e6b253edcea7fc52739c27b803"

    def source_bytes(path, limit):
        if directory.is_symlink() or path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
            raise ValueError("MoE package requires bounded regular source files, not symlinks")
        return path.read_bytes()

    profile_bytes = source_bytes(directory / "profile.json", 16384)
    if hashlib.sha256(profile_bytes).hexdigest() != profile_pin:
        raise ValueError("MoE profile source identity changed; review before repackaging")
    profile = json.loads(profile_bytes)
    dependency = json.loads(gemm_profile.bounded_bytes(gemm_profile.PROFILE_DIR / "profile.json", 16384))
    if gemm_profile.sha(gemm_profile.canonical(dependency)) != dependency_pin or profile["gemm_dependency_sha256"] != dependency_pin:
        raise ValueError("accepted GEMM dependency is stale or unsupported")
    gemm_profile.validate_tables(dependency, gemm_profile.PROFILE_DIR)
    if (profile["parent_image"] != dependency["parent_image"] or profile["model"]["revision"] != dependency["model"]["revision"]
            or profile["devices"] != dependency["devices"] or profile["scope"]["concurrency"] != 1):
        raise ValueError("MoE package is outside its measured identity scope")
    members = {"profile.json": profile_bytes,
               "gemm-dependency.json": gemm_profile.canonical(dependency)}
    for down, filename in ((False, "normal.json"), (True, "down.json")):
        data = source_bytes(directory / filename, 8192)
        if hashlib.sha256(data).hexdigest() != table_pin:
            raise ValueError("MoE table source identity changed")
        table = json.loads(data)
        if (set(table) != {str(m) for m in profile["m_grid"]} or table["1"] != profile["single_token"]
                or any(value != profile["incumbent"] for m, value in table.items() if int(m) >= 2)):
            raise ValueError("MoE table must retain every larger-M incumbent guard")
        shape_name = "E=128,N=384,device_name=AMD_Radeon_AI_PRO_R9700,dtype=fp8_w8a8,block_shape=[128, 128]"
        members["configs/triton_3_8_0/" + shape_name + ("_down" if down else "") + ".json"] = data
    selection = {"schema": 1, "profile": profile["profile"], "qualification": profile["qualification"],
                 "default_selection": "incumbent", "automatic_apply": False,
                 "environment_if_explicitly_selected_and_verified": {"SGLANG_MOE_CONFIG_DIR": "/work/source/moe-config"},
                 "read_only_package_mount": "/work/source/moe-config",
                 "required_environment": dependency["environment"],
                 "unsupported_or_stale_fallback": "omit SGLANG_MOE_CONFIG_DIR; use fresh incumbent MoE process",
                 "configuration_sha256": identity(profile),
                 "cache_reuse": "not qualified by this static configuration; requires fresh owner boot/runtime/source/model/GPU observation and matching cache receipt",
                 "preflight_required": "fresh installed-source/runtime/GPU/model identity and per-device effective table loading; see profile provenance verifier",
                 "must_not_use": "GEMM-only profile helper with this MoE override; deterministic inference or unreviewed concurrent/shape/precision profiles"}
    members["SELECT-ENV.json"] = (json.dumps(selection, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    members["SOURCE.sha256"] = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)).encode()
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("MoE package output already exists; choose a fresh path")
    output.mkdir(mode=0o700)
    payload = output / "payload"
    payload.mkdir(mode=0o700)
    for name, data in sorted(members.items()):
        path = payload / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    with (output / "moe-tables.tar").open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(members.items()):
                entry = tarfile.TarInfo(name)
                entry.mode, entry.uid, entry.gid, entry.mtime, entry.size = 0o644, 0, 0, 0, len(data)
                archive.addfile(entry, io.BytesIO(data))
    record = {"schema": 1, "profile": profile["profile"], "qualification": profile["qualification"],
              "source_profile_sha256": profile_pin, "gemm_dependency_sha256": dependency_pin,
              "members_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(members.items())},
              "tar_sha256": file_hash(output / "moe-tables.tar"), "automatic_apply": False,
              "rollback": profile["rollback"]}
    write(output / "package-identities.json", record)
    return record


def write(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def mib(value):
    match = re.fullmatch(r"([1-9][0-9]*)(Mi|Gi)", str(value))
    if not match:
        raise ValueError("use explicit integer Mi/Gi memory limits")
    return int(match[1]) * (1024 if match[2] == "Gi" else 1)


def image_contract(lock, image):
    """Resolve one exact image to its source contract, not an arbitrary pin.

    The legacy contract remains valid for rollback and the unqualified TP2
    profile. The owner-selected 10.1 image has a deliberately narrower scope.
    """
    prefixes = ("SGLANG", "SGLANG_SINGLE")
    matches = [prefix for prefix in prefixes if lock.get(prefix + "_ROCM_IMAGE") == image]
    if len(matches) != 1:
        raise ValueError("require one reviewed SGLang image contract")
    prefix = matches[0]
    sources = {"srt/server_args.py": lock[prefix + "_SERVER_ARGS_SHA256"],
               "srt/compilation/torch_compile_decoration.py": lock[prefix + "_COMPILE_DECORATION_SHA256"],
               "srt/models/qwen3_5.py": lock[prefix + "_QWEN35_SOURCE_SHA256"]}
    config_digest = lock[prefix + "_ROCM_CONFIG_DIGEST"]
    if (not re.search(r"@sha256:[a-f0-9]{64}$", image)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", config_digest)
            or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in sources.values())):
        raise ValueError("malformed reviewed SGLang image contract")
    return {"image": image, "config_digest": config_digest, "sources": sources,
            "scope": "single-bf16" if prefix == "SGLANG_SINGLE" else "legacy",
            "model_repository": lock.get("SGLANG_SINGLE_MODEL_REPOSITORY"),
            "model_revision": lock.get("SGLANG_SINGLE_MODEL_REVISION")}


def validate_profile_contract(contract, settings):
    """Do not extend the 10.1 selection to untested models, FP8, TP2 or context."""
    if contract["scope"] != "single-bf16":
        return
    required = {"MODEL_REPOSITORY": contract["model_repository"],
                "MODEL_REVISION": contract["model_revision"],
                "MODEL_DTYPE": "bfloat16", "TENSOR_PARALLEL": "1",
                "ATTENTION_BACKEND": "triton", "CONTEXT_LENGTH": "4096",
                "MEM_FRACTION_STATIC": "0.80", "MAX_RUNNING_REQUESTS": "2"}
    if (not isinstance(settings, dict) or any(settings.get(key) != value for key, value in required.items())
            or settings.get("SGLANG_USE_AITER") not in ("0", "false")):
        raise ValueError("ROCm 10.1 selection is limited to the reviewed BF16/TP1/Triton profile")


def validate_runtime_contract(contract, runtime):
    validate_profile_contract(contract, runtime.get("settings"))
    if contract["scope"] != "single-bf16":
        return
    model = runtime.get("model_contract", {})
    launch = runtime.get("launch")
    if (model.get("architectures") != ["Qwen3_5ForConditionalGeneration"]
            or model.get("quant_method") != "unquantized" or len(runtime.get("devices", [])) != 1
            or not isinstance(launch, list) or len(launch) != 1 or not isinstance(launch[0], dict)):
        raise ValueError("ROCm 10.1 observation is outside the reviewed single-GPU model scope")
    command = launch[0]
    tp = [command[key] for key in ("--tp", "--tp-size") if key in command]
    if (tp != ["1"] or command.get("--dtype") != "bfloat16"
            or command.get("--attention-backend") != "triton"
            or command.get("--context-length") != "4096"
            or command.get("--mem-fraction-static") != "0.80"
            or command.get("--max-running-requests") != "2"
            or command.get("--revision") != contract["model_revision"]
            or command.get("--mamba-radix-cache-strategy") != "no_buffer"
            or command.get("--disable-overlap-schedule") is not True
            or command.get("--quantization") is not None):
        raise ValueError("ROCm 10.1 observed launch differs from the reviewed profile")


def experimental_kv_runtime(runtime):
    """Validate the only candidate allowed to relax image/package equality."""
    descriptor = runtime.get("experimental_kv") if isinstance(runtime, dict) else None
    required = {"schema", "runtime", "source_repository", "source_revision", "source_tree_sha256",
                "patch_sha256", "binary_sha256", "compiler_revision", "dependencies_sha256", "codec",
                "configuration_sha256", "configuration", "requested_mode", "observed_mode", "qualification",
                "restart_required"}
    config_keys = {"gpu_arch", "tensor_parallel", "compute_dtype", "attention", "head_dimension",
                   "kv_heads", "query_heads", "page_size", "rotation"}
    if (not isinstance(descriptor, dict) or set(descriptor) != required
            or type(descriptor.get("schema")) is not int or descriptor.get("schema") != 1 or descriptor.get("runtime") != "sglang"
            or descriptor.get("source_repository") != "sgl-project/sglang"
            or not isinstance(descriptor.get("source_revision"), str) or not re.fullmatch(r"[a-f0-9]{40}", descriptor["source_revision"])
            or any(not isinstance(descriptor.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", descriptor[key])
                   for key in ("source_tree_sha256", "patch_sha256", "binary_sha256", "dependencies_sha256", "configuration_sha256"))
            or not isinstance(descriptor.get("compiler_revision"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+:/-]{0,239}", descriptor["compiler_revision"])
            or descriptor.get("codec") != "ultraquant-rdna4-v1" or descriptor.get("requested_mode") != "ultraquant-rdna4-v1"
            or descriptor.get("observed_mode") != "compressed-gpu" or descriptor.get("qualification") != "unqualified"
            or descriptor.get("restart_required") is not True or not isinstance(descriptor.get("configuration"), dict)
            or set(descriptor["configuration"]) != config_keys):
        raise ValueError("experimental-KV quality lacks a complete candidate descriptor")
    config = descriptor["configuration"]
    if (config.get("gpu_arch") != "gfx1201" or type(config.get("tensor_parallel")) is not int or config.get("tensor_parallel") != 1
            or config.get("compute_dtype") != "float16" or config.get("attention") != "causal-full"
            or type(config.get("head_dimension")) is not int or config["head_dimension"] not in (32, 64, 128, 256)
            or type(config.get("kv_heads")) is not int or not 1 <= config["kv_heads"] <= 64
            or type(config.get("query_heads")) is not int or not config["kv_heads"] <= config["query_heads"] <= 128
            or config["query_heads"] % config["kv_heads"] or type(config.get("page_size")) is not int or config.get("page_size") != 1
            or config.get("rotation") != "post-rope-qk" or identity(config) != descriptor["configuration_sha256"]):
        raise ValueError("experimental-KV quality has an unsupported configuration")
    settings, devices, launch = runtime.get("settings"), runtime.get("devices"), runtime.get("launch")
    tp_options = [option for option in ("--tp", "--tp-size") if option in launch[0]] if isinstance(launch, list) and len(launch) == 1 and isinstance(launch[0], dict) else []
    if (not isinstance(settings, dict) or settings.get("MODEL_DTYPE") != "float16" or str(settings.get("TENSOR_PARALLEL")) != "1"
            or not isinstance(devices, list) or len(devices) != 1 or devices[0].get("gfx", "").split(":", 1)[0] != "gfx1201"
            or not isinstance(launch, list) or len(launch) != 1 or launch[0].get("--attention-backend") != "ultraquant-rdna4-v1"
            or launch[0].get("--dtype") != "float16" or len(tp_options) != 1 or launch[0][tp_options[0]] != "1"
            or runtime.get("model_contract", {}).get("architectures") != ["LlamaForCausalLM"]
            or runtime.get("model_contract", {}).get("quant_method") != "unquantized"):
        raise ValueError("experimental-KV descriptor does not match runtime observation")
    return descriptor


def experimental_kv_compiler_identity(baseline, candidate, descriptor):
    """Retain all host/compiler identity except the named candidate extension.

    Candidate SGLang sources are identified by the sealed descriptor rather
    than pretending that they equal the pinned baseline.  All non-SGLang
    packages, compiler/driver/settings and mapped libraries remain equal.
    """
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("experimental-KV quality lacks compiler identity")
    for key in ("python", "host_kernel", "amdgpu_srcversion", "hip", "compiler", "settings"):
        if baseline.get(key) != candidate.get(key):
            raise ValueError("experimental-KV quality changed compiler/driver/settings identity")
    baseline_packages, candidate_packages = baseline.get("packages"), candidate.get("packages")
    if not isinstance(baseline_packages, dict) or not isinstance(candidate_packages, dict):
        raise ValueError("experimental-KV quality lacks compiler package identity")
    for key in set(baseline_packages) | set(candidate_packages):
        if key.lower().replace("_", "-") not in ("sglang", "sglang-kernel", "sgl-kernel") and baseline_packages.get(key) != candidate_packages.get(key):
            raise ValueError("experimental-KV quality changed an unrelated compiler package")
    baseline_libraries, candidate_libraries = baseline.get("loaded_libraries"), candidate.get("loaded_libraries")
    if not isinstance(baseline_libraries, dict) or not isinstance(candidate_libraries, dict):
        raise ValueError("experimental-KV quality lacks mapped GPU-library identity")
    changed_libraries = {key for key in set(baseline_libraries) | set(candidate_libraries)
                         if baseline_libraries.get(key) != candidate_libraries.get(key)}
    if any(Path(key).name != "libuq_hip.so" for key in changed_libraries):
        raise ValueError("experimental-KV quality changed an unrelated mapped GPU library")
    if len(changed_libraries) != 1:
        raise ValueError("experimental-KV quality requires one mapped candidate library")
    library = candidate_libraries[next(iter(changed_libraries))]
    if not isinstance(library, dict) or library.get("sha256") != descriptor["binary_sha256"]:
        raise ValueError("experimental-KV quality candidate library does not match descriptor")
    baseline_sources, candidate_sources = baseline.get("sources"), candidate.get("sources")
    if not isinstance(baseline_sources, dict) or not isinstance(candidate_sources, dict) or set(baseline_sources) != set(candidate_sources):
        raise ValueError("experimental-KV quality changed the observed SGLang source inventory")
    compiler_hash = candidate.get("compiler", {}).get("sha256") if isinstance(candidate.get("compiler"), dict) else None
    if (not isinstance(compiler_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", compiler_hash)
            or f"sha256:{compiler_hash}" not in descriptor["compiler_revision"]):
        raise ValueError("experimental-KV descriptor does not bind the observed compiler")


def evidence(directory):
    directory = Path(directory)
    pod, runtime, compiler = (load(directory / name) for name in ("pod.json", "runtime.json", "compiler.json"))
    lock = dict(line.split("=", 1) for line in (Path(__file__).resolve().parents[2] / "versions.lock").read_text().splitlines()
                if line and not line.startswith("#"))
    if (not pod.get("image_id") or not pod.get("uid")
            or compiler["status"] != "observed-not-qualified" or compiler["schema"] != 1):
        raise ValueError("require observed compiler evidence from the pinned image")
    candidate = "experimental_kv" in runtime
    contract = None if candidate else image_contract(lock, pod["image"])
    actual_digest = re.search(r"sha256:[a-f0-9]{64}$", pod["image_id"])
    if (candidate and not actual_digest) or (not candidate and (not actual_digest or actual_digest[0] not in
                           (contract["image"].split("@")[-1], contract["config_digest"]))):
        raise ValueError("runtime image ID does not identify the locked amd64 manifest/config")
    if not candidate and compiler["sources"].get("srt/server_args.py") != contract["sources"]["srt/server_args.py"]:
        raise ValueError("image CLI source differs from reviewed contract; review before planning")
    if not candidate and compiler["sources"].get("srt/compilation/torch_compile_decoration.py") != contract["sources"]["srt/compilation/torch_compile_decoration.py"]:
        raise ValueError("image compilation source differs from reviewed contract")
    if not candidate and runtime.get("model_contract", {}).get("architectures") == ["Qwen3_5ForConditionalGeneration"] and compiler["sources"].get("srt/models/qwen3_5.py") != contract["sources"]["srt/models/qwen3_5.py"]:
        raise ValueError("image model implementation differs from reviewed contract")
    devices = runtime["devices"]
    uuids = [d["uuid"] for d in devices]
    if (len(devices) not in (1, 2) or len(set(uuids)) != len(uuids)
            or any(not u or u.lower() in ("unknown", "none") for u in uuids)
            or any(d["gfx"].split(":")[0] != "gfx1201" for d in devices)):
        raise ValueError("require distinct observed gfx1201 GPU identities, not ordinal assumptions")
    if not compiler["compiler"] or not compiler["loaded_libraries"] or not compiler["packages"]:
        raise ValueError("compiler and loaded GPU-library identity unavailable")
    if candidate:
        experimental_kv_runtime(runtime)
    else:
        validate_runtime_contract(contract, runtime)
    if not candidate and runtime.get("model_contract", {}).get("architectures") not in (["Qwen3_5ForConditionalGeneration"], ["LlamaForCausalLM"]):
        raise ValueError("model architecture not reviewed for this opt-in workflow")
    return pod, runtime, compiler


def runtime_key(pod, runtime, compiler):
    # Deliberately excludes mutable cache contents, PIDs, clocks and memory usage.
    return {"image": pod["image"], "image_id": pod["image_id"], "resources": pod["resources"],
            "runtime": runtime, "compiler": {key: compiler[key] for key in
                ("python", "host_kernel", "amdgpu_srcversion", "packages", "hip", "compiler", "sources", "loaded_libraries", "settings")}}


def plan(deployment, workload, observed, profile, workers, reserve_mib, worker_mib, reserve_cpus, experimental=False):
    pod, runtime, compiler = observed
    validate(workload, runtime)
    containers = deployment["spec"]["template"]["spec"]["containers"]
    indexes = [i for i, c in enumerate(containers) if c["name"] == "sglang"]
    if len(indexes) != 1:
        raise ValueError("expected one SGLang container")
    index, container = indexes[0], containers[indexes[0]]
    resources = container["resources"]
    if resources != pod["resources"] or resources["requests"] != resources["limits"] or container["image"] != pod["image"]:
        raise ValueError("rendered baseline must match observed Guaranteed Pod resources/image")
    spec = deployment["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}
    if mounts.get("cache") != "/cache" or "persistentVolumeClaim" not in volumes["cache"]:
        raise ValueError("reuse the existing persistent cache mount")
    shm = mib(volumes["shm"]["emptyDir"]["sizeLimit"])
    tp = int(runtime["settings"]["TENSOR_PARALLEL"])
    if tp != len(runtime["devices"]) or tp != int(resources["limits"]["amd.com/gpu"]):
        raise ValueError("TP ranks must match observed exclusive GPU allocation")
    max_requests = int(runtime["settings"]["MAX_RUNNING_REQUESTS"])
    batches = sorted(workload["concurrency"])
    if max(batches) > max_requests:
        raise ValueError("workload exceeds running-request baseline; qualify concurrency separately")
    cg = compiler["cgroup"]
    quota, period = cg["cpu.max"].split()
    if quota == "max" or cg["memory.max"] in (None, "max"):
        raise ValueError("finite observed pod CPU/RAM cgroup limits are required")
    cpu = min(float(resources["limits"]["cpu"]), int(quota) / int(period), compiler["affinity_cpus"])
    memory = min(mib(resources["limits"]["memory"]), int(cg["memory.max"]) // 1024**2)
    if (type(reserve_mib) is not int or type(worker_mib) is not int or worker_mib < 256
            or not finite(reserve_cpus) or reserve_cpus < 1
            or reserve_mib < max(shm, math.ceil(int(cg["memory.current"]) / 1024**2))):
        raise ValueError("reserve the observed host-model envelope including shm; workers need >=256 MiB each")
    maximum = min(16, math.floor((cpu - reserve_cpus) / tp), (memory - reserve_mib) // (worker_mib * tp))
    if maximum < 1 or not compiler["compile_threads_supported"]:
        raise ValueError("no safe compilation worker fits, or Inductor worker control is unsupported")
    if (not workers or len(set(workers)) != len(workers)
            or any(type(w) is not int or w < 1 or w > maximum for w in workers)):
        raise ValueError(f"each requested per-rank worker count must fit 1..{maximum}; no silent clipping")
    args = list(container["args"])
    if any(a in args for a in ("--dp", "--dp-size", "--nnodes", "--pp-size", "--enable-dp-attention")):
        raise ValueError("this bounded planner supports only the existing single-node TP deployment")
    if any(a.startswith(("--cuda-graph", "--torch-compile", "--enable-torch-compile", "--disable-cuda-graph")) for a in args):
        raise ValueError("start from the unmodified baseline, not a previous compilation candidate")
    required = []
    if profile in ("capture", "legacy-compile"):
        required = ["--cuda-graph-bs-decode"]
        args += [required[0], *map(str, batches)]
    if profile == "legacy-compile":
        if not experimental:
            raise ValueError("legacy torch.compile requires --experimental; model execution remains unqualified")
        required += ["--enable-torch-compile", "--torch-compile-max-bs"]
        args += ["--enable-torch-compile", "--torch-compile-max-bs", str(max(batches))]
    elif profile not in ("cache", "capture"):
        raise ValueError("unsupported profile; no forced piecewise/Instinct defaults")
    if not set(required) <= set(compiler["flags"]):
        raise ValueError("requested flags not supported by exact-image help")
    base = {"software_hardware_model": runtime_key(pod, runtime, compiler), "deployment_sha256": identity(deployment),
            "workload_sha256": identity(workload), "profile": profile, "batches": batches,
            "memory_reserve_mib": reserve_mib, "worker_mib": worker_mib, "cpu_reserve": reserve_cpus}
    cases = {}
    for count in workers:
        key = identity({**base, "workers_per_rank": count})
        env = {"TORCHINDUCTOR_COMPILE_THREADS": str(count), "TORCHINDUCTOR_FX_GRAPH_CACHE": "1",
               "TRITON_CACHE_DIR": f"/cache/triton/workstation/{key}",
               "TORCHINDUCTOR_CACHE_DIR": f"/cache/torchinductor/workstation/{key}"}
        # Keep verbose compile/cache diagnostics out of ordinary serving profiles.
        if profile == "legacy-compile":
            env["TORCH_LOGS"] = "+inductor"
        entries = [e for e in container.get("env", []) if e["name"] not in env]
        entries += [{"name": name, "value": value} for name, value in env.items()]
        root = f"/spec/template/spec/containers/{index}"
        cases[f"workers-{count}"] = {"cache_identity": key, "workers_per_rank": count, "total_workers": count * tp,
            "patch": [{"op": "test", "path": "/spec/template/spec", "value": copy.deepcopy(spec)},
                      {"op": "replace", "path": root + "/args", "value": args},
                      {"op": "add", "path": root + "/env", "value": entries}]}
    return {"schema": 1, "status": "plan-only-unqualified", "identity": base, "tp": tp,
            "maximum_workers_per_rank": maximum, "cases": cases,
            "compatibility": "CLI/source checked; model may disable compilation. Require actual dispatch, cache reuse, numerical and memory checks."}


def quality_compare(first, second, atol, rtol):
    if not (finite(atol) and finite(rtol) and 0 <= atol <= .1 and 0 <= rtol <= .1):
        raise ValueError("explicit numerical tolerances must be finite within 0..0.1")
    if not first or len(first) != len(second):
        raise ValueError("missing numerical workload cases")
    differences = []
    for a, b in zip(first, second):
        if a["case"] != b["case"] or a["tokens"] != b["tokens"] or not a["tokens"]:
            raise ValueError("deterministic output tokens or workload shapes differ")
        if len(a["logprobs"]) != len(a["tokens"]) or len(b["logprobs"]) != len(b["tokens"]):
            raise ValueError("missing output logprobs")
        for x, y in zip(a["logprobs"], b["logprobs"]):
            if not finite(x) or not finite(y) or abs(x-y) > atol + rtol * abs(x):
                raise ValueError("nonfinite or changed numerical output")
            differences.append(abs(x-y))
    return {"status": "sampled-output-check-passed", "atol": atol, "rtol": rtol,
            "absolute_logprob_difference": summary(differences), "scope": "not full model or coding-task qualification"}


def compare_quality(baseline, candidate, atol, rtol, memory_only=False, experimental_kv=False):
    for result in (baseline, candidate):
        if (result["status"] != "measured-not-qualified" or result.get("kind") != "kernel-warmup"
                or result.get("memory", {}).get("status") != "checked"):
            raise ValueError("require successful unprofiled, memory-checked runs")
        contract = result["warmup_contract"]
        if contract["repetitions"] != 3 or not contract["cases"] or not contract["concurrency"]:
            raise ValueError("missing complete warmup contract")
        expected = [(repeat, case["context_tokens"], batch, index, case["output_tokens"])
                    for repeat in range(3) for case in contract["cases"]
                    for batch in contract["concurrency"] for index in range(batch)]
        actual = [(*row["case"], len(row["tokens"])) for row in result["quality"]]
        if expected != actual:
            raise ValueError("missing, repeated or incompatible numerical workload cases")
    if baseline["workload_sha256"] != candidate["workload_sha256"]:
        raise ValueError("quality workload differs")
    if baseline["warmup_contract"] != candidate["warmup_contract"]:
        raise ValueError("warmup shapes differ")
    a, b = baseline["identity"], candidate["identity"]
    if memory_only and experimental_kv:
        raise ValueError("quality comparison cannot combine host-memory and experimental-KV modes")
    if memory_only:
        # Explicitly compare a smaller host-memory envelope, not another model,
        # compiler, GPU, launch configuration or CPU allocation. Keep the
        # existing strict comparison unchanged for all other callers.
        normalized = []
        for item in (a, b):
            item = copy.deepcopy(item)
            resources = item["resources"]
            if resources["requests"] != resources["limits"]:
                raise ValueError("memory-only quality requires equal requests/limits")
            for scope in ("requests", "limits"):
                resources[scope].pop("memory")
            normalized.append(item)
        if normalized[0] != normalized[1] or mib(b["resources"]["limits"]["memory"]) >= mib(a["resources"]["limits"]["memory"]):
            raise ValueError("memory-only comparison permits only a smaller host-memory request/limit")
    for key in ("image", "image_id", "resources"):
        if experimental_kv and key in ("image", "image_id"):
            continue
        if memory_only and key == "resources":
            continue
        if a[key] != b[key]:
            raise ValueError("quality comparison changed image/resources")
    for key in ("model_files", "devices", "hip"):
        if a["runtime"][key] != b["runtime"][key]:
            raise ValueError("quality comparison changed model/software/GPU identity")
    if experimental_kv:
        base_descriptor = a["runtime"].get("experimental_kv")
        if base_descriptor is not None:
            raise ValueError("experimental-KV quality needs unchanged baseline and identified candidate descriptor")
        descriptor = experimental_kv_runtime(b["runtime"])
        # The patched SGLang package is the only package identity permitted to
        # change in this narrow mode. Compiler/dependency hashes remain bound
        # by the descriptor and the other ROCm package identities stay equal.
        for key in set(a["runtime"]["packages"]) | set(b["runtime"]["packages"]):
            if key != "sglang" and a["runtime"]["packages"].get(key) != b["runtime"]["packages"].get(key):
                raise ValueError("experimental-KV quality changed an unrelated package")
        experimental_kv_compiler_identity(a.get("compiler"), b.get("compiler"), descriptor)
    elif a["runtime"]["packages"] != b["runtime"]["packages"]:
        raise ValueError("quality comparison changed model/software/GPU identity")
    return {"schema": 1, "status": "sampled-quality-passed-not-qualified",
            "comparison": "experimental-kv" if experimental_kv else ("host-memory-only" if memory_only else "kernel"),
            "baseline_sha256": identity(baseline), "candidate_sha256": identity(candidate),
            "quality": quality_compare(baseline["quality"], candidate["quality"], atol, rtol)}


def compare(cold, warm, cold_start, warm_start, logs, atol, rtol):
    a, b = load(Path(cold)/"result.json"), load(Path(warm)/"result.json")
    if any(r["status"] != "measured-not-qualified" or r.get("kind") != "kernel-warmup" for r in (a, b)):
        raise ValueError("require successful unprofiled kernel warmup/steady runs")
    if a["identity"] != b["identity"] or a["workload_sha256"] != b["workload_sha256"]:
        raise ValueError("software/GPU/model/settings/workload drift across restart")
    if (a["pod"]["uid"], a["pod"]["started_at"]) == (b["pod"]["uid"], b["pod"]["started_at"]):
        raise ValueError("no observed process restart")
    starts = [load(cold_start), load(warm_start)]
    for start, result in zip(starts, (a, b)):
        if (start["status"] != "observed-not-qualified" or start["pod"]["uid"] != result["pod"]["uid"]
                or start["pod"]["started_at"] != result["pod"]["started_at"]
                or not finite(start["elapsed_seconds"]) or start["elapsed_seconds"] < 0):
            raise ValueError("startup observation belongs to a different/failed process")
    checks = compare_quality(a, b, atol, rtol)
    before, after = load(Path(cold)/"compiler-after.json"), load(Path(warm)/"compiler-before.json")
    unchanged = 0
    for name, cache in before["caches"].items():
        other = after["caches"][name]
        if cache["root"] != other["root"]:
            raise ValueError("restart did not retain the same cache namespace")
        unchanged += sum(other["files"].get(path) == record for path, record in cache["files"].items())
    if not unchanged:
        raise ValueError("no retained identical compiler artifacts")
    # Logs are separately retained per TP worker. Files alone do not prove hits.
    tp = int(a["identity"]["runtime"]["settings"]["TENSOR_PARALLEL"])
    if len(logs) != tp or len({file_hash(path) for path in logs}) != tp:
        raise ValueError("provide distinct restart cache-hit logs for every TP rank")
    hits = []
    for path in logs:
        text = Path(path).read_text()
        count = len(re.findall(r"fx graph cache hit", text, re.I))
        if not count:
            raise ValueError("no explicit Inductor cache hit; persisted Triton files alone are inconclusive")
        hits.append({"sha256": file_hash(path), "hits": count})
    return {"schema": 1, "status": "reuse-observed-not-qualified", "identity": a["identity"],
            "workload_sha256": a["workload_sha256"], "quality": checks, "retained_files": unchanged,
            "operator_attributed_rank_logs": hits, "startup_seconds": [s["elapsed_seconds"] for s in starts],
            "warmup_seconds": [a["warmup_seconds"], b["warmup_seconds"]],
            "steady_state": [a["steady_state"], b["steady_state"]],
            "limitations": ["Startup includes model load/JIT/capture; not pure compilation time.",
                            "Rank-log attribution needs owner review. A single restart pair is not promotion evidence.",
                            "Disk/JIT cache state and prefix-cache state are distinct; no automatic cache deletion occurs."]}


def dispatch(trace, run):
    if run["status"] != "measured-not-qualified" or run.get("kind") != "kernel-profile":
        raise ValueError("trace requires a successful, identity-bound profile run")
    opener = gzip.open if str(trace).endswith(".gz") else open
    with opener(trace, "rt") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
        if len(raw) > 128 * 1024 * 1024:
            raise ValueError("trace exceeds bounded 128 MiB parser; reduce profiling steps")
        events = json.loads(raw)["traceEvents"]
    kernels, operators = {}, set()
    for event in events:
        name = event.get("name", "")
        if event.get("cat") == "kernel" and event.get("ph") == "X":
            duration = event.get("dur")
            if not finite(duration) or duration < 0 or not name:
                raise ValueError("invalid GPU kernel event")
            kernels[name] = kernels.get(name, 0) + duration
        if event.get("cat") in ("cpu_op", "hip_runtime", "cuda_runtime"):
            operators.add(name)
    if not kernels:
        raise ValueError("no actual GPU dispatch in trace; CPU-only profiling is insufficient")
    return {"schema": 1, "status": "dispatch-observed-not-qualified", "trace_sha256": file_hash(trace),
            "run_sha256": identity(run), "identity": run["identity"], "workload_sha256": run["workload_sha256"],
            "kernels_us": kernels, "host_operators": sorted(operators),
            "scope": "owner must verify trace belongs to recorded profile path/ranks; kernel names alone do not establish library ownership"}


def seal_tuning(run, observed, proof, artifact, kind, output):
    # A reviewed API/library log plus an exact observed kernel is required before
    # treating an offline tuning result as eligible for an experiment. No loader
    # override is enabled here, and a saved tuning file is not model qualification.
    if observed["run_sha256"] != identity(run) or observed["identity"] != run["identity"]:
        raise ValueError("dispatch/run provenance mismatch")
    record = load(proof)
    if record["backend"] != kind or record["kernel"] not in observed["kernels_us"]:
        raise ValueError("review must identify an actually dispatched kernel")
    filename = record["library_log"]
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", filename):
        raise ValueError("library log must be a direct sibling filename")
    log = Path(proof).parent / filename
    if log.is_symlink() or not log.is_file() or not 0 < log.stat().st_size <= 8 * 1024**2:
        raise ValueError("require a bounded regular dispatch log")
    if file_hash(log) != record["library_log_sha256"]:
        raise ValueError("library log changed")
    marker = "hipblasLtMatmul" if kind == "hipblaslt" else "triton"
    if marker not in log.read_text():
        raise ValueError("no library dispatch marker; do not infer tuning eligibility from GPU model")
    if (not isinstance(record.get("shape"), list) or not record["shape"]
            or any(type(n) is not int or n < 1 for n in record["shape"]) or not record.get("dtype")):
        raise ValueError("review must record the actual operation shape and dtype")
    if Path(artifact).is_symlink() or not 0 < Path(artifact).stat().st_size <= 64 * 1024**2:
        raise ValueError("require a bounded regular tuning result")
    output = Path(output)
    output.mkdir(mode=0o700)
    shutil.copyfile(artifact, output / "tuning-result")
    write(output / "manifest.json", {"schema": 1, "status": "eligible-experiment-not-qualified", "kind": kind,
          "identity": run["identity"], "workload_sha256": run["workload_sha256"],
          "dispatch_sha256": identity(observed), "review_sha256": file_hash(proof),
          "reviewed_operation": {key: record[key] for key in ("kernel", "shape", "dtype", "library_log_sha256")},
          "artifact_sha256": file_hash(output/"tuning-result"),
          "suggested_persistent_directory": "/cache/xdg/workstation-tuning/" + identity(run["identity"]),
          "scope": "operator-reviewed evidence, not automatic attribution or promotion; rerun numerical/memory/performance checks with this exact result"})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    for name in ("deployment", "workload", "evidence", "output"):
        p.add_argument(name)
    p.add_argument("--profile", choices=("cache", "capture", "legacy-compile", "gemm-m1", "bf16-full-b4"), required=True)
    p.add_argument("--workers", default="1,2")
    p.add_argument("--reserve-mib", type=int)
    p.add_argument("--worker-mib", type=int)
    p.add_argument("--reserve-cpus", type=float, default=2)
    p.add_argument("--experimental", action="store_true")
    p.add_argument("--gemm-image", help="built OCI image receipt for the packaged GEMM profile")
    p.add_argument("--gemm-observation", help="fresh runtime/source/device observation; default evidence/gemm-observation.json")
    p.add_argument("--graph-observation", help="fresh owner receipt bundle; default evidence/graph-observation.json")
    p = sub.add_parser("compare")
    for name in ("cold", "warm", "cold_startup", "warm_startup", "output"):
        p.add_argument(name)
    p.add_argument("--rank-log", action="append", required=True)
    p.add_argument("--atol", type=float, required=True)
    p.add_argument("--rtol", type=float, required=True)
    p = sub.add_parser("dispatch")
    for name in ("trace", "run", "output"):
        p.add_argument(name)
    p = sub.add_parser("quality")
    for name in ("baseline", "candidate", "output"):
        p.add_argument(name)
    p.add_argument("--atol", type=float, required=True)
    p.add_argument("--rtol", type=float, required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--memory-only", action="store_true")
    mode.add_argument("--experimental-kv", action="store_true")
    p = sub.add_parser("seal-tuning")
    for name in ("run", "dispatch", "review", "artifact", "output"):
        p.add_argument(name)
    p.add_argument("--kind", choices=("triton", "hipblaslt"), required=True)
    p = sub.add_parser("moe-table-package", help="offline experimental M=1 MoE table payload; never deploy")
    p.add_argument("output")
    p = sub.add_parser("rmsnorm-package", help="offline guarded no-residual RMSNorm source payload; never deploy")
    p.add_argument("output")
    p.add_argument("--source", required=True)
    p.add_argument("--profile", choices=("rmsnorm-no-residual", "rmsnorm-epilogue"), default="rmsnorm-no-residual")
    args = parser.parse_args()
    if args.mode == "plan":
        if args.profile == "bf16-full-b4":
            if (args.gemm_image or args.gemm_observation or args.reserve_mib is not None
                    or args.worker_mib is not None or args.workers != "1,2" or args.reserve_cpus != 2):
                parser.error("bf16-full-b4 does not accept GEMM image or compilation-worker controls")
            observation_path = Path(args.graph_observation or Path(args.evidence) / "graph-observation.json")
            try:
                observation = graph_profile.read_json(observation_path)
            except FileNotFoundError:
                observation = {}  # Explicit unchanged-incumbent fallback, never synthesize proof.
            result = graph_profile.plan(graph_profile.read_json(args.deployment), graph_profile.read_json(args.workload),
                                        observation, experimental=args.experimental)
            graph_profile.emit(args.output, result)
            return
        if args.graph_observation:
            parser.error("graph observation requires --profile bf16-full-b4")
        if args.profile == "gemm-m1":
            if not args.gemm_image:
                parser.error("gemm-m1 requires --gemm-image; no implicit image promotion")
            workload = load(args.workload)
            profile = load(gemm_profile.PROFILE_DIR / "profile.json")
            if (workload.get("schema") != 1 or workload.get("model_revision") != profile["model"]["revision"]
                    or workload.get("concurrency") != [1] or not workload.get("cases")
                    or len(workload["cases"]) > 3 or workload.get("prefix_state") not in ("new-prefix", "warm-prefix")
                    or type(workload.get("repetitions")) is not int or not 3 <= workload["repetitions"] <= 20
                    or type(workload.get("requests_per_worker")) is not int or not 1 <= workload["requests_per_worker"] <= 16
                    or any(type(case.get("context_tokens")) is not int or case["context_tokens"] > 4096
                           for case in workload["cases"])):
                raise ValueError("GEMM profile requires its pinned tokenizer workload at concurrency one within 4096 tokens")
            for case in workload["cases"]:
                ids = case.get("input_ids")
                if (not isinstance(ids, list) or not ids or any(type(token) is not int or token < 0 for token in ids)
                        or type(case.get("output_tokens")) is not int or not 1 <= case["output_tokens"] <= 2048
                        or len(ids) + case["output_tokens"] > case["context_tokens"]
                        or not re.fullmatch(r"[a-f0-9]{64}", str(case.get("source_sha256")))):
                    raise ValueError("GEMM workload requires bounded native token IDs and the pinned corpus identity")
            observation = load(args.gemm_observation or Path(args.evidence) / "gemm-observation.json")
            deployment = load(args.deployment)
            pod = deployment["spec"]["template"]["spec"]
            targets = [row for row in pod["containers"] if row.get("name") == "sglang"]
            if len(targets) == 1:
                target = targets[0]
                if any(row.get("name") == "SGLANG_MOE_CONFIG_DIR" for row in target.get("env", [])):
                    raise ValueError("GEMM-only profile must not inherit a MoE config-directory override")
                table_paths = {"/" + gemm_profile.SITE + gemm_profile.TABLE_DIR + row["name"] for row in profile["tables"]}
                tuning_volumes = {row["name"] for row in target.get("volumeMounts", []) if row.get("mountPath") in table_paths}
                for group in ("containers", "initContainers", "ephemeralContainers"):
                    for container in pod.get(group, []):
                        for mount in container.get("volumeMounts", []):
                            if (mount.get("name") in tuning_volumes
                                    and not (container is target and mount.get("mountPath") in table_paths)):
                                raise ValueError("GEMM tuning-table volumes must be dedicated; shared mounts cannot be removed")
                        if any(device.get("name") in tuning_volumes for device in container.get("volumeDevices", [])):
                            raise ValueError("GEMM tuning-table volumes must not be shared with volume devices")
            result = gemm_profile.plan(deployment, observation, load(args.gemm_image))
            result["workload_sha256"] = identity(workload)
            output = Path(args.output)
            output.mkdir(mode=0o700)
            for name, value in (("plan.json", result), ("gemm-m1.json", result["patch"]), ("incumbent-rollback.json", result["rollback"])):
                write(output / name, value)
            return
        if args.reserve_mib is None or args.worker_mib is None:
            parser.error("compilation profiles require --reserve-mib and --worker-mib")
        if args.gemm_image or args.gemm_observation:
            parser.error("GEMM image/observation inputs require --profile gemm-m1")
        result = plan(load(args.deployment), load(args.workload), evidence(args.evidence), args.profile,
                      [int(v) for v in args.workers.split(",")], args.reserve_mib, args.worker_mib, args.reserve_cpus, args.experimental)
        output = Path(args.output)
        output.mkdir(mode=0o700)
        write(output/"plan.json", result)
        for name, case in result["cases"].items():
            write(output/(name + ".json"), case["patch"])
    elif args.mode == "compare":
        write(args.output, compare(args.cold, args.warm, args.cold_startup, args.warm_startup, args.rank_log, args.atol, args.rtol))
    elif args.mode == "dispatch":
        write(args.output, dispatch(args.trace, load(args.run)))
    elif args.mode == "quality":
        write(args.output, compare_quality(load(args.baseline), load(args.candidate), args.atol, args.rtol,
                                           args.memory_only, args.experimental_kv))
    elif args.mode == "moe-table-package":
        result = moe_table_package(args.output)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    elif args.mode == "rmsnorm-package":
        print(json.dumps(rmsnorm_package(args.output, args.source, args.profile), sort_keys=True, allow_nan=False))
    else:
        seal_tuning(load(args.run), load(args.dispatch), args.review, args.artifact, args.kind, args.output)


if __name__ == "__main__":
    main()
