"""Public tariff selection, offline fallback and publication; no live API calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import telemetry_tariff as tariff


class TariffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "state"
        self.output.mkdir(mode=0o755)
        self.settings = json.loads((ROOT / "infrastructure/observability/host/tariff.json").read_text())
        self.api = dict(self.settings, tariff_code="E-1R-VAR-22-11-01-A", payment_method="DIRECT_DEBIT")
        self.now = tariff.epoch("2026-10-28T12:00:00Z")

    def row(self, value=28.11, **extra):
        return dict({"value_inc_vat": value, "value_exc_vat": 1,
                     "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2027-01-01T00:00:00Z",
                     "payment_method": "DIRECT_DEBIT"}, **extra)

    def fetch(self, url):
        return {"next": None, "results": [self.row(58.43 if "standing-charges" in url else 28.11)]}

    def offline(self, _):
        raise OSError("fixture transport failure")

    def test_manual_rates_do_not_make_network_requests(self):
        def no_network(_):
            self.fail("manual configuration must not fetch")
        values, saved = tariff.refresh(self.settings, None, self.now, no_network)
        self.assertEqual(values["unit_price_gbp_per_kwh"], 0.2811)
        self.assertEqual(values["standing_charge_gbp_per_day"], 0.5843)
        self.assertEqual(values["mode"], 0)
        self.assertEqual(values["auto_configured"], 0)
        self.assertIsNone(saved)

    def test_vat_inclusive_effective_prices_and_cache(self):
        values, saved = tariff.refresh(self.api, None, self.now, self.fetch)
        self.assertEqual(values["unit_price_gbp_per_kwh"], 0.2811)
        self.assertEqual(values["standing_charge_gbp_per_day"], 0.5843)
        self.assertEqual(values["mode"], 1)
        self.assertEqual(values["refresh_success"], 1)
        self.assertEqual(values["valid_until_seconds"], self.now + 7200)
        cached, written = tariff.refresh(self.api, saved, self.now + 3600, self.offline)
        self.assertEqual(cached["mode"], 2)
        self.assertEqual(cached["refresh_success"], 0)
        self.assertEqual(cached["last_success_timestamp_seconds"], self.now)
        self.assertIsNone(written)

    def test_payment_method_future_and_exclusive_expiry(self):
        rows = [self.row(), self.row(40, payment_method="NON_DIRECT_DEBIT"),
                self.row(90, valid_from="2027-01-01T00:00:00Z", valid_to=None)]
        fetch = lambda _: {"next": None, "results": rows}
        result = tariff.price(self.api["tariff_code"], "standard-unit-rates", "DIRECT_DEBIT", self.now, fetch)
        self.assertEqual(result["value"], 28.11)
        result = tariff.price(self.api["tariff_code"], "standard-unit-rates", "DIRECT_DEBIT",
                              tariff.epoch("2027-01-01T00:00:00Z"), fetch)
        self.assertEqual(result["value"], 90)

    def test_ambiguous_missing_or_invalid_prices_do_not_advance_cache(self):
        _, cache = tariff.refresh(self.api, None, self.now, self.fetch)
        for rows in ([], [self.row(), self.row()], [self.row(float("nan"))],
                     [self.row(1e9)], [self.row(valid_from="2026-01-01T00:00:00")],
                     [self.row(valid_to="2025-01-01T00:00:00Z")]):
            with self.subTest(rows=rows):
                values, saved = tariff.refresh(self.api, cache, self.now + 60,
                                               lambda _: {"next": None, "results": rows})
                self.assertEqual(values["mode"], 2)
                self.assertIsNone(saved)

    def test_expired_old_future_or_differently_bound_cache_uses_manual(self):
        _, cache = tariff.refresh(self.api, None, self.now, self.fetch)
        for changed in (dict(cache, fetched_at=self.now - 27 * 3600), dict(cache, fetched_at=self.now + 1),
                        dict(cache, to=self.now), dict(cache, binding="other")):
            values, saved = tariff.refresh(self.api, changed, self.now, self.offline)
            self.assertEqual(values["mode"], 0)
            self.assertEqual(values["auto_configured"], 1)
            self.assertIsNone(saved)

    def test_pagination_and_foreign_urls_are_bounded(self):
        calls = []
        def forever(url):
            calls.append(url)
            return {"results": [], "next": url}
        with self.assertRaises(ValueError):
            tariff.price(self.api["tariff_code"], "standard-unit-rates", "DIRECT_DEBIT", self.now, forever)
        self.assertEqual(len(calls), 4)
        calls.clear()
        def foreign(url):
            calls.append(url)
            return {"results": [], "next": "http://127.0.0.1/private"}
        with self.assertRaises(ValueError):
            tariff.price(self.api["tariff_code"], "standard-unit-rates", "DIRECT_DEBIT", self.now, foreign)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            tariff.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")

    def test_configuration_does_not_accept_accounts_keys_or_other_tariffs(self):
        for changed in (dict(self.api, api_key="not-accepted"), dict(self.api, account="not-accepted"),
                        dict(self.api, tariff_code="E-2R-VAR-22-11-01-A"),
                        dict(self.api, tariff_code="E-1R-AGILE-22-11-01-A"),
                        dict(self.api, tariff_code=None), dict(self.api, payment_method=None),
                        dict(self.api, fallback_unit_pence=True)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                tariff.config(changed)
        for data in ('{"key":1,"key":2}', '{"key":NaN}'):
            with self.assertRaises(ValueError):
                tariff.decode(data)

    def test_atomic_publication_cache_and_symlink_refusal(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.api))
        tariff.run(config_path, self.output, self.now, self.fetch)
        old = (self.output / ".last-good.json").read_bytes()
        tariff.run(config_path, self.output, self.now + 60, self.offline)
        self.assertEqual((self.output / ".last-good.json").read_bytes(), old)
        text = (self.output / "tariff.prom").read_text()
        self.assertIn("workstation_tariff_mode 2\n", text)
        self.assertIn("workstation_tariff_unit_price_gbp_per_kwh 0.2811\n", text)
        self.assertEqual((self.output / ".last-good.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.output / "tariff.prom").stat().st_mode & 0o777, 0o644)
        (self.output / "tariff.prom").rename(self.output / "preserved.prom")
        (self.output / "tariff.prom").symlink_to(self.output / "preserved.prom")
        with self.assertRaises(ValueError):
            tariff.run(config_path, self.output, self.now + 120, self.fetch)
        self.assertEqual((self.output / ".last-good.json").read_bytes(), old)

    def test_hourly_updater_does_not_share_hardware_state_or_gpu_access(self):
        unit = (ROOT / "templates/workstation/telemetry/workstation-tariff.service").read_text()
        for value in ("User=workstation-tariff", "StateDirectory=workstation-tariff", "MemoryMax=64M",
                      "NoNewPrivileges=yes", "CapabilityBoundingSet=", "PrivateDevices=yes"):
            self.assertIn(value, unit)
        self.assertNotIn("StateDirectory=workstation-telemetry", unit)
        source = (ROOT / "lib/workstation/telemetry_tariff.py").read_text()
        self.assertNotIn("subprocess", source)
        self.assertIn("ProxyHandler({})", source)


if __name__ == "__main__":
    unittest.main()
