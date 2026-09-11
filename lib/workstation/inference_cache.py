"""Inventory and owner-reviewable plans for managed inference compiler caches.

This module never deletes data. It only accepts namespace entries recorded in a
private owner registry; a cache directory name or an access time is not proof
of provenance, liveness, ownership, or authority to remove it.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from datetime import datetime


BACKENDS = ("triton", "torchinductor")
KIND = "workstation-inference-cache-registry"
SHA256 = re.compile(r"[a-f0-9]{64}")
MAX_JSON_BYTES = 2 << 20
MAX_NAMESPACES = 128
MAX_TREE_ENTRIES = 100000
MAX_ACTIVE_REFERENCES = 128


class InvalidCache(ValueError):
    """A bounded, nonsecret inventory or plan refusal."""


def require(condition, message):
    if not condition:
        raise InvalidCache(message)


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def checked_directory(path, message):
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)), message)
    for current in (path, *path.parents):
        info = os.lstat(current)
        require(not stat.S_ISLNK(info.st_mode), message)
    info = os.lstat(path)
    require(stat.S_ISDIR(info.st_mode), message)
    return path


def read_bytes(path, message, limit=MAX_JSON_BYTES):
    path = Path(path)
    require(path.is_absolute() and str(path) == os.path.normpath(str(path)), message)
    for current in (path.parent, *path.parent.parents):
        info = os.lstat(current)
        require(not stat.S_ISLNK(info.st_mode), message)
    info = os.lstat(path)
    require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_size <= limit, message)
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    require(len(value) <= limit, message)
    return value


def sha(path):
    return sha_bytes(read_bytes(path, "cache registry/evidence is unsafe or exceeds its bound"))


def read_json(path):
    return json.loads(read_bytes(path, "cache registry/evidence must be a bounded regular file"))


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def checked_root(value):
    return checked_directory(value, "inference cache root must be a configured canonical non-symlink directory")


def parse_time(value):
    if value is None:
        return None
    require(type(value) is str and value.endswith("Z"), "last_managed_use must be an RFC3339 UTC timestamp or null")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InvalidCache("last_managed_use must be an RFC3339 UTC timestamp or null") from error
    return value


def registry(root, path):
    value = read_json(path)
    require(value.get("schema") == 1 and value.get("kind") == KIND and isinstance(value.get("namespaces"), list),
            "unsupported inference cache registry")
    require(len(value["namespaces"]) <= MAX_NAMESPACES, "inference cache registry namespace bound exceeded")
    entries, seen = [], set()
    for item in value["namespaces"]:
        require(isinstance(item, dict) and set(item) == {"backend", "cache_identity", "relative_path",
                "runtime_identity_sha256", "model_identity_sha256", "owner_uid", "owner_gid",
                "active_references", "last_known_good", "last_managed_use"},
                "inference cache registry entry has unsupported fields")
        backend, identity = item["backend"], item["cache_identity"]
        expected = f"{backend}/workstation/{identity}"
        require(backend in BACKENDS and type(identity) is str and SHA256.fullmatch(identity)
                and item["relative_path"] == expected and all(type(item[key]) is str and SHA256.fullmatch(item[key])
                for key in ("runtime_identity_sha256", "model_identity_sha256")),
                "registry namespace provenance or membership is invalid")
        require(type(item["owner_uid"]) is int and item["owner_uid"] >= 0
                and type(item["owner_gid"]) is int and item["owner_gid"] >= 0
                and isinstance(item["active_references"], list)
                and all(type(reference) is str and 0 < len(reference) <= 128 for reference in item["active_references"])
                and len(item["active_references"]) <= MAX_ACTIVE_REFERENCES
                and len(set(item["active_references"])) == len(item["active_references"])
                and type(item["last_known_good"]) is bool,
                "registry namespace owner or liveness fields are invalid")
        parse_time(item["last_managed_use"])
        require(item["relative_path"] not in seen, "duplicate inference cache registry namespace")
        seen.add(item["relative_path"])
        entries.append(item)
    # The caller can use a different host mount location; only the fixed
    # subpaths are accepted, never a registry-controlled filesystem root.
    for backend in BACKENDS:
        backend_parent = root / backend
        parent = backend_parent / "workstation"
        checked_directory(backend_parent, "configured cache root has an unsafe managed backend parent")
        checked_directory(parent, "configured cache root lacks a canonical managed namespace parent")
    return entries


class TreeBudget:
    def __init__(self):
        self.entries = 0

    def add(self):
        self.entries += 1
        require(self.entries <= MAX_TREE_ENTRIES, "managed inference cache tree entry bound exceeded")


def allocated_bytes(info):
    return int(info.st_blocks) * 512


def safe_tree(path, root_device, expected_uid, expected_gid, seen_inodes, tree_budget):
    """Return physical/exclusive bytes; reject links, special files and mounts."""
    physical = exclusive = shared = 0
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        info = os.lstat(current)
        tree_budget.add()
        require(not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode), "registered inference cache namespace is unsafe")
        require(info.st_dev == root_device, "managed inference cache crosses a filesystem boundary")
        require(info.st_uid == expected_uid and info.st_gid == expected_gid,
                "managed inference cache inode owner differs from approved registry")
        with os.scandir(current) as iterator:
            for entry in iterator:
                item = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                tree_budget.add()
                require(not stat.S_ISLNK(info.st_mode), "managed inference cache contains a symlink")
                require(info.st_dev == root_device, "managed inference cache crosses a filesystem boundary")
                require(info.st_uid == expected_uid and info.st_gid == expected_gid,
                        "managed inference cache inode owner differs from approved registry")
                if stat.S_ISDIR(info.st_mode):
                    stack.append(item)
                    continue
                require(stat.S_ISREG(info.st_mode), "managed inference cache contains a non-regular file")
                inode = (info.st_dev, info.st_ino)
                if inode not in seen_inodes:
                    seen_inodes.add(inode)
                    allocated = allocated_bytes(info)
                    physical += allocated
                    if info.st_nlink != 1:
                        shared += allocated
                if info.st_nlink == 1:
                    exclusive += allocated_bytes(info)
    return physical, exclusive, shared


def inventory(cache_root, registry_path, output):
    root = checked_root(cache_root)
    entries = registry(root, registry_path)
    root_stat = root.stat(follow_symlinks=False)
    by_path = {entry["relative_path"]: entry for entry in entries}
    seen_inodes, rows, unregistered, tree_budget = set(), [], [], TreeBudget()
    for backend in BACKENDS:
        parent = root / backend / "workstation"
        for item in sorted(parent.iterdir(), key=lambda value: value.name):
            require(not item.is_symlink(), "managed inference cache contains a symlink")
            relative = str(item.relative_to(root))
            if relative not in by_path:
                tree_budget.add()
                require(len(unregistered) < MAX_NAMESPACES, "unregistered managed namespace bound exceeded")
                unregistered.append(relative)
                continue
            require(item.is_dir(), "registered inference cache namespace is not a directory")
    for entry in entries:
        path = root / entry["relative_path"]
        row = {key: entry[key] for key in ("backend", "cache_identity", "relative_path", "runtime_identity_sha256",
                                           "model_identity_sha256", "active_references", "last_known_good", "last_managed_use")}
        if not path.exists() and not path.is_symlink():
            row.update({"state": "missing-preserve", "physical_bytes": 0, "exclusive_bytes": 0,
                        "shared_bytes": 0, "reclaimable_bytes": 0,
                        "reason": "registered namespace is absent; retain registry/evidence"})
        else:
            info = path.stat(follow_symlinks=False)
            require(not path.is_symlink() and stat.S_ISDIR(info.st_mode), "registered inference cache namespace is unsafe")
            require(info.st_uid == entry["owner_uid"] and info.st_gid == entry["owner_gid"],
                    "registered inference cache namespace owner differs from approved registry")
            physical, exclusive, shared = safe_tree(path, root_stat.st_dev, entry["owner_uid"], entry["owner_gid"],
                                                     seen_inodes, tree_budget)
            active = bool(entry["active_references"])
            protected = active or entry["last_known_good"]
            state = "protected-active" if active else "protected-last-known-good" if entry["last_known_good"] else "managed-disposable"
            row.update({"state": state, "physical_bytes": physical, "exclusive_bytes": exclusive,
                        "shared_bytes": shared, "reclaimable_bytes": exclusive if not protected and shared == 0 else 0,
                        "reason": "active reference" if active else "last-known-good namespace" if entry["last_known_good"]
                        else "hard-linked content is retained" if shared else "managed inactive namespace"})
        rows.append(row)
    usage = shutil.disk_usage(root)
    value = {"schema": 1, "kind": "workstation-inference-cache-inventory", "status": "observed-not-qualified",
             "cache_root": str(root), "registry_sha256": sha(registry_path),
             "filesystem": {"total_bytes": usage.total, "free_bytes": usage.free, "available_bytes": usage.free},
             "namespaces": rows, "unregistered_entries": unregistered,
             "limitations": ["No filesystem atime is used as last managed use.",
                             "Only registry-bound Triton/Inductor workstation namespaces are eligible for a plan.",
                             "Model weights, worker ccache, benchmark evidence and unregistered entries are excluded.",
                             "Inventory and plans do not delete data or prove cache liveness."]}
    encoded = json.dumps(value, indent=2, allow_nan=False).encode()
    require(len(encoded) < MAX_JSON_BYTES, "cache inventory output exceeds bounded JSON size")
    destination = private_directory(output)
    path = destination / "inventory.json"
    path.write_bytes(encoded + b"\n")
    manifest = destination / "SHA256SUMS"
    manifest.write_text(f"{sha(path)}  inventory.json\n")
    os.chmod(path, 0o600)
    os.chmod(manifest, 0o600)
    print(f"{value['status']}: {path}; no cache data was deleted")
    return value


def load_inventory(directory):
    directory = checked_directory(directory, "cache inventory directory is unsafe")
    path = directory / "inventory.json"
    manifest = directory / "SHA256SUMS"
    path_bytes = read_bytes(path, "cache inventory is incomplete or unsafe")
    manifest_bytes = read_bytes(manifest, "cache inventory is incomplete or unsafe", 1024)
    require(manifest_bytes == f"{sha_bytes(path_bytes)}  inventory.json\n".encode(),
            "cache inventory changed; recollect before planning")
    value = read_json(path)
    require(value.get("schema") == 1 and value.get("kind") == "workstation-inference-cache-inventory"
            and value.get("status") == "observed-not-qualified" and isinstance(value.get("namespaces"), list),
            "unsupported cache inventory")
    require(len(value["namespaces"]) <= MAX_NAMESPACES and len(value.get("unregistered_entries", [])) <= MAX_NAMESPACES,
            "cache inventory namespace bound exceeded")
    return value, sha_bytes(path_bytes)


def prune_plan(inventory_directory, reserve_mib, output):
    require(type(reserve_mib) is int and reserve_mib > 0, "inference cache free-space reserve must be positive")
    value, evidence_sha = load_inventory(inventory_directory)
    required = reserve_mib * 1024 * 1024
    free = value["filesystem"].get("available_bytes")
    require(type(free) is int and free >= 0, "cache inventory has no valid filesystem free-space observation")
    candidates = [{key: row[key] for key in ("backend", "cache_identity", "relative_path", "runtime_identity_sha256",
                   "model_identity_sha256", "last_managed_use", "physical_bytes", "exclusive_bytes", "shared_bytes",
                   "reclaimable_bytes", "reason")}
                  for row in value["namespaces"] if row.get("state") == "managed-disposable" and row.get("reclaimable_bytes", 0) > 0]
    disposable = sum(row["reclaimable_bytes"] for row in candidates)
    shortfall = max(0, required - free)
    if shortfall == 0:
        status = "plan-only-no-prune-required"
    elif disposable >= shortfall:
        status = "plan-only-review-required"
    else:
        status = "blocked-insufficient-disposable-space"
    result = {"schema": 1, "kind": "workstation-inference-cache-prune-plan", "status": status,
              "inventory_sha256": evidence_sha, "cache_root": value["cache_root"],
              "observed_available_bytes": free, "required_free_bytes": required, "shortfall_bytes": shortfall,
              "disposable_bytes": disposable, "would_meet_reserve_if_approved": free + disposable >= required,
              "candidates": candidates, "exclusions": {"active_or_last_known_good": [row["relative_path"] for row in value["namespaces"]
                           if row.get("state", "").startswith("protected-")],
                             "unregistered": value.get("unregistered_entries", []),
                             "missing": [row["relative_path"] for row in value["namespaces"] if row.get("state") == "missing-preserve"]},
              "execution": "No executor is implemented. An owner must review this plan and independently revalidate paths, registry, namespace ownership, active references and filesystem space before any future deletion.",
              "limitations": ["This is a plan only; no cache data was deleted.",
                              "Local-path PVC requests are not filesystem quotas.",
                              "Do not infer last use from atime or treat free-space observations as workload capacity."]}
    encoded = json.dumps(result, indent=2, allow_nan=False).encode()
    require(len(encoded) < MAX_JSON_BYTES, "cache prune plan output exceeds bounded JSON size")
    destination = private_directory(output)
    path = destination / "prune-plan.json"
    path.write_bytes(encoded + b"\n")
    manifest = destination / "SHA256SUMS"
    manifest.write_text(f"{sha(path)}  prune-plan.json\n")
    os.chmod(path, 0o600)
    os.chmod(manifest, 0o600)
    print(f"{status}: {path}; no cache data was deleted")
    return 0 if status != "blocked-insufficient-disposable-space" else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    collect = actions.add_parser("inventory")
    collect.add_argument("cache_root")
    collect.add_argument("registry")
    collect.add_argument("output")
    plan = actions.add_parser("plan")
    plan.add_argument("inventory")
    plan.add_argument("output")
    plan.add_argument("--reserve-mib", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.action == "inventory":
            inventory(args.cache_root, args.registry, args.output)
        else:
            return prune_plan(args.inventory, args.reserve_mib, args.output)
    except (InvalidCache, OSError, json.JSONDecodeError, TypeError, KeyError):
        print("FAILED: inference cache inputs are unsafe, unavailable or malformed; no cache data was deleted", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
