#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# Test the production systemd discovery boundary without reading host /proc
# or invoking the host service manager. File-policy fixtures are in test_amd_build.sh.
systemctl() {
  printf '%s\n' "$*" >>"$work/arguments"
  case "$*" in
    'list-unit-files --type=swap --no-legend --no-pager')
      # systemd 261.2 returns 1 for a successful query with no matching units.
      return 1
      ;;
    'list-unit-files --no-legend --no-pager')
      [[ $# == 3 ]] || return 99
      printf '%s' "$UNIT_FILES"
      return "$DISCOVERY_STATUS"
      ;;
    *) return 99 ;;
  esac
}
UNIT_FILES=$'dbus.service static -\nfstrim.timer enabled enabled\n'
DISCOVERY_STATUS=0
ws_no_swap_units_check
[[ $(<"$work/arguments") == 'list-unit-files --no-legend --no-pager' ]]

# The older two-column format is also valid.
UNIT_FILES=$'dbus.service static\nfstrim.timer enabled\n'
ws_no_swap_units_check

expect_refusal() {
  local message=$1
  if (ws_no_swap_units_check) >"$work/refused" 2>&1; then
    printf 'FAIL: accepted inventory expected to fail: %s\n' "$message" >&2
    exit 1
  fi
  [[ $(<"$work/refused") == *"$message"* ]]
}
for state in enabled disabled masked generated; do
  UNIT_FILES=$'dbus.service static -\n'"dev-disk-by\\x2duuid-fixture.swap $state -"$'\n'
  expect_refusal 'configured swap unit found'
done
for UNIT_FILES in '' $' \n\t\n' 'garbage' 'garbage static -' 'dbus.service' 'dbus.service static - extra' $'dbus.service static -\ngarbage\n'; do
  expect_refusal 'unit-file inventory is empty or malformed'
done

# Failure, including a partial otherwise-valid response, never means no swap.
for UNIT_FILES in '' $'dbus.service static -\n'; do
  for DISCOVERY_STATUS in 1 9 127; do
    expect_refusal 'cannot inspect configured swap units'
  done
done
DISCOVERY_STATUS=0
UNIT_FILES=$'dbus.service static -\n'
awk() { return 7; }
expect_refusal 'unit-file inventory is empty or malformed'
unset -f awk
printf 'No-swap unit discovery fixtures passed; no host service manager accessed\n'
