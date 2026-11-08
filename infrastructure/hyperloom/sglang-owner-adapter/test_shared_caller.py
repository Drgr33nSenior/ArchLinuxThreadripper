"""Run the real finite caller with inert step functions; never launch a process."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CALLER = Path(os.environ["SHARED_CALLER_FIXTURE"])


class TestCaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        spec = importlib.util.spec_from_file_location("shared_finite_caller", CALLER)
        self.c = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.c)
        self.steps = []
        self.grant = dict(nonce="0" * 16, global_gpu_attempt_index=1)
    def tearDown(self): self.tmp.cleanup()
    def runner(self, failed):
        def run(name, cwd, argv, timeout, steps_root):
            self.steps.append((name, argv, timeout))
            return dict(name=name, exit_code=int(name == failed))
        return run
    def collector(self, root):
        self.steps.append(("collect", [], 150)); return dict(name="collect", exit_code=0)
    def sampler(self, *args):
        return lambda: dict(name="host-sample", exit_code=0)

    def test_missing_approval_cannot_create_a_run_or_invoke_subprocess(self):
        with patch.object(self.c, "ROOT", self.root), patch.object(self.c.subprocess, "Popen", side_effect=AssertionError("spawn")):
            with self.assertRaises(FileNotFoundError): self.c.admit("0" * 64)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_wrong_power_stops_before_upload_or_launch(self):
        result = self.c.run(self.grant, self.root, self.runner("power-pre"), self.collector, self.sampler)
        self.assertEqual([r[0] for r in self.steps], ["power-pre"])
        self.assertEqual(result["status"], "STOPPED_BEFORE_LAUNCH_PRE_CHECK")
        self.assertFalse(result["automatic_retry"])

    def test_failed_launch_collects_and_observes_failed_release_once(self):
        with patch.object(self.c, "closeout_success", return_value=False):
            result = self.c.run(self.grant, self.root, self.runner("launch"), self.collector, self.sampler)
        self.assertEqual([r[0] for r in self.steps], ["power-pre", "upload", "launch", "collect", "closeout", "power-post", "host-faults"])
        self.assertEqual(next(r[1] for r in self.steps if r[0] == "closeout"), ["observe_failed_closeout.py"])
        self.assertEqual(result["status"], "INCOMPLETE_RETAINED")
        self.assertFalse(result["automatic_retry"] or result["qualification"] or result["selection"])


if __name__ == "__main__": unittest.main()
