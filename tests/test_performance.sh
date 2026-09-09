#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${HOME_LAB_PYTHON:-} ]]; then
  printf 'SKIP: set HOME_LAB_PYTHON for standard-library measurement fixtures\n'
  exit 0
fi
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_measurement.py"
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
SESSION_AI_DEPLOYMENT=sglang
[[ $(ws_session_rollout_timeout sglang) == 2100s ]]
[[ $(ws_session_rollout_timeout gaming) == 300s ]]
SESSION_AI_STARTUP_TIMEOUT_SECONDS=2400
SESSION_TIMEOUT_SECONDS=45
[[ $(ws_session_rollout_timeout sglang) == 2400s ]]
[[ $(ws_session_rollout_timeout gaming) == 45s ]]

# Function mocks only: never invoke TuneD, acquire host locks or tune this Mac.
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
ws_require_arch() { :; }
ws_require_root() { :; }
ws_performance_tuned_lock() { :; }
tuned-adm() {
  printf '%s\n' "$*" >>"$work/tuned.log"
  case $1 in
    active) printf 'Current active profile: balanced\n' ;;
    profile) [[ ${FAIL_RESTORE:-0} != 1 || $2 != balanced ]] ;;
    verify) return 0 ;;
  esac
}
ws_measure_command() { return "${MEASUREMENT_EXIT:-0}"; }
ws_performance_tuned accelerator-performance "$work/success" -- true
[[ $(tail -n 2 "$work/tuned.log" | head -n 1) == 'profile balanced' ]]
if MEASUREMENT_EXIT=7 ws_performance_tuned accelerator-performance "$work/failed" -- false; then
  printf 'failed benchmark incorrectly succeeded\n' >&2
  exit 1
fi
[[ $(tail -n 2 "$work/tuned.log" | head -n 1) == 'profile balanced' ]]
if FAIL_RESTORE=1 ws_performance_tuned accelerator-performance "$work/restore-failed" -- true 2>"$work/restore-error"; then
  printf 'failed profile restoration incorrectly succeeded\n' >&2
  exit 1
fi
grep -Fq 'FAILED to restore TuneD profile balanced' "$work/restore-error"

# The readiness observer never starts/restarts a Pod. Mock a container becoming
# Ready, then verify that already-ready and restarted observations are rejected.
ws_serving_pod() {
  local count=0 ready=true uid=fixture
  [[ ! -f $work/pod-count ]] || count=$(<"$work/pod-count")
  printf '%s\n' "$((count + 1))" >"$work/pod-count"
  if [[ $count == 0 && ${ALREADY_READY:-0} != 1 ]]; then ready=false; fi
  if [[ $count != 0 && ${RESTART_POD:-0} == 1 ]]; then uid=changed; fi
  jq -n --argjson ready "$ready" --arg uid "$uid" \
    '{ready:$ready,uid:$uid,started_at:"2026-09-09T00:00:00.123Z",image_id:"immutable-fixture"}'
}
date() { printf '2026-09-09T00:00:05Z\n'; }
ws_serving_startup fixture cold "$work/startup"
jq -e '.status == "observed-not-qualified" and .cache_state == "cold" and .elapsed_seconds >= 0' "$work/startup/startup.json" >/dev/null
if ALREADY_READY=1 ws_serving_startup fixture warm "$work/already-ready" 2>/dev/null; then
  printf 'already-ready Pod was accepted as startup evidence\n' >&2
  exit 1
fi
printf '0\n' >"$work/pod-count"
if RESTART_POD=1 ws_serving_startup fixture cold "$work/restarted" 2>/dev/null; then
  printf 'restarted Pod was accepted as startup evidence\n' >&2
  exit 1
fi
[[ ! -e $work/restarted/startup.json ]]
printf 'Performance helper and separate startup timeout fixtures passed\n'
