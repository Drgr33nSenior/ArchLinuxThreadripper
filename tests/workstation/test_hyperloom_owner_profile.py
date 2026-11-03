"""Profile window provenance, bounds and C2 coverage; no runtime imports."""

import base64
import copy
import gzip
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import hyperloom_exchange as exchange
import hyperloom_owner_profile as profile


def fixture():
    request = {"id": "a" * 32, "kind": "profile", "source_set_sha256": "b" * 64}
    owner = {"owner_seal_sha256": "c" * 64, "source_set_sha256": request["source_set_sha256"],
             "request_sha256": exchange.digest(exchange.canonical(request))}
    windows = []
    for phase, index in profile.WINDOWS:
        traces = []
        for rank in (0, 1):
            raw = gzip.compress(exchange.canonical({"traceEvents": [
                {"ph": "X", "cat": "kernel", "dur": 3.5, "ts": index, "name": phase + str(rank)}
            ]}), mtime=0)
            traces.append({"rank": rank, "bytes": len(raw), "sha256": exchange.digest(raw),
                           "gzip_base64": base64.b64encode(raw).decode()})
        windows.append({"capture_id": phase + "-" + str(index), "phase": phase, "index": index,
                        "batch": {"rids": ["a", "b"], "batch_size": 2}, "traces": traces})
    return request, {"owner_binding": owner, "profile_capture":
                     dict(owner, schema=1, status="complete", arm="mtp", profiles=windows)}


class OwnerProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.request, self.report = fixture()

    def materialize(self):
        return profile.materialize_captures(self.report, self.request, self.root)

    def test_seven_windows_keep_both_ranks_separate(self):
        windows = self.materialize()
        self.assertEqual(len(windows), 7)
        for window, original in zip(windows, self.report["profile_capture"]["profiles"], strict=True):
            self.assertEqual(window["batch"], original["batch"])
            self.assertEqual(len(window["trace_files"]), 2)
            for rank, path in enumerate(window["trace_files"]):
                path = Path(path)
                self.assertEqual(path.parent.name, original["capture_id"])
                self.assertEqual(path.name, "rank" + str(rank) + ".trace.json.gz")
                self.assertEqual(exchange.digest(path.read_bytes()), original["traces"][rank]["sha256"])
        with self.assertRaises(FileExistsError):
            self.materialize()

    def test_missing_reused_or_misordered_window_refused_before_copy(self):
        good = copy.deepcopy(self.report)
        for change in (
            lambda w: w.pop(),
            lambda w: w.__setitem__(1, copy.deepcopy(w[0])),
            lambda w: w[0]["traces"].reverse(),
            lambda w: w[1]["traces"].__setitem__(0, copy.deepcopy(w[0]["traces"][0])),
        ):
            self.report = copy.deepcopy(good)
            change(self.report["profile_capture"]["profiles"])
            with self.assertRaises(ValueError):
                self.materialize()
            self.assertFalse((self.root / "owner-traces").exists())

    def test_foreign_owner_request_or_source_refused(self):
        for key in ("owner_seal_sha256", "request_sha256", "source_set_sha256"):
            request, report = fixture()
            report["profile_capture"][key] = "f" * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                profile.materialize_captures(report, request, self.root)

    def test_c1_decode_and_changed_pair_never_claim_c2_coverage(self):
        for index in (3, 4, 6):
            self.request, self.report = fixture()
            self.report["profile_capture"]["profiles"][index]["batch"] = {"rids": ["a"], "batch_size": 1}
            with self.assertRaises(ValueError):
                self.materialize()
        self.request, self.report = fixture()
        self.report["profile_capture"]["profiles"][6]["batch"]["rids"] = ["a", "other"]
        with self.assertRaises(ValueError):
            self.materialize()

    def test_foreign_prefill_request_refused(self):
        self.report["profile_capture"]["profiles"][0]["batch"]["rids"] = ["a", "other"]
        with self.assertRaises(ValueError):
            self.materialize()

    def test_trace_digest_and_compression_are_checked(self):
        trace = self.report["profile_capture"]["profiles"][0]["traces"][0]
        trace["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.materialize()
        for raw in (gzip.compress(b"{}"), gzip.compress(b" " * (profile.RAW_CAP + 1)),
                    gzip.compress(b"{}") + gzip.compress(b"{}")):
            self.request, self.report = fixture()
            trace = self.report["profile_capture"]["profiles"][0]["traces"][0]
            trace.update(bytes=len(raw), sha256=exchange.digest(raw), gzip_base64=base64.b64encode(raw).decode())
            with self.assertRaises(ValueError):
                self.materialize()

    def test_oversized_or_non_gpu_window_refused(self):
        trace = self.report["profile_capture"]["profiles"][0]["traces"][0]
        trace["bytes"] = profile.PACKED_CAP["prefill"] + 1
        with self.assertRaises(ValueError):
            self.materialize()
        raw = gzip.compress(exchange.canonical({"traceEvents": [{"ph": "X", "cat": "cpu_op", "dur": 5}]}))
        trace.update(bytes=len(raw), sha256=exchange.digest(raw), gzip_base64=base64.b64encode(raw).decode())
        with self.assertRaises(ValueError):
            self.materialize()


if __name__ == "__main__":
    unittest.main()
