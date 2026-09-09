"""Bounded, read-only Linux samples shared by explicit workstation benchmarks.

No third-party modules; unknown or inaccessible sensors remain null. Sysfs
frequencies are observations, not APERF/MPERF sustained-clock measurements.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cpu_power(sys=Path("/sys")):
    cpu = sys / "devices/system/cpu"
    fields = ("scaling_driver", "scaling_governor", "energy_performance_preference",
              "scaling_cur_freq", "cpuinfo_cur_freq", "scaling_min_freq", "scaling_max_freq",
              "amd_pstate_highest_perf", "amd_pstate_prefcore_ranking", "boost", "affected_cpus")
    return {"amd_pstate_status": read(cpu / "amd_pstate/status"),
            "preferred_core": read(cpu / "amd_pstate/prefcore"),
            "boost": read(cpu / "cpufreq/boost"),
            "policies": {p.name: {key: read(p / key) for key in fields}
                         for p in sorted((cpu / "cpufreq").glob("policy*"))},
            "clock_scope": "sysfs kHz samples; not APERF/MPERF effective clocks"}


def snapshot(sys=Path("/sys"), proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    sensors = {}
    for device in sorted((sys / "class/hwmon").glob("hwmon*")):
        values = {}
        for pattern in ("temp*_input", "power*_average", "power*_input", "energy*_input", "freq*_input"):
            for sensor in device.glob(pattern):
                values[sensor.name] = read(sensor)
        sensors[str(device.resolve())] = {"name": read(device / "name"), "values": values}
    gpus = {}
    for device in sorted((sys / "bus/pci/devices").glob("*")):
        if read(device / "vendor") == "0x1002" and (read(device / "class") or "").startswith("0x03"):
            gpus[device.name] = {key: read(device / key) for key in
                                 ("mem_info_vram_total", "mem_info_vram_used", "gpu_busy_percent",
                                  "current_link_speed", "current_link_width", "pp_dpm_sclk", "pp_dpm_mclk")}
    return {"unix_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(),
            "cpu_power": cpu_power(sys), "gpus_by_bdf": gpus, "hwmon": sensors,
            "host_memory": read(proc / "meminfo"),
            "cgroup": {key: read(cgroup / key) for key in
                       ("memory.current", "memory.peak", "memory.max", "memory.events",
                        "cpu.stat", "cpu.max", "cpuset.cpus.effective", "cpuset.mems.effective")},
            "units": "hwmon ABI: temp millidegrees C, power microwatts, energy microjoules; VRAM bytes",
            "limitations": "Sampled peaks can miss spikes; host root cgroup is not a pod measurement."}


def pod_cgroup(uid, root=Path("/sys/fs/cgroup")):
    """Resolve a Pod UID, not CPU numbers or an assumed container ordinal."""
    import re
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", uid):
        return None
    names = ("pod" + uid, "pod" + uid.replace("-", "_") + ".slice")
    matches = [p for p in root.rglob("*pod*") if p.is_dir()
               and (p.name == names[0] or p.name.endswith("-" + names[1]))]
    return matches[0] if len(matches) == 1 else None


def cgroup_values(path):
    if path is None:
        return {"status": "unavailable"}
    return {"path": str(path), "scope": "pod cgroup; memory.peak is lifetime, memory.current is run-sampled",
            "values": {key: read(path / key) for key in ("memory.current", "memory.peak", "memory.max",
                "memory.events", "cpu.stat", "cpu.max", "cpuset.cpus.effective", "cpuset.mems.effective")}}


class Sampler:
    def __init__(self, path, interval=1.0, collect=snapshot):
        self.path, self.interval, self.collect = path, interval, collect
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        try:
            with open(self.path, "x") as output:
                while True:
                    output.write(json.dumps(self.collect(), allow_nan=False) + "\n")
                    output.flush()
                    if self.stop.wait(self.interval):
                        break
        except Exception as error:
            self.error = type(error).__name__

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=20)
        if self.thread.is_alive() or self.error:
            raise RuntimeError("telemetry sampling failed or did not stop")


def run_command(command, output, timeout):
    """Retain failed evidence too. Never run a shell or detach surviving children."""
    output = Path(output)
    output.mkdir(mode=0o700)
    started = time.monotonic_ns()
    status = "failed"
    returncode = None
    process = None
    try:
        with Sampler(output / "telemetry.jsonl"), open(output / "stdout.txt", "x") as stdout, open(output / "stderr.txt", "x") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
            returncode = process.wait(timeout=timeout)
            status = "measured-not-qualified" if returncode == 0 else "failed"
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            # The foreground process may have exited while a descendant ignored
            # TERM. Kill the remaining owned group, not unrelated host processes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        (output / "run.json").write_text(json.dumps({"schema": 1, "status": status,
            "returncode": returncode, "elapsed_seconds": (time.monotonic_ns() - started) / 1e9,
            "started_monotonic_ns": started, "command_executable": command[0],
            "arguments": "not retained; record non-secret workload inputs separately"}, indent=2) + "\n")
    return returncode


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "cpu-power", "run"))
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "snapshot":
        print(json.dumps(snapshot(), indent=2))
    elif args.action == "cpu-power":
        print(json.dumps(cpu_power(), indent=2))
    else:
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command or not args.output or not 1 <= args.timeout <= 86400:
            parser.error("run requires a new output directory, bounded timeout and command after --")
        def interrupted(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        raise SystemExit(run_command(command, args.output, args.timeout))


if __name__ == "__main__":
    main()
