#!/usr/bin/env bash
# Offline Secure Boot key-check fixtures; no firmware variable is read or written.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PYTHONDONTWRITEBYTECODE=1 "${HOME_LAB_PYTHON:-python3}" "$root/tests/workstation/test_secure_boot.py"
help=$(bash "$root/bin/workstationctl" --help)
grep -Fq 'secure-boot check' <<<"$help"
printf 'PASS: Secure Boot key check fixtures and CLI registration\n'
