"""Secure Boot key check against synthetic EFI variables; no firmware is touched."""
import base64
import contextlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import unittest.mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import secure_boot

OWNER = uuid.UUID("620fbf6c-0dbf-4167-9a27-c721ea5a1195").bytes_le


def esl(kind, entries):
    size = 16 + len(entries[0])
    body = b"".join(OWNER + entry for entry in entries)
    return kind.bytes_le + struct.pack("<III", 28 + len(body), 0, size) + body


class SecureBootCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.efivars, self.keys = Path(temp.name) / "efivars", Path(temp.name) / "keys"
        self.efivars.mkdir()
        self.der = {name: f"local-{name}-certificate".encode() for name in ("PK", "KEK", "db")}
        for name, der in self.der.items():
            (self.keys / name).mkdir(parents=True)
            pem = "-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der).decode() + "-----END CERTIFICATE-----\n"
            (self.keys / name / f"{name}.pem").write_text(pem)
            # Real firmware keeps each vendor's certificates in its own signature list.
            self.write(name, esl(secure_boot.X509, [der]) + esl(secure_boot.X509, [b"vendor-ca-certificate"]))
        self.flags(secure=1, setup=0)

    def write(self, name, payload):
        guid = secure_boot.VARIABLES.get(name, secure_boot.GLOBAL)
        (self.efivars / f"{name}-{guid}").write_bytes(b"\x07\x00\x00\x00" + payload)

    def flags(self, secure, setup):
        self.write("SecureBoot", bytes([secure]))
        self.write("SetupMode", bytes([setup]))

    def run_check(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["secure_boot.py", "check", "--efivars", str(self.efivars), "--keys", str(self.keys)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.object(sys, "argv", argv):
            status = secure_boot.main()
        return status, out.getvalue() + err.getvalue()

    def test_matching_keys_pass(self):
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn("Secure Boot enforcing", text)

    def test_previous_installation_keys_fail_with_reenrolment_advice(self):
        self.write("db", esl(secure_boot.X509, [b"old-install-db-certificate"]))
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("firmware db does not contain this installation's sbctl db certificate", text)
        self.assertIn("sbctl enroll-keys --microsoft --firmware-builtin", text)

    def test_setup_mode_and_missing_pk_fail(self):
        (self.efivars / f"PK-{secure_boot.GLOBAL}").unlink()
        self.flags(secure=0, setup=1)
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("firmware PK is empty", text)
        self.assertIn("setup mode", text)

    def test_image_hashes_are_reported(self):
        self.write("db", esl(secure_boot.X509, [self.der["db"]]) + esl(secure_boot.SHA256, [b"h" * 32, b"i" * 32]))
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn("db holds 2 image hash(es)", text)

    def test_unreadable_keys_fail_closed(self):
        (self.keys / "PK" / "PK.pem").unlink()
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("cannot inspect Secure Boot keys", text)


if __name__ == "__main__":
    unittest.main()
