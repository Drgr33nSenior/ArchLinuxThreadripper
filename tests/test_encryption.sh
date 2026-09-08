#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/common.sh"
source "$repo_root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# Synthetic benchmark output only: no cryptsetup process or device access.
ws_require_arch() { :; }
# shellcheck disable=SC2329
cryptsetup() {
  printf '%s\n' "$*" >> "$work/cryptsetup-arguments"
  if [[ $1 == --version ]]; then printf 'cryptsetup synthetic-test\n'; return; fi
  [[ $1 == benchmark ]] || return 1
  case ${benchmark_case:-valid} in
    valid) printf 'argon2id 4 iterations, 524288 memory, 4 parallel threads (CPUs) for 2000 ms (requested 2000 ms)\n' ;;
    failed) printf 'argon2id 4 iterations, 524288 memory, 4 parallel threads\n'; return 1 ;;
    malformed) printf 'unrecognized output\n' ;;
    weak) printf 'argon2id 4 iterations, 65536 memory, 4 parallel threads\n' ;;
  esac
}

ws_argon2_calibrate "$repo_root/config/install.conf.example" "$work/valid" >/dev/null
jq -e '.pbkdf=="argon2id" and .iterations==4 and .memory_kib==524288 and .parallelism==4 and .requested_unlock_ms==2000' \
  "$work/valid/argon2id.json" >/dev/null
grep -Fxq 'LUKS_MEMORY_KIB=524288' "$work/valid/install-values.conf"
grep -Fxq 'benchmark --pbkdf argon2id --iter-time 2000 --pbkdf-memory 1048576 --pbkdf-parallel 4' "$work/cryptsetup-arguments"
if grep -Eq '(/dev/|luksFormat|luksDump|open)' "$work/cryptsetup-arguments"; then exit 1; fi
if (ws_argon2_calibrate "$repo_root/config/install.conf.example" "$work/valid") >/dev/null 2>&1; then exit 1; fi

for benchmark_case in failed malformed weak; do
  if (ws_argon2_calibrate "$repo_root/config/install.conf.example" "$work/$benchmark_case") >/dev/null 2>&1; then
    echo "invalid Argon2id benchmark accepted: $benchmark_case" >&2; exit 1
  fi
  [[ ! -e $work/$benchmark_case/argon2id.json ]]
done
benchmark_case=valid
sed 's/^LUKS_PARALLEL=.*/LUKS_PARALLEL=48/' "$repo_root/config/install.conf.example" > "$work/invalid.conf"
if (ws_argon2_calibrate "$work/invalid.conf" "$work/invalid") >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/invalid ]]
echo 'Argon2id calibration parsing, bounds and benchmark-only tests passed'
