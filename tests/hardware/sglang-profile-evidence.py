"""Read only this experiment's bounded, per-rank exported Kineto traces.

Executed through the existing exact-Pod exec path, never an arbitrary host path.
Source contract hashes identify the reviewed image's default (not V2) profiler.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys


CONTRACT = {
    "srt/managers/scheduler_components/profiler_manager.py": "b9ba3fdf3594da226e2da92a8340dab91889dc971ca5ece6723a49065d002640",
    "srt/managers/tokenizer_control_mixin.py": "26a2dda02cfc0f1282ddad47145967b85a1444ee783ee3267ddb84b3bca35f3a",
    "srt/entrypoints/http_server.py": "cffbcbc30858ab6bfed2d3904051fc2f2a915fec7af1a143b93902c145dc245d",
}


def read_trace(path):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 128*1024**2:
        raise ValueError("missing/unsafe/oversized rank trace")
    before = path.stat()
    with gzip.open(path, "rb") as stream:
        data = stream.read(128*1024**2 + 1)
    if len(data) > 128*1024**2:
        raise ValueError("decompressed trace exceeds budget")
    events = json.loads(data)["traceEvents"]
    if not any(e.get("cat") == "kernel" and e.get("ph") == "X"
               and type(e.get("dur")) in (float, int) and 0 < e["dur"] < float("inf") for e in events):
        raise ValueError("trace contains no timed GPU kernels")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("trace changed during verification")
    return {"name": path.name, "sha256": digest, "bytes": after.st_size}


def traces(directory, ranks):
    root = Path(directory)
    if (not re.fullmatch(r"/cache/xdg/workstation-profiles/[a-f0-9]{32}", str(root))
            or root.resolve() != root or ranks not in (1, 2)):
        raise ValueError("invalid owned profile directory or TP count")
    result = {str(rank): read_trace(root / f"{root.name}-TP-{rank}.trace.json.gz") for rank in range(ranks)}
    return {"schema": 1, "profile_path": str(root), "traces": result}


def main():
    import sglang
    from sglang.srt.environ import envs
    if envs.SGLANG_PROFILE_V2.get():
        raise ValueError("V2 profiler is not the reviewed contract")
    source = Path(sglang.__file__).parent
    for name, expected in CONTRACT.items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest() != expected:
            raise ValueError("profiler source contract drift")
    if sys.argv[1:] == ["contract"]:
        print(json.dumps({"schema": 1, "sources": CONTRACT, "profile_v2": False}))
    else:
        print(json.dumps(traces(sys.argv[1], int(sys.argv[2]))))


if __name__ == "__main__":
    os.umask(0o077)
    main()
