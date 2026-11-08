#!/usr/bin/env python3
"""Print the file inventory of a Hugging Face model at an exact revision.

Each file gets its size and the hash Hugging Face publishes for it: the LFS
SHA-256 for large files, or the git blob SHA-1 for files stored in git. A
catalog entry embeds this output, and fetch.py checks every byte against it.

Usage: inventory.py REPOSITORY REVISION [--exclude GLOB ...]
"""
import argparse
import fnmatch
import json
import re
import sys
import urllib.request

API = "https://huggingface.co/api/models/{repo}/tree/{rev}?recursive=true&expand=true"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repository")
    parser.add_argument("revision", help="full 40-character commit")
    parser.add_argument("--exclude", action="append", default=[], help="glob of files the server never reads")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must be OWNER/NAME")
    if not re.fullmatch(r"[a-f0-9]{40}", args.revision):
        parser.error("revision must be a full commit, not a branch or tag")
    url = API.format(repo=args.repository, rev=args.revision)
    with urllib.request.urlopen(url, timeout=60) as response:
        tree = json.load(response)
    files = []
    for entry in sorted((e for e in tree if e["type"] == "file"), key=lambda e: e["path"]):
        if any(fnmatch.fnmatch(entry["path"], pattern) for pattern in args.exclude):
            continue
        record = {"path": entry["path"], "size": entry["size"]}
        lfs = entry.get("lfs")
        if lfs:
            record["sha256"] = lfs["oid"]
        else:
            record["git_sha1"] = entry["oid"]
        files.append(record)
    json.dump({"repository": args.repository, "revision": args.revision, "files": files}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
