#!/usr/bin/env python3
"""Host maintenance reports for node-exporter's textfile collector.

Subcommands, each run by its own systemd unit (installed by the host_maintenance Ansible role):

  advisories  Full Arch security-tracker report: scan freshness, fix status,
              findings with no recorded fix and packages the tracker cannot cover.
  health      NVMe SMART health, rasdaemon's recorded RAS events and the state
              of the monitoring daemons and backup configuration.
  retention   Report-only inventory of rootless Podman images and AI-stack build
              outputs with an explicit, conservative deletion-candidate list.
              It never deletes anything.
  job-result  ExecStopPost hook: records a job's result and resource use and
              publishes its validated metrics into the textfile directory.

Collectors write only to their own state directory. Only job-result (run as root
by systemd's "+" prefix) installs files into the textfile directory, after
checking every line against a fixed metric-name prefix for that collector.
Standard library only; no network access except the advisory download.
"""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

TEXTFILE_DIR = Path("/var/lib/workstation-telemetry")
JOB_STATE = Path("/var/lib/workstation-maintenance/jobs.json")
TRACKER_URL = "https://security.archlinux.org/issues/all.json"
CURRENT_MIRROR = "https://geo.mirror.pkgbuild.com/$repo/os/$arch"
MAX_TRACKER_BYTES = 64 * 1024 * 1024
MAX_PUBLISH_BYTES = 512 * 1024
MAX_PUBLISH_LINES = 4000
MAX_INFO_SERIES = 64
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
FIX_CLASSES = ("fix_in_configured_repo", "fix_needs_snapshot_move", "no_fix_recorded")
PREFIXES = {
    "advisories": ("workstation_advisory_", "workstation_update_"),
    "health": ("workstation_nvme_", "workstation_smart_", "workstation_ras_",
               "workstation_maintenance_service_", "workstation_maintenance_backup_",
               "workstation_maintenance_health_"),
    "retention": ("workstation_retention_",),
    "jobs": ("workstation_maintenance_job_",),
}
NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
SAMPLE = re.compile(r'^([a-z_][a-z0-9_]*)(\{[a-z_]+="[^"\\\n]{0,160}"(,[a-z_]+="[^"\\\n]{0,160}")*\})? '
                    r'(-?[0-9]+(\.[0-9]+)?(e[+-]?[0-9]+)?|NaN)$')
COMMENT = re.compile(r"^# (HELP|TYPE) ([a-z_][a-z0-9_]*) [ -~]{1,200}$")


class Failure(Exception):
    """An expected, reportable failure; the message is safe to log."""


# --------------------------------------------------------------------------- output


class Metrics:
    """Prometheus text exposition with bounded, escaped label values."""

    def __init__(self):
        self.lines = []
        self.declared = set()

    def add(self, name, value, labels=None, help_text=None, kind="gauge"):
        if not NAME.match(name):
            raise ValueError("invalid metric name")
        if name not in self.declared:
            self.declared.add(name)
            self.lines.append(f"# HELP {name} {help_text or name.replace('_', ' ')}")
            self.lines.append(f"# TYPE {name} {kind}")
        rendered = ""
        if labels:
            parts = []
            for key, raw in labels.items():
                if not NAME.match(key):
                    raise ValueError("invalid label name")
                text = re.sub(r'["\\\n]', "_", str(raw))[:160]
                parts.append(f'{key}="{text}"')
            rendered = "{" + ",".join(parts) + "}"
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, float) and math.isnan(value):
            number = "NaN"
        elif isinstance(value, float) and not value.is_integer():
            number = repr(value)
        else:
            number = str(int(value))
        self.lines.append(f"{name}{rendered} {number}")

    def text(self):
        return "\n".join(self.lines) + "\n"


def write_atomic(path, text, mode=0o644):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(text)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def validate_textfile(collector, text):
    """Accept only well-formed samples whose names belong to this collector."""
    if collector not in PREFIXES:
        raise Failure("unknown collector")
    if len(text.encode()) > MAX_PUBLISH_BYTES:
        raise Failure("metrics file is oversized")
    lines = text.splitlines()
    if not lines or len(lines) > MAX_PUBLISH_LINES:
        raise Failure("metrics file is empty or has too many lines")
    for line in lines:
        match = COMMENT.match(line) or SAMPLE.match(line)
        if not match:
            raise Failure("metrics file contains an unreviewed line")
        name = match.group(2) if line.startswith("#") else match.group(1)
        if not name.startswith(PREFIXES[collector]):
            raise Failure("metrics file contains a name outside its collector prefix")
    return text if text.endswith("\n") else text + "\n"


def read_regular(path, limit):
    """Read a regular, non-symlink file without following links."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise Failure("refusing a non-regular or oversized file")
        return os.read(descriptor, limit + 1).decode()
    finally:
        os.close(descriptor)


def publish(collector, source, directory=TEXTFILE_DIR):
    text = validate_textfile(collector, read_regular(source, MAX_PUBLISH_BYTES))
    if directory.is_symlink() or not directory.is_dir():
        raise Failure("textfile directory is missing")
    write_atomic(directory / f"maintenance-{collector}.prom", text)


def run(argv, timeout=120, check=True, env=None):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    if check and result.returncode != 0:
        raise Failure(f"{Path(argv[0]).name} exited {result.returncode}")
    return result


def log_event(record):
    """One fixed-schema line under the workstation-maintenance journal identifier."""
    import syslog
    syslog.openlog("workstation-maintenance", 0, syslog.LOG_DAEMON)
    level = syslog.LOG_INFO if record.get("result") == "success" else syslog.LOG_WARNING
    syslog.syslog(level, json.dumps(record, separators=(",", ":")))
    syslog.closelog()


# --------------------------------------------------------------------------- advisories


def vercmp(left, right):
    return int(run(["vercmp", left, right], timeout=10).stdout.strip())


def installed_packages(dbpath=None, config=None):
    argv = ["pacman", "-Q"] + (["--dbpath", dbpath] if dbpath else []) + (["--config", config] if config else [])
    packages = {}
    for line in run(argv).stdout.splitlines():
        name, version = line.split(" ", 1)
        packages[name] = version
    if len(packages) < 50:
        raise Failure("installed package list is implausibly small")
    return packages


def sync_versions(names, dbpath=None, config=None):
    """Versions in the given sync databases; packages absent there are omitted."""
    versions = {}
    if not names:
        return versions
    argv = ["pacman", "-Si"] + (["--dbpath", dbpath] if dbpath else []) + (["--config", config] if config else [])
    output = run(argv + sorted(names), check=False).stdout
    name = None
    for line in output.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Name":
            name = value
        elif key == "Version" and name:
            versions.setdefault(name, value)
    return versions


def load_tracker(path):
    groups = json.loads(Path(path).read_text())
    if not isinstance(groups, list) or len(groups) < 500:
        raise Failure("tracker data is not a plausible advisory list")
    for group in groups:
        if not (isinstance(group, dict) and isinstance(group.get("name"), str)
                and isinstance(group.get("packages"), list) and isinstance(group.get("status"), str)
                and (group.get("fixed") is None or isinstance(group.get("fixed"), str))):
            raise Failure("tracker data has an unexpected group shape")
    return groups


def fetch_tracker(destination):
    request = urllib.request.Request(TRACKER_URL, headers={"User-Agent": "workstation-maintenance/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read(MAX_TRACKER_BYTES + 1)
    if len(body) > MAX_TRACKER_BYTES:
        raise Failure("tracker download is oversized")
    write_atomic(destination, body.decode(), 0o600)


def independent_matches(groups, installed):
    """arch-audit's rule (with --show-testing), re-derived: every group except
    "Not affected" whose recorded fix, if any, is newer than the installed version."""
    matches = {}
    for group in groups:
        if group["status"] == "Not affected":
            continue
        for package in group["packages"]:
            if package not in installed:
                continue
            fixed = group.get("fixed")
            if fixed and vercmp(installed[package], fixed) >= 0:
                continue
            matches.setdefault(package, []).append(group)
    return matches


def refresh_current_db(state):
    """checkupdates-style: current Arch databases in a private path, never /var/lib/pacman."""
    dbpath = state / "current-db"
    (dbpath / "sync").mkdir(parents=True, exist_ok=True)
    local = dbpath / "local"
    if not local.is_symlink():
        local.symlink_to("/var/lib/pacman/local")
    config = state / "pacman-current.conf"
    config.write_text(
        "[options]\nArchitecture = auto\nSigLevel = Required DatabaseOptional\n"
        f"[core]\nServer = {CURRENT_MIRROR}\n[extra]\nServer = {CURRENT_MIRROR}\n")
    run(["fakeroot", "--", "pacman", "-Sy", "--dbpath", str(dbpath), "--config", str(config),
         "--logfile", "/dev/null", "--noconfirm"], timeout=300)
    return str(dbpath), str(config)


def severity_of(groups):
    order = {name: index for index, name in enumerate(SEVERITIES)}
    values = [g.get("severity", "Unknown").lower() for g in groups]
    values = [v if v in order else "unknown" for v in values]
    return min(values, key=order.get)


def advisories(args):
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    report_path = state / "advisories.json"
    previous = json.loads(report_path.read_text()) if report_path.exists() else {}
    now = int(time.time())
    report = {"schema": 1, "run": now, "success": False, "reason": "", "findings": previous.get("findings", []),
              "last_success": previous.get("last_success", 0), "uncovered": previous.get("uncovered", [])}
    try:
        tracker = state / "tracker.json"
        if not args.tracker:
            fetch_tracker(tracker)
        groups = load_tracker(args.tracker or tracker)
        installed = installed_packages()
        # pacman -Qm exits 1 when there are no foreign packages.
        foreign = sorted(run(["pacman", "-Qmq"], check=False).stdout.split())
        audit = json.loads(run(["arch-audit", "--show-testing", "--json", "--source", str(args.tracker or tracker)],
                               timeout=300).stdout)
        if not isinstance(audit, list):
            raise Failure("arch-audit returned no JSON list")
        audit_packages = {p for group in audit for p in group.get("packages", []) if p in installed}
        matches = independent_matches(groups, installed)
        if set(matches) != audit_packages:
            report["crosscheck"] = sorted(set(matches) ^ audit_packages)
            raise Failure("arch-audit and the independent tracker match disagree")
        configured = sync_versions(matches)
        current, current_ok, pending = {}, False, None
        try:
            dbpath, config = refresh_current_db(state)
            current = sync_versions(matches, dbpath, config)
            pending = len([l for l in run(["pacman", "-Qu", "--dbpath", dbpath, "--config", config],
                                          check=False).stdout.splitlines() if l.strip()])
            current_ok = True
            report["current_db_success"] = now
        except (Failure, OSError, subprocess.SubprocessError):
            report["current_db_success"] = previous.get("current_db_success", 0)
        findings = []
        for package, found in sorted(matches.items()):
            fixes = [g["fixed"] for g in found if g.get("fixed")]
            fixed = fixes[0] if fixes else None
            for candidate in fixes[1:]:
                if vercmp(candidate, fixed) > 0:
                    fixed = candidate
            # One open group without a recorded fix leaves the package unfixed.
            if any(not g.get("fixed") for g in found):
                fixed = None
            repo = configured.get(package)
            if fixed is None:
                klass = "no_fix_recorded"
            elif repo and vercmp(repo, fixed) >= 0:
                klass = "fix_in_configured_repo"
            else:
                klass = "fix_needs_snapshot_move"
            newer = current.get(package)
            findings.append({
                "package": package, "installed": installed[package], "class": klass,
                "severity": severity_of(found), "fixed": fixed, "configured_repo": repo,
                "current_arch": newer,
                "newer_in_current_arch": bool(newer and vercmp(newer, installed[package]) > 0),
                "groups": [{"name": g["name"], "status": g["status"], "type": g.get("type"),
                            "affected": g.get("affected"), "fixed": g.get("fixed"),
                            "issues": g.get("issues", [])} for g in found],
            })
        report.update({"success": True, "last_success": now, "findings": findings, "uncovered": foreign,
                       "tracker_groups": len(groups), "installed": len(installed),
                       "pending_updates": pending if current_ok else previous.get("pending_updates")})
    except (Failure, OSError, ValueError, subprocess.SubprocessError) as error:
        report["reason"] = str(error) if isinstance(error, Failure) else type(error).__name__
        report["tracker_groups"] = previous.get("tracker_groups", 0)
        report["pending_updates"] = previous.get("pending_updates")
        report["current_db_success"] = previous.get("current_db_success", 0)
    report["configured_db_timestamp"] = configured_db_timestamp(Path(args.sync_dir))
    write_atomic(report_path, json.dumps(report, indent=2) + "\n", 0o644)
    write_atomic(state / "advisories.prom", advisory_metrics(report), 0o644)
    if not report["success"]:
        print(f"FAILED: advisory scan: {report['reason']}; previous findings are retained", file=sys.stderr)
        return 1
    counts = {}
    for finding in report["findings"]:
        counts[finding["class"]] = counts.get(finding["class"], 0) + 1
    print("advisory scan: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in FIX_CLASSES)
          + f", uncovered={len(report['uncovered'])}")
    return 0


def configured_db_timestamp(sync_dir):
    stamps = [p.stat().st_mtime for p in sync_dir.glob("*.db") if p.name in {"core.db", "extra.db"}]
    return int(min(stamps)) if stamps else 0


def advisory_metrics(report):
    m = Metrics()
    m.add("workstation_advisory_last_run_timestamp_seconds", report["run"], help_text="Last advisory scan attempt")
    m.add("workstation_advisory_last_run_success", report["success"],
          help_text="1 when the last scan downloaded, parsed and cross-checked the tracker")
    m.add("workstation_advisory_last_success_timestamp_seconds", report["last_success"],
          help_text="When the findings below were produced; 0 means never")
    m.add("workstation_advisory_tracker_groups", report.get("tracker_groups") or 0,
          help_text="Advisory groups parsed from the Arch security tracker")
    m.add("workstation_advisory_configured_db_timestamp_seconds", report.get("configured_db_timestamp", 0),
          help_text="Age of the configured (snapshot) core/extra package databases")
    m.add("workstation_advisory_current_db_last_success_timestamp_seconds", report.get("current_db_success") or 0,
          help_text="Last refresh of the private current-Arch comparison databases")
    if report.get("pending_updates") is not None:
        m.add("workstation_update_pending_packages", report["pending_updates"],
              help_text="Installed repository packages with a newer version in current Arch")
    if report["last_success"]:
        counts = {(k, s): 0 for k in FIX_CLASSES for s in SEVERITIES}
        for finding in report["findings"]:
            counts[(finding["class"], finding["severity"])] += 1
        for (klass, severity), value in sorted(counts.items()):
            m.add("workstation_advisory_findings", value, {"class": klass, "severity": severity},
                  "Vulnerable installed packages by fix status and highest severity")
        for finding in report["findings"][:MAX_INFO_SERIES]:
            m.add("workstation_advisory_finding_info", 1,
                  {"package": finding["package"], "class": finding["class"], "severity": finding["severity"],
                   "newer_in_current_arch": str(finding["newer_in_current_arch"]).lower()},
                  "One series per vulnerable installed package (see advisories.json for CVEs)")
        m.add("workstation_advisory_uncovered_packages", len(report["uncovered"]),
              help_text="Foreign/custom packages the Arch tracker cannot cover")
        for package in report["uncovered"][:MAX_INFO_SERIES]:
            m.add("workstation_advisory_uncovered_package_info", 1, {"package": package},
                  "Foreign/custom package outside the tracker's coverage")
    return m.text()


# --------------------------------------------------------------------------- health


NVME_FIELDS = {
    "critical_warning": ("workstation_nvme_critical_warning", "NVMe critical-warning bitfield (0 is healthy)"),
    "temperature": ("workstation_nvme_temperature_celsius", "NVMe composite temperature"),
    "available_spare": ("workstation_nvme_available_spare_percent", "Remaining spare capacity"),
    "available_spare_threshold": ("workstation_nvme_available_spare_threshold_percent", "Vendor spare threshold"),
    "percentage_used": ("workstation_nvme_percentage_used", "Vendor endurance estimate used"),
    "media_errors": ("workstation_nvme_media_errors", "Media and data integrity errors (lifetime)"),
    "num_err_log_entries": ("workstation_nvme_error_log_entries", "Error information log entries (lifetime)"),
    "unsafe_shutdowns": ("workstation_nvme_unsafe_shutdowns", "Unsafe shutdowns (lifetime)"),
    "warning_temp_time": ("workstation_nvme_warning_temperature_minutes", "Minutes above the warning threshold"),
    "critical_comp_time": ("workstation_nvme_critical_temperature_minutes", "Minutes above the critical threshold"),
    "power_on_hours": ("workstation_nvme_power_on_hours", "Power-on hours"),
    "data_units_written": ("workstation_nvme_data_units_written", "Data units written (1 unit = 512,000 bytes)"),
}
RAS_SEVERITY = (
    (re.compile(r"fatal", re.I), "fatal"),
    (re.compile(r"uncorrect", re.I), "uncorrected"),
    (re.compile(r"deferred", re.I), "deferred"),
    (re.compile(r"correct", re.I), "corrected"),
)
RAS_TABLES = {"mc_event": "err_type", "aer_event": "err_type", "mce_record": None,
              "memory_failure_event": None, "non_standard_event": "sev", "extlog_event": None,
              "arm_event": None, "devlink_event": None, "disk_errors": None}


def controller_of(device):
    """by-id path -> nvmeN controller name (kernel names may change across boots)."""
    target = Path(os.path.realpath(device)).name
    match = re.match(r"^(nvme[0-9]+)", target)
    return match.group(1) if match else "unknown"


def smart_sample(device, metrics, now):
    result = run(["smartctl", "--json=c", "-a", "-d", "nvme", device], timeout=60, check=False)
    labels = {"device": controller_of(device)}
    try:
        data = json.loads(result.stdout)
        health = data["nvme_smart_health_information_log"]
        labels["serial"] = data["serial_number"]
        passed = data["smart_status"]["passed"]
    except (ValueError, KeyError, TypeError):
        metrics.add("workstation_smart_device_up", 0, labels, "1 when smartctl returned NVMe health data")
        return False
    metrics.add("workstation_smart_device_up", 1, labels, "1 when smartctl returned NVMe health data")
    metrics.add("workstation_smart_exit_status", result.returncode, labels,
                "smartctl exit bitmask (bit 3 failing, bit 6 error log entries)")
    metrics.add("workstation_nvme_info", 1, dict(labels, model=data.get("model_name", ""),
                                                  firmware=data.get("firmware_version", "")),
                "NVMe identity (stable serial; nvmeN may change across boots)")
    metrics.add("workstation_nvme_smart_passed", bool(passed), labels, "SMART overall health self-assessment")
    for key, (name, text) in NVME_FIELDS.items():
        if isinstance(health.get(key), (int, float)):
            metrics.add(name, health[key], labels, text)
    thresholds = data.get("nvme_composite_temperature_threshold", {})
    for key in ("warning", "critical"):
        if isinstance(thresholds.get(key), int):
            metrics.add(f"workstation_nvme_{key}_temperature_threshold_celsius", thresholds[key], labels,
                        f"Controller-reported {key} composite temperature threshold")
    return True


def ras_counts(database):
    import sqlite3
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for table, column in RAS_TABLES.items():
            if table not in tables:
                continue
            counts[(table, "all")] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if column:
                # Zero-valued series exist before the first event, so a rate or
                # delta over them sees the first error too.
                for severity in ("corrected", "deferred", "uncorrected", "fatal", "other"):
                    counts[(table, severity)] = 0
                for (value, count) in connection.execute(f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}"):
                    severity = next((name for pattern, name in RAS_SEVERITY if pattern.search(str(value))), "other")
                    counts[(table, severity)] = counts.get((table, severity), 0) + count
        return counts
    finally:
        connection.close()


def unit_state(unit):
    active = run(["systemctl", "is-active", unit], check=False).stdout.strip()
    enabled = run(["systemctl", "is-enabled", unit], check=False).stdout.strip()
    return active == "active", enabled in {"enabled", "enabled-runtime", "static", "alias"}


def health(args):
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    m = Metrics()
    m.add("workstation_maintenance_health_timestamp_seconds", now, help_text="Last hardware-health sample")
    ok = all([smart_sample(device, m, now) for device in args.device])
    m.add("workstation_smart_devices_configured", len(args.device), help_text="NVMe devices this sampler watches")
    database = Path(args.ras_db)
    if database.exists():
        try:
            for (table, severity), value in sorted(ras_counts(database).items()):
                m.add("workstation_ras_events_recorded", value, {"table": table, "severity": severity},
                      "Rows rasdaemon has recorded (persistent; increases mean new hardware errors)")
            m.add("workstation_ras_database_readable", 1, help_text="1 when rasdaemon's SQLite database was read")
        except Exception:  # noqa: BLE001 - any sqlite failure is reported as unreadable
            m.add("workstation_ras_database_readable", 0, help_text="1 when rasdaemon's SQLite database was read")
            ok = False
    else:
        m.add("workstation_ras_database_readable", 0, help_text="1 when rasdaemon's SQLite database was read")
    for unit in args.unit:
        active, enabled = unit_state(unit)
        m.add("workstation_maintenance_service_active", active, {"unit": unit},
              "1 when a monitoring daemon or timer is active")
        m.add("workstation_maintenance_service_enabled", enabled, {"unit": unit},
              "1 when it starts at boot")
    # Backups are parked: report "not configured" explicitly instead of letting
    # an absent success timestamp look healthy.
    for kind, path in (("service", "/etc/workstation/service-backup.json"),):
        m.add("workstation_maintenance_backup_configured", Path(path).is_file(), {"kind": kind},
              "1 when a backup configuration exists; 0 means backups are not configured")
    for kind, unit in (("host", "workstation-restic-backup.timer"),):
        m.add("workstation_maintenance_backup_configured", unit_state(unit)[1], {"kind": kind},
              "1 when a backup configuration exists; 0 means backups are not configured")
    write_atomic(state / "health.prom", m.text(), 0o644)
    if not ok:
        print("FAILED: a SMART device or the RAS database could not be read", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- retention

IMAGE_STAGE = {"sglang-gfx1201": "image", "creative-gfx1201": "creative", "graphiti-memory": "graphiti"}
INPUT_STAGES = ("rocm", "torch", "sglang", "comfyui", "swarmui")
LOCK_IMAGES = ("THEROCK_BUILD_IMAGE", "RUNTIME_BASE_IMAGE", "DOTNET_SDK_IMAGE", "COSIGN_IMAGE", "TRIVY_IMAGE")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def read_lock(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def du_bytes(paths):
    if not paths:
        return {}
    result = run(["du", "-s", "-x", "--block-size=1", "--"] + [str(p) for p in paths], timeout=3600, check=False)
    sizes = {}
    for line in result.stdout.splitlines():
        size, _, name = line.partition("\t")
        sizes[name] = int(size)
    return sizes


def kubernetes_images(kubectl):
    """Digest references in live objects; ReplicaSets carry the rollout history."""
    output = run([kubectl, "get", "deployments,replicasets,statefulsets,daemonsets,jobs,cronjobs,pods",
                  "-A", "-o", "json"], timeout=120).stdout
    items = json.loads(output)["items"]
    deployed, rollback = set(), set()
    history = {}
    for item in items:
        kind, meta = item["kind"], item["metadata"]
        spec = item.get("spec", {})
        template = spec.get("jobTemplate", {}).get("spec", {}).get("template", spec.get("template", spec))
        pod = template.get("spec", template) if kind != "Pod" else spec
        refs = set()
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            refs.update(DIGEST.findall(container.get("image", "")))
        if kind == "ReplicaSet":
            owner = next((o["name"] for o in meta.get("ownerReferences", []) if o["kind"] == "Deployment"), None)
            revision = int(meta.get("annotations", {}).get("deployment.kubernetes.io/revision", "0"))
            if owner:
                history.setdefault((meta["namespace"], owner), []).append((revision, refs))
            if spec.get("replicas", 0):
                deployed.update(refs)
        else:
            deployed.update(refs)
    for revisions in history.values():
        revisions.sort(key=lambda entry: entry[0], reverse=True)
        current = revisions[0][1]
        previous = next((refs for _, refs in revisions[1:] if refs - current), set())
        rollback.update(previous - deployed)
    return deployed, rollback


def storage_images(storage):
    images = json.loads((Path(storage) / "overlay-images/images.json").read_text())
    layers = json.loads((Path(storage) / "overlay-layers/layers.json").read_text())
    by_id = {layer["id"]: layer for layer in layers}
    def chain(top):
        seen = []
        while top and top in by_id and top not in seen:
            seen.append(top)
            top = by_id[top].get("parent")
        return seen
    return [dict(image, chain=chain(image.get("layer"))) for image in images], by_id


def active_builds(root, storage, metrics_dir):
    active = set()
    state = Path(metrics_dir) / "ai-stack-state.json"
    if state.is_file():
        for stage, record in json.loads(state.read_text()).items():
            if isinstance(record, dict) and record.get("running"):
                active.add(stage)
    containers = Path(storage) / "overlay-containers/containers.json"
    if containers.is_file():
        for container in json.loads(containers.read_text()):
            for name in container.get("names", []):
                match = re.match(r"^ai-stack-([a-z]+)-", name)
                if match:
                    active.add(match.group(1))
    return active


def receipt(run_dir):
    path = run_dir / "out/RECEIPT.json"
    return json.loads(path.read_text()) if path.is_file() else None


def sha256_file(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def classify_runs(builds, lock, active, deployed, rollback, now, keep_sglang):
    """Run directories: required inputs, rollback rebuild inputs, provenance and candidates."""
    runs = []
    for path in sorted(p for p in builds.iterdir() if p.is_dir() and "-" in p.name):
        stage, key = path.name.split("-", 1)
        record = receipt(path)
        digest_file = path / "out/image.digest"
        published = digest_file.read_text().strip() if digest_file.is_file() else None
        runs.append({"path": str(path), "stage": stage, "key": key, "receipt": record is not None,
                     "published": published, "mtime": int(path.stat().st_mtime), "record": record or {}})
    receipts = {r["stage"]: [] for r in runs}
    for r in runs:
        if r["receipt"]:
            receipts[r["stage"]].append(r)
    current_torch = next((r for r in receipts.get("torch", [])
                          if r["record"].get("pytorch_commit") == lock.get("PYTORCH_COMMIT")
                          and r["record"].get("therock_commit") == lock.get("THEROCK_COMMIT")), None)
    torch_sha = sha256_file(Path(current_torch["path"]) / "out/RECEIPT.json") if current_torch else None
    def matches_lock(r):
        rec = r["record"]
        if r["stage"] == "rocm":
            return rec.get("therock_commit") == lock.get("THEROCK_COMMIT")
        if r["stage"] == "torch":
            return r is current_torch
        if r["stage"] == "sglang":
            return (rec.get("sglang_commit") == lock.get("SGLANG_COMMIT")
                    and rec.get("sglang_patchset_sha256") == lock.get("SGLANG_PATCHSET_SHA256")
                    and rec.get("torch_receipt_sha256") == torch_sha)
        if r["stage"] == "comfyui":
            return (rec.get("comfyui_commit") == lock.get("COMFYUI_COMMIT")
                    and rec.get("torch_receipt_sha256") == torch_sha)
        if r["stage"] == "swarmui":
            return rec.get("swarmui_commit") == lock.get("SWARMUI_COMMIT")
        return False
    for stage, items in receipts.items():
        items.sort(key=lambda r: r["mtime"], reverse=True)
    for r in runs:
        stage = r["stage"]
        newest_receipts = receipts.get(stage, [])[: keep_sglang if stage == "sglang" else 1]
        is_newest = any(r is x for x in newest_receipts)
        if stage in active and r is max((x for x in runs if x["stage"] == stage), key=lambda x: x["mtime"]):
            r["decision"], r["reason"] = "keep", "build in progress"
        elif r["published"] and (r["published"] in deployed or r["published"] in rollback):
            r["decision"], r["reason"] = "keep", "provenance of a deployed or rollback image"
        elif stage in INPUT_STAGES and r["receipt"] and matches_lock(r):
            r["decision"], r["reason"] = "keep", "required input for the current stack.lock pins"
        elif stage in INPUT_STAGES and is_newest:
            r["decision"], r["reason"] = "keep", "newest completed run (rollback rebuild input)"
        elif stage in INPUT_STAGES and not r["receipt"] and now - r["mtime"] > 86400:
            r["decision"], r["reason"] = "candidate", "incomplete run (no RECEIPT.json), idle for over a day"
        elif stage in INPUT_STAGES and r["receipt"]:
            r["decision"], r["reason"] = "candidate", "superseded completed run (inputs no longer pinned)"
        elif r["published"]:
            r["decision"], r["reason"] = "keep", "scan/SBOM provenance of a published image (small)"
        else:
            r["decision"], r["reason"] = "review", "unpublished image stage output"
        r.pop("record")
    return runs


def classify_images(images, layers, lock, deployed, rollback, runs, inputs_complete):
    published = {(r["stage"], r["key"]): r["published"] for r in runs if r["published"]}
    lock_digests = {d for key in LOCK_IMAGES for d in DIGEST.findall(lock.get(key, ""))}
    rows = []
    for image in images:
        names = image.get("names") or []
        tags = [n for n in names if "@" not in n]
        digests = set(DIGEST.findall(" ".join(names)) + DIGEST.findall(image.get("digest", "")))
        row = {"id": image["id"][:12], "names": tags or names[:1], "created": image.get("created", ""),
               "chain": image["chain"], "published": None}
        for tag in tags:
            repository, _, key = tag.rpartition(":")
            stage = IMAGE_STAGE.get(repository.rsplit("/", 1)[-1])
            if stage and (stage, key) in published:
                row["published"] = published[(stage, key)]
                digests.add(row["published"])
        row["repository"] = tags[0].rpartition(":")[0] if tags else ""
        if not names:
            row["decision"], row["reason"] = "intermediate", "build-cache image (no name)"
        elif not inputs_complete:
            row["decision"], row["reason"] = "keep", "inventory incomplete; nothing is proposed"
        elif digests & deployed:
            row["decision"], row["reason"] = "keep", "deployed digest"
        elif digests & rollback:
            row["decision"], row["reason"] = "keep", "previous rollout revision (rollback)"
        elif digests & lock_digests:
            row["decision"], row["reason"] = "keep", "base or tool image pinned in stack.lock"
        elif row["published"]:
            row["decision"], row["reason"] = "candidate", "published to the registry; local copy unused"
        else:
            row["decision"], row["reason"] = "review", "not published: this may be the only copy"
        rows.append(row)
    newest = {}
    for row in rows:
        if row["published"]:
            current = newest.get(row["repository"])
            if current is None or row["created"] > current["created"]:
                newest[row["repository"]] = row
    for row in newest.values():
        if row["decision"] == "candidate":
            row["decision"], row["reason"] = "keep", "newest published build of its repository"
    kept, keep_only = set(), set()
    for row in rows:
        if row["decision"] in {"keep", "review"}:
            kept.update(row["chain"])
        if row["decision"] == "keep":
            keep_only.update(row["chain"])
    candidate_layers, review_layers = set(), set()
    for row in rows:
        if row["decision"] in {"candidate", "review"}:
            # Bytes not shared with any image this policy keeps (review images
            # count as kept for candidates, so the candidate total is safe).
            exclusive = [layer for layer in row["chain"]
                         if layer not in (kept if row["decision"] == "candidate" else keep_only)]
            row["exclusive_bytes"] = sum(layers[layer].get("diff-size", 0) for layer in exclusive)
            (candidate_layers if row["decision"] == "candidate" else review_layers).update(exclusive)
    # Layers no named image needs: intermediate build cache from earlier builds.
    orphan = set(layers) - kept - candidate_layers
    total = sum(layer.get("diff-size", 0) for layer in layers.values())
    return rows, {
        "total_bytes": total,
        "candidate_bytes": sum(layers[l].get("diff-size", 0) for l in candidate_layers),
        "kept_bytes": sum(layers[l].get("diff-size", 0) for l in kept),
        "build_cache_only_bytes": sum(layers[l].get("diff-size", 0) for l in orphan),
        "review_bytes": sum(layers[l].get("diff-size", 0) for l in review_layers - candidate_layers),
    }


def retention(args):
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    root = Path(args.ai_stack_root)
    builds = root / "builds"
    inputs = {}
    lock = {}
    try:
        lock = read_lock(Path(args.pipeline) / "stack.lock")
        inputs["stack_lock"] = True
    except OSError:
        inputs["stack_lock"] = False
    try:
        deployed, rollback = kubernetes_images(args.kubectl)
        inputs["kubernetes"] = True
    except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError):
        deployed, rollback = set(), set()
        inputs["kubernetes"] = False
    try:
        images, layers = storage_images(args.storage)
        inputs["podman"] = True
    except (OSError, ValueError, KeyError):
        images, layers = [], {}
        inputs["podman"] = False
    inputs["builds"] = builds.is_dir()
    complete = all(inputs.values())
    active = active_builds(root, args.storage, args.ai_stack_metrics)
    runs = classify_runs(builds, lock, active, deployed, rollback, now, args.keep_sglang) if inputs["builds"] else []
    if not complete:
        for r in runs:
            if r["decision"] == "candidate":
                r["decision"], r["reason"] = "keep", "inventory incomplete; nothing is proposed"
    sizes = du_bytes([r["path"] for r in runs] + [root / name for name in ("ccache", "src", "trivy-cache")
                                                   if (root / name).is_dir()])
    # Separate du call: GNU du counts a directory once across its arguments.
    sizes.update(du_bytes([Path(r["path"]) / "build" for r in runs if r["stage"] == "rocm"
                           and (Path(r["path"]) / "build").is_dir()]))
    for r in runs:
        r["bytes"] = sizes.get(r["path"], 0)
    image_rows, image_totals = classify_images(images, layers, lock, deployed, rollback, runs, complete)
    caches = {name.replace("-", "_"): sizes.get(str(root / name), 0) for name in ("ccache", "src", "trivy-cache")}
    caches["rocm_build_tree"] = sum(v for k, v in sizes.items() if k.endswith("/build") and "/rocm-" in k)
    report = {
        "schema": 1, "run": now, "deletion_enabled": False, "inputs": inputs, "active_builds": sorted(active),
        "deployed_digests": sorted(deployed), "rollback_digests": sorted(rollback),
        "images": [{k: v for k, v in row.items() if k != "chain"} for row in image_rows],
        "image_totals": image_totals, "runs": runs, "preserved_caches": caches,
    }
    write_atomic(state / "retention.json", json.dumps(report, indent=2) + "\n", 0o644)
    m = Metrics()
    m.add("workstation_retention_last_run_timestamp_seconds", now, help_text="Last retention inventory")
    m.add("workstation_retention_deletion_enabled", 0, help_text="Always 0: the report never deletes")
    for name, value in sorted(inputs.items()):
        m.add("workstation_retention_input_complete", value, {"input": name},
              "1 when this input was read; any 0 suppresses every candidate")
    m.add("workstation_retention_active_builds", len(active), help_text="AI-stack builds currently running")
    for decision in ("keep", "candidate", "review"):
        m.add("workstation_retention_build_bytes", sum(r["bytes"] for r in runs if r["decision"] == decision),
              {"decision": decision}, "AI-stack run directories by proposed decision")
        m.add("workstation_retention_build_runs", sum(1 for r in runs if r["decision"] == decision),
              {"decision": decision}, "AI-stack run directories by proposed decision")
        m.add("workstation_retention_images", sum(1 for r in image_rows if r["decision"] == decision),
              {"decision": decision}, "Named Podman images by proposed decision")
    m.add("workstation_retention_image_bytes", image_totals["total_bytes"], {"decision": "all"},
          "Podman layer bytes (shared layers counted once)")
    m.add("workstation_retention_image_bytes", image_totals["candidate_bytes"], {"decision": "candidate"},
          "Podman layer bytes (shared layers counted once)")
    m.add("workstation_retention_image_bytes", image_totals["build_cache_only_bytes"], {"decision": "build_cache_only"},
          "Podman layer bytes (shared layers counted once)")
    m.add("workstation_retention_image_bytes", image_totals["review_bytes"], {"decision": "review"},
          "Podman layer bytes (shared layers counted once)")
    for name, value in sorted(caches.items()):
        m.add("workstation_retention_preserved_bytes", value, {"cache": name},
              "Compiler, source and scanner caches that this policy always preserves")
    write_atomic(state / "retention.prom", m.text(), 0o644)
    print(f"retention report: {sum(1 for r in runs if r['decision'] == 'candidate')} run and "
          f"{sum(1 for r in image_rows if r['decision'] == 'candidate')} image candidates; "
          f"inputs complete={complete}; deletion disabled")
    return 0 if complete else 1


# --------------------------------------------------------------------------- job-result


def unit_usage(unit):
    output = run(["systemctl", "show", unit, "-p", "CPUUsageNSec", "-p", "MemoryPeak",
                  "-p", "IOReadBytes", "-p", "IOWriteBytes", "-p", "ExecMainStartTimestampMonotonic"],
                 check=False).stdout
    usage = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if value.isdigit() and int(value) < 2**63:
            usage[key] = int(value)
    return usage


def job_result(args):
    job = args.job
    if not re.match(r"^[a-z][a-z0-9-]{0,48}$", job):
        raise Failure("invalid job name")
    result = os.environ.get("SERVICE_RESULT", "unknown")
    success = result == "success"
    published_ok = True
    for item in args.publish:
        collector, _, source = item.partition("=")
        try:
            publish(collector, source)
        except (Failure, OSError) as error:
            published_ok = False
            print(f"WARNING: {collector} metrics not published: {error}", file=sys.stderr)
    usage = unit_usage(args.unit) if args.unit else {}
    now = int(time.time())
    JOB_STATE.parent.mkdir(mode=0o755, exist_ok=True)
    with open(JOB_STATE.parent / ".jobs.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        jobs = json.loads(JOB_STATE.read_text()) if JOB_STATE.exists() else {}
        record = jobs.get(job, {})
        record.update({"last_run": now, "last_result": success and published_ok, "service_result": result})
        if success:
            record["last_success"] = now
        for key, name in (("CPUUsageNSec", "cpu_ns"), ("MemoryPeak", "memory_peak"),
                          ("IOReadBytes", "io_read"), ("IOWriteBytes", "io_write")):
            if key in usage:
                record[name] = usage[key]
        jobs[job] = record
        write_atomic(JOB_STATE, json.dumps(jobs, indent=2, sort_keys=True) + "\n", 0o644)
        m = Metrics()
        for name, record in sorted(jobs.items()):
            # "task", not "job": the scrape's own job label must stay "node".
            labels = {"task": name}
            m.add("workstation_maintenance_job_last_run_timestamp_seconds", record["last_run"], labels,
                  "Last completion of a maintenance job")
            m.add("workstation_maintenance_job_last_success_timestamp_seconds", record.get("last_success", 0), labels,
                  "Last successful completion; 0 means never")
            m.add("workstation_maintenance_job_last_result", record["last_result"], labels,
                  "1 when the last run succeeded and its metrics were published")
            if "cpu_ns" in record:
                m.add("workstation_maintenance_job_cpu_seconds", record["cpu_ns"] / 1e9, labels,
                      "CPU time of the last run")
            for key, metric in (("memory_peak", "memory_peak_bytes"), ("io_read", "io_read_bytes"),
                                ("io_write", "io_write_bytes")):
                if key in record:
                    m.add(f"workstation_maintenance_job_{metric}", record[key], labels, f"Last run {metric}")
        if TEXTFILE_DIR.is_dir() and not TEXTFILE_DIR.is_symlink():
            write_atomic(TEXTFILE_DIR / "maintenance-jobs.prom", m.text())
    log_event({"schema": 1, "job": job, "result": "success" if success and published_ok else "failure",
               "service_result": result if re.match(r"^[a-z-]{1,32}$", result) else "unknown"})
    return 0


# --------------------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("advisories")
    p.add_argument("--state", required=True)
    p.add_argument("--sync-dir", default="/var/lib/pacman/sync")
    p.add_argument("--tracker", help="use a local tracker file (tests); default downloads it")
    p = commands.add_parser("health")
    p.add_argument("--state", required=True)
    p.add_argument("--device", action="append", default=[], help="/dev/disk/by-id path of an NVMe namespace")
    p.add_argument("--ras-db", default="/var/lib/rasdaemon/ras-mc_event.db")
    p.add_argument("--unit", action="append", default=[])
    p = commands.add_parser("retention")
    p.add_argument("--state", required=True)
    p.add_argument("--ai-stack-root", required=True)
    p.add_argument("--pipeline", required=True, help="the pipeline checkout whose stack.lock is current")
    p.add_argument("--storage", required=True, help="rootless containers/storage graph root")
    p.add_argument("--ai-stack-metrics", default="/var/lib/workstation-ai-stack")
    p.add_argument("--kubectl", default="kubectl")
    p.add_argument("--keep-sglang", type=int, default=2)
    p = commands.add_parser("job-result")
    p.add_argument("job")
    p.add_argument("--unit")
    p.add_argument("--publish", action="append", default=[], help="COLLECTOR=PATH")
    args = parser.parse_args(argv)
    handler = {"advisories": advisories, "health": health, "retention": retention,
               "job-result": job_result}[args.command]
    try:
        return handler(args)
    except Failure as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
