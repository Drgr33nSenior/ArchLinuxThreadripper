"""Offline synthetic sysfs/state tests; not USB or PSU hardware qualification."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import telemetry_power as power
import telemetry_sample


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.sysfs, self.proc, self.output = [self.work / name for name in ("sys", "proc", "output")]
        (self.sysfs / "devices").mkdir(parents=True)
        (self.sysfs / "class/hwmon").mkdir(parents=True)
        boot = self.proc / "sys/kernel/random"
        boot.mkdir(parents=True)
        (boot / "boot_id").write_text("11111111-1111-1111-1111-111111111111\n")
        self.output.mkdir(mode=0o700)
        self.device = self.sensor()
        self.hardware = {"unix_ns": 1700000000000000000, "gpus_by_bdf": {},
                         "cpu_power": {"boost": None, "policies": {}}}

    def sensor(self, number=3, attachment="usb1/1-2/0003:1B1C:1C1E.0001"):
        directory = self.sysfs / "devices" / attachment / "hwmon" / f"hwmon{number}"
        directory.mkdir(parents=True)
        for name, value in {"name": "corsairpsu", "power1_label": "power total", "power1_input": "400000000",
                            "power2_label": "power +12v", "power2_input": "350000000",
                            "power3_label": "power +5v", "power3_input": "30000000",
                            "power4_label": "power +3.3v", "power4_input": "20000000"}.items():
            (directory / name).write_text(value + "\n")
        (self.sysfs / "class/hwmon" / f"hwmon{number}").symlink_to(directory)
        return directory

    def sample(self, seconds=100, wall_seconds=None):
        wall_seconds = seconds if wall_seconds is None else wall_seconds
        return power.collect(self.sysfs, self.proc, lambda: (1700000000000000000 + int(wall_seconds * 1e9),
                                                           int(seconds * 1e9)))

    def once(self, seconds=100):
        telemetry_sample.sample_once(self.output, self.sysfs, self.proc, lambda: self.hardware,
                                    lambda: (1700000000000000000 + int(seconds * 1e9), int(seconds * 1e9)))
        return (self.output / "hardware.prom").read_text()

    def state(self):
        return json.loads((self.output / power.STATE_FILE).read_text())

    def test_driver_total_is_not_sum_of_rails_and_enumeration_is_not_identity(self):
        sample = self.sample()
        self.assertEqual(sample["watts"], 400)
        self.assertEqual(sample["rails"], {"12v": 350, "5v": 30, "3v3": 20})
        text = power.metrics(sample)
        self.assertIn("workstation_psu_output_power_watts 400\n", text)
        self.assertNotIn("800", text)
        self.assertNotIn(str(self.work), text)
        self.assertNotIn("1B1C", text)
        replacement = self.device.with_name("hwmon28")
        self.device.rename(replacement)
        (self.sysfs / "class/hwmon/hwmon3").unlink()
        (self.sysfs / "class/hwmon/hwmon28").symlink_to(replacement)
        self.assertEqual(sample["device"], self.sample()["device"])

    def test_not_detected_duplicate_and_missing_total_are_not_zero(self):
        (self.device / "name").write_text("another_driver")
        self.assertEqual(self.sample()["reason"], "not_detected")
        self.assertNotIn("workstation_psu_output_power_watts", power.metrics(self.sample()))
        (self.device / "name").write_text("corsairpsu")
        self.sensor(9, "usb1/1-4/another-device")
        self.assertEqual(self.sample()["reason"], "ambiguous_devices")
        (self.sysfs / "class/hwmon/hwmon9").unlink()
        (self.device / "power1_input").unlink()
        self.assertIsNone(self.sample()["watts"])

    def test_bounded_numerics_and_labels(self):
        for invalid in ("-1", "NaN", "inf", "1.3", "True", "100000000001", "9" * 1000):
            with self.subTest(invalid=invalid[:20]):
                (self.device / "power1_input").write_text(invalid)
                self.assertIsNone(self.sample()["watts"])
        (self.device / "power1_input").write_text("0")
        self.assertEqual(self.sample()["watts"], 0)
        (self.device / "power1_label").write_text("power input AC")
        self.assertIsNone(self.sample()["watts"])
        (self.device / "power1_label").write_text("power total")
        (self.device / "power3_label").write_text("unexpected rail")
        self.assertIsNone(self.sample()["watts"])

    def test_optional_rail_failure_does_not_replace_total(self):
        (self.device / "power2_input").unlink()
        sample = self.sample()
        self.assertEqual(sample["watts"], 400)
        self.assertNotIn("12v", sample["rails"])
        self.assertIn('workstation_psu_rail_available{rail="12v"} 0', power.metrics(sample))

    def test_unrelated_hwmon_layout_or_unreadable_name_does_not_hide_psu(self):
        unrelated = self.sysfs / "devices/virtual/thermal/hwmon7"
        unrelated.mkdir(parents=True)
        (unrelated / "name").write_text("another_driver")
        (self.sysfs / "class/hwmon/hwmon7").symlink_to(unrelated)
        self.assertEqual(self.sample()["watts"], 400)
        (unrelated / "name").unlink()
        self.assertEqual(self.sample()["watts"], 400)
        (unrelated / "name").symlink_to(self.work / "missing")
        self.assertEqual(self.sample()["watts"], 400)
        (unrelated / "name").unlink()
        (unrelated / "name").write_text("corsairpsu")
        self.assertIsNone(self.sample()["watts"])

    def test_sensor_symlink_and_escape_refused_without_reading_target(self):
        unrelated = self.work / "private-sentinel"
        unrelated.write_text("DUMMY_SECRET_SENTINEL")
        (self.device / "power1_input").unlink()
        (self.device / "power1_input").symlink_to(unrelated)
        self.assertIsNone(self.sample()["watts"])
        (self.sysfs / "class/hwmon/hwmon3").unlink()
        (self.sysfs / "class/hwmon/hwmon3").symlink_to(self.work)
        text = power.metrics(self.sample())
        self.assertNotIn("DUMMY_SECRET_SENTINEL", text)
        self.assertIn("invalid_sensor", text)
        self.assertEqual(unrelated.read_text(), "DUMMY_SECRET_SENTINEL")

    def test_irregular_trapezoids_persist_and_publication_is_readable(self):
        self.once(100)
        (self.device / "power1_input").write_text("600000000")
        self.once(130)
        self.assertEqual(self.state()["output_energy_joules"], 15000)
        (self.device / "power1_input").write_text("200000000")
        text = self.once(145)
        self.assertEqual(self.state()["output_energy_joules"], 21000)
        self.assertEqual(self.state()["covered_seconds"], 45)
        self.assertIn("workstation_psu_output_energy_joules_total 21000", text)
        self.assertIn("workstation_psu_energy_available 1", text)
        for name, mode in ((power.STATE_FILE, 0o600), (power.LOCK_FILE, 0o600), ("hardware.prom", 0o644)):
            self.assertEqual(stat.S_IMODE((self.output / name).stat().st_mode), mode)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)

    def test_restrictive_umask_in_separate_process(self):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                           PYTHONPATH=str(ROOT / "tests/workstation"))
        result = subprocess.run([sys.executable, "-c", "import os, unittest; os.umask(0o077); "
                                 "from test_telemetry_power import PowerTests; "
                                 "suite=unittest.TestSuite([PowerTests('test_irregular_trapezoids_persist_and_publication_is_readable')]); "
                                 "result=unittest.TextTestRunner().run(suite); raise SystemExit(not result.wasSuccessful())"],
                                capture_output=True, text=True, timeout=15, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_gap_boot_clock_and_device_changes_do_not_integrate(self):
        for condition in ("missing", "gap", "reboot", "suspend", "clock_backwards", "duplicate", "device", "boot_unknown"):
            with self.subTest(condition=condition):
                baseline, _ = power.advance(None, self.sample(100))
                second = self.sample(130)
                if condition == "missing":
                    baseline, _ = power.advance(baseline, {"watts": None})
                elif condition == "gap":
                    second = self.sample(191)
                elif condition == "reboot":
                    second["boot_id"] = "22222222-2222-2222-2222-222222222222"
                elif condition == "suspend":
                    second = self.sample(130, 160)
                elif condition == "clock_backwards":
                    second = self.sample(130, 99)
                elif condition == "duplicate":
                    second = self.sample(100)
                elif condition == "device":
                    second["device"] = "f" * 64
                else:
                    second["boot_id"] = None
                state, _ = power.advance(baseline, second)
                self.assertEqual(state["output_energy_joules"], 0)
                self.assertEqual(state["covered_seconds"], 0)

    def test_missing_power_retains_totals_and_breaks_continuity(self):
        self.once(100)
        self.once(130)
        (self.device / "power1_input").unlink()
        text = self.once(160)
        self.assertIn("workstation_psu_energy_available 0", text)
        self.assertNotIn("workstation_psu_sample_timestamp_seconds", text)
        self.assertEqual(self.state()["output_energy_joules"], 12000)
        self.assertIsNone(self.state()["anchor"])
        (self.device / "power1_input").write_text("400000000")
        self.once(190)
        self.assertEqual(self.state()["covered_seconds"], 30)
        self.once(220)
        self.assertEqual(self.state()["covered_seconds"], 60)

    def test_corrupt_private_state_preserved_and_other_gauges_publish(self):
        target = self.output / power.STATE_FILE
        for invalid in ("not JSON", "{}", "[]", "DUMMY_SECRET_SENTINEL", "x" * 4097):
            with self.subTest(invalid=invalid[:20]):
                target.write_text(invalid)
                target.chmod(0o600)
                text = self.once()
                self.assertEqual(target.read_text(), invalid)
                self.assertIn("workstation_gpu_count 0", text)
                self.assertIn("workstation_psu_output_power_watts 400", text)
                self.assertIn("workstation_psu_energy_available 0", text)
                self.assertNotIn("workstation_psu_output_energy_joules_total", text)
                self.assertNotIn("DUMMY_SECRET_SENTINEL", text)

    def test_state_shape_nan_boolean_and_duplicate_keys_refused(self):
        valid, _ = power.advance(None, self.sample())
        for field, value in (("schema", True), ("schema", 2), ("output_energy_joules", float("nan")),
                             ("covered_seconds", -1), ("covered_seconds", False), ("anchor", {})):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    power.validate_state({**valid, field: value})
        target = self.output / power.STATE_FILE
        target.write_text(json.dumps(valid)[:-1] + ', "schema":1}')
        target.chmod(0o600)
        text = self.once()
        self.assertIn("state_unavailable", text)

    def test_huge_integer_and_deep_json_state_do_not_stop_hardware(self):
        state, _ = power.advance(None, self.sample())
        target = self.output / power.STATE_FILE
        for invalid in (json.dumps({**state, "output_energy_joules": 10**1000}), "[" * 1500 + "]" * 1500):
            with self.subTest(kind=invalid[:30]):
                self.assertLess(len(invalid), power.MAX_STATE_BYTES)
                target.write_text(invalid)
                target.chmod(0o600)
                text = self.once()
                self.assertEqual(target.read_text(), invalid)
                self.assertIn("workstation_gpu_count 0", text)
                self.assertIn("workstation_psu_output_power_watts 400", text)
                self.assertIn("workstation_psu_energy_available 0", text)

    def test_state_permissions_aliases_and_symlinks_preserve_evidence(self):
        unrelated = self.work / "unrelated"
        unrelated.write_text("preserve")
        state = self.output / power.STATE_FILE
        state.symlink_to(unrelated)
        self.assertIn("state_unavailable", self.once())
        self.assertEqual(unrelated.read_text(), "preserve")
        state.unlink()
        os.link(unrelated, state)
        state.chmod(0o600)
        self.assertIn("state_unavailable", self.once())
        self.assertEqual(unrelated.read_text(), "preserve")
        state.unlink()
        state.write_text(json.dumps(power.advance(None, self.sample())[0]))
        state.chmod(0o644)
        before = state.read_bytes()
        self.assertIn("state_unavailable", self.once())
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o644)

    def test_unsafe_output_refused_before_state_change(self):
        self.once()
        before = (self.output / power.STATE_FILE).read_bytes()
        unrelated = self.work / "unrelated"
        unrelated.write_text("preserve")
        (self.output / "hardware.prom").unlink()
        (self.output / "hardware.prom").symlink_to(unrelated)
        with self.assertRaises(ValueError):
            self.once(130)
        self.assertEqual(unrelated.read_text(), "preserve")
        self.assertEqual((self.output / power.STATE_FILE).read_bytes(), before)
        (self.output / "hardware.prom").unlink()
        os.link(unrelated, self.output / "hardware.prom")
        with self.assertRaises(ValueError):
            self.once(130)

    def test_concurrent_sample_refused_before_collection(self):
        output = power.Directory(self.output)
        self.addCleanup(output.close)
        with output.locked():
            with patch.object(telemetry_sample, "metrics", side_effect=AssertionError("should not collect")):
                with self.assertRaises(BlockingIOError):
                    self.once()
        self.assertFalse((self.output / power.STATE_FILE).exists())
        self.once()

    def test_persistence_failure_does_not_publish_undurable_counter(self):
        self.once()
        before = (self.output / power.STATE_FILE).read_bytes()
        original = power.Directory.write

        def failing(output, name, text, mode):
            if name == power.STATE_FILE:
                raise OSError("DUMMY_SECRET_SENTINEL disk full")
            return original(output, name, text, mode)

        with patch.object(power.Directory, "write", failing):
            text = self.once(130)
        self.assertEqual((self.output / power.STATE_FILE).read_bytes(), before)
        self.assertNotIn("DUMMY_SECRET_SENTINEL", text)
        self.assertNotIn("workstation_psu_output_energy_joules_total", text)
        self.assertIn("workstation_psu_energy_available 0", text)
        self.assertIn("workstation_psu_output_power_watts 400", text)

    def test_publication_failure_then_retry_does_not_double_count(self):
        self.once()
        original = power.Directory.write

        def failing(output, name, text, mode):
            if name == "hardware.prom":
                raise OSError("publication failed")
            return original(output, name, text, mode)

        with patch.object(power.Directory, "write", failing):
            with self.assertRaises(OSError):
                self.once(130)
        self.assertEqual(self.state()["output_energy_joules"], 12000)
        self.once(130)
        self.assertEqual(self.state()["output_energy_joules"], 12000)
        self.once(160)
        self.assertEqual(self.state()["output_energy_joules"], 24000)

    def test_main_uses_single_locked_sample_and_failure_is_sanitized(self):
        with patch.object(telemetry_sample.sys, "platform", "linux"), \
                patch.object(telemetry_sample.sys, "argv", ["telemetry_sample.py", str(self.output)]), \
                patch.object(telemetry_sample, "sample_once") as collect:
            self.assertEqual(telemetry_sample.main(), 0)
            collect.assert_called_once_with(str(self.output))
            collect.side_effect = OSError("DUMMY_SECRET_SENTINEL")
            with patch.object(telemetry_sample.sys, "stderr", io.StringIO()) as stderr:
                self.assertEqual(telemetry_sample.main(), 1)
                self.assertIn("previous sample is retained", stderr.getvalue())
                self.assertNotIn("DUMMY_SECRET_SENTINEL", stderr.getvalue())

    def test_runtime_and_source_allowlists_include_sampler_dependency(self):
        for name in ("source.files", "bridge-runtime.files"):
            files = (ROOT / "infrastructure/packages/bootstrap" / name).read_text().splitlines()
            self.assertEqual(files.count("lib/workstation/telemetry_power.py"), 1)
        source_files = (ROOT / "infrastructure/packages/bootstrap/source.files").read_text().splitlines()
        self.assertIn("tests/workstation/test_telemetry_power.py", source_files)


if __name__ == "__main__":
    unittest.main()
