#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Mock tools, not the provider selector or its output validation.
timeout() {
  shift
  "$@"
}
# Invoked indirectly by the real provider/timeout path.
# shellcheck disable=SC2329
amd-smi() {
  [[ ${SMI_FAIL:-0} == 0 ]] || return 9
  case $1 in
    list) printf '%s\n' "$AMD_LIST" ;;
    version) printf '[{"tool":"AMDSMI Tool","version":"27.0.0"}]\n' ;;
    metric) printf '[{"gpu":0,"temperature":{"edge":"N/A"}}]\n' ;;
    *) return 8 ;;
  esac
}
AMD_LIST='[{"gpu":0,"bdf":"0000:01:00.0","uuid":"fixture-a"},{"gpu":1,"bdf":"0000:02:00.0","uuid":"fixture-b"}]'
ROCM_SDK_PROVIDER=aur-gfx120x-bin
[[ $(ws_gpu_monitor_provider) == amd-smi ]]
mkdir "$work/modern"
ws_gpu_monitor_capture "$work/modern"
jq -e '.provider == "amd-smi" and .status == "observed"' "$work/modern/gpu-monitor.json" >/dev/null
for bad in '' '{}' '[]' '[{"gpu":0,"bdf":"N/A","uuid":"N/A"}]' '{invalid'; do
  AMD_LIST=$bad
  if ws_gpu_monitor_json amd-smi <(printf '%s\n' "$bad") 2>/dev/null; then exit 1; fi
done
mkdir "$work/bad-json" "$work/failed"
if ws_gpu_monitor_capture "$work/bad-json" 2>/dev/null; then exit 1; fi
jq -e '.status == "unsupported-or-invalid-json" and .exit_code == 65' "$work/bad-json/gpu-monitor.json" >/dev/null
if SMI_FAIL=1 ws_gpu_monitor_capture "$work/failed"; then exit 1; fi
jq -e '.status == "command-failed" and .exit_code == 9' "$work/failed/gpu-monitor.json" >/dev/null
# shellcheck disable=SC2329
rocm-smi() { printf '{"card0":{"GPU use (%%)":"0"},"card1":{"GPU use (%%)":"0"}}\n'; }
ROCM_SDK_PROVIDER=arch
[[ $(ws_gpu_monitor_provider) == rocm-smi ]]
mkdir "$work/legacy"
ws_gpu_monitor_capture "$work/legacy"
jq -e '.provider == "rocm-smi" and .status == "observed"' "$work/legacy/gpu-monitor.json" >/dev/null
unset -f amd-smi rocm-smi
# Mask only tool discovery to make this unavailable case portable to AMD hosts.
command() {
  if [[ $* == '-v amd-smi' || $* == '-v rocm-smi' ]]; then return 1; fi
  builtin command "$@"
}
mkdir "$work/unavailable"
if ws_gpu_monitor_capture "$work/unavailable"; then exit 1; fi
jq -e '.status == "unavailable" and .exit_code == 127' "$work/unavailable/gpu-monitor.json" >/dev/null
printf 'AMD-SMI, legacy, malformed/error and unavailable monitoring fixtures passed; no GPU accessed\n'
