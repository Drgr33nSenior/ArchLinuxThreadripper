#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_shared_service.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_webui_presets.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_assistant_acceptance.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_memory.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_service_tools.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_handoff.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_web_search.py"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_creative.py"
