#!/usr/bin/env bash
# Protected task acceptance contract: standard library only, authored fixtures, no model.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
PYTHONDONTWRITEBYTECODE=1 "${HOME_LAB_PYTHON:-python3}" "$root/tests/workstation/test_task_acceptance.py"
PYTHONDONTWRITEBYTECODE=1 "${HOME_LAB_PYTHON:-python3}" "$root/tests/workstation/test_task_acceptance_lifetime.py"
