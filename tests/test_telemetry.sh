#!/usr/bin/env bash
# Offline fixtures only: no real journal, disks, services or Kubernetes API.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -n ${HOME_LAB_PYTHON:-} ]] || {
  printf 'BLOCKED: HOME_LAB_PYTHON required for telemetry fixtures\n' >&2
  exit 1
}
command -v kubectl >/dev/null || {
  printf 'BLOCKED: kubectl required for offline telemetry render\n' >&2
  exit 1
}
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_telemetry.py"
source "$root/lib/common.sh"
source "$root/lib/workstation/runtime.sh"
TELEMETRY_ENABLED=false
TELEMETRY_API_ADDRESS=untrusted
ws_load_config "$root/config/workstation.conf.example"
[[ $TELEMETRY_ENABLED == true && -z $TELEMETRY_API_ADDRESS ]]
source "$root/lib/bootstrap/common.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
logger() {
  printf '%s\n' "${*: -1}" >>"$work/events"
  return "${LOGGER_STATUS:-0}"
}
BOOTSTRAP_EVENTS=1
BOOTSTRAP_DRY_RUN=1
BOOTSTRAP_EVENT_RUN=20260911T120000Z-123
BOOTSTRAP_EVENT_CONFIG=$(printf 'nonsecret fixture' | shasum -a 256 | awk '{print $1}')
fixture_ok() { :; }
fixture_bad() { return 7; }
if bootstrap_event dummy-secret-sentinel started 0; then exit 1; fi
[[ ! -e $work/events ]]
bootstrap_stage preflight fixture_ok DUMMY_SECRET_SENTINEL
if bootstrap_stage storage fixture_bad DUMMY_SECRET_SENTINEL; then exit 1; else [[ $? == 7 ]]; fi
LOGGER_STATUS=1
if bootstrap_stage storage fixture_bad DUMMY_SECRET_SENTINEL 2>"$work/warning"; then exit 1; else [[ $? == 7 ]]; fi
grep -Fq 'telemetry incomplete' "$work/warning"
[[ $(wc -l <"$work/events") == *6* ]]
if grep -q DUMMY_SECRET_SENTINEL "$work/events"; then exit 1; fi
jq -se 'length == 6 and .[0].outcome == "started" and .[1].outcome == "succeeded" and .[3].exit_code == 7 and all(.[]; .mode == "dry-run")' "$work/events" >/dev/null
BOOTSTRAP_EVENTS=0
bootstrap_stage preflight fixture_ok
[[ $(wc -l <"$work/events") == *6* ]]
printf 'Telemetry configuration, stage-event and hardware fixtures passed (no hardware qualification)\n'
