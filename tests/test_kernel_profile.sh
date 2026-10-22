#!/usr/bin/env bash
# Offline hardware-policy fixtures; no target host or kernel is changed.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -n ${HOME_LAB_PYTHON:-} ]] || {
  printf 'BLOCKED: HOME_LAB_PYTHON required for kernel profile fixtures\n' >&2
  exit 1
}
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_kernel_profile.py"
printf 'Kernel hardware-profile fixtures passed; no native build or boot was performed\n'
