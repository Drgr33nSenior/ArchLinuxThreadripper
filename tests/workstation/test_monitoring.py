"""Offline monitoring fixtures: no package transaction, systemd or device probe."""
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import monitoring


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prom = self.root / "var/lib/workstation-telemetry/hardware.prom"
        self.prom.parent.mkdir(parents=True)
        self.prom.write_text("workstation_hardware_sample_timestamp_seconds 1000\n")
        self.sensor = self.root / "class/hwmon/hwmon3"
        self.sensor.mkdir(parents=True)
        (self.sensor / "name").write_text("corsairpsu")
        (self.sensor / "fan1_input").write_text("0")
        (self.sensor / "power1_input").write_text("102000000")
        self.calls = []

    def query(self, argv):
        self.calls.append(argv)
        if argv[0] == "pacman":
            self.assertEqual(argv[:3], ["pacman", "-Qq", "--"])
            return 0, argv[-1] + "\n", ""
        self.assertEqual(argv[:3], ["systemctl", "show", "--no-pager"])
        self.assertIn(argv[-1], monitoring.UNITS)
        oneshot = argv[-1] == "workstation-telemetry.service"
        fields = {"LoadState": "loaded", "ActiveState": "inactive" if oneshot else "active",
                  "UnitFileState": "static" if oneshot else "enabled",
                  "Result": "success", "ExecMainStatus": "0"}
        names = argv[3].removeprefix("--property=").split(",")
        return 0, "".join(f"{key}={fields[key]}\n" for key in names), ""

    def inspect(self, query=None):
        return monitoring.inspect(self.root, self.root, query or self.query, now=1001)

    def test_local_readiness_does_not_claim_full_sensor_or_cluster_qualification(self):
        report = self.inspect()
        self.assertEqual(report["status"], "observed")
        self.assertIn("unqualified", report["scope"])
        self.assertEqual(report["cpu_energy"], [])
        self.assertEqual(report["edac"], [])
        self.assertEqual(len(self.calls), len(monitoring.PACKAGES) + len(monitoring.UNITS))
        self.assertEqual(report["hwmon"][str(self.sensor.resolve())]["values"]["fan1_input"], "0")

    def test_missing_packages_are_distinct_from_failed_queries(self):
        for rc, out, error, expected in (
            (1, "", "error: package 'nvme-cli' was not found\n", "missing"),
            (1, "", "database failure", "unavailable"),
            (0, "", "", "unavailable"),
            (0, "wrong-package\n", "", "unavailable"),
            (None, "", "", "unavailable"),
        ):
            def run(argv):
                return (rc, out, error) if argv[-1] == "nvme-cli" else self.query(argv)
            with self.subTest(expected=expected, rc=rc):
                report = self.inspect(run)
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["packages"]["nvme-cli"], expected)
                self.assertNotIn("database failure", json.dumps(report))

    def test_disabled_timer_and_failed_sampler_are_not_ready(self):
        for replacement in ("ActiveState=inactive", "LoadState=not-found", "Result=exit-code", "ExecMainStatus=1"):
            def run(argv):
                rc, out, err = self.query(argv)
                if argv[0] == "systemctl":
                    key, _ = replacement.split("=")
                    out = "\n".join(replacement if line.startswith(key + "=") else line
                                    for line in out.splitlines()) + "\n"
                return rc, out, err
            with self.subTest(replacement=replacement):
                self.assertEqual(self.inspect(run)["status"], "incomplete")

    def test_failed_or_partial_unit_discovery_cannot_succeed(self):
        for rc, output in ((1, "LoadState=loaded\n"), (0, ""),
                           (0, "LoadState=loaded\nActiveState=active\nActiveState=active\n"),
                           (0, "LoadState=loaded\nmalformed\nUnitFileState=enabled\n")):
            def run(argv):
                return (rc, output, "") if argv[0] == "systemctl" else self.query(argv)
            with self.subTest(rc=rc, output=output):
                report = self.inspect(run)
                self.assertEqual(report["status"], "incomplete")
                self.assertTrue(all(u["status"] == "unavailable" for u in report["units"].values()))

    def test_textfile_requires_one_finite_fresh_producer_timestamp(self):
        for content, expected in (("", "invalid"),
                                  ("workstation_hardware_sample_timestamp_seconds NaN\n", "invalid"),
                                  ("workstation_hardware_sample_timestamp_seconds 1e999\n", "invalid"),
                                  ("workstation_hardware_sample_timestamp_seconds 0\n", "invalid"),
                                  ("workstation_hardware_sample_timestamp_seconds 900\n", "stale"),
                                  ("workstation_hardware_sample_timestamp_seconds 1012\n", "stale"),
                                  ("workstation_hardware_sample_timestamp_seconds 1000\n" * 2, "invalid"),
                                  ("x" * 262145, "invalid")):
            with self.subTest(expected=expected):
                self.prom.write_text(content)
                report = self.inspect()
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["sample"]["status"], expected)

    def test_no_follow_or_fifo_block_for_sample_file(self):
        self.prom.unlink()
        self.assertEqual(self.inspect()["sample"]["status"], "missing")
        unrelated = self.root / "unrelated"
        unrelated.write_text("SYNTHETIC_PRIVATE_SENTINEL")
        self.prom.symlink_to(unrelated)
        self.assertEqual(self.inspect()["sample"]["status"], "unavailable")
        self.prom.unlink()
        os.mkfifo(self.prom)
        self.assertEqual(self.inspect()["sample"]["status"], "invalid")

    def test_freshness_boundaries_and_denied_sample(self):
        for timestamp, expected in ((911, "fresh"), (910.9, "stale"),
                                    (1011, "fresh"), (1011.1, "stale")):
            with self.subTest(timestamp=timestamp):
                self.prom.write_text(f"workstation_hardware_sample_timestamp_seconds {timestamp}\n")
                self.assertEqual(self.inspect()["sample"]["status"], expected)
        with patch.object(monitoring.os, "open", side_effect=PermissionError()):
            report = self.inspect()
        self.assertEqual(report["sample"]["status"], "unavailable")
        self.assertEqual(report["status"], "incomplete")

    def test_denied_and_unsupported_hwmon_reads_preserve_other_channels(self):
        temperature = self.sensor / "temp1_input"
        temperature.write_text("42000")
        original = Path.read_text
        for code in (errno.EACCES, errno.EOPNOTSUPP, errno.ENODATA):
            def read(path, *args, **kwargs):
                if path == temperature:
                    raise OSError(code, "synthetic inaccessible sensor")
                return original(path, *args, **kwargs)
            with self.subTest(errno=code), patch.object(Path, "read_text", read):
                device = self.inspect()["hwmon"][str(self.sensor.resolve())]
                self.assertIsNone(device["values"]["temp1_input"])
                self.assertFalse(device["available"]["temp1_input"])
                self.assertEqual(device["values"]["fan1_input"], "0")
                self.assertTrue(device["available"]["fan1_input"])

    def test_sensor_failure_and_missing_energy_are_not_zero(self):
        (self.sensor / "temp1_input").symlink_to("unavailable")
        (self.sensor / "curr1_input").write_text("malformed")
        zone = self.root / "class/powercap/intel-rapl:0"
        zone.mkdir(parents=True)
        (zone / "name").write_text("package-0")
        (zone / "max_energy_range_uj").write_text("65532610987")
        ecc = self.root / "devices/system/edac/mc/mc0"
        ecc.mkdir(parents=True)
        (ecc / "ce_count").write_text("0")
        report = self.inspect()
        sensors = report["hwmon"][str(self.sensor.resolve())]
        self.assertIsNone(sensors["values"]["temp1_input"])
        self.assertFalse(sensors["available"]["temp1_input"])
        self.assertFalse(sensors["available"]["curr1_input"])
        self.assertIsNone(report["cpu_energy"][0]["energy_uj"])
        self.assertEqual(report["edac"], [{"controller": "mc0", "corrected": 0, "uncorrected": None}])

    def test_empty_or_unreadable_hwmon_cannot_be_ready(self):
        for inventory in ({}, {"fixture": {"values": {"temp1_input": None}}}):
            with patch.object(monitoring, "hwmon", return_value=inventory):
                self.assertEqual(self.inspect()["status"], "incomplete")

    def test_query_timeout_missing_tool_output_bounds_and_environment(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("fixture", 3)):
            with patch.object(monitoring.subprocess, "run", side_effect=error):
                self.assertEqual(monitoring.query(["pacman", "-Qq", "--", "nvme-cli"]), (None, "", ""))
        with patch.object(monitoring.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "x" * 8193, "")) as run:
            self.assertEqual(monitoring.query(["fixture"]), (None, "", ""))
            self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_cli_incomplete_exit_and_linux_boundary(self):
        with patch.object(sys, "argv", ["monitoring.py"]), patch.object(sys, "platform", "linux"), \
                patch.object(monitoring, "inspect", return_value={"status": "incomplete"}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(monitoring.main(), 10)
            self.assertEqual(json.loads(output.getvalue())["status"], "incomplete")
        with patch.object(sys, "argv", ["monitoring.py"]), patch.object(sys, "platform", "darwin"), \
                patch.object(monitoring, "inspect", side_effect=AssertionError("no host discovery")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(monitoring.main(), 1)

    def test_cli_discovery_failure_is_unavailable(self):
        with patch.object(sys, "argv", ["monitoring.py"]), patch.object(sys, "platform", "linux"), \
                patch.object(monitoring, "inspect", side_effect=OSError("SYNTHETIC_PRIVATE_SENTINEL")), \
                contextlib.redirect_stderr(io.StringIO()) as error, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(monitoring.main(), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("SYNTHETIC_PRIVATE_SENTINEL", error.getvalue())


if __name__ == "__main__":
    unittest.main()
