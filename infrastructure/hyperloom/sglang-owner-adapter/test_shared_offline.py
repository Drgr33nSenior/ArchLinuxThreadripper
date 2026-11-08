"""Offline composition tests; physical owner replay remains a separate gate."""
import copy
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_offline as offline
import shared_owner as owner
import shared_diagnostic as core
import test_shared_owner


class TestOffline(unittest.TestCase):
    def setUp(self):
        self.fixture = test_shared_owner.TestOwner(); self.fixture.setUp()
        self.root = self.fixture.root
        self.reader = NS(read=lambda p: json.loads(p.read_bytes()), held=self.held)
        self.entry = NS(modules=lambda: owner, CONFIG=dict(menu=self.fixture.menu))
    def tearDown(self):
        self.fixture.tearDown()
    @staticmethod
    def held(path, pin, limit):
        raw = core._file(path.parent, path.name, limit)
        core.need(core.sha(raw) == pin, "summary pin")
        return raw
    def prepare(self):
        for start, arm in ((1, "bf16"), (2, "kv4")):
            root, ids, pins, mounts, devices, summary, http_pin = self.fixture.paired(start)
            if start == 2:
                for rank in (0, 1):
                    cap_path = root / f"CAPTURE-rank-{rank}.json"
                    cap = json.loads(cap_path.read_bytes())
                    cap["raw_mount"] = dict(path=f"/mnt/shared-trace-raw-rank-{rank}", fstype="tmpfs",
                                           capacity_bytes=32 << 20, verified=True)
                    cap_raw = core.canonical(cap); cap_path.write_bytes(cap_raw)
                    close_path = root / pins[rank]["file"]
                    close = json.loads(close_path.read_bytes()); close["capture_sha256"] = core.sha(cap_raw)
                    close_raw = core.canonical(close); close_path.write_bytes(close_raw)
                    pins[rank].update(sha256=core.sha(close_raw), bytes=len(close_raw))
            out = self.root / (arm + "-artifacts"); out.mkdir()
            shutil.copytree(root, out / "shared-diagnostic")
            receipt = dict(schema=1, identities=ids, close_pins=pins, device_pins=devices,
                           http_pin=http_pin, summary_sha256=core.sha(summary),
                           trust_root="parent-barrier-before-teardown", diagnostic_success=False)
            (out / "result.json").write_bytes(core.canonical(dict(status="HTTP_ARM_COMPLETE",
                       parent_error=None, shared_diagnostic=receipt)))
            paired = self.root / (arm + "-http-client-tree") / "paired"; paired.mkdir(parents=True)
            (paired / "RUN-SUMMARY.json").write_bytes(summary)
    def test_complete_real_reader_path_keeps_noncomparable_work(self):
        self.prepare()
        result = offline.compare(self.reader, self.entry, self.root)
        self.assertEqual(result["cross_start"]["status"], "NOT_COMPARABLE")
        self.assertEqual(len(result["rank_comparisons"]), 7)
        self.assertEqual([r["start"] for r in result["inventory"]], [1, 2])
        self.assertFalse(result["speedup_claim"])
    def test_foreign_http_summary_refuses(self):
        self.prepare()
        path = self.root / "kv4-http-client-tree/paired/RUN-SUMMARY.json"
        path.write_bytes((self.root / "bf16-http-client-tree/paired/RUN-SUMMARY.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "summary pin"):
            offline.compare(self.reader, self.entry, self.root)
    def test_missing_close_receipt_refuses(self):
        self.prepare()
        (self.root / "kv4-artifacts/shared-diagnostic/SHARED-CLOSE-rank-1.json").unlink()
        with self.assertRaises(FileNotFoundError):
            offline.compare(self.reader, self.entry, self.root)
    def stub(self):
        pins = dict(package_sha256="a" * 64)
        request = dict(operation_root="/sealed-operation", operation_seal_sha256="b"*64,
                       workload_seal_sha256="c"*64, protected_seal_sha256="d"*64,
                       package_root="/authored/package-v2", expected_package_pins=pins)
        config = dict(quality_root="/quality", reader_sha256="e"*64, entry_sha256="f"*64,
                      package_root=request["package_root"], package_pins=pins)
        return config, request
    def test_failed_full_release_replay_prevents_compare(self):
        config, request = self.stub()
        reader = NS(read=lambda p: request, replay_retained=lambda **kw: (_ for _ in ()).throw(ValueError("release")))
        with patch.object(offline, "CONFIG", config), patch.object(offline, "load", return_value=reader), \
                patch.object(offline, "compare") as compare:
            with self.assertRaisesRegex(ValueError, "release"):
                offline.run(self.root / "request.json", self.root / "output")
            compare.assert_not_called()
    def test_wrong_package_refuses_before_replay(self):
        config, request = self.stub(); request = copy.deepcopy(request)
        request["expected_package_pins"]["package_sha256"] = "0"*64
        reader = NS(read=lambda p: request)
        with patch.object(offline, "CONFIG", config), patch.object(offline, "load", return_value=reader):
            with self.assertRaisesRegex(ValueError, "frozen package"):
                offline.run(self.root / "request.json", self.root / "output")
            self.assertFalse((self.root / "output").exists())


if __name__ == "__main__":
    unittest.main()
