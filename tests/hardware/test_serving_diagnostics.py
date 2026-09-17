"""Bounded SGLang observations; synthetic HTTP only, no server or GPU."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import serving


class ServingDiagnosticsTests(unittest.TestCase):
    def observe(self, text):
        with mock.patch.object(serving, "request", return_value=io.BytesIO(text.encode())):
            return serving.server_metrics("http://127.0.0.1:1")

    def test_timestamp_is_not_the_metric_value(self):
        result = self.observe('sglang:num_queue_reqs{model_name="private fixture"} 2 1750000000000\n')
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["metrics"][0]["value"], 2)

    def test_exact_names_and_series_identity_without_private_labels(self):
        result = self.observe('sglang:num_queue_reqs_secret 99\n'
                              'sglang:spec_accept_rate{model_name="private",dp_rank="0"} 0.5\n'
                              'sglang:spec_accept_rate{dp_rank="1",model_name="private"} 0.75\n'
                              'sglang:mamba_used_tokens{model_name="private"} 7\n')
        self.assertEqual(len(result["metrics"]), 3)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotEqual(result["metrics"][0]["series_sha256"], result["metrics"][1]["series_sha256"])
        self.assertEqual(result["metrics"][2]["value"], 7)

    def test_bad_or_partial_observations_never_become_zero_or_success(self):
        for text in ('sglang:spec_accept_rate NaN\n', 'sglang:spec_accept_rate 1.1\n',
                     'sglang:mamba_used_tokens -1\n', 'sglang:mamba_used_tokens 1.5\n',
                     'sglang:num_queue_reqs 1\nsglang:spec_accept_rate broken\n',
                     'sglang:num_queue_reqs{a="1",a="2"} 1\n',
                     'sglang:num_queue_reqs{a="1",b="2"} 1\nsglang:num_queue_reqs{b="2",a="1"} 2\n',
                     '# ' + 'x' * (8 * 1024 * 1024)):
            with self.subTest(text=text[:80]):
                result = self.observe(text)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["metrics"], [])

    def test_zero_is_observed_but_missing_is_unavailable(self):
        self.assertEqual(self.observe('sglang:mamba_used_tokens 0\n')["metrics"][0]["value"], 0)
        self.assertEqual(self.observe('# no supported metrics\n')["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
