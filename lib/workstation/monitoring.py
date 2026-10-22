"""Read-only local readiness; no probes, module loads, SDKs or cluster access."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from measurement import hwmon, read


PACKAGES = ("lm_sensors", "dmidecode", "nvme-cli", "smartmontools", "grafana-alloy")
UNITS = ("workstation-telemetry.timer", "workstation-telemetry.service", "workstation-alloy.service")


def query(argv):
    # Fixed metadata queries only; do not inherit alternate buses, credentials
    # or pagers. Never echo command stderr or unrelated package information.
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3,
                                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        if len(result.stdout) + len(result.stderr) <= 8192:
            return result.returncode, result.stdout, result.stderr
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return None, "", ""


def packages(run=query):
    result = {}
    for name in PACKAGES:
        rc, out, err = run(["pacman", "-Qq", "--", name])
        if rc == 0 and out == name + "\n":
            result[name] = "installed"
        elif rc == 1 and not out and err == f"error: package '{name}' was not found\n":
            result[name] = "missing"
        else:
            result[name] = "unavailable"
    return result


def units(run=query):
    result = {}
    for name in UNITS:
        fields = ["LoadState", "ActiveState", "UnitFileState"]
        if name == "workstation-telemetry.service":
            fields += ["Result", "ExecMainStatus"]
        rc, out, _ = run(["systemctl", "show", "--no-pager", "--property=" + ",".join(fields), name])
        rows = [line.split("=", 1) for line in out.splitlines()]
        valid = (rc == 0 and len(rows) == len(fields) and all(len(row) == 2 for row in rows)
                 and {row[0] for row in rows} == set(fields))
        state = dict(rows) if valid else {}
        if state.get("LoadState") == "not-found":
            status = "missing"
        elif state.get("LoadState") != "loaded":
            status = "unavailable"
        elif name == "workstation-telemetry.service":
            status = "observed" if state.get("Result") == "success" and state.get("ExecMainStatus") == "0" else "failed"
        else:
            status = "active" if state.get("ActiveState") == "active" else "inactive"
        # Only fixed, short systemd metadata values, never full unit contents.
        result[name] = {"status": status, **{key: value for key, value in state.items()
                                            if re.fullmatch(r"[a-z0-9-]{0,40}", value)}}
    return result


def freshness(path, now):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                return {"status": "invalid"}
            data = source.read(262145)
        if len(data) > 262144:
            return {"status": "invalid"}
        lines = [line for line in data.decode("ascii").splitlines()
                 if line.startswith("workstation_hardware_sample_timestamp_seconds")]
        if len(lines) != 1 or not re.fullmatch(
                r"workstation_hardware_sample_timestamp_seconds [0-9.eE+-]+", lines[0]):
            return {"status": "invalid"}
        timestamp = float(lines[0].split()[1])
        if not math.isfinite(timestamp) or timestamp <= 0:
            return {"status": "invalid"}
        age = now - timestamp
        return {"status": "fresh" if -10 <= age <= 90 else "stale", "age_seconds": age}
    except FileNotFoundError:
        return {"status": "missing"}
    except (OSError, UnicodeError, ValueError):
        return {"status": "unavailable"}


def counter(path):
    value = read(path)
    return int(value) if value and re.fullmatch(r"[0-9]{1,20}", value) else None


def inspect(sysfs=Path("/sys"), root=Path("/"), run=query, now=None):
    installed, services = packages(run), units(run)
    sample = freshness(root / "var/lib/workstation-telemetry/hardware.prom",
                       time.time() if now is None else now)
    sensors = hwmon(sysfs)
    readable = 0
    for device in sensors.values():
        device["available"] = {key: bool(value is not None and re.fullmatch(r"-?[0-9]+", value))
                               for key, value in device["values"].items()}
        readable += sum(device["available"].values())
    # Existing powercap/EDAC files only. Absent or denied counters remain null.
    energy = [{"zone": path.name, "name": read(path / "name"),
               "energy_uj": counter(path / "energy_uj"),
               "max_energy_range_uj": counter(path / "max_energy_range_uj")}
              for path in sorted((sysfs / "class/powercap").glob("intel-rapl:*"))]
    ecc = [{"controller": path.name, "corrected": counter(path / "ce_count"),
            "uncorrected": counter(path / "ue_count")}
           for path in sorted((sysfs / "devices/system/edac/mc").glob("mc[0-9]*"))]
    ready = (all(value == "installed" for value in installed.values()) and readable > 0
             and sample["status"] == "fresh"
             and services[UNITS[0]]["status"] == "active"
             and services[UNITS[1]]["status"] == "observed"
             and services[UNITS[2]]["status"] == "active")
    return {"schema": 1, "status": "observed" if ready else "incomplete",
            "scope": "local tools, units and sampler freshness only; sensor coverage, cluster delivery and accuracy are unqualified",
            "packages": installed, "units": services, "sample": sample, "hwmon": sensors,
            "cpu_energy": energy, "edac": ecc,
            "limitations": ["A loaded module does not prove a bound or readable sensor.",
                            "Empty inventories and null counters are unavailable, not zero.",
                            "Energy counters can wrap; package and core domains overlap.",
                            "CPU energy permissions are unchanged; the unprivileged sampler does not collect powercap."]}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if sys.platform != "linux":
        print("UNAVAILABLE: monitoring status requires the installed Linux host", file=sys.stderr)
        return 1
    try:
        result = inspect()
    except (OSError, ValueError):
        print("UNAVAILABLE: local monitoring discovery failed", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "observed" else 10


if __name__ == "__main__":
    sys.exit(main())
