"""Download the pinned blocklists for the egress image and check each against its record.

Usage: fetch-lists.py blocklists.json OUTPUT_DIRECTORY
Writes each list and sources.json (name, file, refresh_url, entries) for the proxy.
A list whose address is not pinned to a commit, or whose size or SHA-256 differs from
the record, stops the build.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request

LIMIT = 32 << 20


def main():
    record, out = json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    sources = []
    for item in record["lists"]:
        if not re.fullmatch(r"[a-z]{1,16}", item["name"]) or item["file"] != item["name"] + ".txt":
            raise SystemExit(f'STOP: list {item["name"]!r} needs a short lowercase name and <name>.txt')
        for key in ("url", "refresh_url"):
            if not item[key].startswith("https://raw.githubusercontent.com/"):
                raise SystemExit(f'STOP: list {item["name"]} {key} must be a raw.githubusercontent.com address')
        if not re.search(r"/[a-f0-9]{40}/", item["url"]):
            raise SystemExit(f'STOP: list {item["name"]} url must be pinned to a full commit')
        if not 0 < item["bytes"] <= LIMIT:
            raise SystemExit(f'STOP: list {item["name"]} is outside the size bound')
        request = urllib.request.Request(item["url"], headers={"User-Agent": "ai-stack-egress-stage"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read(LIMIT + 1)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != item["bytes"] or digest != item["sha256"]:
            raise SystemExit(f'STOP: list {item["name"]} is {len(data)} bytes, sha256 {digest}; the record differs')
        (out / item["file"]).write_bytes(data)
        sources.append({"name": item["name"], "file": item["file"], "refresh_url": item["refresh_url"],
                        "entries": item["entries"]})
    (out / "sources.json").write_text(json.dumps(sources, indent=1) + "\n")
    print("EGRESS_LISTS=" + ",".join(f'{s["name"]}:{s["entries"]}' for s in sources))


if __name__ == "__main__":
    main()
