#!/usr/bin/env bash
# KUBECTL_EXTERNAL_DIFF target for sandbox-golive.sh. kubectl accepts no path arguments in
# that variable, so this wrapper names the checker. Writes JSON to $GOLIVE_DIFF_OUT.
#
#   GOLIVE_DIFF_OUT=OUT.json KUBECTL_EXTERNAL_DIFF=kdiff.sh kubectl diff -f FILE
if [[ ${1:-} == -h || ${1:-} == --help ]]; then
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
  exit 0
fi
exec python3 -I "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/golive_check.py" kdiff "$@"
