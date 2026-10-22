"""Capture and validate a bounded kernel/hardware compatibility contract."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


CONFIG_SET = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.+)$")
CONFIG_UNSET = re.compile(r"^# (CONFIG_[A-Z0-9_]+) is not set$")
HEX_FIELD = re.compile(r"^0x[0-9a-f]+$")
# Linux preserves case in module names (for example xt_NFLOG and ipt_REJECT).
# Do not lowercase evidence: these names must also match packaged inventories.
MODULE = re.compile(r"^[A-Za-z0-9_]+$")
PROFILE_NAME = re.compile(r"^[a-z0-9-]+$")
PCI_BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]$")
DRIVER = re.compile(r"^[a-z0-9_-]+$")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def load_policy(path: Path) -> dict:
    policy = load_json(path)
    if not isinstance(policy, dict) or policy.get("schema") != 1:
        raise ValueError("unsupported kernel hardware policy")
    if not isinstance(policy.get("profile"), str) or not PROFILE_NAME.fullmatch(policy["profile"]):
        raise ValueError("invalid kernel hardware policy profile")
    platform = policy.get("platform")
    if not isinstance(platform, dict) or not platform or any(
            not isinstance(name, str) or not isinstance(value, str) or not value
            for name, value in platform.items()):
        raise ValueError("invalid kernel hardware platform policy")
    cpu = policy.get("cpu")
    if not isinstance(cpu, dict) or not cpu:
        raise ValueError("invalid kernel hardware CPU policy")
    for name in ("vendor_id", "model_name"):
        if not isinstance(cpu.get(name), str) or not cpu[name]:
            raise ValueError(f"invalid kernel hardware CPU field: {name}")
    for name in ("family", "model", "stepping", "logical_cpus"):
        if not isinstance(cpu.get(name), int) or isinstance(cpu[name], bool) or cpu[name] < 0:
            raise ValueError(f"invalid kernel hardware CPU field: {name}")
    pci = policy.get("pci")
    required_config = policy.get("required_config")
    if not isinstance(pci, list) or not pci or not isinstance(required_config, dict) or not required_config:
        raise ValueError("incomplete kernel hardware policy")
    names = []
    pci_modules = set()
    for requirement in pci:
        if not isinstance(requirement, dict):
            raise ValueError("invalid PCI requirement")
        name = requirement.get("name")
        if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
            raise ValueError("invalid PCI requirement name")
        names.append(name)
        if any(not isinstance(requirement.get(field), str) or not HEX_FIELD.fullmatch(requirement[field])
               for field in ("vendor", "device", "class_prefix")):
            raise ValueError(f"invalid PCI identity requirement: {name}")
        count = requirement.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"invalid PCI count requirement: {name}")
        drivers = requirement.get("drivers")
        device_modules = requirement.get("modules")
        if (not isinstance(drivers, list) or drivers != sorted(set(drivers)) or
                any(not isinstance(item, str) or not MODULE.fullmatch(item) for item in drivers)):
            raise ValueError(f"invalid PCI driver requirement: {name}")
        if (not isinstance(device_modules, list) or len(device_modules) != len(set(device_modules)) or
                any(item is not None and (not isinstance(item, str) or not MODULE.fullmatch(item))
                    for item in device_modules)):
            raise ValueError(f"invalid PCI module requirement: {name}")
        allow_unbound = requirement.get("allow_unbound", False)
        if not isinstance(allow_unbound, bool) or (not allow_unbound and (not drivers or not device_modules)):
            raise ValueError(f"invalid PCI binding requirement: {name}")
        pci_modules.update(item for item in device_modules if item is not None)
    if len(names) != len(set(names)):
        raise ValueError("kernel hardware PCI requirements must have unique names")
    for name, accepted in required_config.items():
        if (not isinstance(name, str) or not CONFIG_SET.fullmatch(f"{name}=y") or
                not isinstance(accepted, list) or not accepted or
                accepted != sorted(set(accepted)) or any(value not in ("m", "n", "y") for value in accepted)):
            raise ValueError(f"invalid policy configuration requirement: {name}")
    modules = policy.get("required_modules")
    if not isinstance(modules, list) or not modules or modules != sorted(set(modules)):
        raise ValueError("kernel hardware policy modules must be sorted and unique")
    if any(not isinstance(item, str) or not MODULE.fullmatch(item) for item in modules):
        raise ValueError("invalid required module name")
    if not pci_modules.issubset(modules):
        raise ValueError("PCI module requirements are missing from the required module inventory")
    return policy


def parse_config(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        setting = CONFIG_SET.fullmatch(line)
        unset = CONFIG_UNSET.fullmatch(line)
        if not setting and not unset:
            continue
        name = (setting or unset).group(1)
        value = setting.group(2) if setting else "n"
        if name in result:
            raise ValueError(f"duplicate kernel configuration option: {name}")
        result[name] = value
    if not result:
        raise ValueError("kernel configuration is empty or malformed")
    return result


def check_config(policy: dict, config: dict[str, str]) -> list[str]:
    failures = []
    for name, accepted in sorted(policy["required_config"].items()):
        if not isinstance(accepted, list) or not accepted or any(value not in ("y", "m", "n") for value in accepted):
            raise ValueError(f"invalid policy configuration requirement: {name}")
        actual = config.get(name, "n")
        if actual not in accepted:
            failures.append(f"{name}={actual} (requires {'/'.join(accepted)})")
    return failures


def cpu_identity(proc_root: Path) -> dict:
    blocks = read_text(proc_root / "cpuinfo").split("\n\n")
    rows = []
    for block in blocks:
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if "processor" in fields:
            rows.append(fields)
    if not rows:
        raise ValueError("CPU inventory is unavailable")
    keys = ("vendor_id", "cpu family", "model", "stepping", "model name")
    identity = {key: {row.get(key) for row in rows} for key in keys}
    if any(len(values) != 1 or None in values for values in identity.values()):
        raise ValueError("CPU identity is incomplete or inconsistent")
    return {
        "vendor_id": next(iter(identity["vendor_id"])),
        "family": int(next(iter(identity["cpu family"]))),
        "model": int(next(iter(identity["model"]))),
        "stepping": int(next(iter(identity["stepping"]))),
        "logical_cpus": len(rows),
        "model_name": next(iter(identity["model name"])),
    }


def symlink_name(path: Path) -> str | None:
    try:
        target = os.readlink(path)
    except FileNotFoundError:
        return None
    return Path(target).name


def pci_inventory(sys_root: Path) -> list[dict]:
    result = []
    root = sys_root / "bus/pci/devices"
    for path in sorted(root.iterdir() if root.is_dir() else []):
        required = [path / name for name in ("vendor", "device", "class")]
        if not all(item.is_file() for item in required):
            continue
        fields = {name: read_text(path / name).lower() for name in ("vendor", "device", "class")}
        if any(not HEX_FIELD.fullmatch(value) for value in fields.values()):
            raise ValueError(f"invalid PCI identity at {path.name}")
        driver = symlink_name(path / "driver")
        module = symlink_name(path / "driver/module") if driver else None
        result.append({"bdf": path.name.lower(), **fields, "driver": driver, "module": module})
    if not result:
        raise ValueError("PCI inventory is unavailable")
    return result


def loaded_modules(proc_root: Path) -> list[str]:
    result = []
    for line in read_text(proc_root / "modules").splitlines():
        name = line.split(None, 1)[0].replace("-", "_")
        if not MODULE.fullmatch(name):
            raise ValueError("invalid loaded module inventory")
        result.append(name)
    return sorted(set(result))


def secure_regular(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"profile input is not a regular file: {path.name}")
    if details.st_uid not in (0, os.getuid()) or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"profile input ownership or mode is unsafe: {path.name}")


def validate_profile(policy_path: Path, profile_dir: Path) -> dict:
    if profile_dir.is_symlink() or not profile_dir.is_dir():
        raise ValueError("kernel profile directory is missing or is a symlink")
    details = profile_dir.stat()
    if details.st_uid not in (0, os.getuid()) or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("kernel profile directory ownership or mode is unsafe")
    profile_path = profile_dir / "profile.json"
    config_path = profile_dir / "current.config"
    manifest_path = profile_dir / "MANIFEST.json"
    for path in (profile_path, config_path, manifest_path):
        secure_regular(path)
    manifest = load_json(manifest_path)
    expected_manifest = {"current.config": file_digest(config_path), "profile.json": file_digest(profile_path)}
    if manifest != expected_manifest:
        raise ValueError("kernel profile manifest does not match its files")
    policy = load_policy(policy_path)
    profile = load_json(profile_path)
    if not isinstance(profile, dict) or profile.get("schema") != 1 or profile.get("status") != "observed":
        raise ValueError("an observed kernel profile is required")
    if profile.get("policy_sha256") != file_digest(policy_path):
        raise ValueError("kernel profile policy identity changed")
    failures = profile.get("failures")
    if failures != [] or "error_class" in profile:
        raise ValueError("kernel profile contains failed observations")
    platform = profile.get("platform")
    cpu = profile.get("cpu")
    devices = profile.get("pci")
    if not isinstance(platform, dict) or not isinstance(cpu, dict) or not isinstance(devices, list):
        raise ValueError("kernel profile hardware identity is incomplete")
    bdfs = []
    for device in devices:
        if not isinstance(device, dict) or not isinstance(device.get("bdf"), str) or not PCI_BDF.fullmatch(device["bdf"]):
            raise ValueError("kernel profile contains an invalid PCI address")
        bdfs.append(device["bdf"])
        if any(not isinstance(device.get(field), str) or not HEX_FIELD.fullmatch(device[field])
               for field in ("vendor", "device", "class")):
            raise ValueError("kernel profile contains an invalid PCI identity")
        for field in ("driver", "module"):
            value = device.get(field)
            if value is not None and (not isinstance(value, str) or not DRIVER.fullmatch(value)):
                raise ValueError(f"kernel profile contains an invalid PCI {field}")
    if len(bdfs) != len(set(bdfs)):
        raise ValueError("kernel profile contains duplicate PCI addresses")
    identity_failures, device_modules = check_identity(policy, platform, cpu, devices)
    if identity_failures:
        raise ValueError("kernel profile hardware identity does not satisfy policy")
    loaded = profile.get("loaded_modules")
    if (not isinstance(loaded, list) or loaded != sorted(set(loaded)) or
            any(not isinstance(item, str) or not MODULE.fullmatch(item) for item in loaded)):
        raise ValueError("kernel profile loaded-module evidence is invalid")
    expected_modules = sorted(set(policy["required_modules"]) | set(device_modules) | set(loaded))
    if profile.get("required_modules") != expected_modules:
        raise ValueError("kernel profile required-module set is incomplete or changed")
    current_config = profile.get("current_config")
    if (not isinstance(current_config, dict) or current_config.get("sha256") != file_digest(config_path) or
            not isinstance(current_config.get("source"), str) or not current_config["source"]):
        raise ValueError("kernel profile current-configuration identity changed")
    config = parse_config(config_path.read_text(encoding="utf-8"))
    candidate_config_failures = check_config(policy, config)
    if profile.get("candidate_config_failures") != candidate_config_failures:
        raise ValueError("kernel profile candidate-configuration gap evidence changed")
    selected = {name: config.get(name, "n") for name in sorted(policy["required_config"])}
    if current_config.get("required") != selected:
        raise ValueError("kernel profile selected configuration evidence changed")
    if not isinstance(profile.get("kernel_release"), str) or not profile["kernel_release"]:
        raise ValueError("kernel profile release evidence is unavailable")
    return profile


def kernel_config(proc_root: Path, boot_root: Path, modules_root: Path, release: str) -> tuple[bytes, str]:
    proc_config = proc_root / "config.gz"
    if proc_config.is_file():
        with gzip.open(proc_config, "rb") as stream:
            return stream.read(), "/proc/config.gz"
    candidates = [modules_root / release / "build/.config", boot_root / f"config-{release}"]
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            return path.read_bytes(), str(path)
    raise ValueError("running kernel configuration is unavailable")


def platform_identity(sys_root: Path) -> dict:
    root = sys_root / "class/dmi/id"
    names = ("board_vendor", "board_name", "board_version", "bios_version")
    result = {name: read_text(root / name) for name in names}
    if any(not value for value in result.values()):
        raise ValueError("platform identity is unavailable")
    return result


def check_identity(policy: dict, platform: dict, cpu: dict, devices: list[dict]) -> tuple[list[str], list[str]]:
    failures = []
    matched_modules = []
    for name, expected in policy["platform"].items():
        if platform.get(name) != expected:
            failures.append(f"platform {name} mismatch")
    for name, expected in policy["cpu"].items():
        if cpu.get(name) != expected:
            failures.append(f"CPU {name} mismatch")
    for requirement in policy["pci"]:
        matches = [item for item in devices if item["vendor"] == requirement["vendor"]
                   and item["device"] == requirement["device"]
                   and item["class"].startswith(requirement["class_prefix"])]
        if len(matches) != requirement["count"]:
            failures.append(f"PCI {requirement['name']} count {len(matches)} (requires {requirement['count']})")
            continue
        for item in matches:
            if requirement.get("allow_unbound") and item["driver"] is None and item["module"] is None:
                continue
            if item["driver"] not in requirement["drivers"] or item["module"] not in requirement["modules"]:
                failures.append(f"PCI {requirement['name']} at {item['bdf']} has unexpected driver/module")
            elif item["module"]:
                matched_modules.append(item["module"].replace("-", "_"))
    return failures, matched_modules


def capture(policy_path: Path, output: Path, *, sys_root: Path = Path("/sys"),
            proc_root: Path = Path("/proc"), boot_root: Path = Path("/boot"),
            modules_root: Path = Path("/usr/lib/modules"), release: str | None = None) -> dict:
    os.umask(0o077)
    if output.exists() or output.is_symlink():
        raise ValueError("kernel profile output already exists")
    output.mkdir(mode=0o700)
    policy = load_policy(policy_path)
    report = {"schema": 1, "status": "failed", "policy_sha256": file_digest(policy_path),
              "scope": "read-only hardware/config compatibility; not performance or kernel qualification"}
    try:
        release = release or os.uname().release
        platform = platform_identity(sys_root)
        cpu = cpu_identity(proc_root)
        devices = pci_inventory(sys_root)
        modules = loaded_modules(proc_root)
        raw_config, config_source = kernel_config(proc_root, boot_root, modules_root, release)
        if len(raw_config) > 8 * 1024 * 1024:
            raise ValueError("running kernel configuration exceeds the 8 MiB profile limit")
        config_text = raw_config.decode("utf-8", errors="strict")
        parsed = parse_config(config_text)
        identity_failures, device_modules = check_identity(policy, platform, cpu, devices)
        candidate_config_failures = check_config(policy, parsed)
        # Preserve observed modules in addition to the recovery/workload policy.
        # This adds closure; it never disables unloaded drivers like localmodconfig.
        required_modules = sorted(set(policy["required_modules"]) | set(device_modules) | set(modules))
        (output / "current.config").write_text(config_text, encoding="utf-8")
        report.update({"kernel_release": release, "platform": platform, "cpu": cpu, "pci": devices,
                       "loaded_modules": modules, "required_modules": required_modules,
                       "current_config": {"source": config_source, "sha256": digest(raw_config),
                                          "required": {name: parsed.get(name, "n") for name in sorted(policy["required_config"])}},
                       "candidate_config_failures": candidate_config_failures,
                       "failures": identity_failures})
        if not report["failures"]:
            report["status"] = "observed"
    except Exception as error:
        report["error_class"] = type(error).__name__
        report["failures"] = [str(error)]
    write_json(output / "profile.json", report)
    write_json(output / "MANIFEST.json", {name: file_digest(output / name) for name in sorted(("current.config", "profile.json"))
                                           if (output / name).is_file()})
    return report


def module_inventory(path: Path) -> set[str]:
    modules = set()
    for line in read_text(path).splitlines():
        name = line.strip().replace("-", "_")
        if not MODULE.fullmatch(name):
            raise ValueError("invalid built module inventory")
        if name in modules:
            raise ValueError(f"duplicate built module inventory entry: {name}")
        modules.add(name)
    if not modules:
        raise ValueError("built module inventory is empty")
    return modules


def validate_build(policy_path: Path, profile_dir: Path, config_path: Path,
                   modules_path: Path, output: Path) -> dict:
    policy = load_policy(policy_path)
    profile = validate_profile(policy_path, profile_dir)
    profile_path = profile_dir / "profile.json"
    expected_modules = profile["required_modules"]
    config = parse_config(config_path.read_text(encoding="utf-8"))
    config_failures = check_config(policy, config)
    built_modules = module_inventory(modules_path)
    missing_modules = sorted(set(expected_modules) - built_modules)
    result = {"schema": 1, "status": "pass" if not config_failures and not missing_modules else "failed",
              "policy_sha256": file_digest(policy_path), "profile_sha256": file_digest(profile_path),
              "effective_config_sha256": file_digest(config_path), "module_inventory_sha256": file_digest(modules_path),
              "required_modules": expected_modules, "config_failures": config_failures,
              "missing_modules": missing_modules,
              "scope": "built config/module closure against observed hardware contract; boot and workload qualification remain required"}
    write_json(output, result)
    if result["status"] != "pass":
        raise ValueError("built kernel does not satisfy the observed hardware contract")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("capture")
    collect.add_argument("policy", type=Path)
    collect.add_argument("output", type=Path)
    validate = subparsers.add_parser("validate-build")
    validate.add_argument("policy", type=Path)
    validate.add_argument("profile_directory", type=Path)
    validate.add_argument("config", type=Path)
    validate.add_argument("modules", type=Path)
    validate.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            return 0 if capture(args.policy, args.output)["status"] == "observed" else 1
        validate_build(args.policy, args.profile_directory, args.config, args.modules, args.output)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"kernel-profile: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
