#!/usr/bin/env bash
# Fixture/mocked control-path tests only. Never contacts a cluster or /dev/dri.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
SESSION_CONTEXT=fixture-context
SESSION_NODE=fixture-node
SESSION_NAMESPACE='ai-home-lab'
SESSION_AI_DEPLOYMENT=sglang
SESSION_GAME_DEPLOYMENT='parent-steam-headless'
SESSION_TIMEOUT_SECONDS=1
SESSION_STREAMING_PATH=sunshine
export SESSION_CONTEXT SESSION_NODE SESSION_NAMESPACE SESSION_AI_DEPLOYMENT SESSION_GAME_DEPLOYMENT SESSION_TIMEOUT_SECONDS SESSION_STREAMING_PATH
mkdir "$work/api"
fixture_node='{"status":{"allocatable":{"cpu":"42","memory":"48234496Ki"}}}'
printf '{"items":[]}\n' >"$work/api/pods.json"
printf '{"items":[]}\n' >"$work/api/rs.json"
for name in "$SESSION_AI_DEPLOYMENT" "$SESSION_GAME_DEPLOYMENT"; do
  jq -n --arg name "$name" '{metadata:{name:$name,uid:($name+"-uid"),resourceVersion:"7",annotations:{"workstation.ai/qualification":"qualified"}},
    spec:{replicas:0,strategy:{type:"Recreate"},template:{spec:{containers:[{name:"workload",
      image:("registry.invalid/fixture@sha256:"+("a"*64)),
      env:[{name:"ENABLE_STEAM",value:"true"},{name:"ENABLE_SUNSHINE",value:"true"}],
      securityContext:{privileged:false,allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
      resources:{requests:{cpu:"20",memory:"32Gi"},limits:{cpu:"20",memory:"32Gi"},claims:[{name:"gpu-0"}]}}],
      resourceClaims:[{name:"gpu-0",resourceClaimName:"gpu-0"}],
      volumes:[]}}}}' >"$work/api/$name.json"
done
set_document() {
  local file="$1" filter="$2"
  jq "$filter" "$file" >"$work/document.tmp"
  mv "$work/document.tmp" "$file"
}
set_document "$work/api/sglang.json" '.spec.replicas=1'
ws_require_arch() { :; }
ws_require_root() { :; }
ws_session_target_check() { printf '%s\n' "$fixture_node"; }
ws_hardware_cpu_topology() { printf '{"cpus":[{"cpu":0,"thread_siblings":[0,24]},{"cpu":24,"thread_siblings":[0,24]}]}\n'; }
ws_session_deployment() { command jq . "$work/api/$1.json"; }
stat() {
  if [[ ${1:-} == -c && ${2:-} == '%u:%a' ]]; then printf '0:700\n'; else command stat "$@"; fi
}
flock() { [[ ! -e "$work/locked" ]]; }
sync() { :; } # macOS fixture run: Linux sync -f durability is target-qualified.
ws_session_build_gate() {
  if [[ "$1" == inhibit ]]; then touch "$work/build-inhibit"; else rm -f "$work/build-inhibit"; fi
}
ws_session_kubectl() {
  if [[ ${1:-} == -n ]]; then shift 2; fi
  case "$1:${2:-}" in
    get:namespace) printf '{"metadata":{"labels":{"pod-security.kubernetes.io/enforce":"baseline"}}}\n' ;;
    get:pods) command jq . "$work/api/pods.json" ;;
    get:replicasets) command jq . "$work/api/rs.json" ;;
    scale:*)
      printf '%s\n' "$*" >>"$work/scale.log"
      [[ ! -f "$work/fail-scale" ]] || return 1
      local name="${2#deployment/}" requested='' current='' version='' arg
      for arg in "$@"; do
        case "$arg" in --replicas=*) requested="${arg#*=}" ;; --current-replicas=*) current="${arg#*=}" ;; --resource-version=*) version="${arg#*=}" ;; esac
      done
      [[ "$current" == "$(jq -r .spec.replicas "$work/api/$name.json")" && "$version" == 7 ]] || return 1
      set_document "$work/api/$name.json" ".spec.replicas=$requested"
      ;;
    rollout:status) [[ ! -e "$work/fail-rollout" && ! -e "$work/fail-rollout-${3#deployment/}" ]] ;;
    *)
      printf 'unexpected mock API command: %s\n' "$*" >&2
      return 2
      ;;
  esac
}
expect_failure() {
  if (
    set -e
    "$@"
  ) >"$work/failure.log" 2>&1; then
    printf 'expected rejection: %s\n' "$*" >&2
    exit 1
  fi
}

# Real budget predicate: default overhead 0, Ki node memory and Gi request.
expected_template="$(ws_session_deployment sglang | jq -cS .spec.template)"
expected_template_sha256="$(printf '%s' "$expected_template" | shasum -a 256 | awk '{print $1}')"
sha256sum() { shasum -a 256 "$@"; }
WORKSTATION_SESSION_AI_TEMPLATE_SHA256="$expected_template_sha256" ws_session_template_check sglang "$(ws_session_deployment sglang)"
WORKSTATION_SESSION_AI_TEMPLATE_SHA256="$expected_template_sha256" expect_failure ws_session_template_check sglang "$(ws_session_deployment sglang | jq '.spec.template.spec.containers[0].image="tampered:latest"')"
ws_session_capacity_check "$(ws_session_deployment sglang)" "$fixture_node"
with_overhead="$(ws_session_deployment sglang | jq '.spec.template.spec.overhead={cpu:"100m",memory:"1Gi"}')"
ws_session_capacity_check "$with_overhead" "$fixture_node"
expect_failure ws_session_capacity_check "$with_overhead" '{"status":{"allocatable":{"cpu":"20","memory":"32Gi"}}}'
odd="$(ws_session_deployment sglang | jq '.spec.template.spec.containers[0].resources.requests.cpu="19" | .spec.template.spec.containers[0].resources.limits.cpu="19"')"
expect_failure ws_session_capacity_check "$odd" "$fixture_node"
pod_level="$(ws_session_deployment sglang | jq '.spec.template.spec.resources={requests:{cpu:"2"}}')"
expect_failure ws_session_capacity_check "$pod_level" "$fixture_node"
sidecar="$(ws_session_deployment sglang | jq '.spec.template.spec.initContainers=[(.spec.template.spec.containers[0] + {restartPolicy:"Always"})]')"
expect_failure ws_session_capacity_check "$sidecar" "$fixture_node"
# GPUs come from DRA claims gpu-N: two cards pass; none, three, an unknown claim
# or the retired device-plugin resource fail closed.
tp2="$(ws_session_deployment sglang | jq '.spec.template.spec.resourceClaims+=[{name:"gpu-1",resourceClaimName:"gpu-1"}] | .spec.template.spec.containers[0].resources.claims+=[{name:"gpu-1"}]')"
ws_session_capacity_check "$tp2" "$fixture_node"
expect_failure ws_session_capacity_check "$(jq '.spec.template.spec.resourceClaims=[] | .spec.template.spec.containers[0].resources.claims=[]' <<<"$tp2")" "$fixture_node"
expect_failure ws_session_capacity_check "$(jq '.spec.template.spec.resourceClaims+=[{name:"gpu-2",resourceClaimName:"gpu-2"}]' <<<"$tp2")" "$fixture_node"
expect_failure ws_session_capacity_check "$(ws_session_deployment sglang | jq '.spec.template.spec.resourceClaims+=[{name:"other",resourceClaimName:"foreign-claim"}]')" "$fixture_node"
expect_failure ws_session_capacity_check "$(ws_session_deployment sglang | jq '.spec.template.spec.containers[0].resources.requests["amd.com/gpu"]=1 | .spec.template.spec.containers[0].resources.limits["amd.com/gpu"]=1')" "$fixture_node"

# Exact UID chain: a deceptive generated-name prefix is not managed ownership.
printf '{"items":[{"metadata":{"uid":"rs-real","ownerReferences":[{"uid":"sglang-uid","kind":"Deployment"}]}}]}\n' >"$work/api/rs.json"
printf '{"items":[{"metadata":{"uid":"pod-real","ownerReferences":[{"uid":"rs-real","kind":"ReplicaSet"}]}},{"metadata":{"uid":"pod-foreign","ownerReferences":[{"uid":"rs-foreign","name":"sglang-deceptive","kind":"ReplicaSet"}]}}]}\n' >"$work/api/pods.json"
[[ "$(ws_session_managed_pods sglang | jq -c 'map(.metadata.uid)')" == '["pod-real"]' ]]
# Pending/unbound consumers are checked globally before any mutation.
printf '{"items":[{"metadata":{"uid":"pending-unmanaged"},"status":{"phase":"Pending"},"spec":{"resourceClaims":[{"name":"gpu","resourceClaimName":"gpu-1"}],"containers":[{"resources":{"claims":[{"name":"gpu"}]}}]}}]}\n' >"$work/api/pods.json"
expect_failure ws_session_other_gpu_check
expect_failure ws_session_gpu_free "$work/unused-hardware.json"
# Any claim counts as GPU use (unknown claims too), as does the retired resource.
printf '{"items":[{"metadata":{"uid":"templated"},"status":{"phase":"Running"},"spec":{"resourceClaims":[{"name":"g","resourceClaimTemplateName":"any-gpu"}],"containers":[{}]}}]}\n' >"$work/api/pods.json"
expect_failure ws_session_other_gpu_check
printf '{"items":[{"metadata":{"uid":"legacy"},"status":{"phase":"Running"},"spec":{"containers":[{"resources":{"limits":{"amd.com/gpu":1}}}]}}]}\n' >"$work/api/pods.json"
expect_failure ws_session_other_gpu_check
expect_failure ws_session_gpu_free "$work/unused-hardware.json"
printf '{"items":[{"metadata":{"uid":"cpu-only"},"status":{"phase":"Running"},"spec":{"containers":[{"resources":{"requests":{"cpu":"1"}}}]}}]}\n' >"$work/api/pods.json"
ws_session_other_gpu_check
printf '{"items":[]}\n' >"$work/api/pods.json"
printf '{"items":[]}\n' >"$work/api/rs.json"

# A failed PCI enumeration must stop before /sys or /proc holder inspection.
# Force failure without output so this fixture cannot touch real GPU devices.
(
  ws_pci_amd_gpus() { printf '[{"bdf":"0000:01:00.0","device_id":"0x7550","driver":"amdgpu"}]\n'; }
  ws_pci_amd_gpus | jq '{pci_gpus:.}' >"$work/holder-hardware.json"
  # Called indirectly by the canonical ws_session_gpu_free implementation.
  # shellcheck disable=SC2329
  jq() {
    if [[ ${*: -1} == '.[].bdf' ]]; then return 42; fi
    command jq "$@"
  }
  # shellcheck disable=SC2329
  findmnt() {
    printf 'holder probe reached\n' >"$work/holder-probe"
    return 1
  }
  expect_failure ws_session_gpu_free "$work/holder-hardware.json"
  if [[ -e $work/holder-probe ]]; then
    printf 'failed PCI enumeration reached the holder probe\n' >&2
    exit 1
  fi
)

# From here only the host holder probe is mocked, including a failed release.
ws_session_gpu_free() { [[ ! -e "$work/gpu-held" ]] || ws_die 'fixture GPU still held'; }
ws_session_plan gaming | jq -e '.status=="plan-only-no-cluster-contact"' >/dev/null
# A retired lifecycle label is ignored rather than gating the plan.
SESSION_ENVIRONMENT=prd ws_session_plan ai | jq -e '.status=="plan-only-no-cluster-contact"' >/dev/null
set_document "$work/api/parent-steam-headless.json" '.metadata.annotations["workstation.ai/qualification"]="pending"'
expect_failure ws_session_switch gaming "$work/fixture.json" "$work/state" --execute
[[ ! -e "$work/scale.log" && ! -e "$work/state/state.json" ]]
set_document "$work/api/parent-steam-headless.json" '.metadata.annotations["workstation.ai/qualification"]="qualified" | .spec.template.spec.containers[0].image="fixture:latest"'
expect_failure ws_session_switch gaming "$work/fixture.json" "$work/state" --execute
[[ ! -e "$work/scale.log" ]]
set_document "$work/api/parent-steam-headless.json" '.spec.template.spec.containers[0].image=("registry.invalid/fixture@sha256:"+("a"*64))'

ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
jq -e '.phase=="ready" and .mode=="maintenance" and .previous.ai==1' "$work/state/state.json" >/dev/null
[[ ! -e "$work/build-inhibit" ]]
ws_session_switch restore "$work/fixture.json" "$work/state" --execute
[[ "$(jq -r .spec.replicas "$work/api/sglang.json")" == 1 ]]
ws_session_switch ai "$work/fixture.json" "$work/state" --execute
count="$(wc -l <"$work/scale.log")"
touch "$work/build-inhibit"
ws_session_switch ai "$work/fixture.json" "$work/state" --execute
[[ "$count" == "$(wc -l <"$work/scale.log")" ]]
[[ ! -e "$work/build-inhibit" ]]

touch "$work/locked"
expect_failure ws_session_switch gaming "$work/fixture.json" "$work/state" --execute
rm "$work/locked"
[[ "$count" == "$(wc -l <"$work/scale.log")" ]]
touch "$work/gpu-held"
expect_failure ws_session_switch gaming "$work/fixture.json" "$work/state" --execute
jq -e '.phase=="failed" and .previous.ai==1' "$work/state/state.json" >/dev/null
[[ "$(jq -r .spec.replicas "$work/api/parent-steam-headless.json")" == 0 && -e "$work/build-inhibit" ]]
rm "$work/gpu-held"
ws_session_switch restore "$work/fixture.json" "$work/state" --execute
[[ ! -e "$work/build-inhibit" ]]
touch "$work/fail-rollout"
expect_failure ws_session_switch gaming "$work/fixture.json" "$work/state" --execute
jq -e '.phase=="failed"' "$work/state/state.json" >/dev/null
rm "$work/fail-rollout"
ws_session_switch restore "$work/fixture.json" "$work/state" --execute
[[ "$(jq -r .spec.replicas "$work/api/sglang.json")" == 1 && "$(jq -r .spec.replicas "$work/api/parent-steam-headless.json")" == 0 ]]
set_document "$work/api/sglang.json" '.metadata.uid="replaced-deployment"'
expect_failure ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
grep -q 'different UID' "$work/failure.log"

# Installed Bridge policy closes the caller-selected-directory race without
# contacting /etc or changing the original standalone behavior in fixtures.
ws_session_installed_policy() { SESSION_STATE_DIRECTORY="$work/state"; }
expect_failure ws_session_switch maintenance "$work/fixture.json" "$work/other-state" --execute
grep -q 'canonical session policy' "$work/failure.log"
WORKSTATION_SESSION_LOCK_FD=4 expect_failure ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
grep -q 'canonical lock' "$work/failure.log"
printf 'session fixture tests passed (no cluster or hardware execution)\n'

# Optional shared service uses the same lock, UID checks and failure journal.
set_document "$work/api/sglang.json" '.metadata.uid="sglang-uid"'
SESSION_SERVICE_ENABLED=true
SESSION_IMAGE_DEPLOYMENT=swarmui
SESSION_SERVICE_CONTROL_FILE=/fixture/gateway-control-key
cp "$work/api/parent-steam-headless.json" "$work/api/swarmui.json"
set_document "$work/api/swarmui.json" '.metadata.name="swarmui" | .metadata.uid="swarmui-uid" | .spec.replicas=0 | .spec.template.spec.containers[0].resources={requests:{cpu:"12",memory:"12Gi"},limits:{cpu:"12",memory:"12Gi"},claims:[{name:"gpu-1"}]} | .spec.template.spec.resourceClaims=[{name:"gpu-1",resourceClaimName:"gpu-1"}]'
# shellcheck disable=SC2329 # called indirectly by ws_session_switch
ws_session_service_model() { printf 'Qwen3.5-9B\n'; }
ws_session_service() { printf '%s\n' "$*" >>"$work/service-phases"; }
ws_session_switch split "$work/fixture.json" "$work/state" --execute
[[ $(jq -r .spec.replicas "$work/api/sglang.json") == 1 && $(jq -r .spec.replicas "$work/api/swarmui.json") == 1 ]]
grep -Fxq 'ready Qwen3.5-9B true' "$work/service-phases"
touch "$work/locked"
count=$(wc -l <"$work/scale.log")
expect_failure ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
[[ $(wc -l <"$work/scale.log") == "$count" ]]
rm "$work/locked"
touch "$work/fail-rollout"
expect_failure ws_session_switch split "$work/fixture.json" "$work/state" --execute
jq -e '.phase=="failed" and .image_uid=="swarmui-uid"' "$work/state/state.json" >/dev/null
[[ $(jq -r .spec.replicas "$work/api/swarmui.json") == 0 ]]
grep -Fxq unavailable "$work/service-phases"
rm "$work/fail-rollout"
ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
[[ $(jq -r .spec.replicas "$work/api/sglang.json") == 0 && $(jq -r .spec.replicas "$work/api/swarmui.json") == 0 ]]
# Called indirectly by ws_session_switch below.
# shellcheck disable=SC2329
ws_session_service_model() { printf 'Qwen3.8-27B-FP8\n'; }
count=$(wc -l <"$work/scale.log")
expect_failure ws_session_switch split "$work/fixture.json" "$work/state" --execute
[[ $(wc -l <"$work/scale.log") == "$count" ]]
# Split needs one distinct card each: both on gpu-0 is refused before any scale.
# shellcheck disable=SC2329 # called indirectly by ws_session_switch
ws_session_service_model() { printf 'Qwen3.5-9B\n'; }
cp "$work/api/swarmui.json" "$work/swarmui.bak"
set_document "$work/api/swarmui.json" '.spec.template.spec.resourceClaims=[{name:"gpu-0",resourceClaimName:"gpu-0"}] | .spec.template.spec.containers[0].resources.claims=[{name:"gpu-0"}]'
expect_failure ws_session_switch split "$work/fixture.json" "$work/state" --execute
[[ $(wc -l <"$work/scale.log") == "$count" ]]
mv "$work/swarmui.bak" "$work/api/swarmui.json"
printf 'shared service split/drain/failure fixture tests passed\n'

# Shared: the two-GPU model plus one image generator on each of its cards.
# shellcheck disable=SC2329
ws_session_service_model() { printf 'Qwen3.8-27B-MXFP4\n'; }
set_document "$work/api/sglang.json" '.spec.template.spec.resourceClaims=[{name:"gpu-0",resourceClaimName:"gpu-0"},{name:"gpu-1",resourceClaimName:"gpu-1"}] | .spec.template.spec.containers[0].resources={requests:{cpu:"16",memory:"14Gi"},limits:{cpu:"16",memory:"14Gi"},claims:[{name:"gpu-0"},{name:"gpu-1"}]}'
cp "$work/api/swarmui.json" "$work/api/comfyui-gpu0.json"
set_document "$work/api/comfyui-gpu0.json" '.metadata.name="comfyui-gpu0" | .metadata.uid="comfyui-gpu0-uid" | .spec.template.spec.containers[0].resources={requests:{cpu:"8",memory:"10Gi"},limits:{cpu:"8",memory:"10Gi"},claims:[{name:"gpu-0"}]} | .spec.template.spec.resourceClaims=[{name:"gpu-0",resourceClaimName:"gpu-0"}]'
count=$(wc -l <"$work/scale.log")
# Without the second image Deployment configured, shared is refused up front.
expect_failure ws_session_switch shared "$work/fixture.json" "$work/state" --execute
grep -q 'SESSION_IMAGE2_DEPLOYMENT' "$work/failure.log"
SESSION_IMAGE2_DEPLOYMENT=comfyui-gpu0
SESSION_IMAGE2_DEPLOYMENT=swarmui expect_failure ws_session_config_validate
# The one-GPU 9B profile cannot serve shared; nothing is scaled.
# shellcheck disable=SC2329 # called indirectly by ws_session_switch
ws_session_service_model() { printf 'Qwen3.5-9B\n'; }
expect_failure ws_session_switch shared "$work/fixture.json" "$work/state" --execute
grep -q 'two-GPU' "$work/failure.log"
# shellcheck disable=SC2329 # called indirectly by ws_session_switch
ws_session_service_model() { printf 'Qwen3.8-27B-MXFP4\n'; }
# Both image generators on one card is refused, as is a CPU budget the node cannot hold.
cp "$work/api/comfyui-gpu0.json" "$work/comfyui.bak"
set_document "$work/api/comfyui-gpu0.json" '.spec.template.spec.resourceClaims=[{name:"gpu-1",resourceClaimName:"gpu-1"}] | .spec.template.spec.containers[0].resources.claims=[{name:"gpu-1"}]'
expect_failure ws_session_switch shared "$work/fixture.json" "$work/state" --execute
grep -q 'shared device allocation' "$work/failure.log"
cp "$work/comfyui.bak" "$work/api/comfyui-gpu0.json"
set_document "$work/api/comfyui-gpu0.json" '.spec.template.spec.containers[0].resources.requests.cpu="16" | .spec.template.spec.containers[0].resources.limits.cpu="16"'
expect_failure ws_session_switch shared "$work/fixture.json" "$work/state" --execute
grep -q 'cannot fit' "$work/failure.log"
mv "$work/comfyui.bak" "$work/api/comfyui-gpu0.json"
[[ $(wc -l <"$work/scale.log") == "$count" ]]
# SGLang starts first, then each image generator; the gateway admits images last.
: >"$work/service-phases"
ws_session_switch shared "$work/fixture.json" "$work/state" --execute
for name in sglang swarmui comfyui-gpu0; do [[ $(jq -r .spec.replicas "$work/api/$name.json") == 1 ]]; done
[[ "$(tail -n 3 "$work/scale.log" | sed -E 's/^scale deployment\/([a-z0-9-]+).*/\1/' | paste -sd, -)" == sglang,swarmui,comfyui-gpu0 ]]
[[ "$(tail -n 1 "$work/service-phases")" == 'ready Qwen3.8-27B-MXFP4 true' ]]
jq -e '.phase=="ready" and .mode=="shared" and .image2=="comfyui-gpu0" and .image2_uid=="comfyui-gpu0-uid" and .previous.image2==0' "$work/state/state.json" >/dev/null
# Maintenance stops all three.
ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
for name in sglang swarmui comfyui-gpu0; do [[ $(jq -r .spec.replicas "$work/api/$name.json") == 0 ]]; done
# The second generator failing to start fails the session and closes the gateway
# (as for split); images are never admitted. Recovery is an explicit switch.
touch "$work/fail-rollout-comfyui-gpu0"
: >"$work/service-phases"
expect_failure ws_session_switch shared "$work/fixture.json" "$work/state" --execute
jq -e '.phase=="failed"' "$work/state/state.json" >/dev/null
[[ "$(tail -n 1 "$work/service-phases")" == unavailable ]] && ! grep -q '^ready' "$work/service-phases"
rm "$work/fail-rollout-comfyui-gpu0"
# Recorded ownership of the second generator cannot be silently dropped.
SESSION_IMAGE2_DEPLOYMENT='' expect_failure ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
grep -q 'second image ownership' "$work/failure.log"
ws_session_switch maintenance "$work/fixture.json" "$work/state" --execute
printf 'shared mode (two-GPU model plus two image generators) fixture tests passed\n'
