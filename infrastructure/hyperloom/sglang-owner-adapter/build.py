"""Build a private, unqualified Hyperloom owner-backend source tree.

Does not install, execute Hyperloom, change the stock gfx12 launch gate, or
authorize native work. The pinned upstream files and local delta are retained.
"""

import argparse
import base64
import csv
import difflib
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import shutil


HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("workstation_pinned_sdk_patch", HERE / "patch_sdk.py")
patch_sdk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch_sdk)
REPOSITORY = HERE.parents[2]
MODULES = ("hyperloom_sglang.py", "hyperloom_latency.py", "hyperloom_exchange.py",
           "hyperloom_owner_backend.py", "hyperloom_sdk_adapter.py", "hyperloom_owner_profile.py",
           "hyperloom_chatgpt.py", "hyperloom_owner_baseline.py")
BACKEND = "hyperloom/orchestrator/actions/executors/benchmark_backend.py"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def build(upstream, destination):
    upstream, destination = Path(upstream), Path(destination)
    if not upstream.is_absolute() or upstream.resolve() != upstream or not upstream.is_dir():
        raise ValueError("canonical extracted upstream wheel required")
    if not destination.is_absolute() or destination.resolve() != destination or destination.exists():
        raise ValueError("fresh canonical adapter destination required")
    lock = json.loads((HERE / "UPSTREAM.json").read_bytes())
    members = [path for path in upstream.rglob("*") if path.is_file()]
    if any(path.is_symlink() for path in upstream.rglob("*")):
        raise ValueError("linked upstream member")
    if len(members) > 10000 or sum(path.stat().st_size for path in members) > 256 << 20:
        raise ValueError("upstream tree exceeds bounds")
    record_name = lock["record"]["path"]
    record_raw = (upstream / record_name).read_bytes()
    if sha(record_raw) != lock["record"]["sha256"]:
        raise ValueError("upstream wheel RECORD drift")
    recorded = set()
    for name, digest, size in csv.reader(io.StringIO(record_raw.decode())):
        part = PurePosixPath(name)
        if part.is_absolute() or ".." in part.parts or name in recorded or part.as_posix() != name:
            raise ValueError("invalid wheel member name")
        recorded.add(name)
        member = upstream / name
        if member.is_symlink() or not member.is_file():
            raise ValueError("missing or linked wheel member")
        if name == record_name:
            if digest or size:
                raise ValueError("unexpected RECORD self-reference")
            continue
        raw = member.read_bytes()
        observed = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        if digest != observed or str(len(raw)) != size:
            raise ValueError("upstream wheel member drift: " + name)
    for name, digest in lock["source_files"].items():
        member = upstream / name
        if not member.is_file() or member.is_symlink() or sha(member.read_bytes()) != digest:
            raise ValueError("upstream source drift: " + name)
    if {path.relative_to(upstream).as_posix() for path in members} != recorded:
        raise ValueError("unexpected upstream wheel member")
    before_sources = {name: (upstream / name).read_text() for name in (BACKEND, *patch_sdk.FILES)}
    # Copy the complete package tree so Python uses normal package resolution,
    # not an import-time monkeypatch or a mixture of package versions.
    shutil.copytree(upstream, destination)
    path = destination / BACKEND
    before = path.read_text()
    replacements = [
        ('KNOWN_BENCHMARK_BACKENDS = frozenset({"magpie", "bypass"})',
         'KNOWN_BENCHMARK_BACKENDS = frozenset({"magpie", "bypass", "workstation_owner"})'),
        ('    name = resolve_backend_name()\n    if name == "bypass":',
         '    name = resolve_backend_name()\n    if name == "workstation_owner":\n'
         '        from hyperloom_owner_backend import OwnerBackend\n\n'
         '        return OwnerBackend()\n    if name == "bypass":'),
    ]
    after = before
    for old, new in replacements:
        if after.count(old) != 1:
            raise ValueError("pinned backend patch no longer applies")
        after = after.replace(old, new, 1)
    compile(after, BACKEND, "exec", dont_inherit=True)
    path.write_text(after)
    pins = {BACKEND: sha(path.read_bytes())}
    for name in patch_sdk.adapt(destination):
        pins[name] = sha((destination / name).read_bytes())
    for name in MODULES:
        raw = (REPOSITORY / "lib/workstation" / name).read_bytes()
        compile(raw, name, "exec", dont_inherit=True)
        (destination / name).write_bytes(raw)
        pins[name] = sha(raw)
    difference = "".join(line for name, original in before_sources.items()
                         for line in difflib.unified_diff(original.splitlines(True),
                             (destination / name).read_text().splitlines(True),
                             fromfile="upstream/" + name, tofile="adapter/" + name))
    (destination / "WORKSTATION-ADAPTER.diff").write_text(difference)
    record = {"schema": 1, "status": "CPU_ADAPTER_NOT_QUALIFIED", "upstream": lock,
              "files": pins, "diff_sha256": sha(difference.encode()), "gpu_work": False,
              "stock_runner_gate_changed": False, "private_qualification_hook": True,
              "automatic_optimization": False,
              "remaining": ["controller packet derivation and execution", "native workload/profile qualification",
                            "native validation of balanced-latency KEEP propagation", "kernel evaluation routing",
                            "campaign supervisor and real optimizer validation"]}
    (destination / "WORKSTATION-ADAPTER.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.upstream, args.output), sort_keys=True))
