"""CPU-only rocprof conversion semantics and refusal tests."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import rocprof_trace as adapter


def capture():
    return {"rocprofiler-sdk-tool": [{
        "metadata": {"pid": 7, "init_time": 2**60, "fini_time": 2**60 + 10000},
        "agents": [{"id": {"handle": 22}, "type": 2, "gpu_index": 0, "name": "gfx1201",
                    "location_id": 49920, "domain": 0}],
        "kernel_symbols": [{"kernel_id": 9, "demangled_kernel_name": "test_gemm"}],
        "strings": {"buffer_records": [{"kind": "HIP_RUNTIME_API_EXT", "operations": ["hipLaunchKernel"]}]},
        "buffer_records": {
            "kernel_dispatch": [{"start_timestamp": 2**60 + 1001, "end_timestamp": 2**60 + 9002,
                                 "correlation_id": {"internal": 4}, "stream_id": {"handle": 5},
                                 "dispatch_info": {"dispatch_id": 1, "kernel_id": 9,
                                     "agent_id": {"handle": 22}, "queue_id": {"handle": 3},
                                     "grid_size": {"x": 1024, "y": 1, "z": 1},
                                     "workgroup_size": {"x": 512, "y": 1, "z": 1}}}],
            "hip_api": [{"start_timestamp": 2**60 + 10, "end_timestamp": 2**60 + 20,
                         "correlation_id": {"internal": 4}, "thread_id": 7, "kind": 0, "operation": 0}],
            "memory_copy": [{"omitted": True}]}}]}


class ConversionTests(unittest.TestCase):
    def convert(self, data):
        return adapter.convert(json.dumps(data).encode())

    def test_duration_geometry_device_and_hip_correlation_without_operator_invention(self):
        result = self.convert(capture())
        kernel, hip = result["traceEvents"][:2]
        self.assertEqual((kernel["ts"], kernel["dur"]), (1.001, 8.001))
        self.assertEqual(kernel["args"]["grid"], [2, 1, 1])
        self.assertEqual(kernel["args"]["block"], [512, 1, 1])
        self.assertEqual(kernel["args"]["correlation"], hip["args"]["correlation"])
        self.assertEqual(hip["cat"], "cuda_runtime")
        self.assertNotEqual(kernel["pid"], hip["pid"])
        self.assertEqual(result["deviceProperties"][0]["pciBusID"], "0000:c3:00.0")
        self.assertNotIn("External id", json.dumps(result))
        self.assertNotIn('"cat": "cpu_op"', json.dumps(result))
        self.assertFalse(result["rocprof_conversion"]["operator_attribution"])
        self.assertEqual(result["rocprof_conversion"]["omitted_record_counts"], {"memory_copy": 1})

    def test_rejects_incomplete_ambiguous_or_unusable_captures(self):
        for case in ("empty", "multiprocess", "unknown_symbol", "unknown_agent", "duplicate_dispatch",
                     "duplicate_agent", "bad_gpu", "zero_duration", "reversed", "outside", "bool_time",
                     "bad_operation", "bad_geometry"):
            data = capture()
            process = data["rocprofiler-sdk-tool"][0]
            kernels = process["buffer_records"]["kernel_dispatch"]
            row = kernels[0]
            if case == "empty": kernels.clear()
            elif case == "multiprocess": data["rocprofiler-sdk-tool"].append(copy.deepcopy(process))
            elif case == "unknown_symbol": row["dispatch_info"]["kernel_id"] = 100
            elif case == "unknown_agent": row["dispatch_info"]["agent_id"]["handle"] = 100
            elif case == "duplicate_dispatch": kernels.append(copy.deepcopy(row))
            elif case == "duplicate_agent": process["agents"].append(copy.deepcopy(process["agents"][0]))
            elif case == "bad_gpu": process["agents"][0]["type"] = 1
            elif case == "zero_duration": row["end_timestamp"] = row["start_timestamp"]
            elif case == "reversed": row["end_timestamp"] = row["start_timestamp"] - 1
            elif case == "outside": row["end_timestamp"] = process["metadata"]["fini_time"] + 1
            elif case == "bool_time": row["start_timestamp"] = True
            elif case == "bad_operation": process["buffer_records"]["hip_api"][0]["operation"] = 100
            elif case == "bad_geometry": row["dispatch_info"]["workgroup_size"]["x"] = 0
            with self.subTest(case=case), self.assertRaises((ValueError, KeyError)):
                self.convert(data)

    def test_rejects_truncation_duplicate_keys_nonfinite_and_oversize(self):
        for raw in (b'{"rocprofiler-sdk-tool":', b'{"x":1,"x":2}', b'{"x":NaN}', b' ' * (adapter.MAX_BYTES + 1)):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                adapter.convert(raw)

    def test_output_is_exclusive_and_input_must_be_bounded_regular_unlinked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / "source.json", root / "trace.json"
            source.write_text(json.dumps(capture()))
            adapter.convert_file(source, target)
            original = target.read_bytes()
            with self.assertRaises(FileExistsError): adapter.convert_file(source, target)
            self.assertEqual(target.read_bytes(), original)
            link = root / "linked.json"
            link.symlink_to(source)
            with self.assertRaises(OSError): adapter.convert_file(link, root / "new.json")
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError): adapter.convert_file(fifo, root / "new.json")
            source.write_text("{}")
            with self.assertRaises(ValueError): adapter.convert_file(source, root / "new.json")
            self.assertFalse((root / "new.json").exists())


if __name__ == "__main__":
    unittest.main()
