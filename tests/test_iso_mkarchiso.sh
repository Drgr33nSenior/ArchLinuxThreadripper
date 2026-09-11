#!/usr/bin/env bash
# Synthetic package query only: no keyring, package installation or Docker.
# shellcheck disable=SC2329 # Functions consumed by the generated child program.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/infrastructure/iso/mkarchiso.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fixture="$root/tests/fixtures/mkarchiso-pkglist.txt"
program=$(mkarchiso_program "$fixture")
export pacstrap_dir="$work/live root" pacman_conf="$work/verified builder.conf"
export isofs_dir="$work/iso" install_dir=arch arch=x86_64 buildmode=iso
export calls="$work/calls"
_msg_info() { :; }
pacman() {
  printf '%s\n' "$@" >"$calls"
  [[ $# == 5 && $1 == -Q && $2 == --root && $3 == "$pacstrap_dir" &&
    $4 == --config && $5 == "$pacman_conf" ]] || return 9
  [[ ${QUERY_EMPTY:-false} == true ]] || printf 'synthetic-package 1.0-1\n'
  if [[ ${QUERY_DIAGNOSTIC:-false} == true ]]; then printf 'error: synthetic unknown signer\n' >&2; fi
  return "${QUERY_STATUS:-0}"
}
export -f _msg_info pacman
# Invoke in a conditional too: a failed query must stop even if errexit is masked.
invocation=$'\nif _make_pkglist; then printf "continued\\n"; fi'
actual=$(bash -euo pipefail -c "$program$invocation")
[[ $actual == continued && $(<"$isofs_dir/arch/pkglist.x86_64.txt") == 'synthetic-package 1.0-1' ]]
[[ ! -e $pacstrap_dir ]] # No private keyring/live-root writes by the query.
if QUERY_STATUS=7 bash -euo pipefail -c "$program$invocation" >"$work/failed" 2>&1; then exit 1; fi
grep -q 'inventory query failed' "$work/failed"
if grep -q continued "$work/failed"; then exit 1; fi
if QUERY_EMPTY=true bash -euo pipefail -c "$program$invocation" >"$work/empty" 2>&1; then exit 1; fi
grep -q 'inventory is empty' "$work/empty"
if grep -q continued "$work/empty"; then exit 1; fi
# Pacman can print signature errors and still exit zero for a local query.
if QUERY_DIAGNOSTIC=true bash -euo pipefail -c "$program$invocation" >"$work/diagnostic" 2>&1; then exit 1; fi
grep -q 'synthetic unknown signer' "$work/diagnostic"
grep -q 'inventory emitted diagnostics' "$work/diagnostic"
if grep -q continued "$work/diagnostic"; then exit 1; fi
# No match / duplicate match is upstream drift, never a silently unadapted build.
printf 'changed upstream\n' >"$work/drift"
if mkarchiso_program "$work/drift" >/dev/null 2>&1; then exit 1; fi
cp "$fixture" "$work/duplicate"
command cat "$fixture" >>"$work/duplicate"
if mkarchiso_program "$work/duplicate" >/dev/null 2>&1; then exit 1; fi
if mkarchiso_program "$work/missing" >/dev/null 2>&1; then exit 1; fi
# Package installation and the separate bootstrap branch are not adapted.
diff -u <(sed -n '/"bootstrap")/,/;;/p' "$fixture") <(sed -n '/"bootstrap")/,/;;/p' <<<"$program")
printf 'Archiso package-list keyring context, failure, empty-result and drift tests passed\n'
