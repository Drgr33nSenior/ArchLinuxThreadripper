"""tools/measure: comfy_mem.py phase peaks and comfy_summary.py's one-line view."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/measure"))
import comfy_mem
import comfy_summary

GIB = 1 << 30


def row(t, a, b):
    def pod(gib):
        return {"current": gib * GIB, "anon": gib * GIB // 2, "file": GIB, "shmem": GIB // 4, "working_set": gib * GIB}
    return {"t": t, "pod-a": pod(a), "pod-b": pod(b)}


class ComfyMemTests(unittest.TestCase):
    def test_marks_parse_and_phases_take_their_peaks(self):
        marks = comfy_mem.parse_marks("100.0 cold\n\n105.5 warm\n")
        self.assertEqual([(100.0, "cold"), (105.5, "warm")], marks)
        rows = [row(99.0, 9, 9), row(100.0, 2, 3), row(102.0, 6, 4), row(106.0, 5, 7), row(107.0, 1, 1)]
        summary = comfy_mem.summarise(rows, marks, {"pod-a": 9 * GIB, "pod-b": 10 * GIB})
        self.assertEqual({"pod-a": 9.0, "pod-b": 10.0}, summary["limit_gib"])
        # The row before the first mark belongs to no phase; the last phase runs to the end.
        self.assertEqual({"current": 6.0, "anon": 3.0, "file": 1.0, "shmem": 0.25, "working_set": 6.0},
                         summary["cold"]["pod-a"])
        self.assertEqual(4.0, summary["cold"]["pod-b"]["current"])
        self.assertEqual(7.0, summary["warm"]["pod-b"]["current"])

    def test_no_rows_keeps_only_the_limits(self):
        self.assertEqual({"limit_gib": {"p": 1.0}}, comfy_mem.summarise([], [(1.0, "cold")], {"p": GIB}))

    def test_cgroup_path_of_a_pod_uid(self):
        self.assertEqual("/sys/fs/cgroup/kubepods.slice/kubepods-podab_cd.slice", str(comfy_mem.pod_path("ab-cd")))


SUMMARY = {"limit_gib": {"a": 9.0},
           "cold": {"a": {"current": 6.0, "anon": 3.0, "file": 1.0, "shmem": 0.25, "working_set": 5.5}},
           "warm": {"a": {"current": 7.0, "anon": 4.0, "file": 1.0, "shmem": 0.5, "working_set": 6.5}}}


class ComfySummaryTests(unittest.TestCase):
    def test_one_line_per_summary(self):
        self.assertEqual("cold a: anon+shmem 3.25 ws 5.50 cur 6.00 | warm a: anon+shmem 4.50 ws 6.50 cur 7.00",
                         comfy_summary.line(SUMMARY))

    def test_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.json"
            path.write_text(json.dumps(SUMMARY))
            result = subprocess.run([sys.executable, str(ROOT / "tools/measure/comfy_summary.py"), str(path)],
                                    capture_output=True, text=True, check=True)
        self.assertEqual(comfy_summary.line(SUMMARY) + "\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
