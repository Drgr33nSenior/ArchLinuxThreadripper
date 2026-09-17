"""Run inside the selected SGLang container. Emit only allowlisted evidence."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import stat


LAUNCH_SCALARS = frozenset((
    "--model-path", "--revision", "--served-model-name", "--dtype", "--tp", "--tp-size",
    "--context-length", "--mem-fraction-static", "--max-running-requests", "--max-queued-requests",
    "--model-loader-extra-config", "--schedule-policy", "--chunked-prefill-size", "--attention-backend",
    "--stream-interval", "--torch-compile-max-bs", "--cuda-graph-backend-decode",
    "--cuda-graph-backend-prefill", "--cuda-graph-tc-compiler", "--kv-cache-dtype", "--page-size",
    "--max-total-tokens", "--quantization", "--speculative-algorithm", "--speculative-num-steps",
    "--speculative-eagle-topk", "--speculative-num-draft-tokens", "--speculative-draft-model-path",
    "--speculative-draft-model-revision", "--speculative-draft-model-quantization",
    "--speculative-adaptive-config", "--mamba-ssm-dtype", "--mamba-full-memory-ratio",
    "--max-mamba-cache-size", "--mamba-radix-cache-strategy",
    "--linear-attn-decode-backend", "--linear-attn-prefill-backend",
))
LAUNCH_BOOLEANS = frozenset(("--enable-torch-compile", "--disable-overlap-schedule",
                             "--speculative-adaptive", "--disable-radix-cache"))
LAUNCH_LISTS = frozenset(("--cuda-graph-bs-decode", "--cuda-graph-bs-prefill"))


def observed_launch(args):
    """Project actual argv, not effective defaults; refuse ambiguous known options.

    This helper is streamed into a container on its own. The offline consumer
    maintains its own validation boundary; a regression test checks their sets.
    Unknown arguments (including authentication) are never returned.
    """
    observed = {}
    index = 0
    while index < len(args):
        argument = args[index]
        option, equals, value = argument.partition("=")
        index += 1
        if option not in LAUNCH_SCALARS | LAUNCH_BOOLEANS | LAUNCH_LISTS:
            continue
        if option in observed:
            raise RuntimeError(f"observed SGLang argument is duplicated: {option}")
        if option in LAUNCH_BOOLEANS:
            if equals:
                raise RuntimeError(f"observed boolean SGLang argument is malformed: {option}")
            observed[option] = True
            continue
        if not equals:
            if index >= len(args) or not args[index] or args[index].startswith("--"):
                raise RuntimeError(f"observed SGLang argument is malformed: {option}")
            value = args[index]
            index += 1
        if not value or len(value) > 4096:
            raise RuntimeError(f"observed SGLang argument is malformed: {option}")
        if option in LAUNCH_LISTS:
            values = [value]
            while index < len(args) and args[index] and not args[index].startswith("--"):
                values.append(args[index])
                index += 1
            if len(values) > 256 or any(not item.isdigit() or int(item) < 1 for item in values):
                raise RuntimeError(f"observed SGLang list argument is malformed: {option}")
            observed[option] = values
        else:
            observed[option] = value
    observed.setdefault("--enable-torch-compile", False)
    return observed


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def experiment_artifacts(launch, model, model_files, models_root=Path("/models")):
    """Bind staged draft/config bytes; never follow a URL or read outside /models."""
    def discovery_error(error):
        raise error

    result = {}
    for option in ("--speculative-draft-model-path", "--speculative-adaptive-config"):
        if option not in launch:
            continue
        path = Path(launch[option])
        if (not path.is_absolute() or path.resolve() != path or path.is_symlink()
                or not (path == models_root or models_root in path.parents)):
            raise RuntimeError("experiment artifact must be staged without symlinks under /models")
        if option == "--speculative-draft-model-path" and path == model:
            files = model_files
        else:
            if option == "--speculative-draft-model-path":
                if not path.is_dir():
                    raise RuntimeError("staged draft directory is unavailable")
                paths = (Path(directory) / name
                         for directory, directories, filenames in os.walk(path, onerror=discovery_error, followlinks=False)
                         for name in directories + filenames)
            else:
                if not path.is_file() or path.stat().st_size > 64 * 1024:
                    raise RuntimeError("adaptive configuration must be a bounded staged file")
                paths = (path,)
            files = {}
            for item in paths:
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError("experiment artifact contains a symlink")
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or len(files) >= 2048:
                    raise RuntimeError("experiment artifact is not a bounded regular-file tree")
                checksum = digest(item)
                after = item.stat()
                if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise RuntimeError("experiment artifact changed during hashing")
                name = item.name if item == path else str(item.relative_to(path))
                files[name] = {"bytes": info.st_size, "sha256": checksum}
        if len(files) > 2048:
            raise RuntimeError("experiment artifact is not a bounded regular-file tree")
        if not files or (option == "--speculative-draft-model-path" and not any(name.endswith(".safetensors") for name in files)):
            raise RuntimeError("experiment artifact inventory is incomplete")
        result[option] = {"path": str(path), "files": files}
    return result


def process_start_ticks(pid):
    """Read Linux /proc field 22 without trusting a reusable PID."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    end = raw.rfind(")")
    fields = raw[end + 2:].split()
    if end < 0 or len(fields) <= 19 or not fields[19].isdigit() or int(fields[19]) < 1:
        raise RuntimeError("process start ticks are malformed")
    return int(fields[19])


def parent_pid(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("PPid:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
    raise RuntimeError("process parent identity is unavailable")


def launcher_owns_worker(worker, launcher):
    """Accept the launcher itself or a bounded descendant relationship."""
    current = worker
    for _ in range(64):
        if current == launcher:
            return True
        if current <= 1:
            return False
        current = parent_pid(current)
    return False


def mapped_uq_binary(pid):
    """Hash the exact mapped package-local backend extension, not a PATH entry."""
    matches = {}
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6 or not fields[5].startswith("/") or fields[5].endswith(" (deleted)"):
            continue
        path = Path(fields[5])
        if path.name != "libuq_hip.so":
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise RuntimeError("mapped experimental KV binary is unsafe")
        identity = (info.st_dev, info.st_ino)
        if identity in matches and matches[identity][0] != path:
            raise RuntimeError("mapped experimental KV binary has conflicting paths")
        matches[identity] = (path, digest(path))
    if len(matches) != 1:
        raise RuntimeError("expected exactly one mapped experimental KV binary")
    return next(iter(matches.values()))[1]


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate_descriptor(value, model_config):
    required = {"schema", "runtime", "source_repository", "source_revision", "source_tree_sha256",
                "patch_sha256", "binary_sha256", "compiler_revision", "dependencies_sha256", "codec",
                "configuration_sha256", "configuration", "requested_mode", "observed_mode", "qualification",
                "restart_required"}
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("experimental KV receipt descriptor keys are malformed")
    config = value["configuration"]
    config_keys = {"gpu_arch", "tensor_parallel", "compute_dtype", "attention", "head_dimension",
                   "kv_heads", "query_heads", "page_size", "rotation"}
    hashes = ("source_tree_sha256", "patch_sha256", "binary_sha256", "dependencies_sha256", "configuration_sha256")
    if (type(value["schema"]) is not int or value["schema"] != 1 or value["runtime"] != "sglang" or value["source_repository"] != "sgl-project/sglang"
            or not isinstance(value["source_revision"], str) or not re.fullmatch(r"[a-f0-9]{40}", value["source_revision"])
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]) for key in hashes)
            or not isinstance(value["compiler_revision"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+:/-]{0,239}", value["compiler_revision"])
            or value["codec"] != "ultraquant-rdna4-v1" or value["requested_mode"] != "ultraquant-rdna4-v1"
            or value["observed_mode"] != "compressed-gpu" or value["qualification"] != "unqualified"
            or value["restart_required"] is not True or not isinstance(config, dict) or set(config) != config_keys
            or config["gpu_arch"] != "gfx1201" or type(config["tensor_parallel"]) is not int or config["tensor_parallel"] != 1
            or config["compute_dtype"] != "float16" or config["attention"] != "causal-full"
            or type(config["head_dimension"]) is not int or config["head_dimension"] not in (32, 64, 128, 256)
            or type(config["kv_heads"]) is not int or not 1 <= config["kv_heads"] <= 64
            or type(config["query_heads"]) is not int or not config["kv_heads"] <= config["query_heads"] <= 128
            or config["query_heads"] % config["kv_heads"] or type(config["page_size"]) is not int or config["page_size"] != 1
            or config["rotation"] != "post-rope-qk" or canonical_digest(config) != value["configuration_sha256"]):
        raise RuntimeError("experimental KV receipt descriptor values are malformed")
    if model_config.get("architectures") != ["LlamaForCausalLM"] or model_config.get("quantization_config"):
        raise RuntimeError("experimental KV receipt model architecture is unsupported")
    heads, kv_heads = model_config.get("num_attention_heads"), model_config.get("num_key_value_heads")
    head_dimension = model_config.get("head_dim")
    if head_dimension is None and type(model_config.get("hidden_size")) is int and type(heads) is int and heads:
        head_dimension = model_config["hidden_size"] // heads
    if (type(heads) is not int or type(kv_heads) is not int or type(head_dimension) is not int
            or config["query_heads"] != heads or config["kv_heads"] != kv_heads or config["head_dimension"] != head_dimension):
        raise RuntimeError("experimental KV receipt configuration does not match model")


def experimental_kv_receipt(launches, model_config):
    candidates = [(pid, launch) for pid, launch in launches
                  if launch.get("--attention-backend") == "ultraquant-rdna4-v1"]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError("experimental KV collection requires one observed SGLang launcher")
    launcher, _launch = candidates[0]
    directory = Path("/tmp/sglang-experimental-kv")
    info = directory.lstat()
    launcher_uid = Path(f"/proc/{launcher}").stat().st_uid
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != launcher_uid
            or info.st_mode & 0o077):
        raise RuntimeError("experimental KV receipt directory is unsafe")
    receipts = []
    launcher_ticks = process_start_ticks(launcher)
    for path in directory.glob("*.json"):
        if not re.fullmatch(r"[1-9][0-9]*\.json", path.name):
            raise RuntimeError("experimental KV receipt directory has an unexpected entry")
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != launcher_uid
                or info.st_mode & 0o077 or not 1 <= info.st_size <= 16 * 1024):
            raise RuntimeError("experimental KV receipt is unsafe")
        value = json.loads(path.read_text())
        required = {"schema", "pid", "start_ticks", "launcher_pid", "launcher_start_ticks", "descriptor"}
        if (not isinstance(value, dict) or set(value) != required or value["schema"] != 1
                or any(type(value[key]) is not int or value[key] < 1 for key in
                       ("pid", "start_ticks", "launcher_pid", "launcher_start_ticks"))
                or path.stem != str(value["pid"])):
            raise RuntimeError("experimental KV receipt process binding is malformed")
        # Keep prior launcher receipts for recovery/audit, but never bind them
        # to this launch. A matching launcher identity is still strict below.
        if value["launcher_pid"] != launcher or value["launcher_start_ticks"] != launcher_ticks:
            continue
        if (not launcher_owns_worker(value["pid"], launcher) or value["start_ticks"] != process_start_ticks(value["pid"])
                or Path(f"/proc/{value['pid']}").stat().st_uid != launcher_uid):
            raise RuntimeError("experimental KV receipt process binding is malformed")
        validate_descriptor(value["descriptor"], model_config)
        if mapped_uq_binary(value["pid"]) != value["descriptor"]["binary_sha256"]:
            raise RuntimeError("experimental KV mapped binary digest differs from receipt")
        receipts.append(value["descriptor"])
    if len(receipts) != 1:
        raise RuntimeError("experimental KV requested backend needs one successful receipt")
    return receipts[0]


def main():
    keys = ("MODEL_PATH", "MODEL_REVISION", "MODEL_REPOSITORY", "SERVED_MODEL_NAME", "MODEL_DTYPE",
            "CONTEXT_LENGTH", "TENSOR_PARALLEL", "MEM_FRACTION_STATIC", "MAX_RUNNING_REQUESTS",
            "ATTENTION_BACKEND", "SGLANG_USE_AITER", "SGLANG_USE_AITER_RMSNORM",
            "SGLANG_USE_AITER_AR", "SGLANG_ROCM_FUSED_DECODE_MLA",
            "HSA_OVERRIDE_CPU_AFFINITY_DEBUG", "TRITON_HIP_USE_BLOCK_PINGPONG",
            "TORCHINDUCTOR_COMPILE_THREADS", "TORCHINDUCTOR_FX_GRAPH_CACHE", "TORCH_LOGS",
            "TRITON_CACHE_DIR", "TORCHINDUCTOR_CACHE_DIR", "SGLANG_TORCH_COMPILE_MODE",
            "HIPBLASLT_TUNING_OVERRIDE_FILE")
    settings = {key: os.environ.get(key) for key in keys}
    model = Path(settings["MODEL_PATH"] or "")
    if not model.is_absolute() or not model.is_dir() or not settings["MODEL_REVISION"]:
        raise RuntimeError("MODEL_PATH and MODEL_REVISION must identify staged model files")
    files = {}
    for path in sorted(model.rglob("*")):
        if path.is_file():
            if path.is_symlink():
                raise RuntimeError("stage regular verified model files, not mutable external symlinks")
            files[str(path.relative_to(model))] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    if not any(name.endswith(".safetensors") for name in files):
        raise RuntimeError("model has no safetensors weights")
    config = json.loads((model / "config.json").read_text())
    model_contract = {"architectures": config.get("architectures"), "model_type": config.get("model_type"),
                      "quant_method": (config.get("quantization_config") or {}).get("quant_method") or "unquantized"}
    packages = {}
    for name in ("sglang", "torch", "triton", "pytorch-triton-rocm", "aiter", "transformers"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    import torch
    if not torch.version.hip or not torch.cuda.is_available():
        raise RuntimeError("container does not have functional ROCm PyTorch")
    devices = []
    for index in range(torch.cuda.device_count()):
        prop = torch.cuda.get_device_properties(index)
        devices.append({"index": index, "name": prop.name, "gfx": prop.gcnArchName,
                        "bytes": prop.total_memory, "uuid": str(getattr(prop, "uuid", "unknown"))})
    if not devices or any(not d["gfx"].split(":")[0] == "gfx1201" for d in devices):
        raise RuntimeError("allocated devices do not report gfx1201")
    launch, launch_processes = [], []
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = process.read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if "sglang.launch_server" in args:
            observed = observed_launch(args)
            launch.append(observed)
            launch_processes.append((int(process.parent.name), observed))
    result = {"schema": 1, "settings": settings, "packages": packages,
                      "hip": torch.version.hip, "devices": devices, "launch": launch,
                      "model_files": files, "model_contract": model_contract}
    if len(launch) == 1:
        artifacts = experiment_artifacts(launch[0], model, files)
        if artifacts:
            result["experiment_artifacts"] = artifacts
    descriptor = experimental_kv_receipt(launch_processes, config)
    if descriptor is not None:
        result["experimental_kv"] = descriptor
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
