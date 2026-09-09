#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
"${HOME_LAB_PYTHON:-python3}" "$root/tests/hardware/test_install_console.py"
