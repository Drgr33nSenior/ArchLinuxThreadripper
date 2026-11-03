#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PYTHONDONTWRITEBYTECODE=1 "${HOME_LAB_PYTHON:-python3}" "$root/tests/hardware/test_model_readiness.py"
