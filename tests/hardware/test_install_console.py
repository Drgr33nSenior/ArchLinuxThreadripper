"""PTY/credential-lifecycle fixtures; no real login, network or installation."""
import errno
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = b"DUMMY_NOT_A_REAL_API_KEY_12345"


class ConsoleTests(unittest.TestCase):
    def run_console(self, mode="api-key", condition=None, stop=None):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            (fixture / "bin").mkdir()
            (fixture / "ram").mkdir()
            (fixture / "target").mkdir()
            (fixture / "existing-auth").write_text("untouched")
            executable = fixture / "bin/codex"
            shutil.copyfile(ROOT / "tests/fixtures/install-codex-mock.sh", executable)
            executable.chmod(0o755)
            if condition:
                (fixture / condition).touch()
            if stop:
                (fixture / "wait").touch()
            pid, terminal = pty.fork()
            if pid == 0:
                os.execvp("bash", ["bash", str(ROOT / "tests/fixtures/install-console.sh"),
                                   str(ROOT), str(fixture), mode])
            output = bytearray()
            sent = interrupted = finished = False
            deadline = time.monotonic() + 15
            try:
                while time.monotonic() < deadline:
                    if select.select([terminal], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(terminal, 65536)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            break
                        if not chunk:
                            break
                        output.extend(chunk)
                    if b"API key (hidden):" in output and not sent:
                        os.write(terminal, SENTINEL + b"\n")
                        sent = True
                    if b"READY" in output and stop and not interrupted:
                        os.kill(pid, stop)
                        interrupted = True
                else:
                    self.fail("console fixture timed out")
                _, status = os.waitpid(pid, 0)
                finished = True
                code = os.waitstatus_to_exitcode(status)
                self.assertNotIn(SENTINEL, output)
                self.assertEqual(list((fixture / "ram").iterdir()), [])
                self.assertEqual(list((fixture / "target").iterdir()), [])
                self.assertEqual((fixture / "existing-auth").read_text(), "untouched")
                commands = (fixture / "commands").read_text() if (fixture / "commands").exists() else ""
                self.assertNotIn(SENTINEL.decode(), commands)
                paths = (fixture / "paths").read_text().splitlines() if (fixture / "paths").exists() else []
                self.assertLessEqual(len(set(paths)), 1, "login and client must share isolated configuration")
                if paths:
                    with (fixture / "effective-config.toml").open("rb") as source:
                        config = tomllib.load(source)
                    self.assertEqual(config.get("model"), "gpt-6.1-sol")
                    self.assertEqual(config["cli_auth_credentials_store"], "file")
                    self.assertEqual(config["mcp_oauth_credentials_store"], "file")
                    self.assertEqual(config["sandbox_mode"], "read-only")
                    self.assertEqual(config["approval_policy"], "on-request")
                    self.assertEqual(config["shell_environment_policy"]["inherit"], "none")
                return code, commands
            finally:
                if not finished:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(terminal)

    def test_api_and_device_auth(self):
        for mode in ("api-key", "device-code"):
            with self.subTest(mode=mode):
                code, commands = self.run_console(mode)
                self.assertEqual(code, 0)
                self.assertIn("login status", commands)
                self.assertIn("--sandbox read-only --ask-for-approval on-request", commands)
                self.assertNotIn("--with-api-key" if mode == "device-code" else "--device-auth", commands)

    def test_auth_failure_does_not_start_session(self):
        code, commands = self.run_console(condition="login-fail")
        self.assertNotEqual(code, 0)
        self.assertNotIn("--cd", commands)

    def test_primary_client_failure(self):
        code, _ = self.run_console(condition="client-fail")
        self.assertEqual(code, 7)

    def test_interrupt_cleanup(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                code, _ = self.run_console(stop=sig)
                self.assertEqual(code, 128 + sig)

    def test_prerequisites_block_authentication(self):
        for condition in ("offline", "wrong-platform", "wrong-fs", "swap"):
            with self.subTest(condition=condition):
                code, commands = self.run_console(condition=condition)
                self.assertNotEqual(code, 0)
                self.assertEqual(commands, "")

    def test_desktop_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "codex/config.toml"
            command = ["bash", "-c", '''
                source "$1/lib/workstation/runtime.sh"
                ws_require_arch() { :; }
                ws_require_user() { :; }
                ws_codex_configure "$2"
            ''', "fixture", str(ROOT), str(output)]
            subprocess.run(command, check=True, capture_output=True)
            with output.open("rb") as source:
                config = tomllib.load(source)
            self.assertEqual(config.get("model"), "gpt-6.1-sol")
            self.assertEqual(config["cli_auth_credentials_store"], "keyring")
            self.assertEqual(config["mcp_oauth_credentials_store"], "keyring")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            original = output.read_bytes()
            refused = subprocess.run(command, capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(b"refusing to overwrite", refused.stderr)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
