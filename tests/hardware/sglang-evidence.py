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
            "HSA_OVERRIDE_CPU_AFFINITY_DEBUG", "TRITON_HIP_USE_BLOCK_PINGPONG")
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
               "--context-length", "--mem-fraction-static", "--max-running-requests",
               "--chunked-prefill-size", "--attention-backend", "--stream-interval"}
    launch = []
    for process in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = process.read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if "sglang.launch_server" in args:
            launch.append({a: args[i + 1] for i, a in enumerate(args[:-1]) if a in allowed})
    print(json.dumps({"schema": 1, "settings": settings, "packages": packages,
                      "hip": torch.version.hip, "devices": devices, "launch": launch,
                      "model_files": files}, indent=2))


if __name__ == "__main__":
    main()
