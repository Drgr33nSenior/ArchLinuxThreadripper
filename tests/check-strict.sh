#!/usr/bin/env bash
# Strict software checks; image/Linux-only gates are retained, never qualified.
set -uo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$root" || exit 1
umask 077
mkdir -p test-results
output=$(mktemp -d "$root/test-results/strict-check.XXXXXX") || exit 1
printf 'Strict validation evidence: %s\n' "$output"
if [[ -z ${HOME_LAB_PYTHON:-} || ! -x $HOME_LAB_PYTHON ]]; then
  printf 'BLOCKED: HOME_LAB_PYTHON must select the prepared private Python\n' | tee "$output/check.log"
  exit 1
fi
if ! "$HOME_LAB_PYTHON" -c 'import jinja2, yaml'; then
  printf 'BLOCKED: selected Python needs Jinja2 and PyYAML\n' | tee "$output/check.log"
  exit 1
fi
make -k check 2>&1 | tee "$output/check.log"
status=${PIPESTATUS[0]}
printf '%s\n' "$status" >"$output/make.exit"
awk '/SKIP:|SKIPPED:|BLOCKED:|^OK \(skipped=/' "$output/check.log" >"$output/skips.txt"
unexpected=0
while IFS= read -r skip; do
  case $skip in
    'SKIP: set HOME_LAB_GAME_IMAGE to an already-built local image ID for streaming smoke tests' | \
      'SKIP: set HOME_LAB_WAYLAND_RUNTIME_IMAGE for isolated Linux process-group tests' | \
      'SKIP: systemd-analyze verification requires Linux') ;;
    *) unexpected=1 ;;
  esac
done <"$output/skips.txt"
if ((status != 0 || unexpected != 0)); then
  printf 'FAILED: software check failure or unexpected skip; inspect %s\n' "$output"
  exit 1
fi
printf 'PASS: strict software checks; image/Linux exceptions are listed in %s/skips.txt. Hardware remains unqualified.\n' "$output"
