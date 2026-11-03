"""CPU acceptance of an inert generated owner and grading template.

OWNER_PREPARATION selects the fresh packet. QUALITY_FIXTURE_ROOT selects a
retained input/decode pair. Rebinding those answers to the new request identities
is explicitly a fixture; it proves template behavior, not a new native decode.
"""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PreparedOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["OWNER_PREPARATION"]).resolve()
        cls.ids = json.loads((cls.root / "author/PACKAGE-IDENTITIES.json").read_bytes())
        cls.count = 2 * cls.ids["requests_per_start"]
        cls.template = cls.root / "quality-template-c4ab"
        sys.path.insert(0, str(cls.template / "source"))
        cls.contract = load("quality_contract", cls.template / "source/quality_contract.py")
        cls.menu = json.loads((cls.template / "source/EXPECTED-QUALITY.json").read_bytes())

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.template / "source"))

    def test_caller_closure_finite_limits_and_no_registration(self):
        binding = json.loads((self.root / "author/LAUNCHER-BINDINGS.json").read_bytes())
        for name, pin in binding["upload_inventory"].items():
            self.assertEqual(digest((self.root / "author/owner-upload" / name).read_bytes()), pin)
        plan = json.loads((self.root / "launcher/CALLER-PLAN.json").read_bytes())
        for name, pin in plan["caller_inventory"].items():
            self.assertEqual(digest((self.root / "launcher/caller-preview" / name).read_bytes()), pin)
        expected = dict(requests=self.count, natural_grades=self.count, model_starts=2,
                        owner_seconds=6360, caller_seconds=7160, cpu_grading_seconds=320)
        self.assertEqual({k: plan["limits"][k] for k in expected}, expected)
        self.assertFalse(self.ids["launch_authorized"])
        self.assertFalse((self.root / "REGISTRATION.json").exists())

    def test_caller_attempts_once_and_retains_each_failure(self):
        caller = load("_hyperloom_test_caller", self.root / "launcher/execute_once.py")
        for fail in (None, "power-pre", "upload", "launch", "collect", "closeout", "power-post"):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                calls = []

                def runner(name, *args):
                    calls.append(name)
                    return dict(name=name, exit_code=int(name == fail))

                def collector(*args):
                    return runner("collect")

                caller.closeout_success = lambda *args: True
                result = caller.run(dict(nonce="a" * 16, global_gpu_attempt_index=999999),
                    Path(temporary).resolve(), runner, collector,
                    lambda *args: lambda: dict(name="sample", exit_code=0))
                self.assertLessEqual(calls.count("launch"), 1)
                if fail in ("power-pre", "upload"):
                    self.assertNotIn("launch", calls)
                else:
                    self.assertEqual(calls.count("collect"), 1)
                    self.assertIn("power-post", calls)
                self.assertEqual(result["status"] == "COMPLETE", fail is None)
                self.assertFalse(result["automatic_retry"])

    def test_unregistered_entry_refuses(self):
        caller = load("_hyperloom_unregistered_caller", self.root / "launcher/execute_once.py")
        with self.assertRaises(FileNotFoundError):
            caller.admit("0" * 64)

    def fixture(self):
        source = Path(os.environ["QUALITY_FIXTURE_ROOT"])
        input_raw = (source / "source/QUALITY-CPU-INPUT.json").read_bytes()
        decode_raw = (source / "retained-cpu/output/quality/DECODE-RESULT.json").read_bytes()
        self.assertEqual(digest(input_raw), "66a8b2f472f1ca961e63b77aa3cc90fb76ccfc1c8b12d2e33c86a147714bc7af")
        self.assertEqual(digest(decode_raw), "322a6b65b6459ed3618bfa3086a23f8d882212b347f96a282736d2e68deafb7c")
        original = json.loads(input_raw)
        decoded = json.loads(decode_raw)["answers"]
        rows, answers = [], []
        for plan in self.menu:
            index = next(i for i, row in enumerate(original["rows"])
                         if row["arm"] == plan["arm"] and row["expected_answer"] == plan["expected_answer"]
                         and row["minimum_final_tokens"] == plan["minimum_final_tokens"])
            row, answer = copy.deepcopy(original["rows"][index]), copy.deepcopy(decoded[index])
            for key in ("ordinal", "case", "request_body_sha256"):
                row[key] = answer[key] = plan[key]
            rows.append(row)
            answers.append(answer)
        value = dict(original, mode=self.contract.MODE, rows=rows)
        return value, answers

    def test_complete_rebound_fixture_passes_and_wrong_oracle_fails(self):
        binder = load("_hyperloom_quality_binder", self.template / "bind_future.py")
        pin = digest((self.template / "PROJECTION-INVENTORY.json").read_bytes())
        binder.authenticate_projection(self.template, pin)
        value, answers = self.fixture()
        self.contract.validate_input(value, self.menu)
        result = self.contract.grade_answers(value, self.menu, answers, [])
        self.assertEqual(result["decoded_rows"], self.count)
        self.assertTrue(result["quality_pass"] and result["long_answer_coverage_pass"])
        self.assertFalse(result["qualification"])
        answers[-1]["grade_value_sha256"] = "0" * 64
        self.assertFalse(self.contract.grade_answers(value, self.menu, answers, [])["quality_pass"])
        if self.count == 60:
            self.assertEqual(sum(row["minimum_final_tokens"] == 512 for row in self.menu), 18)

    def test_missing_or_misbound_answer_refused(self):
        value, answers = self.fixture()
        with self.assertRaises(ValueError):
            self.contract.grade_answers(value, self.menu, answers[:-1], [])
        answers[-1]["request_body_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.contract.grade_answers(value, self.menu, answers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
