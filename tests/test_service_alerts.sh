#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
promtool test rules "$root/tests/fixtures/service-backup-alerts.yaml"
"$HOME_LAB_PYTHON" "$root/tests/service/test_alert_delivery.py"
