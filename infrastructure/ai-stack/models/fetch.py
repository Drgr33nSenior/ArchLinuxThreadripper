#!/usr/bin/env python3
"""Download a catalog entry's model and draft, verifying every file.

Files land in ROOT/<owner>/<name>/<revision>/. A file is installed only after
its size and published hash match the catalog; partial downloads resume.
VERIFIED.json is written last, so its presence means the revision is complete.
Standard library only, so it runs in the pinned base image without installs.

Usage: fetch.py CATALOG_ENTRY ROOT
"""
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

URL = "https://huggingface.co/{repo}/resolve/{rev}/{path}"
CHUNK = 8 << 20
# Dirty NFS page cache counts against the container's memory limit and the
# download outpaces the NAS, so flush to the server at this interval.
SYNC_EVERY = 256 << 20


def hasher(record: dict):
    """The catalogue's hash for a file and its expected value (cache.py shares this)."""
    if "sha256" in record:
        return hashlib.sha256(), record["sha256"]
    # Git blob identity: sha1("blob <size>\0" + content).
    return hashlib.sha1(f"blob {record['size']}\0".encode()), record["git_sha1"]


def digest(path: pathlib.Path, record: dict) -> bool:
    h, expected = hasher(record)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest() == expected


def download(url: str, part: pathlib.Path, size: int) -> None:
    for attempt in range(1, 6):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have == size:
            return
        request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                if have and response.status != 206:
                    have = 0  # server ignored the range; start over
                with part.open("ab" if have else "wb") as out:
                    unsynced = 0
                    for chunk in iter(lambda: response.read(CHUNK), b""):
                        out.write(chunk)
                        unsynced += len(chunk)
                        if unsynced >= SYNC_EVERY:
                            out.flush()
                            os.fsync(out.fileno())
                            unsynced = 0
                    out.flush()
                    os.fsync(out.fileno())
            if part.stat().st_size == size:
                return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            print(f"retry {attempt}: {part.name}: {error}", flush=True)
        time.sleep(min(60, 5 * attempt))
    raise SystemExit(f"STOP: could not download {url}")


def fetch(spec: dict, root: pathlib.Path) -> pathlib.Path:
    repo, rev = spec["repository"], spec["revision"]
    target = root / repo / rev
    marker = target / "VERIFIED.json"
    if marker.exists() and json.loads(marker.read_text()) == spec:
        print(f"VERIFIED={target}", flush=True)
        return target
    for record in spec["files"]:
        relative = pathlib.PurePosixPath(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"STOP: unsafe catalog path {record['path']!r}")
        path = target / relative
        if path.exists() and path.stat().st_size == record["size"] and digest(path, record):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        print(f"FETCH {repo}/{record['path']} ({record['size']} bytes)", flush=True)
        download(URL.format(repo=repo, rev=rev, path=record["path"]), part, record["size"])
        if not digest(part, record):
            part.unlink()
            raise SystemExit(f"STOP: hash mismatch for {repo}/{record['path']}")
        os.replace(part, path)
    marker.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"VERIFIED={target}", flush=True)
    return target


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    entry = json.loads(pathlib.Path(sys.argv[1]).read_text())
    root = pathlib.Path(sys.argv[2])
    for key in ("model", "draft"):
        if key in entry:
            fetch(entry[key], root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
