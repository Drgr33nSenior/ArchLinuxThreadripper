"""Boundary checks for the source-derived verification metadata projection."""
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_verify
import verify_case_contract as model


class CaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (compile_verify.SOURCE_ROOT / "native-attention/kernels/ops/attention/decode_attention.py").read_bytes()
        cls.metadata = (compile_verify.owner.INFRA / "scratchpad/fp8-norm8-replay.7jdnvw_d/native-metadata/kernels/ops/attention/metadata.py").read_bytes()

    def test_all_workload_rows_and_causal_prefix_sums(self):
        result = model.project(self.source, self.metadata)
        self.assertFalse(result["physical_split_values_observed"])
        for case in result["cases"].values():
            self.assertEqual(len(case["rows"]), 8 * len(case["prefixes"]))
            for index, row in enumerate(case["rows"]):
                self.assertEqual(case["indptr"][index + 1] - case["indptr"][index], row["length"])
                self.assertEqual(row["tail_slots"][1] - row["tail_slots"][0], row["causal_row"] + 1)
            self.assertLess(case["highest_used_slot"], model.SLOTS)

    def test_long_rounding_and_short_empty_splits(self):
        result = model.project(self.source, self.metadata)["cases"]
        long = result["p119990-rows8-sequential"]["rows"][0]["splits"]
        self.assertEqual(long[0]["end"], 7520)
        self.assertEqual(long[0]["iterations"], 235)
        self.assertEqual(long[-1]["iterations"], 225)
        short = result["p724-rows8"]["rows"][0]["splits"]
        self.assertEqual(sum(r["active"] for r in short), 12)
        self.assertEqual(short[11]["last_block_tokens"], 21)
        self.assertEqual([r["iterations"] for r in short[12:]], [0] * 4)

    def test_prefetch_consumption_at_nonzero_split_boundaries(self):
        for length in (725, 732, 59994, 60001, 119991, 119998):
            for split in model.split_spans(self.source, length, 16):
                start, end = split["start"], split["end"]
                if not split["active"]:
                    self.assertEqual(split["iterations"], 0)
                    continue
                current = list(range(start, min(start + 32, end)))
                consumed = []
                for block in range(start, end, 32):
                    upcoming = list(range(block + 32, min(block + 64, end)))
                    consumed.extend(current)
                    current = upcoming
                self.assertEqual(consumed, list(range(start, end)))
                self.assertEqual(current, [])

    def test_changed_sources_and_invalid_lengths_refuse(self):
        with self.assertRaises(ValueError):
            model.singleton_splits(self.metadata + b"\n", 725)
        with self.assertRaises(ValueError):
            model.split_spans(self.source + b"\n", 725, 16)
        for value in (False, 0, -1, 131049, 725.0):
            with self.assertRaises(ValueError):
                model.singleton_splits(self.metadata, value)


if __name__ == "__main__":
    unittest.main()
