#!/usr/bin/env bash
# Offline run-record-v1 fixtures only: no model, gateway, PSU or GPU access.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -n ${HOME_LAB_PYTHON:-} ]] || {
  printf 'BLOCKED: HOME_LAB_PYTHON required for run-record fixtures\n' >&2
  exit 1
}
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_run_record.py"
