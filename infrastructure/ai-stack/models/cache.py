#!/usr/bin/env python3
"""Keep a local, verified copy of exactly the model revisions a server uses.

The NAS stays the archive of every revision (read-only here). The local cache holds
only the catalogue entry's model and draft, at the same <owner>/<name>/<revision>
paths as the NAS, so the server's --model-path arguments are unchanged.

For each wanted revision:
- If CACHE/<repo>/<rev>/VERIFIED.json equals the catalogue entry and every file has its
  catalogued size, nothing is read (the normal start: about a second).
- Otherwise the NAS copy must itself carry a VERIFIED.json equal to the entry (written by
  fetch.py after it verified every file). Each file is copied into CACHE/.staging/ and
  hashed in the same pass (sha256 for weights, git blob sha1 for small files, as in
  fetch.py); size and hash must match the catalogue. VERIFIED.json is written last and
  the revision directory is renamed into place in one step.
Then every cached revision not in the entry, and any staging leftovers, are deleted:
the cache never accumulates old revisions. A size cap and a free-space check bound it.

Standard library only. Usage: cache.py ENTRY SOURCE_ROOT CACHE_ROOT [--limit-gib N]
"""
import json
import os
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch import hasher  # noqa: E402  one definition of how catalogue hashes are checked

CHUNK = 16 << 20
STAGING = ".staging"
MARGIN = 4 << 30  # free space kept beyond the copy itself


def safe(relative):
    path = pathlib.PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SystemExit(f"STOP: unsafe catalogue path {relative!r}")
    return path


def wanted(entry):
    specs = [entry[key] for key in ("model", "draft") if key in entry]
    if not specs:
        raise SystemExit("STOP: the entry names no model or draft")
    for spec in specs:
        safe(spec["repository"]), safe(spec["revision"])
        for record in spec["files"]:
            safe(record["path"])
    return specs


def complete(target, spec):
    """Marker equals the entry and every file has its catalogued size (no rehash)."""
    marker = target / "VERIFIED.json"
    try:
        if json.loads(marker.read_text()) != spec:
            return False
        return all((target / r["path"]).stat().st_size == r["size"] for r in spec["files"])
    except (OSError, ValueError):
        return False


def copy_verified(source, destination, record):
    h, expected = hasher(record)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            h.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
        # Keep the copy from filling the page cache charged to this container.
        for f in (src, dst):
            try:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
    if destination.stat().st_size != record["size"] or h.hexdigest() != expected:
        destination.unlink()
        raise SystemExit(f"STOP: size or hash mismatch for {record['path']}; the NAS copy is not as catalogued")


def stage(spec, source_root, cache_root):
    repo, rev = spec["repository"], spec["revision"]
    target = cache_root / repo / rev
    if complete(target, spec):
        print(f"CACHED={repo}/{rev}", flush=True)
        return
    source = source_root / repo / rev
    if not complete(source, spec):
        raise SystemExit(f"STOP: the NAS copy of {repo}/{rev} is missing or not verified; stage it with render-fetch.py")
    staging = cache_root / STAGING / repo / rev
    shutil.rmtree(staging, ignore_errors=True)
    started, total = time.monotonic(), 0
    for record in spec["files"]:
        print(f"COPY {repo}/{record['path']} ({record['size']} bytes)", flush=True)
        copy_verified(source / record["path"], staging / record["path"], record)
        total += record["size"]
    (staging / "VERIFIED.json").write_text(json.dumps(spec, indent=2) + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():  # an incomplete or outdated copy at the final path
        shutil.rmtree(target)
    os.replace(staging, target)
    seconds = time.monotonic() - started
    print(f"STAGED={repo}/{rev} bytes={total} seconds={seconds:.1f} MBps={total / max(seconds, 1e-6) / 1e6:.0f}", flush=True)


def prune(specs, cache_root):
    """Delete every cached revision the entry does not name, and staging leftovers."""
    keep = {(s["repository"], s["revision"]) for s in specs}
    shutil.rmtree(cache_root / STAGING, ignore_errors=True)
    for owner in [p for p in cache_root.iterdir() if p.is_dir() and p.name != STAGING]:
        for name in [p for p in owner.iterdir() if p.is_dir()]:
            for rev in [p for p in name.iterdir() if p.is_dir()]:
                if (f"{owner.name}/{name.name}", rev.name) not in keep:
                    print(f"PRUNE {owner.name}/{name.name}/{rev.name}", flush=True)
                    shutil.rmtree(rev)
            if not any(name.iterdir()):
                name.rmdir()
        if not any(owner.iterdir()):
            owner.rmdir()


def main(argv):
    limit_gib = 64
    if "--limit-gib" in argv:
        i = argv.index("--limit-gib")
        limit_gib = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 3:
        raise SystemExit(__doc__)
    entry = json.loads(pathlib.Path(argv[0]).read_text())
    source_root, cache_root = pathlib.Path(argv[1]), pathlib.Path(argv[2])
    specs = wanted(entry)
    size = sum(r["size"] for s in specs for r in s["files"])
    if size > limit_gib << 30:
        raise SystemExit(f"STOP: the entry needs {size >> 30} GiB, over the {limit_gib} GiB cache limit")
    # Old revisions go first, so their space counts towards the new copy.
    prune(specs, cache_root)
    missing = sum(r["size"] for s in specs if not complete(cache_root / s["repository"] / s["revision"], s)
                  for r in s["files"])
    free = shutil.disk_usage(cache_root).free
    if missing and free < missing + MARGIN:
        raise SystemExit(f"STOP: {missing >> 30} GiB to copy but only {free >> 30} GiB free in the cache volume")
    for spec in specs:
        stage(spec, source_root, cache_root)
    shutil.rmtree(cache_root / STAGING, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
