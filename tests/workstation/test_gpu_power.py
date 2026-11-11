import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import gpu_power  # noqa: E402

POLICY = ROOT / "infrastructure/gpu-power/policy.json"
PROFILES = ["BOOTUP_DEFAULT", "3D_FULL_SCREEN", "POWER_SAVING", "VIDEO", "VR", "COMPUTE", "CUSTOM"]


def profile_table(selected):
    return "".join(f" {i} {name}{'*' if name == selected else ''}:\n" for i, name in enumerate(PROFILES))


class FakeSysfs:
    """Two R9700s in a temporary tree, with amdgpu's write semantics for the files the helper uses."""

    def __init__(self, root, level="high", profile="COMPUTE", cap_watts=300, vendor="0x1002"):
        self.root = Path(root)
        self.writes = []
        for address in ("0000:23:00.0", "0000:c3:00.0"):
            device = self.root / "sys/bus/pci/devices" / address
            hwmon = device / "hwmon/hwmon3"
            hwmon.mkdir(parents=True)
            (device / "vendor").write_text(vendor + "\n")
            (device / "power_dpm_force_performance_level").write_text(level + "\n")
            (device / "pp_power_profile_mode").write_text(profile_table(profile))
            (hwmon / "power1_cap").write_text(f"{cap_watts * 1_000_000}\n")
            (hwmon / "power1_cap_min").write_text("210000000\n")
            (hwmon / "power1_cap_max").write_text("300000000\n")

    def write(self, path, value):
        path = Path(path)
        self.writes.append((path.parent.name, path.name, str(value)))
        if path.name == "pp_power_profile_mode":
            level = (path.parent / "power_dpm_force_performance_level").read_text().strip()
            if level != "manual":
                raise OSError("EINVAL: profile requires the manual level")
            path.write_text(profile_table(PROFILES[int(value)]))
        else:
            path.write_text(f"{value}\n")


def run(args, fake):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gpu_power.main(args, write=fake.write, wait=0)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


class GpuPowerTests(unittest.TestCase):
    def test_policy_is_the_measured_setting(self):
        policy = gpu_power.load_policy(POLICY)
        self.assertEqual(policy["cards"], ["0000:23:00.0", "0000:c3:00.0"])
        self.assertEqual((policy["performance_level"], policy["power_profile"], policy["power_cap_watts"]),
                         ("auto", "BOOTUP_DEFAULT", 235))

    def test_apply_moves_both_cards_from_pinned_clocks_to_the_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSysfs(tmp, level="high", profile="COMPUTE", cap_watts=300)
            code, states = run(["apply", str(POLICY), "--sysfs-root", tmp], fake)
            self.assertEqual(code, 0)
            self.assertEqual([s["card"] for s in states], ["0000:23:00.0", "0000:c3:00.0"])
            for state in states:
                self.assertEqual((state["performance_level"], state["power_profile"], state["power_cap_watts"], state["ok"]),
                                 ("auto", "BOOTUP_DEFAULT", 235, True))
            # per card: cap, manual (so the profile write is accepted), profile index, then the level
            per_card = [w[1:] for w in fake.writes[:4]]
            self.assertEqual(per_card, [("power1_cap", "235000000"), ("power_dpm_force_performance_level", "manual"),
                                        ("pp_power_profile_mode", "0"), ("power_dpm_force_performance_level", "auto")])

    def test_check_reports_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeSysfs(tmp, level="high", profile="BOOTUP_DEFAULT", cap_watts=235)
            code, states = run(["check", str(POLICY), "--sysfs-root", tmp], fake)
            self.assertEqual(code, 1)
            self.assertEqual(fake.writes, [])
            self.assertEqual([s["ok"] for s in states], [False, False])
            fake2 = FakeSysfs(Path(tmp) / "good", level="auto", profile="BOOTUP_DEFAULT", cap_watts=235)
            code, states = run(["check", str(POLICY), "--sysfs-root", str(Path(tmp) / "good")], fake2)
            self.assertEqual((code, [s["ok"] for s in states]), (0, [True, True]))

    def test_apply_refuses_a_cap_outside_the_card_range_and_non_amd_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            data = json.loads(POLICY.read_text())
            policy.write_text(json.dumps({**data, "power_cap_watts": 350}))
            fake = FakeSysfs(Path(tmp) / "a")
            with self.assertRaisesRegex(RuntimeError, "outside power1_cap_max"):
                run(["apply", str(policy), "--sysfs-root", str(Path(tmp) / "a")], fake)
            self.assertEqual(fake.writes, [])
            fake = FakeSysfs(Path(tmp) / "b", vendor="0x10de")
            with self.assertRaisesRegex(RuntimeError, "not an AMD GPU"):
                run(["apply", str(POLICY), "--sysfs-root", str(Path(tmp) / "b")], fake)

    def test_policy_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            base = json.loads(POLICY.read_text())
            for bad in ({"cards": ["gpu-0"]}, {"performance_level": "turbo"}, {"power_cap_watts": "235"}, {"power_profile": "compute"}):
                path.write_text(json.dumps({**base, **bad}))
                with self.assertRaises(ValueError, msg=bad):
                    gpu_power.load_policy(path)

    def test_unit_runs_the_packaged_helper_on_the_packaged_policy(self):
        unit = (ROOT / "templates/workstation/gpu/workstation-gpu-power.service").read_text()
        exec_start = re.search(r"^ExecStart=(.+)$", unit, re.M).group(1).split()
        self.assertEqual(exec_start, ["/usr/bin/python3", "-B",
                                      "/usr/lib/bridge/workstation-runtime/lib/workstation/gpu_power.py", "apply",
                                      "/usr/lib/bridge/workstation-runtime/infrastructure/gpu-power/policy.json"])
        self.assertIn("Type=oneshot", unit)
        self.assertIn("Before=k3s.service", unit)
        # sysfs writes need /sys writable: ProtectKernelTunables would make it read-only
        self.assertNotIn("ProtectKernelTunables", unit)
        lists = [(ROOT / "infrastructure/packages/bootstrap" / name).read_text().splitlines()
                 for name in ("bridge-runtime.files", "source.files")]
        for files in lists:
            for path in ("lib/workstation/gpu_power.py", "infrastructure/gpu-power/policy.json",
                         "templates/workstation/gpu/workstation-gpu-power.service"):
                self.assertIn(path, files)
        self.assertIn("tests/workstation/test_gpu_power.py", lists[1])
        self.assertIn("workstation-gpu-power.service", (ROOT / "infrastructure/packages/bootstrap/PKGBUILD").read_text())


if __name__ == "__main__":
    unittest.main()
