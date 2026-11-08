"""Request-aligned, report-only reader for caller-side sysfs samples.

Samples and client group records share the host CLOCK_MONOTONIC of one boot.
A window is resolved only with at least MIN_SAMPLES readable samples inside it.
Both legacy and passive reads of the same sensor are retained side by side;
their disagreement is reported, never averaged away. gpu_metrics bytes stay
undecoded unless an installed-kernel structure binding is supplied, and no
binding is available locally. No physical bandwidth, energy, throttling or cap
residency field is produced.
"""
from decimal import Decimal
import json
import re
import statistics

BDFS = ("0000:23:00.0", "0000:c3:00.0")
MIN_SAMPLES = 2
# Aggregated fields; each is resolved separately (R8).
FIELDS = ("legacy_power_uw", "passive_power_uw", "passive_gpu_busy_pct", "sysfs_mem_busy_pct",
          "hwmon_sclk_hz", "hwmon_mclk_hz")
PROHIBITED = ("physical_gbps", "energy_joules", "throttle_cause", "cap_residency")
DPM = re.compile(r"^\s*(\d+):\s*(\d+)Mhz\s*(\*)?\s*$")


def need(ok, message):
    if not ok:
        raise ValueError("resource reader: " + message)


def _int(text):
    """Integer sysfs text, or the retained error/absence state."""
    if text is None:
        return None, "ABSENT"
    if type(text) is dict:
        if text.get("absent") is True:
            return None, "ABSENT"
        if "error" in text:
            need(type(text["error"]) is str and len(text["error"]) <= 32, "bounded error state")
            return None, "ERR:" + text["error"]
        text = text.get("text")
    need(type(text) is str and len(text) <= 64, "bounded sysfs text")
    if text.startswith("ERR:"):
        return None, text
    need(re.fullmatch(r"-?\d+", text) is not None, "integer sysfs text")
    return int(text), None


def _dpm(text):
    if type(text) is not dict or "text" not in text:
        return None
    current = [int(m[2]) for m in (DPM.match(line) for line in text["text"].splitlines()) if m and m[3]]
    need(len(current) <= 1, "one current DPM level")
    return current[0] if current else None


def normalize(line, boot_id):
    row = json.loads(line)
    need(set(row) >= {"seq", "record", "exit_code", "failure"}, "sample fields")
    rec = row["record"]
    need(rec["boot_id"] == boot_id, "sample boot identity")
    need(set(rec["devices"]) == set(BDFS), "sample device set")
    out = {"seq": row["seq"], "monotonic": Decimal(str(rec["monotonic"])), "devices": {}}
    passive = rec.get("passive", {}).get("devices", {})
    for bdf in BDFS:
        legacy = rec["devices"][bdf]
        p = passive.get(bdf, {})
        hw = next(iter(p.get("hwmon", {}).get("directories", {}).values()), {})
        power_l, err_l = _int(legacy.get("power1_average"))
        power_p, err_p = _int(hw.get("power", {}).get("power1_average"))
        # R8: every field keeps its own read error; none is discarded.
        busy_l, err_bl = _int(legacy.get("busy_percent"))
        busy_p, err_bp = _int(p.get("gpu_busy_percent"))
        mem_p, err_m = _int(p.get("mem_busy_percent"))
        sclk, err_s = _int(hw.get("freq", {}).get("freq1", {}).get("input"))
        mclk, err_mc = _int(hw.get("freq", {}).get("freq2", {}).get("input"))
        cap, err_c = _int(legacy.get("power1_cap"))
        gm = p.get("gpu_metrics", {})
        out["devices"][bdf] = {
            "legacy_power_uw": power_l, "passive_power_uw": power_p, "power_errors": [err_l, err_p],
            "legacy_busy_pct": busy_l, "passive_gpu_busy_pct": busy_p, "sysfs_mem_busy_pct": mem_p,
            "hwmon_sclk_hz": sclk, "hwmon_mclk_hz": mclk, "dpm_sclk_mhz": _dpm(p.get("pp_dpm_sclk")),
            "dpm_mclk_mhz": _dpm(p.get("pp_dpm_mclk")), "configured_cap_uw": cap,
            "field_errors": {"legacy_power_uw": err_l, "passive_power_uw": err_p, "legacy_busy_pct": err_bl,
                             "passive_gpu_busy_pct": err_bp, "sysfs_mem_busy_pct": err_m,
                             "hwmon_sclk_hz": err_s, "hwmon_mclk_hz": err_mc, "configured_cap_uw": err_c},
            "runtime_status": [p.get("runtime_status_before", {}).get("text"),
                               p.get("runtime_status_after", {}).get("text")],
            "gpu_metrics": {k: gm.get(k) for k in ("bytes", "header_hex", "sha256")} if gm else None}
    return out


def read_samples(lines, boot_id):
    need(type(lines) is list and 0 < len(lines) <= 4096, "bounded sample count")
    rows = [normalize(line, boot_id) for line in lines]
    need([r["seq"] for r in rows] == sorted({r["seq"] for r in rows}), "unique ordered sample sequence")
    need(all(a["monotonic"] < b["monotonic"] for a, b in zip(rows, rows[1:])), "strictly increasing sample clock")
    return rows


def windows(groups, role):
    out = []
    for g in groups:
        need(set(g) >= {"group_id", "prepared_monotonic", "received_monotonic", "excluded_warmup", "ordinals"},
             "client group fields")
        a, b = Decimal(str(g["prepared_monotonic"])), Decimal(str(g["received_monotonic"]))
        need(a < b, "positive group window")
        out.append({"role": role, "group_id": g["group_id"], "warmup": g["excluded_warmup"] is True,
                    "begin": a, "end": b})
    out.sort(key=lambda w: w["begin"])
    need(all(x["end"] <= y["begin"] for x, y in zip(out, out[1:])), "non-overlapping client groups")
    return out


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def align(samples, role_windows, min_samples=MIN_SAMPLES):
    """Per-window sample membership; unresolved windows carry no aggregate values."""
    report = []
    for w in role_windows:
        inside = [s for s in samples if w["begin"] <= s["monotonic"] <= w["end"]]
        row = {"role": w["role"], "group_id": w["group_id"], "warmup": w["warmup"],
               "seconds": str(w["end"] - w["begin"]), "samples": [s["seq"] for s in inside]}
        if len(inside) < min_samples:
            row["status"] = "UNRESOLVED_TOO_FEW_SAMPLES"
        else:
            row["status"] = "RESOLVED_POINT_SAMPLES"
            row["devices"] = {}
            for bdf in BDFS:
                d = [s["devices"][bdf] for s in inside]
                # R8: a field median needs min_samples readable values of that field.
                coverage = {f: sum(1 for x in d if x[f] is not None) for f in FIELDS}
                med = lambda f: _median([x[f] for x in d]) if coverage[f] >= min_samples else None
                diffs = [x["legacy_power_uw"] - x["passive_power_uw"] for x in d
                         if x["legacy_power_uw"] is not None and x["passive_power_uw"] is not None]
                row["devices"][bdf] = {
                    "median_legacy_power_w": None if med("legacy_power_uw") is None else med("legacy_power_uw") / 1e6,
                    "median_passive_power_w": None if med("passive_power_uw") is None else med("passive_power_uw") / 1e6,
                    "power_read_disagreements": sum(1 for v in diffs if v != 0),
                    "max_abs_power_read_difference_w": max((abs(v) for v in diffs), default=0) / 1e6,
                    "median_sysfs_mem_busy_pct": med("sysfs_mem_busy_pct"),
                    "median_gpu_busy_pct": med("passive_gpu_busy_pct"),
                    "median_hwmon_sclk_mhz": None if med("hwmon_sclk_hz") is None else med("hwmon_sclk_hz") / 1e6,
                    "median_hwmon_mclk_mhz": None if med("hwmon_mclk_hz") is None else med("hwmon_mclk_hz") / 1e6,
                    "field_samples": coverage,
                    "unresolved_fields": [f for f in FIELDS if coverage[f] < min_samples],
                    "field_errors": {f: sorted({x["field_errors"][f] for x in d
                                                if x["field_errors"][f] not in (None, "ABSENT")})
                                     for f in sorted(d[0]["field_errors"])},
                    "errors": sorted({e for x in d for e in x["field_errors"].values() if e not in (None, "ABSENT")})}
        report.append(row)
    return report


def gpu_metrics_decode(raw_header_hex, binding=None):
    """Refuse decode unless the installed kernel struct for this exact header is bound."""
    need(type(raw_header_hex) is str and re.fullmatch(r"[0-9a-f]{8}", raw_header_hex) is not None, "header hex")
    need(binding is not None, "gpu_metrics structure binding absent: installed kernel source not retained")
    need(binding.get("header_hex") == raw_header_hex and binding.get("kernel_release")
         and binding.get("source_sha256"), "gpu_metrics binding identity")
    need(False, "gpu_metrics field decoder not implemented without a reviewed struct layout")


def summary(samples, aligned):
    resolved = [r for r in aligned if r["status"] == "RESOLVED_POINT_SAMPLES"]
    headers = sorted({s["devices"][b]["gpu_metrics"]["header_hex"] for s in samples for b in BDFS
                      if s["devices"][b]["gpu_metrics"] and s["devices"][b]["gpu_metrics"]["header_hex"]})
    out = {"samples": len(samples), "windows": len(aligned), "resolved_windows": len(resolved),
           "unresolved_windows": len(aligned) - len(resolved),
           "unresolved_group_ids": [r["group_id"] for r in aligned if r["status"] != "RESOLVED_POINT_SAMPLES"],
           "gpu_metrics_headers": headers, "gpu_metrics_decoded": False,
           "interpretation": "report-only point samples; activity percentages and hwmon averages, not physical "
                             "GB/s, energy, throttling or cap residency"}
    for key in PROHIBITED:
        out[key] = None
    return out
