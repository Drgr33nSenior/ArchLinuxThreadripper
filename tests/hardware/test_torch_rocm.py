"""Production benchmark control-flow fixtures; no torch or GPU required."""
import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class Tensor:
    def to(self, _):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def __matmul__(self, _):
        return self


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.torch = types.ModuleType("torch")
        self.torch.float32, self.torch.float16, self.torch.bfloat16 = "fp32", "fp16", "bf16"
        self.torch.randn = Mock(side_effect=lambda *args: Tensor())
        self.torch.empty_like = Mock(side_effect=lambda *args: Tensor())
        self.torch.manual_seed = Mock()
        self.torch.mm = Mock()
        self.torch.testing = types.SimpleNamespace(assert_close=Mock())
        self.duration = 2.0
        self.torch.cuda = types.SimpleNamespace(
            set_device=Mock(), is_bf16_supported=Mock(return_value=True),
            Event=Mock(side_effect=lambda **kwargs: types.SimpleNamespace(
                record=Mock(), synchronize=Mock(), elapsed_time=Mock(side_effect=lambda _: self.duration))))
        modules = {"torch": self.torch, "torch.distributed": types.ModuleType("torch.distributed"),
                   "torch.multiprocessing": types.ModuleType("torch.multiprocessing")}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec = importlib.util.spec_from_file_location("torch_rocm_fixture", Path(__file__).with_name("torch-rocm.py"))
        self.producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.producer)

    def test_all_cases_all_readbacks_and_event_units(self):
        rows = self.producer.matmul_benchmark(2)
        self.assertEqual(len(rows), 12)
        self.assertEqual({(r["device"], r["dimension"], r["dtype"]) for r in rows},
                         {(g, n, d) for g in (0, 1) for n in (1024, 2048) for d in ("fp32", "fp16", "bf16")})
        for row in rows:
            self.assertEqual(row["seconds"], [0.002] * 10)
            self.assertEqual(row["median_seconds"], 0.002)
            self.assertEqual(row["warmups"], 2)
            self.assertEqual(row["correctness"], "passed")
        self.assertEqual(self.torch.mm.call_count, 144)
        self.assertTrue(all(set(call.kwargs) == {"out"} for call in self.torch.mm.call_args_list))
        self.assertEqual(self.torch.testing.assert_close.call_count, 144)
        self.assertEqual(self.torch.cuda.Event.call_count, 288)
        self.assertTrue(all(call.kwargs == {"enable_timing": True} for call in self.torch.cuda.Event.call_args_list))
        self.torch.manual_seed.assert_called_once_with(42)

    def test_invalid_event_times_refused(self):
        for value in (0, -1, math.inf, math.nan):
            with self.subTest(value=value):
                self.duration = value
                with self.assertRaisesRegex(RuntimeError, "duration"):
                    self.producer.matmul_benchmark(1)

    def test_bad_readback_is_failure_not_timing_success(self):
        self.torch.testing.assert_close.side_effect = AssertionError("fixture mismatch")
        with self.assertRaises(AssertionError):
            self.producer.matmul_benchmark(1)

    def test_bf16_unavailable_cannot_be_complete_benchmark(self):
        self.torch.cuda.is_bf16_supported.return_value = False
        with self.assertRaisesRegex(RuntimeError, "BF16"):
            self.producer.matmul_benchmark(1)

    def test_conflicting_modes_are_refused_before_device_access(self):
        with patch.object(sys, "argv", ["probe", "--count", "2", "--model", "R9700", "--collective", "--matmul-benchmark"]):
            with self.assertRaises(SystemExit) as raised:
                self.producer.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
