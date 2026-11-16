"""tools/kv-trial/kv_trial_report.py: event parsing, exposure windows and site values (no Prometheus)."""
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/kv-trial"))
import kv_trial_report as report

EVENTS = (ROOT / "tests/fixtures/tools/kv-events.log").read_text().splitlines()
UTC = dt.UTC


def at(hour, minute):
    return dt.datetime(2026, 11, 14, hour, minute, tzinfo=UTC)


class ParseTests(unittest.TestCase):
    def test_kubectl_timestamps_parse_to_utc(self):
        self.assertEqual(dt.datetime(2026, 11, 14, 18, 45, 10, 123456, tzinfo=UTC),
                         report.when("2026-11-14T18:45:10.123456789Z"))

    def test_events_split_into_shrinks_grows_and_other_lines(self):
        shrinks, grows, other = report.parse_events(EVENTS, at(18, 42))
        self.assertEqual(1, grows)
        self.assertEqual([0, 4096, 512], [s["evicted"] for s in shrinks])
        self.assertEqual([1200, 300, 0], [s["moved"] for s in shrinks])
        self.assertEqual(2.5, max(s["seconds"] for s in shrinks))
        # Routine lines (self-test, running requests) are not "other"; the deferred grow is.
        self.assertEqual(["2026-11-14T20:00:00 grow deferred: fault=none, waiting for idle"], other)

    def test_since_filters_earlier_events(self):
        shrinks, grows, _ = report.parse_events(EVENTS, at(19, 35))
        self.assertEqual((1, 0), (len(shrinks), grows))

    def test_first_event_is_the_default_start(self):
        self.assertEqual(at(18, 40), report.first_event(EVENTS).replace(microsecond=0))
        self.assertIsNone(report.first_event(["unrelated"]))


class WindowTests(unittest.TestCase):
    def test_overlapping_windows_merge_and_the_rest_fills_the_gaps(self):
        shrinks, _, _ = report.parse_events(EVENTS, at(18, 42))
        exposed, rest = report.windows(shrinks, at(18, 42), at(21, 0), dt.timedelta(minutes=30))
        # The evict-free shrink at 19:00 opens no window; 19:30 and 19:40 overlap.
        self.assertEqual([[at(19, 30), at(20, 10)]], exposed)
        self.assertEqual([[at(18, 42), at(19, 30)], [at(20, 10), at(21, 0)]], rest)

    def test_a_window_stops_at_now(self):
        shrinks = [{"at": at(19, 30), "evicted": 1}]
        exposed, rest = report.windows(shrinks, at(19, 0), at(19, 40), dt.timedelta(minutes=30))
        self.assertEqual([[at(19, 30), at(19, 40)]], exposed)
        self.assertEqual([[at(19, 0), at(19, 30)]], rest)

    def test_rate_per_100_needs_a_request(self):
        self.assertIsNone(report.per100({"large": 0.0, "requests": 0.0}))
        self.assertEqual(25.0, report.per100({"large": 1.0, "requests": 4.0}))


class SiteValueTests(unittest.TestCase):
    """site_value goes through tools/lib/tools.sh, so the shell loader's rules apply."""

    def site(self, text, mode=0o600, **env):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "site.env"
            path.write_text(text)
            path.chmod(mode)
            environment = {"WORKSTATION_SITE_ENV": str(path), "PROMETHEUS_URL": "", **env}
            with mock.patch.dict(os.environ, environment):
                return report.site_value("PROMETHEUS_URL")

    def test_environment_wins_then_the_file_then_none(self):
        text = "# comment\r\nPROMETHEUS_URL=https://prometheus.example.invalid\r\n"
        self.assertEqual("https://prometheus.example.invalid", self.site(text))
        self.assertEqual("http://env.example.invalid",
                         self.site(text, PROMETHEUS_URL="http://env.example.invalid"))
        self.assertIsNone(self.site("NAS_ADDRESS=192.0.2.10\n"))

    def test_the_loader_rules_apply(self):
        for text, mode, message in (("PROMETHEUS_URL=a\nPROMETHEUS_URL=b\n", 0o600, "Duplicate key"),
                                    ("PROMETEUS_URL=typo\n", 0o600, "Unknown key"),
                                    ("PROMETHEUS_URL=a\n", 0o620, "writable by others")):
            with self.subTest(message), self.assertRaises(SystemExit) as caught:
                self.site(text, mode)
            self.assertIn(message, str(caught.exception))

    def test_values_are_literal(self):
        self.assertEqual('"$(id)"', self.site('PROMETHEUS_URL="$(id)"\n'))


if __name__ == "__main__":
    unittest.main()
