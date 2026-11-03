from array import array
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("profile_c2", ROOT / "infrastructure/hyperloom/sglang-owner-adapter/profile_c2.py")
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


class Mode:
    def __init__(self, name):
        self.name = name

    def is_extend(self):
        return self.name == "EXTEND"

    def is_decode(self):
        return self.name == "DECODE"


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.requests = [SimpleNamespace(rid="pair-" + str(i), cache_salt="salt-" + str(i),
                                        origin_input_ids=array("i", range(6000))) for i in range(2)]
        raw = (json.dumps(list(range(6000)), separators=(",", ":")) + "\n").encode()
        self.frozen = {"requests": [dict(rid=r.rid, cache_salt=r.cache_salt, input_tokens=6000,
                                          input_ids_sha256=hashlib.sha256(raw).hexdigest()) for r in self.requests]}
        self.capture = SimpleNamespace(report=dict(prefill_tokens=0, prefill_batches=0, decode_batches=0, captures=[]),
                                       disabled=False, seen_request=False)
        profile.c2_initialize(self.capture)

    def batch(self, members, starts=None, lengths=None):
        return SimpleNamespace(reqs=[self.requests[i] for i in members],
            forward_mode=Mode("EXTEND" if starts is not None else "DECODE"),
            prefix_lens=starts, extend_lens=lengths, extend_num_tokens=sum(lengths) if lengths is not None else None)

    def step(self, batch):
        selected = profile.c2_select(self.capture, batch, self.frozen)
        if selected:
            phase, index = selected
            self.capture.report["captures"].append(dict(phase=phase, index=index,
                batch=copy.deepcopy(self.capture.c2_selected_batch), status="COMPLETE"))
        return selected

    def prefill(self):
        for members, starts, lengths in (
            ([0], [0], [2048]),
            ([0], [2048], [2048]),
            ([0, 1], [4096, 0], [1904, 144]),
            ([1], [144], [2048]),
            ([0, 1], [6000, 2192], [1, 2048]),
            ([1], [4240], [1760]),
        ):
            self.step(self.batch(members, starts, lengths))

    def complete(self):
        self.prefill()
        for _ in range(4):
            self.step(self.batch([0, 1]))

    def test_real_array_tokens_mixed_prefill_and_actual_pair_decode(self):
        self.complete()
        self.assertTrue(profile.c2_complete(self.capture.report, self.frozen))
        self.assertEqual(self.capture.report["prefill_tokens"], 12000)
        self.assertEqual([(c["phase"], c["index"]) for c in self.capture.report["captures"]],
                         [("prefill", 0), ("prefill", 1), ("prefill", 2)] + [("decode", i) for i in range(4)])
        middle = self.capture.report["captures"][1]["batch"]
        self.assertEqual(middle["rids"], ["pair-0", "pair-1"])
        self.assertEqual(middle["prompt_tokens_after"], 6144)

    def test_one_request_decode_does_not_claim_c2_coverage(self):
        self.prefill()
        for _ in range(8):
            self.assertIsNone(self.step(self.batch([1])))
        self.assertFalse(profile.c2_complete(self.capture.report, self.frozen))
        self.assertEqual(self.capture.report["decode_batches"], 0)

    def test_decode_before_prefill_is_refused(self):
        with self.assertRaisesRegex(ValueError, "before complete"):
            self.step(self.batch([0, 1]))

    def test_unrelated_request_is_ignored_but_contaminated_batch_refused(self):
        other = SimpleNamespace(rid="unrelated")
        self.assertIsNone(profile.c2_select(self.capture, SimpleNamespace(reqs=[other]), self.frozen))
        with self.assertRaisesRegex(ValueError, "unexpected"):
            profile.c2_select(self.capture, SimpleNamespace(reqs=[self.requests[0], other]), self.frozen)

    def test_missing_or_repeated_cold_interval_is_refused(self):
        self.step(self.batch([0], [0], [2048]))
        for start in (0, 2050):
            with self.subTest(start=start), self.assertRaisesRegex(ValueError, "skipped, reused"):
                self.step(self.batch([0], [start], [2048]))

    def test_token_or_salt_mismatch_is_refused(self):
        self.requests[0].origin_input_ids[0] = 42
        with self.assertRaisesRegex(ValueError, "token identity"):
            self.step(self.batch([0], [0], [2048]))
        self.requests[0].cache_salt = "wrong"
        with self.assertRaisesRegex(ValueError, "namespace"):
            self.step(self.batch([0], [0], [2048]))

    def test_device_like_extent_is_not_converted_or_inspected(self):
        class DeviceValue:
            def __int__(self):
                raise AssertionError("device access")

            def __iter__(self):
                raise AssertionError("device access")

        batch = self.batch([0], [0], [2048])
        batch.prefix_lens = DeviceValue()
        with self.assertRaisesRegex(ValueError, "CPU scheduler"):
            self.step(batch)

    def test_trace_batch_substitution_fails_replay(self):
        self.complete()
        self.capture.report["captures"][-1]["batch"]["rids"] = ["pair-1"]
        with self.assertRaisesRegex(ValueError, "witness differ"):
            profile.c2_complete(self.capture.report, self.frozen)

    def test_wrong_milestone_cannot_claim_complete(self):
        self.complete()
        self.capture.report["batch_coverage"]["prefill"][1]["prompt_tokens_before"] = 100
        self.capture.report["batch_coverage"]["prefill"][1]["prompt_tokens_after"] = 101
        with self.assertRaisesRegex(ValueError, "milestone"):
            profile.c2_complete(self.capture.report, self.frozen)


if __name__ == "__main__":
    unittest.main()
