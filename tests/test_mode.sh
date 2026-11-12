#!/usr/bin/env bash
# `workstationctl mode`: render, checks and the session switch it hands over to.
# Fixture only: the cluster, the service renderer and the session switch are mocked.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

SESSION_CONTEXT='fixture-context'
SESSION_NODE='fixture-node'
SESSION_NAMESPACE='ai-home-lab'
SESSION_AI_DEPLOYMENT=sglang
SESSION_GAME_DEPLOYMENT='parent-steam-headless'
SESSION_TIMEOUT_SECONDS=1
SESSION_STREAMING_PATH=sunshine
SESSION_SERVICE_ENABLED=true
SESSION_IMAGE_DEPLOYMENT=swarmui
SESSION_IMAGE2_DEPLOYMENT='comfyui-gpu0'
SESSION_SERVICE_CONTROL_FILE=/fixture/gateway-control-key
MODE_STATE_DIRECTORY="$work/state"
MODE_RENDER_DIRECTORY="$work/modes"
printf '{}\n' >"$work/site.json"
MODE_SERVICE_SITE="$work/site.json"
printf '{}\n' >"$work/hardware.json"
image="registry.invalid/sglang@sha256:$(printf 'a%.0s' {1..64})"

ws_require_arch() { :; }
ws_require_root() { :; }
ws_session_installed_policy() { :; }
ws_mode_hardware() { printf '%s\n' "$work/hardware.json"; }
stat() {
  if [[ ${1:-} == -c && ${2:-} == '%u:%a' ]]; then printf '0:700\n'; else command stat "$@"; fi
}
# ws_mode runs in a subshell, so the fixture clock lives in a file.
printf '0' >"$work/clock"
date() {
  local tick
  tick=$(($(cat "$work/clock") + 1))
  printf '%s' "$tick" >"$work/clock"
  printf '20261112T1200%02dZ\n' "$tick"
}
curl() { printf '{"phase":"ready","model":"Qwen3.8-27B-MXFP4","images":true,"active":0}'; }
ws_session_switch() { printf '%s\n' "$*" >>"$work/switch.log"; }
ws_session_kubectl() {
  if [[ ${1:-} == -n ]]; then shift 2; fi
  case "$1:${2:-}:${3:-}" in
    get:deployment:sglang) jq -n --arg image "$(cat "$work/live-image")" '{spec: {replicas: 1, template: {spec: {containers: [{name: "sglang", image: $image}]}}}}' ;;
    get:deployments:*) jq -n '{items: [{metadata: {name: "sglang"}, spec: {replicas: 1}}, {metadata: {name: "litellm"}, spec: {replicas: 1}}]}' ;;
    get:persistentvolumeclaims:*) printf '{"items":[]}\n' ;;
    get:service:litellm) printf '{"spec":{"clusterIP":"10.0.0.9"}}\n' ;;
    *)
      printf 'unexpected mock API command: %s\n' "$*" >&2
      return 2
      ;;
  esac
}
printf '%s' "$image" >"$work/live-image"

# The renderer is mocked; service_mode.py runs for real.
cat >"$work/python" <<EOF
#!/usr/bin/env bash
if [[ \$1 == */service.py && \$2 == render ]]; then
  printf '%s\n' "\$*" >>"$work/render.log"
  mkdir -p "\$4"
  jq -n --arg status "\$(cat "$work/contract-status")" '{status: \$status, contract: "fixture.UQS",
    selection: {arm: "UQS"}, budgets: {context_length: 81888, max_total_tokens: 163840}}' >"\$4/serving-contract.json"
  for name in sglang swarmui comfyui-gpu0 litellm; do
    printf -- '---\napiVersion: apps/v1\nkind: Deployment\nmetadata: {name: %s, namespace: ai-home-lab}\nspec: {replicas: 0}\n' "\$name"
  done >"\$4/workloads.yaml"
  exit 0
fi
exec python3 "\$@"
EOF
chmod 755 "$work/python"
ws_measure_python() { printf '%s\n' "$work/python"; }
printf 'active' >"$work/contract-status"
expect_failure() {
  if ("$@") >"$work/failure.log" 2>&1; then
    printf 'expected rejection: %s\n' "$*" >&2
    exit 1
  fi
}
latest_render() { find "$work/modes" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1; }

# family without --execute: renders and checks, never switches.
ws_mode family 2>/dev/null >/dev/null
[[ ! -e "$work/switch.log" ]]
grep -q -- '--mode shared --experience assistant-memory' "$work/render.log"
grep -q -- "--runtime-image $image" "$work/render.log"
render="$(latest_render)"
jq -e '.session == "shared" and .contract.arm == "UQS" and .replicas.sglang == 0 and .replicas.litellm == 1' "$render/mode-plan.json" >/dev/null
jq -e '[.items[] | select(.kind == "Deployment") | {(.metadata.name): .spec.replicas}] | add ==
  {"sglang": 0, "swarmui": 0, "comfyui-gpu0": 0, "litellm": 1}' "$render/apply.json" >/dev/null

# family --execute: hands the prepared render to the shared session switch.
ws_mode family --execute 2>/dev/null >/dev/null
render="$(latest_render)"
[[ "$(tail -n 1 "$work/switch.log")" == "shared $work/hardware.json $work/state --execute $render/apply.json" ]]

# coder: the large render and the ai session.
ws_mode coder --execute 2>/dev/null >/dev/null
grep -q -- '--mode large' "$work/render.log"
[[ "$(tail -n 1 "$work/switch.log")" == "ai $work/hardware.json $work/state --execute $(latest_render)/apply.json" ]]

# gaming and maintenance switch the session without rendering, even with no site file.
renders=$(wc -l <"$work/render.log")
MODE_SERVICE_SITE='' ws_mode gaming --execute 2>/dev/null
[[ "$(tail -n 1 "$work/switch.log")" == "gaming $work/hardware.json $work/state --execute" ]]
ws_mode maintenance 2>/dev/null
[[ "$(tail -n 1 "$work/switch.log")" == "gaming $work/hardware.json $work/state --execute" ]]
[[ $(wc -l <"$work/render.log") == "$renders" ]]

# Refusals happen before any switch.
switches=$(wc -l <"$work/switch.log")
expect_failure ws_mode holiday --execute
grep -q 'family, coder, gaming or maintenance' "$work/failure.log"
expect_failure ws_mode family --now
printf 'provisional' >"$work/contract-status"
expect_failure ws_mode family --execute
grep -q 'not active' "$work/failure.log"
printf 'active' >"$work/contract-status"
MODE_SERVICE_SITE='' expect_failure ws_mode coder --execute
grep -q 'MODE_SERVICE_SITE' "$work/failure.log"
printf 'registry.invalid/sglang:latest' >"$work/live-image"
expect_failure ws_mode family --execute
grep -q 'not pinned by digest' "$work/failure.log"
printf '%s' "$image" >"$work/live-image"
mv "$work/hardware.json" "$work/hardware.moved"
expect_failure ws_mode family --execute
grep -q 'no hardware evidence for this boot' "$work/failure.log"
mv "$work/hardware.moved" "$work/hardware.json"
[[ $(wc -l <"$work/switch.log") == "$switches" ]]
printf 'mode command fixture tests passed (no cluster contact)\n'
