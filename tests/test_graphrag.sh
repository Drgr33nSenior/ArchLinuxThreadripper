#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
"$HOME_LAB_PYTHON" -B "$root/tests/workstation/test_graphrag_pilot.py"
bash "$root/bin/workstationctl" graphrag --help >/dev/null 2>&1 && exit 1
bash "$root/bin/workstationctl" help | grep -Fq 'graphrag prepare'
