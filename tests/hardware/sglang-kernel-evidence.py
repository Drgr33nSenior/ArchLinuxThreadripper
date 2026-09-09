"""Read-only, bounded compiler/cache observation inside the selected SGLang Pod.

Run after sglang-evidence.py. This does not import models or compile a graph.
Cache files and loaded-library hashes are private evidence, never executable input.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cache_tree(path):
    root = Path(path)
    if not root.is_absolute() or root.is_symlink() or not any(
            root == Path(base) or Path(base) in root.parents
            for base in ("/cache/triton", "/cache/torchinductor")):
        raise ValueError("cache must stay inside the existing compiler-cache mount")
    files, total = {}, 0
    if not root.exists():
        return {"root": str(root), "files": files, "bytes": 0}
    if root.resolve() != root:
        raise ValueError("cache parent is a symlink")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("cache contains an external/mutable symlink")
        if not path.is_file():
            continue
        stat = path.stat()
        total += stat.st_size
        if total > 20 * 1024**3 or len(files) >= 50000:
            raise ValueError("cache evidence exceeds 20 GiB/50000 files; review retention first")
        checksum = digest(path)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("cache changed while hashing; retry during an idle window")
        files[str(path.relative_to(root))] = {"sha256": checksum, "bytes": stat.st_size}
    return {"root": str(root), "files": files, "bytes": total}


def main():
    import sglang
    import torch
    import torch._inductor.config as config
    source = Path(sglang.__file__).parent
    sources = {}
    for name in ("srt/server_args.py", "srt/compilation/torch_compile_decoration.py",
                 "srt/model_executor/runner/base_runner.py", "srt/models/qwen3_5.py"):
        path = source / name
        sources[name] = digest(path) if path.is_file() else None
    # Help must execute in this exact image; source presence alone is not a CLI test.
    help_run = subprocess.run([sys.executable, "-m", "sglang.launch_server", "--help"],
                              capture_output=True, text=True, timeout=90)
    if help_run.returncode:
        raise RuntimeError("exact-image SGLang help failed; profile generation is blocked")
    flags = sorted(set(re.findall(r"--[a-z][a-z0-9-]+", help_run.stdout)))
    packages = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name", "")
        if re.search(r"torch|triton|rocm|aiter|sglang|sgl.kernel|transformers", name, re.I):
            packages[name] = dist.version
    sdk = Path(os.environ.get("ROCM_HOME", "/opt/rocm"))
    compiler = next((str(path) for path in (sdk/"llvm/bin/amdclang++", sdk/"llvm/bin/clang++")
                     if path.is_absolute() and path.is_file() and os.access(path, os.X_OK)), None)
    compiler = compiler or shutil.which("amdclang++") or shutil.which("clang++")
    compiler_record = None
    if compiler:
        version = subprocess.run([compiler, "--version"], capture_output=True, text=True, timeout=15)
        if version.returncode == 0:
            compiler_record = {"path": compiler, "sha256": digest(Path(compiler)), "version": version.stdout}
    libraries = {}
    # Maps of server/TP processes, not the helper's own imports. Never dump environ.
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes()
            if not any(value in cmd for value in (b"sglang", b"sgl_scheduler")):
                continue
            for line in (proc / "maps").read_text().splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and re.search(r"/(?:lib)?[^/]*(?:hip|rocblas|hsa|torch|triton)[^/]*\.so", fields[5], re.I):
                    path = Path(fields[5])
                    if path.is_file() and str(path) not in libraries:
                        libraries[str(path)] = {"resolved": str(path.resolve()), "sha256": digest(path)}
        except (PermissionError, ProcessLookupError, FileNotFoundError):
            continue
    cgroup = {}
    for name in ("cpu.max", "cpuset.cpus.effective", "memory.max", "memory.current", "memory.peak", "memory.events"):
        path = Path("/sys/fs/cgroup") / name
        cgroup[name] = path.read_text().strip() if path.is_file() else None
    caches = {key: cache_tree(os.environ[key]) for key in ("TRITON_CACHE_DIR", "TORCHINDUCTOR_CACHE_DIR")}
    settings = {key: os.environ.get(key) for key in (
        "TORCHINDUCTOR_COMPILE_THREADS", "TORCHINDUCTOR_FX_GRAPH_CACHE", "TORCH_LOGS",
        "SGLANG_TORCH_COMPILE_MODE", "HIPBLASLT_TUNING_OVERRIDE_FILE")}
    print(json.dumps({"schema": 1, "status": "observed-not-qualified", "python": platform.python_version(),
        "host_kernel": platform.release(),
        "amdgpu_srcversion": Path("/sys/module/amdgpu/srcversion").read_text().strip()
            if Path("/sys/module/amdgpu/srcversion").is_file() else None,
        "packages": packages, "hip": torch.version.hip, "compiler": compiler_record,
        "sources": sources, "help_sha256": hashlib.sha256(help_run.stdout.encode()).hexdigest(), "flags": flags,
        "compile_threads_supported": hasattr(config, "compile_threads"),
        "helper_compile_threads": config.compile_threads, "settings": settings,
        "loaded_libraries": libraries, "cgroup": cgroup, "affinity_cpus": len(os.sched_getaffinity(0)),
        "caches": caches,
        "scope": "helper configuration is not proof of server compilation; inspect effective launch and profiler/cache-hit evidence"}, indent=2))


if __name__ == "__main__":
    main()
