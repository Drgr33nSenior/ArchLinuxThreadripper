"""Post-install orchestration with an injected runner; no command, host or cluster is touched."""
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import postinstall

USER = os.geteuid()
BOOT_A = "11111111-2222-3333-4444-555555555555"
BOOT_B = "66666666-7777-8888-9999-aaaaaaaaaaaa"
GATED = ("ansible-playbook", "kubectl", "pacman", "makepkg", "systemctl", "credentials", "credential",
         "--execute", "dev", "provision", "switch")


class FakeRunner:
    """Records argv and creates each new output directory the real command would create."""

    def __init__(self, state):
        self.state, self.calls, self.fail = state, [], set()

    def __call__(self, argv, cwd, stdout, stderr):
        self.calls.append(argv)
        for part in argv:
            if part.startswith(str(self.state / "outputs")) and not os.path.lexists(part):
                Path(part).mkdir(mode=0o700)
                (Path(part) / "result.json").write_text(json.dumps({"argv": argv}))
        stdout.write(b"synthetic output\n")
        return 1 if any(name in argv for name in self.fail) else 0

    def names(self):
        return [" ".join(call[1:4]) for call in self.calls]


class PostinstallTests(unittest.TestCase):
    def setUp(self):
        if USER == 0:
            self.skipTest("run as an unprivileged user; root is simulated")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.state = self.base / "state"
        self.runner = FakeRunner(self.state)
        self.config = self.base / "install.conf"
        self.config.write_text("HOSTNAME=fixture\n")
        self.site = self.base / "site.json"
        self.site.write_text("{}\n")
        self.chart = self.base / "chart.tgz"
        self.chart.write_bytes(b"chart")

    def make(self, euid=0, boot=BOOT_A, **inputs):
        defaults = {"install_config": self.config, "service_site": self.site, "gpu_chart": self.chart}
        defaults.update(inputs)
        self.out = io.StringIO()
        return postinstall.Postinstall(self.state, {k: str(v) if v else None for k, v in defaults.items()},
                                       runner=self.runner, euid=euid, boot_id=boot, out=self.out)

    def run_as(self, euid, attest=None, **kwargs):
        return self.make(euid=euid, **kwargs).run(attest)

    def record(self, step):
        return json.loads((self.state / "records" / f"{step}.json").read_text())

    def advance_to_gate(self, gate):
        """Attest every gate before `gate`, alternating root and user runs as the guidance requests."""
        for _ in range(40):
            status = self.run_as(0)
            if status == postinstall.STOP_PRIVILEGE:
                status = self.run_as(USER)
            if status == postinstall.STOP_GATE:
                pending = next(s for s in postinstall.steps() if s.gate and
                               not (self.state / "records" / f"{s.ident}.json").exists())
                if pending.ident == gate:
                    return
                self.assertIn(self.run_as(USER, pending.ident),
                              (postinstall.STOP_PRIVILEGE, postinstall.STOP_GATE, 0))
            elif status != postinstall.STOP_PRIVILEGE:
                self.fail(f"unexpected status {status}: {self.out.getvalue()}")
        self.fail("gate not reached")

    def test_plan_executes_and_creates_nothing(self):
        orchestrator = self.make(euid=USER)
        self.assertEqual(orchestrator.plan(), 0)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.runner.calls, [])
        text = self.out.getvalue()
        self.assertIn("owner-gate", text)
        self.assertIn("blocked-by-gate", text)
        self.assertIn("--owner-completed secure-boot-unlock", text)
        order = [s.ident for s in orchestrator.steps]
        self.assertLess(order.index("rocm-validate"), order.index("k3s-host"))
        self.assertLess(order.index("hardware-collect"), order.index("k3s-host"))

    def test_first_run_stops_at_gate_with_private_state(self):
        self.assertEqual(self.run_as(USER), postinstall.STOP_GATE)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ("records", "logs", "outputs"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o700)
        self.assertEqual(self.runner.calls, [])
        self.assertIn("STOP: owner gate 'secure-boot-unlock'", self.out.getvalue())

    def test_refuses_symlinks_and_loose_modes(self):
        target = self.base / "target"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_as(USER)
        self.state.unlink()
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            self.run_as(os.geteuid())
        self.state.chmod(0o700)
        (self.state / "records").mkdir(mode=0o700)
        (self.state / "records" / "status.json").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.make(euid=os.geteuid()).plan()

    def test_owner_attestation_must_be_a_gate_and_is_not_execution(self):
        with self.assertRaisesRegex(ValueError, "owner gate"):
            self.run_as(USER, "status")
        self.assertEqual(self.run_as(USER, "secure-boot-unlock"), postinstall.STOP_PRIVILEGE)
        record = self.record("secure-boot-unlock")
        self.assertEqual(record["kind"], "owner-attestation")
        self.assertEqual(self.runner.calls, [])
        self.assertIn("sudo ", self.out.getvalue())
        self.assertIn("must run as root", self.out.getvalue())

    def test_missing_input_blocks_without_running(self):
        self.run_as(USER, "secure-boot-unlock")
        self.assertEqual(self.run_as(0, install_config=None), postinstall.STOP_INPUT)
        self.assertIn("--install-config FILE", self.out.getvalue())
        self.assertEqual(self.runner.calls, [])

    def test_records_resume_and_boot_binding(self):
        self.run_as(USER, "secure-boot-unlock")
        self.assertEqual(self.run_as(0), postinstall.STOP_GATE)
        self.assertIn("STOP: owner gate 'packages'", self.out.getvalue())
        self.assertIn("dev setup server", self.out.getvalue())
        self.assertEqual(self.runner.names(), ["--config {} verify".format(self.config), "status", "swap validate"])
        record = self.record("bootstrap-verify")
        for key in ("step", "argv", "input_sha256", "boot_id", "exit_status", "started_utc", "finished_utc",
                    "log_sha256"):
            self.assertIn(key, record)
        self.assertEqual((record["boot_id"], record["exit_status"]), (BOOT_A, 0))
        self.assertEqual(record["argv"][-1], "verify")
        self.assertEqual(stat.S_IMODE(os.stat(self.state / "records/bootstrap-verify.json").st_mode), 0o600)
        # Same boot and inputs: nothing reruns.
        self.runner.calls.clear()
        self.assertEqual(self.run_as(0), postinstall.STOP_GATE)
        self.assertEqual(self.runner.calls, [])
        # A new boot reruns only boot-bound checks; the attested gate stays.
        self.assertEqual(self.run_as(0, boot=BOOT_B), postinstall.STOP_GATE)
        self.assertEqual(len(self.runner.calls), 3)
        # Changed reviewed configuration reruns the dependent verification only.
        self.runner.calls.clear()
        self.config.write_text("HOSTNAME=changed\n")
        self.assertEqual(self.run_as(0, boot=BOOT_B), postinstall.STOP_GATE)
        self.assertEqual(self.runner.names(), ["--config {} verify".format(self.config)])

    def test_failure_stops_and_is_retried(self):
        self.run_as(USER, "secure-boot-unlock")
        self.runner.fail.add("status")
        self.assertEqual(self.run_as(0), postinstall.STOP_FAILED)
        self.assertEqual(self.record("status")["exit_status"], 1)
        self.assertNotIn("swap validate", self.runner.names())
        self.assertIn("FAILED: 'status'", self.out.getvalue())
        self.runner.fail.clear()
        self.runner.calls.clear()
        self.assertEqual(self.run_as(0), postinstall.STOP_GATE)
        self.assertEqual(self.runner.names(), ["status", "swap validate"])

    def test_full_chain_never_executes_owner_actions(self):
        self.advance_to_gate("service-rollout")
        executed = [" ".join(call) for call in self.runner.calls]
        for call in self.runner.calls:
            for word in GATED:
                self.assertNotIn(word, call, call)
        records = {s.ident: self.record(s.ident) for s in postinstall.steps()
                   if (self.state / "records" / f"{s.ident}.json").exists()}
        self.assertNotIn("service-rollout", records)
        hardware = records["hardware-collect"]["output"]
        self.assertIn(f"{hardware}/hardware.json", records["resources-plan"]["argv"])
        self.assertEqual(records["hardware-collect"]["euid"], 0)
        self.assertEqual(records["rocm-validate"]["euid"], USER)
        self.assertEqual(records["telemetry-plan"]["euid"], 0)
        self.assertIn("--experience", records["service-render"]["argv"])
        self.assertIn("assistant", records["service-render"]["argv"])
        self.assertEqual(records["service-plan"]["argv"][-4:-1],
                         [f"{records['resources-plan']['output']}/resource-plan.json",
                          records["service-render"]["output"], records["telemetry-render"]["output"]])
        self.assertTrue((Path(records["gpu-operator-render"]["output"]) / "rendered.yaml").read_bytes())
        for record in records.values():
            self.assertNotIn("password", json.dumps(record).lower())
        self.assertTrue(any("rag verify-models" in call for call in executed))
        text = self.out.getvalue()
        self.assertIn("STOP: owner gate 'service-rollout'", text)

    def test_k3s_gate_prints_current_boot_hardware_report(self):
        self.advance_to_gate("k3s-host")
        hardware = self.record("hardware-collect")["output"]
        text = self.out.getvalue()
        self.assertIn(f"lab_hardware_report={hardware}/hardware.json", text)
        self.assertIn("lab_manage_service=false", text)
        self.assertIn("CHOSEN_LAB_ALIAS", text)
        self.assertIn("ansible_connection=local", text)
        # A reboot re-collects hardware before the gate is shown again.
        self.runner.calls.clear()
        self.assertEqual(self.run_as(0, boot=BOOT_B), postinstall.STOP_PRIVILEGE)
        self.assertNotEqual(self.record("hardware-collect")["output"], hardware)
        self.assertEqual(self.record("hardware-collect")["boot_id"], BOOT_B)

    def test_upstream_change_invalidates_dependants(self):
        self.advance_to_gate("k3s-package")
        first = self.record("resources-plan")["input_sha256"]
        self.run_as(0, boot=BOOT_B)
        self.assertNotEqual(self.record("resources-plan")["input_sha256"], first)

    def test_memory_profile_is_an_explicit_render_option(self):
        orchestrator = postinstall.Postinstall(self.state, {"service_site": str(self.site)}, runner=self.runner,
                                               euid=USER, boot_id=BOOT_A, out=io.StringIO(), with_memory=True)
        render = next(step for step in postinstall.steps() if step.ident == "service-render")
        argv = render.build(orchestrator, "/out")
        self.assertEqual("assistant-memory", argv[argv.index("--experience") + 1])
        self.assertIn("--with-memory", orchestrator.rerun())
        plain = postinstall.Postinstall(self.state, {"service_site": str(self.site)}, runner=self.runner,
                                        euid=USER, boot_id=BOOT_A, out=io.StringIO())
        argv = render.build(plain, "/out")
        self.assertEqual("assistant", argv[argv.index("--experience") + 1])
        self.assertNotIn("--with-memory", plain.rerun())

    def test_cli_rejects_attestation_in_plan(self):
        status = postinstall.main(["plan", str(self.state), "--owner-completed", "packages"])
        self.assertEqual(status, postinstall.STOP_USAGE)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
