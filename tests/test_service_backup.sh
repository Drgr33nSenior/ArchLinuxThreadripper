#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_service_backup.py"
