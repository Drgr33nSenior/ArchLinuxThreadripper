#!/usr/bin/env python3
"""Read-only check that the firmware trusts this installation's Secure Boot keys.

A reinstall creates new sbctl keys, but the firmware keeps whatever was enrolled
before until it is put into setup mode and re-enrolled. Every UKI then verifies
against the local certificate yet fails with a Secure Boot violation. This
compares the firmware's PK, KEK and db with /var/lib/sbctl/keys, reports the
Secure Boot and setup-mode state, and flags image hashes in db (hash enrolment
from the firmware menu only trusts specific files, so the next kernel update
breaks again). It reads EFI variables and certificates only and never writes.

Usage: secure_boot.py check [--efivars DIR] [--keys DIR]
"""
import argparse
import hashlib
from pathlib import Path
import ssl
import struct
import sys
import uuid

GLOBAL = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
IMAGE_SECURITY = "d719b2cb-3d3a-4596-a3bc-dad00e67656f"
X509 = uuid.UUID("a5c059a1-94e4-4aa7-87b5-ab155c2bf072")
SHA256 = uuid.UUID("c1c41626-504c-4092-aca9-41f936934328")
VARIABLES = {"PK": GLOBAL, "KEK": GLOBAL, "db": IMAGE_SECURITY}


def variable(efivars, name, guid):
    """EFI variable payload without its 4-byte attribute prefix, or None if absent."""
    try:
        return (efivars / f"{name}-{guid}").read_bytes()[4:]
    except FileNotFoundError:
        return None


def signature_lists(data):
    """Yield (type, entry) for every entry in an EFI_SIGNATURE_LIST sequence."""
    offset = 0
    while offset + 28 <= len(data):
        kind = uuid.UUID(bytes_le=data[offset:offset + 16])
        list_size, header_size, entry_size = struct.unpack_from("<III", data, offset + 16)
        if list_size < 28 or entry_size <= 16 or offset + list_size > len(data):
            raise ValueError("malformed EFI signature list")
        body = data[offset + 28 + header_size:offset + list_size]
        for start in range(0, len(body) - entry_size + 1, entry_size):
            yield kind, body[start + 16:start + entry_size]  # skip the 16-byte owner GUID
        offset += list_size


def local_certificate(keys, name):
    pem = (keys / name / f"{name}.pem").read_text()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def flag(efivars, name):
    data = variable(efivars, name, GLOBAL)
    return data is not None and len(data) >= 1 and data[0] == 1


def check(efivars, keys):
    problems, notes = [], []
    for name, guid in VARIABLES.items():
        data = variable(efivars, name, guid)
        if data is None:
            problems.append(f"firmware {name} is empty (setup mode or keys cleared)")
            continue
        entries = list(signature_lists(data))
        certificates = {hashlib.sha256(entry).hexdigest() for kind, entry in entries if kind == X509}
        if local_certificate(keys, name) not in certificates:
            problems.append(f"firmware {name} does not contain this installation's sbctl {name} certificate")
        hashes = sum(1 for kind, _ in entries if kind == SHA256)
        if name == "db" and hashes:
            notes.append(f"db holds {hashes} image hash(es); they trust only those exact files")
    secure_boot, setup_mode = flag(efivars, "SecureBoot"), flag(efivars, "SetupMode")
    if setup_mode:
        problems.append("firmware is in setup mode; enroll keys with `sbctl enroll-keys --microsoft --firmware-builtin`")
    if not secure_boot:
        notes.append("Secure Boot is not enforcing this boot")
    return problems, notes, secure_boot


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("check",))
    parser.add_argument("--efivars", type=Path, default=Path("/sys/firmware/efi/efivars"))
    parser.add_argument("--keys", type=Path, default=Path("/var/lib/sbctl/keys"))
    args = parser.parse_args()
    try:
        problems, notes, secure_boot = check(args.efivars, args.keys)
    except (OSError, ValueError) as error:
        print(f"FAILED: cannot inspect Secure Boot keys ({error.__class__.__name__}: {error}); run as root on the installed host",
              file=sys.stderr)
        return 1
    for note in notes:
        print(f"NOTE: {note}")
    if problems:
        for problem in problems:
            print(f"FAILED: {problem}", file=sys.stderr)
        print("Re-enroll from firmware setup mode: sbctl enroll-keys --microsoft --firmware-builtin; "
              "see docs/INSTALLATION.md section 5.", file=sys.stderr)
        return 1
    print(f"PASS: firmware PK, KEK and db trust this installation's sbctl keys; Secure Boot "
          f"{'enforcing' if secure_boot else 'NOT enforcing'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
