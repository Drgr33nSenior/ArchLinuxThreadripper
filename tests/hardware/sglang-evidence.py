"""Run inside the selected SGLang container. Emit only allowlisted evidence."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


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
                      "quant_method": (config.get("quantization_config") or {}).get("quant_method")}
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
    allowed = {"--model-path", "--revision", "--served-model-name", "--dtype", "--tp", "--tp-size",
               "--context-length", "--mem-fraction-static", "--max-running-requests", "--max-queued-requests",
               "--model-loader-extra-config", "--schedule-policy",
               "--chunked-prefill-size", "--attention-backend", "--stream-interval", "--torch-compile-max-bs",
               "--cuda-graph-backend-decode", "--cuda-graph-backend-prefill", "--cuda-graph-tc-compiler"}
    launch = []
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = process.read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if "sglang.launch_server" in args:
            observed = {}
            for index, argument in enumerate(args[:-1]):
                if argument in allowed:
                    if index + 1 >= len(args) or not args[index + 1] or args[index + 1].startswith("--"):
                        raise RuntimeError(f"observed SGLang argument is malformed: {argument}")
                    if argument in observed:
                        raise RuntimeError(f"observed SGLang argument is duplicated: {argument}")
                    observed[argument] = args[index + 1]
                elif "=" in argument:
                    option, value = argument.split("=", 1)
                    if option in allowed:
                        if not value or option in observed:
                            raise RuntimeError(f"observed SGLang argument is malformed or duplicated: {option}")
                        observed[option] = value
            observed["--enable-torch-compile"] = "--enable-torch-compile" in args
            for option in ("--cuda-graph-bs-decode", "--cuda-graph-bs-prefill"):
                if option in args:
                    values = []
                    for value in args[args.index(option)+1:]:
                        if not value or value.startswith("--"):
                            break
                        values.append(value)
                    observed[option] = values
            launch.append(observed)
    print(json.dumps({"schema": 1, "settings": settings, "packages": packages,
                      "hip": torch.version.hip, "devices": devices, "launch": launch,
                      "model_files": files, "model_contract": model_contract}, indent=2))


if __name__ == "__main__":
    main()
