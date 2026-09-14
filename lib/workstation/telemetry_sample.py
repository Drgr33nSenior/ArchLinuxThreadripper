"""Bounded sysfs textfile metrics; no SDK, subprocess, network or GPU ownership."""

import argparse
import json
import math
from pathlib import Path
import re
import sys

from measurement import read, snapshot
import telemetry_power


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def metrics(sample, sysfs=Path("/sys")):
    lines = []

    def emit(name, value, labels=None):
        value = number(value)
        if value is None:
            return
        tags = ""
        if labels:
            tags = "{" + ",".join(f"{k}={json.dumps(str(v))}" for k, v in sorted(labels.items())) + "}"
        lines.append(f"{name}{tags} {value:.12g}")

    emit("workstation_hardware_sample_timestamp_seconds", sample["unix_ns"] / 1e9)
    emit("workstation_gpu_count", len(sample["gpus_by_bdf"]))
    for bdf, values in sample["gpus_by_bdf"].items():
        if not re.fullmatch(r"[a-fA-F0-9]{4}:[a-fA-F0-9]{2}:[a-fA-F0-9]{2}\.[0-7]", bdf):
            continue
        device = sysfs / "bus/pci/devices" / bdf
        identity = {"pci_bdf": bdf}
        for field in ("unique_id", "device"):
            value = read(device / field)
            identity[field] = value if value and re.fullmatch(r"(?:0x)?[a-fA-F0-9]{1,32}", value) else "unknown"
        emit("workstation_gpu_info", 1, identity)
        labels = {"pci_bdf": bdf}
        for key, suffix in (("mem_info_vram_total", "vram_total_bytes"),
                            ("mem_info_vram_used", "vram_used_bytes"),
                            ("gpu_busy_percent", "busy_percent"), ("current_link_width", "pcie_width")):
            value = number(values.get(key))
            valid = value is not None and value >= 0 and (key != "gpu_busy_percent" or value <= 100)
            emit("workstation_gpu_sensor_available", int(valid), {**labels, "sensor": suffix})
            if valid:
                emit("workstation_gpu_" + suffix, value, labels)
        for key, suffix in (("pp_dpm_sclk", "core_clock_hertz"), ("pp_dpm_mclk", "memory_clock_hertz")):
            active = re.findall(r"(?im)^\s*\d+:\s*([0-9]+)\s*Mhz\s*\*\s*$", values.get(key) or "")
            emit("workstation_gpu_sensor_available", int(len(active) == 1), {**labels, "sensor": suffix})
            if len(active) == 1:
                emit("workstation_gpu_" + suffix, int(active[0]) * 1000000, labels)
        for hwmon in (device / "hwmon").glob("hwmon*"):
            for pattern, suffix, divisor in (("temp*_input", "temperature_celsius", 1000),
                                             ("power*_average", "power_average_watts", 1000000),
                                             ("power*_input", "power_input_watts", 1000000)):
                for sensor in hwmon.glob(pattern):
                    if not re.fullmatch(r"(?:temp|power)[0-9]+_(?:input|average)", sensor.name):
                        continue
                    value = number(read(sensor))
                    if value is not None:
                        emit("workstation_gpu_" + suffix, value / divisor, {**labels, "sensor": sensor.name})
    power = sample["cpu_power"]
    for policy, fields in power.get("policies", {}).items():
        if not re.fullmatch(r"policy[0-9]+", policy):
            continue
        driver = fields.get("scaling_driver")
        governor = fields.get("scaling_governor")
        epp = fields.get("energy_performance_preference")
        # Fixed categorical values only, not arbitrary sysfs contents.
        driver = driver if driver in {"amd-pstate", "amd-pstate-epp", "acpi-cpufreq"} else "unknown"
        governor = governor if governor in {"performance", "powersave", "schedutil", "ondemand", "conservative", "userspace"} else "unknown"
        epp = epp if epp in {"performance", "balance_performance", "balance_power", "power", "default"} else "unknown"
        emit("workstation_cpu_policy_info", 1, {"policy": policy, "driver": driver, "governor": governor, "epp": epp})
    emit("workstation_cpu_boost_enabled", power.get("boost"))
    # Never emit raw /proc/meminfo, cgroup paths, logs or process arguments.
    return "\n".join(lines) + "\n"


def write_textfile(directory, text):
    output = telemetry_power.Directory(directory)
    try:
        with output.locked():
            output.write("hardware.prom", text, 0o644)
    finally:
        output.close()


def sample_once(directory, sysfs=Path("/sys"), proc=Path("/proc"), collect=snapshot, clock=None):
    """Keep sampling, durable energy and public publication under one lock."""
    output = telemetry_power.Directory(directory)
    try:
        with output.locked():
            # Refuse unsafe publication before updating any accumulated state.
            output.check_existing("hardware.prom")
            hardware = collect()
            power = telemetry_power.collect(sysfs, proc, clock)
            energy, reason = output.energy(power)
            output.write("hardware.prom", metrics(hardware, sysfs) + telemetry_power.metrics(power, energy, reason), 0o644)
    finally:
        output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="existing private-owned textfile directory")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("target Linux only; fixtures must call the pure metrics function")
    try:
        sample_once(args.directory)
    except (OSError, ValueError):
        print("FAILED: hardware textfile sampling; previous sample is retained and will become stale", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
