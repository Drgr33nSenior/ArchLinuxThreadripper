#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${HOME_LAB_PYTHON:-} ]]; then
  printf 'SKIP: set HOME_LAB_PYTHON for SGLang memory fixtures\n'
  exit 0
fi
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_serving_memory.py"
