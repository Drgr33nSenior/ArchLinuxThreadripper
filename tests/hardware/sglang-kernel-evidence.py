"""Read-only, bounded compiler/cache observation inside the selected SGLang Pod.

Run after sglang-evidence.py. This does not import models or compile a graph.
Cache files and loaded-library hashes are private evidence, never executable input.
"""
import hashlib
import importlib.metadata
import json
import os
from datetime import datetime, timezone
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


def storage_observation(value, allowed_roots, observed_at):
    """Read only the mounted model/cache filesystem without path fallback."""
    if not isinstance(value, str) or not value:
        return {"status": "unknown", "reason": "configured storage root is absent"}
    raw = Path(value)
    bases = tuple(Path(base) for base in allowed_roots)
    # Reject lexical escapes before probing a caller-selected filesystem.
    if (not raw.is_absolute() or not bases or any(not base.is_absolute() for base in bases)
            or not any(raw == base or base in raw.parents for base in bases)):
        return {"status": "unknown", "reason": "configured storage root is outside the reviewed mount"}
    try:
        # Mounted roots and the configured root must themselves have canonical,
        # non-symlink ancestry.  Do not turn a reviewed lexical mount into an
        # unreviewed resolved destination.
        if any(base.is_symlink() or base.resolve(strict=True) != base for base in bases):
            return {"status": "unknown", "reason": "reviewed storage mount is symlinked or unavailable"}
        if raw.is_symlink() or raw.resolve(strict=True) != raw or not raw.is_dir():
            return {"status": "unknown", "reason": "configured storage root is unsafe or unavailable"}
        before = raw.stat(follow_symlinks=False)
        usage = os.statvfs(raw)
        after = raw.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return {"status": "unknown", "reason": "configured storage root cannot be observed"}
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or usage.f_frsize <= 0):
        return {"status": "unknown", "reason": "configured storage root changed or has invalid filesystem data"}
    total, available = usage.f_frsize * usage.f_blocks, usage.f_frsize * usage.f_bavail
    if total <= 0 or available < 0 or available > total:
        return {"status": "unknown", "reason": "configured storage filesystem counters are invalid"}
    return {"status": "observed", "path": str(raw), "filesystem_device": before.st_dev,
            "root_inode": before.st_ino, "total_bytes": total, "available_bytes": available,
            "observed_at": observed_at,
            "scope": "read-only statvfs snapshot; not sampled during model loading or a pure loader timer"}


def runtime_capabilities(source, flags, sources):
    """Record only source/help-proven candidate controls from this exact image.

    A current web manual cannot prove an option or JSON key exists in the
    immutable image.  The offline planners fail closed unless both the exact
    help output and the relevant hashed source fragments establish it.
    """
    server_args = source / "srt/server_args.py"
    try:
        server_text = server_args.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        server_text = ""
    found = {}
    for path in sorted(set((source / "srt/model_loader").rglob("*.py")) |
                       set((source / "srt/managers").rglob("*.py")) |
                       set((source / "srt").glob("*scheduler*.py"))):
        try:
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        found[str(path.relative_to(source))] = {"sha256": digest(path), "text": text}

    def capability(name, flag, key, server_marker, source_markers, directories, enable_key=None):
        matches = {path: item["sha256"] for path, item in found.items()
                   if any(path.startswith(directory) for directory in directories) and
                   all(marker in item["text"] for marker in source_markers)}
        if flag in flags and server_marker in server_text and matches:
            result = {"status": "supported-source-contract", "flag": flag, "key": key, "sources": matches}
            if enable_key:
                result["enable_key"] = enable_key
            return result
        reasons = []
        if flag not in flags:
            reasons.append("flag absent from exact-image help")
        if server_marker not in server_text:
            reasons.append("server argument source does not expose the control")
        if not matches:
            reasons.append("runtime implementation source does not expose the control")
        return {"status": "unsupported", "reason": "; ".join(reasons), "flag": flag, "key": key,
                "sources": matches}

    return {
        "model_loader_threads": capability("model_loader_threads", "--model-loader-extra-config", "num_threads",
                                             "model_loader_extra_config", ("num_threads", "enable_multithread_load"),
                                             ("srt/model_loader/",), "enable_multithread_load"),
        "bounded_queue": capability("bounded_queue", "--max-queued-requests", "maximum_queued_requests",
                                     "max_queued_requests", ("max_queued_requests",), ("srt/managers/", "srt/")),
        "interactive_priority": {"status": "unsupported-current-client-path",
                                 "reason": "existing private clients do not convey a reviewed engine priority field"},
    }


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
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    storage = {"schema": 1, "observed_at": observed_at, "roots": {
        "model": storage_observation(os.environ.get("MODEL_PATH"), (Path("/models"),), observed_at),
        "triton": storage_observation(os.environ.get("TRITON_CACHE_DIR"), (Path("/cache/triton"),), observed_at),
        "torchinductor": storage_observation(os.environ.get("TORCHINDUCTOR_CACHE_DIR"), (Path("/cache/torchinductor"),), observed_at)}}
    settings = {key: os.environ.get(key) for key in (
        "TORCHINDUCTOR_COMPILE_THREADS", "TORCHINDUCTOR_FX_GRAPH_CACHE", "TORCH_LOGS",
        "SGLANG_TORCH_COMPILE_MODE", "HIPBLASLT_TUNING_OVERRIDE_FILE",
        "MODEL_PATH", "TRITON_CACHE_DIR", "TORCHINDUCTOR_CACHE_DIR")}
    print(json.dumps({"schema": 1, "status": "observed-not-qualified", "python": platform.python_version(),
        "host_kernel": platform.release(),
        "amdgpu_srcversion": Path("/sys/module/amdgpu/srcversion").read_text().strip()
            if Path("/sys/module/amdgpu/srcversion").is_file() else None,
        "packages": packages, "hip": torch.version.hip, "compiler": compiler_record,
        "sources": sources, "help_sha256": hashlib.sha256(help_run.stdout.encode()).hexdigest(), "flags": flags,
        "runtime_capabilities": runtime_capabilities(source, flags, sources),
        "compile_threads_supported": hasattr(config, "compile_threads"),
        "helper_compile_threads": config.compile_threads, "settings": settings,
        "loaded_libraries": libraries, "cgroup": cgroup, "affinity_cpus": len(os.sched_getaffinity(0)),
        "caches": caches, "storage": storage,
        "scope": "helper configuration is not proof of server compilation; inspect effective launch and profiler/cache-hit evidence"}, indent=2))


if __name__ == "__main__":
    main()
