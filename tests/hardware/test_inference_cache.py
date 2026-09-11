"""Synthetic cache inventory/prune-plan tests; no cache deletion or Pod access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import inference_cache as cache


def digest(character):
    return character * 64


class InferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS exposes /var through a compatibility symlink. The production
        # contract requires the configured cache root itself and its ancestors
        # to be canonical and symlink-free, so fixtures use the real path.
        self.work = Path(self.temp.name).resolve()
        self.root = self.work / "cache"
        for backend in cache.BACKENDS:
            (self.root / backend / "workstation").mkdir(parents=True)
        self.uid, self.gid = os.getuid(), os.getgid()

    def tearDown(self):
        self.temp.cleanup()

    def entry(self, backend, character, active=(), last_good=False):
        identity = digest(character)
        path = self.root / backend / "workstation" / identity
        path.mkdir()
        (path / "kernel.bin").write_bytes(b"x" * 17)
        return {"backend": backend, "cache_identity": identity,
                "relative_path": f"{backend}/workstation/{identity}",
                "runtime_identity_sha256": digest("e"), "model_identity_sha256": digest("f"),
                "owner_uid": self.uid, "owner_gid": self.gid, "active_references": list(active),
                "last_known_good": last_good, "last_managed_use": "2026-09-11T12:00:00Z"}

    def registry(self, entries):
        path = self.work / "registry.json"
        path.write_text(json.dumps({"schema": 1, "kind": cache.KIND, "namespaces": entries}))
        return path

    def test_inventory_and_plan_distinguish_managed_protected_and_unregistered(self):
        disposable = self.entry("triton", "a")
        active = self.entry("torchinductor", "b", active=("pod:fixture",))
        last_good = self.entry("triton", "c", last_good=True)
        (self.root / "triton/workstation" / digest("d")).mkdir()
        registry = self.registry([disposable, active, last_good])
        evidence = cache.inventory(self.root, registry, self.work / "inventory")
        self.assertEqual((self.work / "inventory").stat().st_mode & 0o077, 0)
        self.assertEqual((self.work / "inventory/inventory.json").stat().st_mode & 0o077, 0)
        rows = {row["cache_identity"]: row for row in evidence["namespaces"]}
        self.assertEqual(rows[digest("a")]["state"], "managed-disposable")
        self.assertGreater(rows[digest("a")]["reclaimable_bytes"], 0)
        self.assertEqual(rows[digest("b")]["state"], "protected-active")
        self.assertEqual(rows[digest("c")]["state"], "protected-last-known-good")
        self.assertEqual(evidence["unregistered_entries"], [f"triton/workstation/{digest('d')}"])
        self.assertEqual(cache.prune_plan(self.work / "inventory", 10**9, self.work / "plan"), 1)
        plan = json.loads((self.work / "plan/prune-plan.json").read_text())
        self.assertEqual((self.work / "plan/prune-plan.json").stat().st_mode & 0o077, 0)
        self.assertEqual(plan["status"], "blocked-insufficient-disposable-space")
        self.assertEqual([row["cache_identity"] for row in plan["candidates"]], [digest("a")])
        self.assertIn(active["relative_path"], plan["exclusions"]["active_or_last_known_good"])
        self.assertTrue((self.root / disposable["relative_path"] / "kernel.bin").is_file())

    def test_unsafe_links_owner_drift_and_path_escape_refuse(self):
        entry = self.entry("triton", "a")
        registry = self.registry([entry])
        (self.root / entry["relative_path"] / "escape").symlink_to(self.work / "outside")
        with self.assertRaises(cache.InvalidCache):
            cache.inventory(self.root, registry, self.work / "unsafe")
        (self.root / entry["relative_path"] / "escape").unlink()
        bad = dict(entry)
        bad["relative_path"] = "../outside"
        with self.assertRaises(cache.InvalidCache):
            cache.registry(self.root, self.registry([bad]))
        drift = dict(entry)
        drift["owner_uid"] = self.uid + 1
        with self.assertRaises(cache.InvalidCache):
            cache.inventory(self.root, self.registry([drift]), self.work / "drift")

    def test_root_and_managed_parent_symlinks_refuse(self):
        entry = self.entry("triton", "a")
        registry = self.registry([entry])
        through_link = self.work / "linked-work"
        through_link.symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(cache.InvalidCache):
            cache.inventory(through_link / "cache", registry, self.work / "ancestor-link")

        outside = self.work / "outside-backend"
        (outside / "workstation").mkdir(parents=True)
        import shutil
        shutil.rmtree(self.root / "triton")
        (self.root / "triton").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(cache.InvalidCache):
            cache.registry(self.root, registry)

    def test_all_namespace_inodes_must_match_registry_owner(self):
        entry = self.entry("triton", "a")
        namespace = self.root / entry["relative_path"]
        original_scandir = os.scandir

        class EntryWithForeignOwner:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.path = wrapped.path

            def stat(self, follow_symlinks=False):
                observed = self.wrapped.stat(follow_symlinks=follow_symlinks)
                fields = list(observed)
                fields[4] = observed.st_uid + 1
                return os.stat_result(fields)

        class WrappedScan:
            def __init__(self, path):
                self.iterator = original_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                self.iterator.close()

            def __iter__(self):
                return self

            def __next__(self):
                return EntryWithForeignOwner(next(self.iterator))

        with mock.patch.object(cache.os, "scandir", WrappedScan):
            with self.assertRaises(cache.InvalidCache):
                cache.safe_tree(namespace, self.root.stat().st_dev, self.uid, self.gid, set(), cache.TreeBudget())

    def test_registry_and_tree_bounds_refuse(self):
        oversized = self.work / "oversized-registry.json"
        oversized.write_bytes(b"{" + b"x" * cache.MAX_JSON_BYTES)
        with self.assertRaises(cache.InvalidCache):
            cache.registry(self.root, oversized)
        many = self.work / "many-registry.json"
        many.write_text(json.dumps({"schema": 1, "kind": cache.KIND, "namespaces": [{}] * (cache.MAX_NAMESPACES + 1)}))
        with self.assertRaises(cache.InvalidCache):
            cache.registry(self.root, many)

        entry = self.entry("triton", "a")
        namespace = self.root / entry["relative_path"]
        with mock.patch.object(cache, "MAX_TREE_ENTRIES", 1):
            with self.assertRaises(cache.InvalidCache):
                cache.safe_tree(namespace, self.root.stat().st_dev, self.uid, self.gid, set(), cache.TreeBudget())

    def test_hardlinks_are_not_double_counted_or_prunable(self):
        first = self.entry("triton", "a")
        second = self.entry("torchinductor", "b")
        first_file = self.root / first["relative_path"] / "kernel.bin"
        second_file = self.root / second["relative_path"] / "kernel.bin"
        second_file.unlink()
        os.link(first_file, second_file)
        result = cache.inventory(self.root, self.registry([first, second]), self.work / "inventory")
        allocated = first_file.stat().st_blocks * 512
        self.assertEqual(sum(row["physical_bytes"] for row in result["namespaces"]), allocated)
        self.assertEqual(sum(row["reclaimable_bytes"] for row in result["namespaces"]), 0)
        self.assertEqual(sum(row["shared_bytes"] for row in result["namespaces"]), allocated)

    def test_missing_registry_namespace_preserves_evidence_and_atime_is_not_used(self):
        absent = {"backend": "triton", "cache_identity": digest("a"),
                  "relative_path": f"triton/workstation/{digest('a')}",
                  "runtime_identity_sha256": digest("e"), "model_identity_sha256": digest("f"),
                  "owner_uid": self.uid, "owner_gid": self.gid, "active_references": [],
                  "last_known_good": False, "last_managed_use": None}
        evidence = cache.inventory(self.root, self.registry([absent]), self.work / "inventory")
        self.assertEqual(evidence["namespaces"][0]["state"], "missing-preserve")
        self.assertIn("atime", " ".join(evidence["limitations"]))
        self.assertEqual(cache.prune_plan(self.work / "inventory", 10**9, self.work / "plan"), 1)

    def test_modified_inventory_or_unconfigured_root_refuses(self):
        entry = self.entry("triton", "a")
        with self.assertRaises(cache.InvalidCache):
            cache.inventory("relative-cache", self.registry([entry]), self.work / "bad")
        cache.inventory(self.root, self.registry([entry]), self.work / "inventory")
        (self.work / "inventory/inventory.json").write_text("{}")
        with self.assertRaises(cache.InvalidCache):
            cache.prune_plan(self.work / "inventory", 1, self.work / "plan")


if __name__ == "__main__":
    unittest.main()
