"""Read-only Corsair hwmon DC output samples and gap-aware estimated energy.

This is not an AC/wall energy meter. No USB commands, controls or subprocesses
are used. Missing samples never contribute a zero or extrapolated interval.
"""

import errno
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time


STATE_FILE = ".psu-energy.json"
LOCK_FILE = ".psu-energy.lock"
MAX_STATE_BYTES = 4096
MAX_GAP_SECONDS = 90
CLOCK_TOLERANCE_SECONDS = 0.25
MAX_POWER_WATTS = 100000  # Parsing bound, not a rating or a hardware policy.
RAILS = ((2, "12v", "power +12v"), (3, "5v", "power +5v"), (4, "3v3", "power +3.3v"))
BOOT_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")


def _read_at(directory, name, limit=128):
    """Read a fixed regular attribute, without following a final symlink."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("non-regular sensor attribute")
        value = os.read(descriptor, limit + 1)
        if len(value) > limit:
            raise ValueError("oversized sensor attribute")
        return value.decode("ascii").strip()
    finally:
        os.close(descriptor)


def _watts(value):
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise ValueError("invalid power sample")
    watts = int(value) / 1000000
    if watts > MAX_POWER_WATTS:
        raise ValueError("power sample outside parsing bound")
    return watts


def _boot_id(proc):
    try:
        descriptor = os.open(Path(proc) / "sys/kernel/random", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            value = _read_at(descriptor, "boot_id")
            return value if BOOT_ID.fullmatch(value) else None
        finally:
            os.close(descriptor)
    except (OSError, ValueError, UnicodeError):
        return None


def collect(sysfs=Path("/sys"), proc=Path("/proc"), clock=None):
    """Discover one driver-backed attachment, independent of hwmon enumeration."""
    unavailable = {"reason": "not_detected", "watts": None, "rails": {}}
    try:
        devices = (Path(sysfs) / "devices").resolve(strict=True)
        matches = []
        with os.scandir(Path(sysfs) / "class/hwmon") as entries:
            for count, entry in enumerate(entries):
                if count >= 128:
                    raise ValueError("too many hwmon entries")
                if not re.fullmatch(r"hwmon[0-9]{1,8}", entry.name):
                    continue
                path = Path(entry.path).resolve(strict=True)
                path.relative_to(devices)
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    try:
                        name = _read_at(descriptor, "name")
                    except (OSError, ValueError, UnicodeError):
                        # Unrelated hwmon devices need not provide readable names.
                        continue
                    if name == "corsairpsu":
                        if path.parent.name != "hwmon":
                            raise ValueError("unexpected Corsair hwmon layout")
                        matches.append(path)
                finally:
                    os.close(descriptor)
        if not matches:
            return unavailable
        if len(matches) != 1:
            return {**unavailable, "reason": "ambiguous_devices"}
        path = matches[0]
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if _read_at(descriptor, "name") != "corsairpsu" or _read_at(descriptor, "power1_label") != "power total":
                raise ValueError("unexpected total power identity")
            watts = _watts(_read_at(descriptor, "power1_input"))
            rails = {}
            for channel, rail, label in RAILS:
                try:
                    observed_label = _read_at(descriptor, f"power{channel}_label")
                except FileNotFoundError:
                    continue
                if observed_label != label:
                    raise ValueError("unexpected rail identity")
                try:
                    rails[rail] = _watts(_read_at(descriptor, f"power{channel}_input"))
                except (OSError, ValueError, UnicodeError):
                    # A missing optional rail never replaces the total reading.
                    continue
        finally:
            os.close(descriptor)
        unix_ns, monotonic_ns = clock() if clock else (time.time_ns(), time.monotonic_ns())
        identity = str(path.parent.parent.relative_to(devices))
        return {"reason": "available", "watts": watts, "rails": rails,
                "device": hashlib.sha256(identity.encode()).hexdigest(),
                "boot_id": _boot_id(proc), "unix_ns": unix_ns, "monotonic_ns": monotonic_ns}
    except FileNotFoundError:
        return {**unavailable, "reason": "sensor_unavailable"}
    except (OSError, ValueError, UnicodeError):
        return {**unavailable, "reason": "invalid_sensor"}


def _finite(value, maximum=1e30):
    # Check integer bounds before conversion: malformed JSON can contain integers
    # larger than a float, even within the small on-disk state size limit.
    return type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value)


def _anchor(value):
    return (isinstance(value, dict) and set(value) == {"boot_id", "device", "unix_ns", "monotonic_ns", "watts"}
            and isinstance(value["boot_id"], str) and BOOT_ID.fullmatch(value["boot_id"]) is not None
            and isinstance(value["device"], str) and re.fullmatch(r"[a-f0-9]{64}", value["device"]) is not None
            and all(type(value[key]) is int and 0 < value[key] < 2**63 for key in ("unix_ns", "monotonic_ns"))
            and _finite(value["watts"], MAX_POWER_WATTS))


def validate_state(value):
    if (not isinstance(value, dict) or set(value) != {"schema", "output_energy_joules", "covered_seconds", "anchor"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not _finite(value["output_energy_joules"]) or not _finite(value["covered_seconds"])
            or (value["anchor"] is not None and not _anchor(value["anchor"]))):
        raise ValueError("invalid PSU energy state; retain for owner inspection")
    return value


def advance(previous, sample):
    """Pure transition: only a bounded observed interval adds energy/coverage."""
    state = dict(validate_state(previous)) if previous is not None else {
        "schema": 1, "output_energy_joules": 0.0, "covered_seconds": 0.0, "anchor": None}
    current = {key: sample.get(key) for key in ("boot_id", "device", "unix_ns", "monotonic_ns", "watts")}
    if not _anchor(current):
        state["anchor"] = None
        return state, "sample_unavailable"
    old = state["anchor"]
    reason = "new_interval"
    if old is not None:
        elapsed = (current["monotonic_ns"] - old["monotonic_ns"]) / 1e9
        wall_elapsed = (current["unix_ns"] - old["unix_ns"]) / 1e9
        if current["boot_id"] != old["boot_id"]:
            reason = "boot_changed"
        elif current["device"] != old["device"]:
            reason = "device_changed"
        elif not 0 < elapsed <= MAX_GAP_SECONDS or not 0 < wall_elapsed <= MAX_GAP_SECONDS:
            reason = "sample_gap"
        elif abs(elapsed - wall_elapsed) > CLOCK_TOLERANCE_SECONDS:
            reason = "clock_discontinuity"
        else:
            state["output_energy_joules"] += (old["watts"] + current["watts"]) * elapsed / 2
            state["covered_seconds"] += elapsed
            reason = "available"
    state["anchor"] = current
    return validate_state(state), reason


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate energy state key")
        result[key] = value
    return result


class Directory:
    """Pinned caller-owned state/textfile directory with bounded atomic writes."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            os.close(self.fd)
            raise ValueError("textfile directory must be caller-owned and not group/world writable")

    def close(self):
        os.close(self.fd)

    def _file(self, descriptor, mode=None):
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
            raise ValueError("refusing unowned, aliased or unsafe telemetry file")

    def check_existing(self, name, mode=None):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("refusing a symlink telemetry file") from None
            raise
        try:
            self._file(descriptor, mode)
        finally:
            os.close(descriptor)

    @contextmanager
    def locked(self):
        descriptor = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=self.fd)
        try:
            self._file(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield self
        finally:
            os.close(descriptor)

    def read_state(self):
        try:
            descriptor = os.open(STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            self._file(descriptor, 0o600)
            value = os.read(descriptor, MAX_STATE_BYTES + 1)
            if len(value) > MAX_STATE_BYTES:
                raise ValueError("oversized PSU energy state")
            return validate_state(json.loads(value, object_pairs_hook=_pairs))
        finally:
            os.close(descriptor)

    def write(self, name, text, mode):
        if name not in {STATE_FILE, "hardware.prom"} or mode not in {0o600, 0o644}:
            raise ValueError("unsupported telemetry output")
        self.check_existing(name, 0o600 if name == STATE_FILE else None)
        temporary = ".telemetry-" + secrets.token_hex(12)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.fd)
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(text)
                output.flush()
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
            self.check_existing(name, 0o600 if name == STATE_FILE else None)
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            temporary = None
            os.fsync(self.fd)
        finally:
            if temporary is not None:
                os.unlink(temporary, dir_fd=self.fd)

    def energy(self, sample):
        try:
            previous = self.read_state()
            state, reason = advance(previous, sample)
            if previous is None and state["anchor"] is None:
                return None, "sample_unavailable"
            self.write(STATE_FILE, json.dumps(state, allow_nan=False, sort_keys=True) + "\n", 0o600)
            return state, reason
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None, "state_unavailable"


def metrics(sample, energy=None, energy_reason="state_unavailable"):
    lines = [f'workstation_psu_available {int(sample.get("watts") is not None)}',
             f'workstation_psu_status{{reason="{sample["reason"]}"}} 1']
    if sample.get("watts") is not None:
        lines.extend([f'workstation_psu_output_power_watts {sample["watts"]:.12g}',
                      f'workstation_psu_sample_timestamp_seconds {sample["unix_ns"] / 1e9:.12g}'])
        for _, rail, _ in RAILS:
            value = sample["rails"].get(rail)
            lines.append(f'workstation_psu_rail_available{{rail="{rail}"}} {int(value is not None)}')
            if value is not None:
                lines.append(f'workstation_psu_rail_power_watts{{rail="{rail}"}} {value:.12g}')
    available = energy is not None and energy["anchor"] is not None
    lines.append(f"workstation_psu_energy_available {int(available)}")
    lines.append(f'workstation_psu_energy_status{{reason="{energy_reason}"}} 1')
    if energy is not None:
        lines.extend([f'workstation_psu_output_energy_joules_total {energy["output_energy_joules"]:.12g}',
                      f'workstation_psu_energy_covered_seconds_total {energy["covered_seconds"]:.12g}'])
        if available:
            lines.append(f'workstation_psu_energy_sample_timestamp_seconds {energy["anchor"]["unix_ns"] / 1e9:.12g}')
    return "\n".join(lines) + "\n"
