#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${HOME_LAB_PYTHON:-} ]]; then
  printf 'SKIP: set HOME_LAB_PYTHON for standard-library measurement fixtures\n'
  exit 0
fi
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_measurement.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_llama_runtime.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_model_kernels.py"
PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/test_serving_orchestration.py"
for suite in test_performance_profiles.py test_performance_bundle.py test_coding_eval.py test_serving_runtime.py test_inference_cache.py; do
  PYTHONDONTWRITEBYTECODE=1 "$HOME_LAB_PYTHON" "$root/tests/hardware/$suite"
done
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
SESSION_AI_DEPLOYMENT=sglang
[[ $(ws_session_rollout_timeout sglang) == 2100s ]]
[[ $(ws_session_rollout_timeout gaming) == 300s ]]
SESSION_AI_STARTUP_TIMEOUT_SECONDS=2400
SESSION_TIMEOUT_SECONDS=45
[[ $(ws_session_rollout_timeout sglang) == 2400s ]]
[[ $(ws_session_rollout_timeout gaming) == 45s ]]

# Function mocks only: never invoke TuneD, acquire host locks or tune this Mac.
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
ws_session_config_validate() { :; }
SESSION_NAMESPACE=fixture
ws_session_kubectl() {
  jq -n --arg variant "${LAUNCH_VARIANT:-original}" '
    {metadata:{name:"fixture",uid:"11111111-2222-3333-4444-555555555555"},
      spec:{nodeName:"reviewed",containers:[{name:"sglang",image:"fixture",
        args:["--model-path","/fixture"],env:[{name:"FIXTURE_PRIVATE",value:"synthetic-private-value"}],
        envFrom:[{configMapRef:{name:$variant}}]}]},
      status:{phase:"Running",containerStatuses:[{name:"sglang",imageID:"fixture-image",containerID:"fixture-container",
        restartCount:3,ready:false,state:{running:{startedAt:"2026-09-09T00:00:00Z"}}}]}}'
}
ws_serving_pod fixture >"$work/identity.json"
jq -e '.restart_count == 3 and .container_id == "fixture-container"
  and (.launch_spec_sha256|test("^[0-9a-f]{64}$"))' "$work/identity.json" >/dev/null
if grep -Fq 'synthetic-private-value' "$work/identity.json"; then
  printf 'launch fingerprint exposed a private environment value\n' >&2
  exit 1
fi
expected_hash=$(common::sha256_file <(ws_session_kubectl | jq -cS '.spec.containers[0] |
  {command:(.command//[]),args:(.args//[]),env:(.env//[]),envFrom:(.envFrom//[])}'))
[[ $(jq -r .launch_spec_sha256 "$work/identity.json") == "$expected_hash" ]]
[[ $(LAUNCH_VARIANT=changed ws_serving_pod fixture | jq -r .launch_spec_sha256) != "$expected_hash" ]]
ws_require_arch() { :; }
ws_require_root() { :; }
ws_performance_tuned_lock() { :; }
tuned-adm() {
  printf '%s\n' "$*" >>"$work/tuned.log"
  case $1 in
    active) printf 'Current active profile: balanced\n' ;;
    profile) [[ ${FAIL_RESTORE:-0} != 1 || $2 != balanced ]] ;;
    verify) return 0 ;;
  esac
}
ws_measure_command() { return "${MEASUREMENT_EXIT:-0}"; }
ws_performance_tuned accelerator-performance "$work/success" -- true
[[ $(tail -n 2 "$work/tuned.log" | head -n 1) == 'profile balanced' ]]
if MEASUREMENT_EXIT=7 ws_performance_tuned accelerator-performance "$work/failed" -- false; then
  printf 'failed benchmark incorrectly succeeded\n' >&2
  exit 1
fi
[[ $(tail -n 2 "$work/tuned.log" | head -n 1) == 'profile balanced' ]]
if FAIL_RESTORE=1 ws_performance_tuned accelerator-performance "$work/restore-failed" -- true 2>"$work/restore-error"; then
  printf 'failed profile restoration incorrectly succeeded\n' >&2
  exit 1
fi
grep -Fq 'FAILED to restore TuneD profile balanced' "$work/restore-error"

# The readiness observer never starts/restarts a Pod. Mock a container becoming
# Ready, then verify that already-ready and restarted observations are rejected.
ws_serving_pod() {
  local count=0 ready=true uid=fixture
  [[ ! -f $work/pod-count ]] || count=$(<"$work/pod-count")
  printf '%s\n' "$((count + 1))" >"$work/pod-count"
  if [[ $count == 0 && ${ALREADY_READY:-0} != 1 ]]; then ready=false; fi
  if [[ $count != 0 && ${RESTART_POD:-0} == 1 ]]; then uid=changed; fi
  jq -n --argjson ready "$ready" --arg uid "$uid" \
    '{ready:$ready,uid:$uid,started_at:"2026-09-09T00:00:00.123Z",image_id:"immutable-fixture"}'
}
date() { printf '2026-09-09T00:00:05Z\n'; }
ws_serving_startup fixture cold "$work/startup"
jq -e '.status == "observed-not-qualified" and .cache_state == "cold" and .elapsed_seconds >= 0' "$work/startup/startup.json" >/dev/null
if ALREADY_READY=1 ws_serving_startup fixture warm "$work/already-ready" 2>/dev/null; then
  printf 'already-ready Pod was accepted as startup evidence\n' >&2
  exit 1
fi
printf '0\n' >"$work/pod-count"
if RESTART_POD=1 ws_serving_startup fixture cold "$work/restarted" 2>/dev/null; then
  printf 'restarted Pod was accepted as startup evidence\n' >&2
  exit 1
fi
[[ ! -e $work/restarted/startup.json ]]

# Memory mode samples the exact UID on the local selected node, and preserves
# partial evidence as failed on every incomplete observation. All host/cluster
# boundaries are functions; no real node, cgroup or workload is touched.
SESSION_NODE=reviewed
ws_require_user() { :; }
ws_rocm_boot_id_read() { printf 'fixture-boot\n'; }
ws_session_kubectl() {
  [[ $* == 'get node reviewed -o json' ]] || return 1
  local boot=fixture-boot
  [[ ${STARTUP_CASE:-} != wrong-boot ]] || boot=other-boot
  jq -n --arg boot "$boot" '{status:{nodeInfo:{bootID:$boot},allocatable:{"amd.com/gpu":"2"}}}'
}
ws_serving_pod() {
  local count=0 ready=false uid=11111111-2222-3333-4444-555555555555 restart=0 container=fixture-container
  [[ ! -f $work/pod-count ]] || count=$(<"$work/pod-count")
  printf '%s\n' "$((count + 1))" >"$work/pod-count"
  [[ ${STARTUP_CASE:-} != probe-failure || $count == 0 ]] || return 1
  [[ ${STARTUP_CASE:-} != initial-probe-failure ]] || return 1
  if [[ ${STARTUP_CASE:-} == late-final-probe && $count == 2 ]]; then command sleep 2; fi
  if [[ $count != 0 ]]; then
    ready=true
    case ${STARTUP_CASE:-} in
      restart) restart=1 ;;
      container-change) container=changed-container ;;
      uid-change) uid=aaaaaaaa-2222-3333-4444-555555555555 ;;
      timeout | INT | TERM) ready=false ;;
    esac
  fi
  jq -n --argjson ready "$ready" --arg uid "$uid" --arg container "$container" --argjson restart "$restart" \
    '{ready:$ready,uid:$uid,node:"reviewed",started_at:"2026-09-09T00:00:00.123Z",
      image:("fixture@sha256:"+("a"*64)),image_id:"immutable-fixture",container_id:$container,restart_count:$restart}'
}
ws_measure_python() { printf 'fixture_memory_python\n'; }
date() {
  [[ ${STARTUP_CASE:-} != invalid-clock ]] || {
    printf 'invalid\n'
    return
  }
  printf '2026-09-09T00:00:05Z\n'
}
fixture_memory_python() {
  [[ $1 == "$root/lib/workstation/measurement.py" && $2 == pod-memory && $3 == 11111111-2222-3333-4444-555555555555 ]] || return 1
  local count=0 id=1:2 current=123
  [[ ! -f $work/memory-count ]] || count=$(<"$work/memory-count")
  printf '%s\n' "$((count + 1))" >"$work/memory-count"
  [[ ${STARTUP_CASE:-} != missing-cgroup ]] || return 1
  [[ ${STARTUP_CASE:-} != final-sample-failure || $count == 0 ]] || return 1
  if [[ ${STARTUP_CASE:-} == late-ready && $count == 1 ]]; then command sleep 2; fi
  if [[ $count != 0 && ${STARTUP_CASE:-} == changed-cgroup ]]; then id=1:3; fi
  [[ ${STARTUP_CASE:-} != invalid-current ]] || current=-1
  [[ ${STARTUP_CASE:-} != stale-sample ]] || count=0
  jq -n --arg id "$id" --arg current "$current" --argjson count "$count" \
    '{monotonic_ns:($count+1),unix_ns:($count+1),host_memory:"MemTotal: 1000 kB\nMemAvailable: 700 kB",
      pod_cgroup:{path:"/sys/fs/cgroup/pod-fixture",id:$id,scope:"lifetime peak",
        values:{"memory.current":$current,"memory.peak":"999","memory.events":"oom 0\noom_kill 0"}}}'
}
sleep() {
  case ${STARTUP_CASE:-} in INT | TERM) kill -s "$STARTUP_CASE" "$BASHPID" ;; *) : ;; esac
}
printf '0\n' >"$work/pod-count"
ws_serving_startup fixture cold "$work/memory-success" --memory
jq -e '.status == "observed-not-qualified" and .cache_state == "cold" and .memory_telemetry == "memory.jsonl"
  and .pod_before.ready == false and .pod.ready == true and .error_category == null' "$work/memory-success/startup.json" >/dev/null
jq -se 'length == 2 and all(.[]; .pod_cgroup.id == "1:2" and .pod_cgroup.values["memory.peak"] == "999")' "$work/memory-success/memory.jsonl" >/dev/null
for scenario in initial-probe-failure probe-failure missing-cgroup final-sample-failure invalid-current changed-cgroup stale-sample restart container-change uid-change timeout late-ready late-final-probe INT TERM wrong-boot invalid-clock; do
  printf '0\n' >"$work/pod-count"
  printf '0\n' >"$work/memory-count"
  timeout_seconds=30
  [[ $scenario != timeout ]] || timeout_seconds=0
  if [[ $scenario == late-ready || $scenario == late-final-probe ]]; then timeout_seconds=1; fi
  status=0
  STARTUP_CASE=$scenario SESSION_AI_STARTUP_TIMEOUT_SECONDS=$timeout_seconds \
    ws_serving_startup fixture warm "$work/memory-$scenario" --memory 2>"$work/memory-$scenario.stderr" || status=$?
  [[ $status != 0 ]] || {
    printf 'incomplete memory case succeeded: %s\n' "$scenario" >&2
    exit 1
  }
  jq -e '.status == "failed" and (.error_category|type == "string" and length > 0)
    and .memory_telemetry == "memory.jsonl" and .ready_observed_at == null' "$work/memory-$scenario/startup.json" >/dev/null
  if [[ $scenario == INT || $scenario == TERM ]]; then
    [[ $status == 130 && $scenario == INT || $status == 143 && $scenario == TERM ]]
    jq -e '.error_category == "interrupted" and (.interrupted_signal == 2 or .interrupted_signal == 15)' "$work/memory-$scenario/startup.json" >/dev/null
  fi
done
printf 'Performance helper and separate startup timeout fixtures passed\n'
