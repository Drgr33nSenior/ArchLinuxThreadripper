#!/usr/bin/env bash
# Elastic KV trial: keep SGLang's tail lines (grow, shrink with moved/evicted tokens, refusals). Pod logs
# go when the pod restarts and Loki only holds host logs, so a user timer (kv-trial-collect.timer) runs
# this every 5 minutes. Read-only to the cluster. Run on the workstation.
#
#   collect.sh          KV_TRIAL_SINCE=15m sets how far back each run reads (kubectl logs --since)
#
# Output: ~/ai-stack/kv-trial/events.log (one TP0 line per event, kubectl UTC timestamp first, sorted,
# duplicates dropped). kv_trial_report.py reads it.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
dir=$HOME/ai-stack/kv-trial
mkdir -p "$dir"
logs=$(kubectl -n ai-home-lab logs deploy/sglang -c sglang --timestamps --since="${KV_TRIAL_SINCE:-15m}")
new=$(grep 'TP0\] Elastic KV tail' <<<"$logs" || true)
[[ -n $new ]] || exit 0
{
  cat "$dir/events.log" 2>/dev/null || true
  printf '%s\n' "$new"
} | sort -u >"$dir/events.tmp"
mv "$dir/events.tmp" "$dir/events.log"
