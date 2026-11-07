#!/usr/bin/env bash
# Post-install orchestration and its CLI dispatch; no stage command is executed.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
"$HOME_LAB_PYTHON" "$root/tests/workstation/test_postinstall.py"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
work=$(cd -- "$work" && pwd -P)
export WORKSTATION_PYTHON=$HOME_LAB_PYTHON
"$root/bin/workstationctl" --help | grep -Fq 'postinstall plan|run <private-state-directory>'
# plan is read-only: it prints the ordered gate and creates no state.
"$root/bin/workstationctl" postinstall plan "$work/state" >"$work/plan.txt"
grep -Fq "STOP: owner gate 'secure-boot-unlock'" "$work/plan.txt"
grep -Fq 'Nothing was run.' "$work/plan.txt"
[[ ! -e $work/state ]]
status=0
"$root/bin/workstationctl" postinstall resume "$work/state" >/dev/null 2>&1 || status=$?
[[ $status == 2 ]]
status=0
"$root/bin/workstationctl" postinstall plan "$work/state" --owner-completed packages >/dev/null 2>&1 || status=$?
[[ $status == 2 && ! -e $work/state ]]
# run creates only the private state skeleton before stopping at the first gate.
status=0
"$root/bin/workstationctl" postinstall run "$work/state" >"$work/run.txt" || status=$?
[[ $status == 3 ]]
[[ -z $(find "$work/state/records" "$work/state/logs" "$work/state/outputs" -mindepth 1) ]]
mode=$(stat -c %a "$work/state" 2>/dev/null || stat -f %Lp "$work/state")
[[ $mode == 700 ]]
echo 'Post-install orchestration tests passed (injected runner; no stage executed)'
