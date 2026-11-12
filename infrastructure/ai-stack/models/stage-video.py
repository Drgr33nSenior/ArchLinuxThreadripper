#!/usr/bin/env python3
"""Stage a pinned ComfyUI file set (a catalog entry of kind comfyui-file-set) into a model directory.

Usage: stage-video.py CATALOG_ENTRY DEST_DIR [--check]

Each file is downloaded from its pinned repository revision (never a branch) into
DEST_DIR/<dir>/<file name>, resumed from <name>.part, and renamed only after its size and
sha256 match the entry. --check verifies what is present without downloading.

For the Wan stack, DEST_DIR is the creative-models volume's Video/ directory (local NVMe),
for example /var/lib/rancher/k3s/storage/<creative-models pv>/Video; run as root and
chown the result to 1000:1000, the ComfyUI pod's user.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--check"):
        raise SystemExit(__doc__)
    entry = json.loads(pathlib.Path(sys.argv[1]).read_text())
    dest = pathlib.Path(sys.argv[2])
    check_only = len(sys.argv) == 4
    if entry.get("schema") != 1 or entry.get("kind") != "comfyui-file-set":
        raise SystemExit("STOP: not a comfyui-file-set catalog entry")
    bad = 0
    for item in entry["files"]:
        if (not re.fullmatch(r"[a-f0-9]{40}", item["revision"]) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or item["dir"] not in {"diffusion_models", "loras", "text_encoders", "vae"}):
            raise SystemExit(f"STOP: entry is not pinned: {item['path']}")
        target = dest / item["dir"] / item["path"].rsplit("/", 1)[-1]
        if target.exists() and target.stat().st_size == item["size"] and sha256(target) == item["sha256"]:
            print(f"ok   {target.name}", flush=True)
            continue
        if check_only:
            print(f"MISSING or different: {target}", flush=True)
            bad += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = f"https://huggingface.co/{item['repository']}/resolve/{item['revision']}/{item['path']}"
        print(f"get  {target.name} ({item['size'] / 1e9:.2f} GB)", flush=True)
        subprocess.run(["curl", "-fL", "--retry", "5", "--retry-delay", "5", "-C", "-", "-sS",
                        "-A", "model-fetch/1.0", "-o", str(part), url], check=True)
        if part.stat().st_size != item["size"] or sha256(part) != item["sha256"]:
            raise SystemExit(f"STOP: size/sha256 mismatch for {target.name}; kept {part}")
        part.replace(target)
        print(f"ok   {target.name}", flush=True)
    if bad:
        raise SystemExit(f"STOP: {bad} file(s) missing or different")
    print("STAGED " + entry["alias"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
