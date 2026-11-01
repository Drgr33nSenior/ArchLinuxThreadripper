#!/usr/bin/env bash
# CPU-only package identity and offline execution boundaries.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -n ${HOME_LAB_PYTHON:-} ]] || {
  printf 'BLOCKED: HOME_LAB_PYTHON required for Hyperloom fixtures\n' >&2
  exit 1
}
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_hyperloom.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_hyperloom_sglang.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_hyperloom_owner.py"
WORKSTATION_PYTHON="$HOME_LAB_PYTHON" bash "$root/bin/workstationctl" performance hyperloom --help >/dev/null
printf 'Hyperloom isolated-tool fixtures passed; no GPU or LLM provider used\n'
