#!/usr/bin/env bash
# Offline run-record-v1 checks: fixtures, in-process services and one short local
# command. No model, deployed gateway, cluster, PSU or GPU is contacted.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -n ${HOME_LAB_PYTHON:-} ]] || {
  printf 'BLOCKED: HOME_LAB_PYTHON required for run-record fixtures\n' >&2
  exit 1
}
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_run_record.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/workstation/test_run_trace.py"

# workstationctl performance run, attempt form: the real dispatch, shell helper and
# measurement CLI. Only the Arch-host gate is replaced, so this also runs in CI.
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fixtures=$root/tests/fixtures/run-record
record=("$HOME_LAB_PYTHON" "$root/lib/workstation/run_record.py")
mkdir -p "$work/cli/bin" "$work/cli/lib/workstation"
cp "$root/bin/workstationctl" "$work/cli/bin/workstationctl"
ln -s "$root/lib/common.sh" "$work/cli/lib/common.sh"
cat >"$work/cli/lib/workstation/runtime.sh" <<EOF
source "$root/lib/workstation/runtime.sh"
ws_require_arch() { :; }
EOF
cli() { PYTHONDONTWRITEBYTECODE=1 WORKSTATION_PYTHON=$HOME_LAB_PYTHON bash "$work/cli/bin/workstationctl" performance run "$@"; }
jq -n '{task_id: "fixture-completed", task_version: "v1", run_id: "fixture-run-1", attempt_id: "fixture-cli-1",
  arm_id: "fixture-arm", parent_id: null, cause: "initial"}' >"$work/identity.json"
plan_sha256=$(jq -r .plan_sha256 "$fixtures/attempts.jsonl" | sort -u)
context=$(PYTHONDONTWRITEBYTECODE=1 "${record[@]}" context --plan "$fixtures/plan.json" --identity "$work/identity.json" | jq -r .value)
[[ $context == "v1;task=fixture-completed;task_version=v1;run=fixture-run-1;attempt=fixture-cli-1;arm=fixture-arm;parent=-;cause=initial;plan=$plan_sha256" ]]

# The workload receives the run context that the receipt records.
cli "$work/attempt" 30 --run-plan "$fixtures/plan.json" --run-identity "$work/identity.json" -- \
  "$HOME_LAB_PYTHON" -c 'import os; print(os.environ["WORKSTATION_RUN_CONTEXT"])'
[[ $(<"$work/attempt/stdout.txt") == "$context" ]]
jq -e --arg plan "$plan_sha256" '.kind == "attempt-start" and .plan_sha256 == $plan
  and .identity.attempt_id == "fixture-cli-1"' "$work/attempt/run-start.json" >/dev/null
jq -e --arg plan "$plan_sha256" --slurpfile start "$work/attempt/run-start.json" '.status == "measured-not-qualified"
  and .plan_sha256 == $plan and .identity == $start[0].identity
  and .started_unix_ns == $start[0].started_unix_ns' "$work/attempt/run.json" >/dev/null

# The receipt and its telemetry become joinable records.
PYTHONDONTWRITEBYTECODE=1 "${record[@]}" attempt "$work/attempt" >"$work/attempts.jsonl"
jq -e --arg plan "$plan_sha256" '.kind == "attempt" and .outcome == "completed" and .plan_sha256 == $plan
  and .identity.attempt_id == "fixture-cli-1"' "$work/attempts.jsonl" >/dev/null
PYTHONDONTWRITEBYTECODE=1 "${record[@]}" resources "$work/attempt/telemetry.jsonl" --psu "$work/psu.jsonl" --gpu "$work/gpu.jsonl"
[[ -s $work/psu.jsonl && -f $work/gpu.jsonl ]]

# A plan edited after the attempt started no longer matches the bound digest.
jq '.latency_targets.task_wall_seconds = 1' "$fixtures/plan.json" >"$work/edited-plan.json"
: >"$work/none.jsonl"
if PYTHONDONTWRITEBYTECODE=1 "${record[@]}" report --plan "$work/edited-plan.json" --attempts "$work/attempts.jsonl" \
  --acceptance "$work/none.jsonl" >/dev/null 2>"$work/edited.err"; then
  printf 'a plan edited after the attempt was accepted\n' >&2
  exit 1
fi
grep -Fq 'run record rejected:' "$work/edited.err"

# An undeclared task or an incomplete attempt form never starts a workload.
jq '.task_id = "fixture-undeclared"' "$work/identity.json" >"$work/undeclared.json"
if cli "$work/undeclared" 30 --run-plan "$fixtures/plan.json" --run-identity "$work/undeclared.json" -- true 2>"$work/undeclared.err"; then
  printf 'an attempt for an undeclared task was started\n' >&2
  exit 1
fi
grep -Fq 'run record rejected:' "$work/undeclared.err"
[[ ! -e $work/undeclared ]]
for form in "--run-plan $fixtures/plan.json -- true" "--run-identity $work/identity.json -- true" \
  "--run-plan $fixtures/plan.json --run-identity $work/identity.json --"; do
  # shellcheck disable=SC2086 # Each form is a fixed argument list without spaces.
  if cli "$work/usage" 30 $form >/dev/null 2>&1; then
    printf 'incomplete attempt form accepted: %s\n' "$form" >&2
    exit 1
  else
    [[ $? == 2 ]]
  fi
  [[ ! -e $work/usage ]]
done

# The plain form still records no run identity.
cli "$work/plain" 30 -- true
jq -e '.identity == null and .plan_sha256 == null' "$work/plain/run.json" >/dev/null
[[ ! -e $work/plain/run-start.json ]]
printf 'workstationctl performance run attempt form passed\n'
