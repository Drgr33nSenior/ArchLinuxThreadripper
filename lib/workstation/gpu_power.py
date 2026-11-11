#!/usr/bin/env python3
"""Hold the Radeon GPUs to the measured power policy (workstation-gpu-power.service).

The policy file (infrastructure/gpu-power/policy.json) names each card by PCI address and
sets one performance level, power profile and power cap for all of them. ``apply`` writes
the cap, selects the profile (amdgpu only accepts a profile in the ``manual`` level), then
sets the level, reads everything back and exits 1 if any card differs. ``check`` only reads
and exits 1 on any difference, for example after a manual ``rocm-smi --setperflevel``.

Both cards must match: SGLang splits the model across them (TP2), so every step waits for
the slower card. A GPU reset returns a card to the driver defaults; run ``apply`` again.

Usage: gpu_power.py {apply,check} POLICY [--sysfs-root DIR]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

AMD_VENDOR = "0x1002"
LEVELS = ("auto", "low", "high", "manual", "profile_standard", "profile_min_sclk", "profile_min_mclk", "profile_peak")
PROFILE_LINE = re.compile(r"^\s*(\d+)\s+([A-Z0-9_]+)\s*(\*)?")
WAIT_SECONDS = 60


def load_policy(path):
    policy = json.loads(Path(path).read_text())
    cards = policy.get("cards")
    if not isinstance(cards, list) or not cards or not all(
            isinstance(c, str) and re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", c) for c in cards):
        raise ValueError("policy cards must be PCI addresses like 0000:23:00.0")
    if policy.get("performance_level") not in LEVELS:
        raise ValueError(f"policy performance_level must be one of {', '.join(LEVELS)}")
    if not re.fullmatch(r"[A-Z0-9_]+", str(policy.get("power_profile", ""))):
        raise ValueError("policy power_profile must be a profile name such as BOOTUP_DEFAULT")
    watts = policy.get("power_cap_watts")
    if not isinstance(watts, int) or isinstance(watts, bool) or watts <= 0:
        raise ValueError("policy power_cap_watts must be a positive integer")
    return policy


class Card:
    def __init__(self, root, address):
        self.address = address
        self.device = Path(root) / "sys/bus/pci/devices" / address

    def hwmon(self):
        found = sorted(self.device.glob("hwmon/hwmon*/power1_cap"))
        if len(found) != 1:
            raise RuntimeError(f"{self.address}: expected one hwmon power1_cap, found {len(found)}")
        return found[0].parent

    def ready(self):
        return (self.device / "power_dpm_force_performance_level").exists() and bool(
            list(self.device.glob("hwmon/hwmon*/power1_cap")))

    def read(self, name):
        return (self.device / name).read_text().strip()

    def profiles(self):
        """{name: index} and the selected profile name."""
        table, selected = {}, None
        for line in self.read("pp_power_profile_mode").splitlines():
            m = PROFILE_LINE.match(line)
            if m:
                table[m.group(2)] = int(m.group(1))
                if m.group(3):
                    selected = m.group(2)
        return table, selected

    def state(self):
        hw = self.hwmon()
        return {"card": self.address,
                "performance_level": self.read("power_dpm_force_performance_level"),
                "power_profile": self.profiles()[1],
                "power_cap_watts": int((hw / "power1_cap").read_text()) // 1_000_000}


def matches(state, policy):
    return (state["performance_level"] == policy["performance_level"]
            and state["power_profile"] == policy["power_profile"]
            and state["power_cap_watts"] == policy["power_cap_watts"])


def sysfs_write(path, value):
    Path(path).write_text(f"{value}\n")


def apply_card(card, policy, write=sysfs_write):
    if card.read("vendor") != AMD_VENDOR:
        raise RuntimeError(f"{card.address}: not an AMD GPU (vendor {card.read('vendor')})")
    hw = card.hwmon()
    cap = policy["power_cap_watts"] * 1_000_000
    for bound, ok in (("power1_cap_min", lambda b: cap >= b), ("power1_cap_max", lambda b: cap <= b)):
        if (hw / bound).exists() and not ok(int((hw / bound).read_text())):
            raise RuntimeError(f"{card.address}: {policy['power_cap_watts']} W is outside {bound}")
    table, _ = card.profiles()
    if policy["power_profile"] not in table:
        raise RuntimeError(f"{card.address}: no power profile {policy['power_profile']} ({', '.join(table)})")
    write(hw / "power1_cap", cap)
    write(card.device / "power_dpm_force_performance_level", "manual")
    write(card.device / "pp_power_profile_mode", table[policy["power_profile"]])
    if policy["performance_level"] != "manual":
        write(card.device / "power_dpm_force_performance_level", policy["performance_level"])


def main(argv=None, write=sysfs_write, wait=WAIT_SECONDS):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("apply", "check"))
    parser.add_argument("policy")
    parser.add_argument("--sysfs-root", default="/", help="for tests: a directory standing in for /")
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    cards = [Card(args.sysfs_root, address) for address in policy["cards"]]
    if args.action == "apply":
        # amdgpu binds after udev; give the cards a moment to appear at boot
        deadline = time.monotonic() + wait
        while not all(card.ready() for card in cards) and time.monotonic() < deadline:
            time.sleep(1)
        for card in cards:
            if not card.ready():
                raise SystemExit(f"{card.address}: GPU power files not present after {wait} s")
            apply_card(card, policy, write)
    failed = False
    for card in cards:
        state = card.state()
        state["ok"] = matches(state, policy)
        failed |= not state["ok"]
        print(json.dumps(state), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
