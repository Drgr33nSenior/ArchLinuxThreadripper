"""Exercise real publishers, collector and reader with synthetic native work."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_owner as owner
import shared_diagnostic as core
import shared_readout as readout
from test_shared_diagnostic import identity, menu, originals, scheduler_class, drive
import test_shared_capture as capture_fixture
from test_shared_readout import http_fixture
from shared_capture import CaptureObserver


class TestOwner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.menu = menu()
    def tearDown(self): self.tmp.cleanup()

    def paired(self, start=1):
        root = self.root / f"start-{start}"; root.mkdir()
        identities = []; pins = []; mounts = []; devices = []; records = []
        for rank in (0, 1):
            if start == 1:
                ident = dict(identity(rank), start=1, nonce="1" * 16)
                o = CaptureObserver(root, ident, self.menu)
                cls = scheduler_class()
                with owner.worker_scope(o, cls, originals(cls)): drive(cls, self.menu)
                mounts.append(None); devices.append(None)
            else:
                f = capture_fixture.TestCapture(); f.setUp()
                try:
                    f.identity = dict(identity(rank), start=2, nonce="2" * 16)
                    f.menu = self.menu
                    torch = capture_fixture.FakeTorch(rank=rank); o = f.observer(torch)
                    with patch("test_shared_capture.d.scope", owner.worker_scope): f.drive(o, torch)
                    for p in f.root.iterdir():
                        if p.is_file(): shutil.copyfile(p, root / p.name)
                    ident = o.identity; mounts.append(o.backend.mount)
                    device = dict(rank=rank, pid=ident["pid"], worker_identity_sha256=ident["worker_identity_sha256"],
                                  device_uuid=f"GPU-fixture-{rank}", trace_device_index=0)
                    devices.append(owner._publish(root, f"SHARED-DEVICE-rank-{rank}.json", device))
                finally: f.tearDown()
            identities.append(ident); pins.append(o.owner_close_pin)
            records.append(core.read_recording(root, ident, self.menu, o.summary_sha256))
        summary = core.canonical(http_fixture(records[0], self.menu))
        http_pin = owner.publish_http(root, identities[0], self.menu, summary, core.sha(summary))
        return root, identities, pins, mounts, devices, summary, http_pin

    def replay(self, data):
        root, identities, pins, mounts, devices, summary, http_pin = data
        return owner.read_start(root, identities, self.menu, pins, http_pin, summary, core.sha(summary),
                                raw_mounts=mounts, device_pins=devices)

    def test_complete_both_rank_unprofiled_publication_and_replay(self):
        result = self.replay(self.paired())
        self.assertEqual(result["status"], "OWNER_OBSERVATIONS_REPLAYED")
        self.assertEqual(result["rank_comparisons"], [])
        self.assertFalse(result["diagnostic_success"])
        self.assertTrue(result["upstream_source_wire_quality_release_required"])

    def test_profiled_publication_collection_and_all_seven_joins(self):
        result = self.replay(self.paired(2))
        self.assertEqual(len(result["rank_comparisons"]), 7)
        self.assertEqual(len(result["transport_index"]["files"]), 53)
        self.assertFalse(result["transport_index"]["semantic_validation"])

    def test_complete_cross_start_path_keeps_work_difference_visible(self):
        a = self.replay(self.paired(1)); b = self.replay(self.paired(2))
        # The synthetic capture fixture has more decode rounds; it must not be
        # relabelled equal work merely because publication and joins succeeded.
        result = readout.compare_starts(a["records"], b["records"], a["http"], b["http"])
        self.assertEqual(result["status"], "NOT_COMPARABLE")

    def test_receipt_pin_cannot_be_replaced_by_a_collector_hash(self):
        data = self.paired(); root, _, pins, _, _, _, _ = data
        path = root / pins[0]["file"]; value = json.loads(path.read_bytes())
        value["core_summary_sha256"] = "f" * 64; path.write_bytes(core.canonical(value))
        self.assertIn(path.name, owner.collect(root)["files"])
        with self.assertRaisesRegex(ValueError, "owner artifact digest"): self.replay(data)

    def test_wrong_start_http_receipt_and_source_digest_refused(self):
        data = self.paired(); root, identities, _, _, _, summary, http_pin = data
        with self.assertRaisesRegex(ValueError, "validated HTTP summary identity"):
            owner.publish_http(root, identities[0], self.menu, summary, "f" * 64)
        value = json.loads((root / http_pin["file"]).read_bytes()); value["start_binding"]["start"] = 2
        raw = core.canonical(value); (root / http_pin["file"]).write_bytes(raw)
        http_pin.update(sha256=core.sha(raw), bytes=len(raw))
        with self.assertRaisesRegex(ValueError, "HTTP receipt / start binding"): self.replay(data)

    def test_single_use_publishers_refuse_overwrite(self):
        data = self.paired(); root, identities, pins, _, _, summary, _ = data
        receipt = json.loads((root / pins[0]["file"]).read_bytes())
        with self.assertRaises(FileExistsError): owner.publish_worker_close(root, identities[0], receipt)
        with self.assertRaises(FileExistsError): owner.publish_http(root, identities[0], self.menu, summary, core.sha(summary))

    def test_menu_completion_publishes_before_worker_scope_returns(self):
        o = CaptureObserver(self.root, dict(identity(), start=1), self.menu)
        cls = scheduler_class(); expected = originals(cls)
        with patch.object(owner, "publish_worker_close", wraps=owner.publish_worker_close) as publish:
            with owner.worker_scope(o, cls, expected):
                drive(cls, self.menu)
                self.assertTrue((self.root / "SHARED-CLOSE-rank-0.json").is_file())
                self.assertIs(cls.run_batch, expected["run_batch"])
                self.assertIs(cls.process_batch_result, expected["process_batch_result"])
                self.assertNotIn("process", o.__dict__)
            self.assertEqual(publish.call_count, 1)

    def test_parent_barrier_binds_both_rank_receipts(self):
        data = self.paired(); root, identities, pins, *_ = data
        actual = owner.wait_worker_close(root, identities, self.menu, 10.0, clock=lambda: 1.0,
                                         sleep=lambda _: self.fail("both receipts already exist"))
        self.assertEqual(actual, pins)

    def test_profiled_parent_barrier_binds_both_rank_receipts(self):
        data = self.paired(2); root, identities, pins, *_ = data
        self.assertEqual(owner.wait_worker_close(root, identities, self.menu, 10.0, clock=lambda: 1.0), pins)

    def test_parent_barrier_waits_for_partial_exclusive_write(self):
        data = self.paired(); root, identities, pins, *_ = data
        path = root / pins[0]["file"]; raw = path.read_bytes(); path.write_bytes(b"{")
        now = [1.0]; slept = []
        def sleep(seconds):
            slept.append(seconds); now[0] += seconds; path.write_bytes(raw)
        actual = owner.wait_worker_close(root, identities, self.menu, 10.0, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(actual, pins); self.assertEqual(len(slept), 1)

    def test_failed_start_keeps_zero_single_and_missing_rank_without_success_reader(self):
        with patch.object(owner, "read_start", side_effect=AssertionError("success path")):
            result = owner.failed_start(self.root / "absent")
            self.assertIsNone(result["transport_index"])
            self.assertEqual(owner.failed_start(self.root)["present_close_ranks"], [])
            (self.root / "SHARED-CLOSE-rank-1.json").write_bytes(b"{")
            result = owner.failed_start(self.root)
            self.assertEqual(result["present_close_ranks"], [1])
            self.assertFalse(result["independent_release"])
            self.assertFalse(result["grading_performed"])

    def test_complete_reader_rejects_extra_allowed_file_for_wrong_start(self):
        data = self.paired()
        (data[0] / "SHARED-DEVICE-rank-0.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "exact shared start file set"): self.replay(data)

    def test_device_publisher_checks_actual_namespace_and_is_exclusive(self):
        ident = dict(identity(1), start=2)
        devices = [dict(index=r, uuid=f"GPU-fixture-{r}") for r in (0, 1)]
        cuda = NS(current_device=lambda: 1, is_current_stream_capturing=lambda: False,
                  get_device_properties=lambda index: NS(uuid=devices[index]["uuid"]))
        pin = owner.publish_device(self.root, ident, NS(cuda=cuda), devices, 1)
        value = json.loads((self.root / pin["file"]).read_bytes())
        self.assertEqual(value["trace_device_index"], 1)
        self.assertEqual(value["device_uuid"], "GPU-fixture-1")
        with self.assertRaises(FileExistsError): owner.publish_device(self.root, ident, NS(cuda=cuda), devices, 1)
        cuda.current_device = lambda: 0
        with self.assertRaisesRegex(ValueError, "worker device boundary"):
            owner.publish_device(self.root, ident, NS(cuda=cuda), devices, 1)

    def test_instance_check_rejects_subclass_shadow_and_second_instance(self):
        from test_shared_diagnostic import batch
        for mutation in ("subclass", "shadow", "second"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve(); cls = scheduler_class()
                o = CaptureObserver(root, dict(identity(), start=1), self.menu)
                with owner.worker_scope(o, cls, originals(cls)):
                    instance = type("Other", (cls,), {})() if mutation == "subclass" else cls()
                    if mutation == "shadow": instance.process_batch_result = lambda *a: None
                    if mutation == "second":
                        first = cls(); b = batch(self.menu[3:4])
                        first.process_batch_result(b, first.run_batch(b))
                    with self.assertRaisesRegex(ValueError, "Scheduler instance"):
                        instance.run_batch(batch(self.menu[3:4]))
                    self.assertTrue((root / "SHARED-CLOSE-rank-0.json").is_file())

    def test_disabled_observer_run_publishes_before_return(self):
        from test_shared_diagnostic import batch
        cls = scheduler_class(); o = CaptureObserver(self.root, dict(identity(), start=1), self.menu)
        with owner.worker_scope(o, cls, originals(cls)):
            o.disabled = True
            cls().run_batch(batch(self.menu[3:4]))
            self.assertTrue((self.root / "SHARED-CLOSE-rank-0.json").is_file())

    def test_bounded_reader_rejects_hardlink_fifo_and_changed_file(self):
        path = self.root / "SHARED-CLOSE-rank-0.json"; path.write_bytes(b"{}")
        os.link(path, self.root / "linked")
        with self.assertRaisesRegex(ValueError, "bounded regular"): core._file(self.root, path.name, 4096)
        (self.root / "linked").unlink(); path.unlink(); os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "bounded regular"): core._file(self.root, path.name, 4096)
        path.unlink(); path.write_bytes(b"{}")
        original_read = os.read
        def altered(fd, size):
            raw = original_read(fd, size)
            path.write_bytes(b"changed")
            return raw
        with patch("shared_diagnostic.os.read", side_effect=altered):
            with self.assertRaisesRegex(ValueError, "artifact changed"): core._file(self.root, path.name, 4096)

    def test_parent_barrier_never_exceeds_deadline_or_accepts_foreign_worker(self):
        identities = [dict(identity(r), start=1) for r in (0, 1)]
        now = [1.0]
        def sleep(seconds): now[0] += seconds
        with self.assertRaisesRegex(ValueError, "close publication deadline"):
            owner.wait_worker_close(self.root, identities, self.menu, 1.04, clock=lambda: now[0], sleep=sleep)
        self.assertLessEqual(now[0], 1.04)
        data = self.paired(); root, identities, pins, *_ = data
        row = json.loads((root / pins[0]["file"]).read_bytes()); row["identity"]["pid"] += 1000
        (root / pins[0]["file"]).write_bytes(core.canonical(row))
        with self.assertRaisesRegex(ValueError, "published worker identity"):
            owner.wait_worker_close(root, identities, self.menu, 10.0, clock=lambda: 1.0)

    def test_callback_failure_publishes_before_scheduler_can_catch_it(self):
        o = CaptureObserver(self.root, dict(identity(), start=1), self.menu)
        cls = scheduler_class(); error = RuntimeError("native callback")
        def fail(*args): raise error
        cls.run_batch = fail; expected = originals(cls)
        from test_shared_diagnostic import batch
        with owner.worker_scope(o, cls, expected):
            with self.assertRaises(RuntimeError) as caught: cls().run_batch(batch(self.menu[3:4]))
            self.assertIs(caught.exception, error)
            self.assertTrue((self.root / "SHARED-CLOSE-rank-0.json").is_file())
            self.assertIs(cls.run_batch, fail)

    def test_authenticated_worker_entry_refuses_before_output_and_restores(self):
        from test_shared_runtime_binding import fixture
        cls, sources = fixture(); ident = dict(identity(), start=1)
        sources["stage"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "callable source digest"):
            with owner.authenticated_worker_scope(self.root, ident, self.menu, cls, sources):
                self.fail("must refuse before entering worker")
        self.assertEqual(list(self.root.iterdir()), [])
        cls, sources = fixture(); expected = originals(cls)
        with owner.authenticated_worker_scope(self.root, ident, self.menu, cls, sources) as observer:
            self.assertIsNot(cls.run_batch, expected["run_batch"])
        self.assertIs(cls.run_batch, expected["run_batch"])
        self.assertIsNotNone(observer.owner_close_pin)

    def test_native_exception_retains_failure_receipt_and_restores(self):
        ident = dict(identity(), start=1); o = CaptureObserver(self.root, ident, self.menu)
        cls = scheduler_class(); expected = originals(cls)
        class Sentinel(BaseException): pass
        error = Sentinel("original")
        with self.assertRaises(Sentinel) as caught:
            with owner.worker_scope(o, cls, expected): raise error
        self.assertIs(caught.exception, error)
        self.assertIs(cls.run_batch, expected["run_batch"])
        retained = json.loads((self.root / "SHARED-CLOSE-rank-0.json").read_bytes())
        self.assertEqual(retained["core_summary_sha256"], o.summary_sha256)
        record = core.read_recording(self.root, ident, self.menu, o.summary_sha256)
        self.assertEqual(record["status"], "INCOMPLETE")
        self.assertFalse(owner.collect(self.root)["independent_release"])

    def test_publication_interrupt_never_replaces_native_exception(self):
        o = CaptureObserver(self.root, dict(identity(), start=1), self.menu)
        cls = scheduler_class(); expected = originals(cls); error = RuntimeError("model")
        with patch.object(owner, "publish_worker_close", side_effect=KeyboardInterrupt("publish")):
            with self.assertRaises(RuntimeError) as caught:
                with owner.worker_scope(o, cls, expected): raise error
        self.assertIs(caught.exception, error); self.assertIs(cls.run_batch, expected["run_batch"])

    def test_foreign_hook_kept_and_failure_receipt_published(self):
        o = CaptureObserver(self.root, dict(identity(), start=1), self.menu)
        cls = scheduler_class(); expected = originals(cls)
        foreign = lambda *a: None
        with self.assertRaisesRegex(ValueError, "foreign hook retained"):
            with owner.worker_scope(o, cls, expected): cls.run_batch = foreign
        self.assertIs(cls.run_batch, foreign)
        self.assertIs(cls.process_batch_result, expected["process_batch_result"])
        receipt = json.loads((self.root / "SHARED-CLOSE-rank-0.json").read_bytes())
        self.assertEqual(receipt["core_summary_sha256"], o.summary_sha256)
        record = core.read_recording(self.root, o.identity, self.menu, o.summary_sha256)
        self.assertEqual(record["status"], "INCOMPLETE")

    def test_capture_write_failure_retains_core_pin_without_success(self):
        o = CaptureObserver(self.root, dict(identity(), start=1), self.menu); cls = scheduler_class()
        with patch("shared_capture.exclusive", side_effect=OSError("full")):
            with owner.worker_scope(o, cls, originals(cls)): drive(cls, self.menu)
        receipt = json.loads((self.root / "SHARED-CLOSE-rank-0.json").read_bytes())
        self.assertIsNone(receipt["capture_sha256"]); self.assertEqual(receipt["capture_status"], "INCOMPLETE")
        self.assertIsNotNone(receipt["core_summary_sha256"])

    def test_unknown_symlink_and_oversize_artifacts_refused(self):
        for name, action in (
            ("unrelated.txt", lambda p: p.write_bytes(b"{}")),
            ("unrelated-directory", lambda p: p.mkdir()),
            ("CAPTURE-rank-0.json", lambda p: p.mkdir()),
            ("SHARED-extra.json", lambda p: p.write_bytes(b"{}")),
            ("CAPTURE-rank-0.json", lambda p: p.symlink_to(__file__)),
            ("SHARED-CLOSE-rank-0.json", lambda p: p.write_bytes(b"x" * 4097))):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder).resolve(); action(root / name)
                with self.assertRaises(ValueError): owner.collect(root)


if __name__ == "__main__": unittest.main()
