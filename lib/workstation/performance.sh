#!/usr/bin/env bash
# Explicit benchmarks only; no install-time load or automatic cluster changes.

ws_measure_python() { printf '%s\n' "${WORKSTATION_PYTHON:-python3}"; }

ws_measure_command() (
  local output=$1 seconds=$2
  shift 2
  exec "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/measurement.py" --output "$output" --timeout "$seconds" run -- "$@"
)

ws_serving_pod() {
  local pod=$1
  ws_session_config_validate
  [[ $pod =~ ^[a-z0-9][a-z0-9.-]*$ ]] || ws_die 'provide the exact SGLang pod name'
  ws_session_kubectl -n "$SESSION_NAMESPACE" get pod "$pod" -o json | jq -e '
    select(.metadata.deletionTimestamp == null and .status.phase == "Running") |
    . as $p | [.spec.containers[] | select(.name == "sglang")] | select(length == 1) | .[0] as $c |
    {name:$p.metadata.name,uid:$p.metadata.uid,node:$p.spec.nodeName,
     image:$c.image,image_id:([$p.status.containerStatuses[] | select(.name == "sglang") | .imageID][0]),
     started_at:([$p.status.containerStatuses[] | select(.name == "sglang") | .state.running.startedAt][0]),
     resources:$c.resources,shm:[$p.spec.volumes[]? | select(.name == "shm") | .emptyDir],
     ready:([$p.status.containerStatuses[] | select(.name == "sglang") | .ready][0])}'
}

ws_serving_evidence() (
  set -euo pipefail
  local pod=$1 output=$2 before after
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new evidence directory'
  before=$(ws_serving_pod "$pod") || ws_die 'cannot identify running SGLang pod'
  jq -e --arg node "$SESSION_NODE" '.node == $node and .ready and (.image|test("@sha256:[a-f0-9]{64}$"))' <<< "$before" >/dev/null \
    || ws_die 'pod must be Ready on the reviewed node with an immutable image'
  umask 077
  mkdir -- "$output"
  printf '%s\n' "$before" > "$output/pod.json"
  # No environment dump, credentials, Secret read, shell or host-path access.
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - \
    < "$(ws_repo_root)/tests/hardware/sglang-evidence.py" > "$output/runtime.json"
  jq -e '.schema == 1 and (.model_files|length > 0) and (.devices|length > 0)' "$output/runtime.json" >/dev/null
  jq -e --argjson pod "$before" '(.devices|length) == ($pod.resources.limits["amd.com/gpu"]|tonumber)
    and $pod.resources.requests == $pod.resources.limits' "$output/runtime.json" >/dev/null \
    || ws_die 'visible GPU count or Guaranteed resources do not match the declared pod allocation'
  after=$(ws_serving_pod "$pod")
  [[ $before == "$after" ]] || ws_die 'pod identity, resources or readiness changed during hashing'
  ws_note "private serving evidence retained in $output; no model was downloaded or server changed"
)

ws_serving_benchmark() (
  set -euo pipefail
  local workload=$1 evidence=$2 output=$3 pod before after port_dir port_pid port attempt boot
  local -a identity=()
  ws_require_arch; ws_require_user
  [[ -f $evidence/pod.json ]] || ws_die 'collect serving-evidence first'
  pod=$(jq -er .name "$evidence/pod.json")
  before=$(ws_serving_pod "$pod")
  [[ $(jq -cS . <<< "$before") == "$(jq -cS . "$evidence/pod.json")" ]] || ws_die 'serving evidence no longer describes the live pod'
  boot=$(ws_rocm_boot_id_read /proc/sys/kernel/random/boot_id)
  ws_session_kubectl get node "$SESSION_NODE" -o json | jq -e --arg boot "$boot" \
    '.status.nodeInfo.bootID == $boot and (.status.allocatable["amd.com/gpu"] == "2")' >/dev/null \
    || ws_die 'run serving measurements on the selected two-GPU node so host telemetry describes the workload'
  # Bind a fresh loopback port to this exact Pod, never an existing public route
  # or an operator tunnel whose destination cannot be verified here.
  port_dir=$(mktemp -d)
  [[ -z ${SESSION_KUBECONFIG:-} ]] || identity=(--kubeconfig "$SESSION_KUBECONFIG")
  setsid kubectl "${identity[@]}" --context "$SESSION_CONTEXT" -n "$SESSION_NAMESPACE" \
    port-forward --address=127.0.0.1 "pod/$pod" :30000 > "$port_dir/forward.txt" 2>&1 &
  port_pid=$!
  trap 'kill -TERM -- "-$port_pid" 2>/dev/null || true; wait "$port_pid" 2>/dev/null || true; rm -f -- "$port_dir/forward.txt" "$port_dir/pod-before.json"; rmdir -- "$port_dir"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  port=''
  for ((attempt=0; attempt<30; attempt++)); do
    kill -0 "$port_pid" 2>/dev/null || ws_die 'private pod port-forward failed'
    port=$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\) -> 30000$/\1/p' "$port_dir/forward.txt")
    [[ -z $port ]] || break
    sleep 1
  done
  [[ $port =~ ^[1-9][0-9]{0,4}$ ]] || ws_die 'private pod port-forward did not become ready'
  export AGENT_BASE_URL="http://127.0.0.1:$port/v1"
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - snapshot \
    < "$(ws_repo_root)/lib/workstation/measurement.py" > "$port_dir/pod-before.json"
  timeout --kill-after=5s 21600 "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/serving.py" "$workload" "$evidence" "$output" \
    || ws_die 'serving benchmark failed; no successful measurement record'
  cp -- "$port_dir/pod-before.json" "$output/pod-resources-before.json"
  ws_hardware_collect "$output/hardware"
  ws_detected_gpu_target "$output/hardware/hardware.json" >/dev/null
  [[ $boot == "$(ws_rocm_boot_id_read "$output/hardware/boot-id.txt")" ]] || ws_die 'host boot changed during serving benchmark'
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - snapshot \
    < "$(ws_repo_root)/lib/workstation/measurement.py" > "$output/pod-resources-after.json"
  # Re-hash the actual serving mount after the run, not just the old local record.
  ws_serving_evidence "$pod" "$output/after"
  [[ $(jq -cS . "$output/after/pod.json") == "$(jq -cS . "$evidence/pod.json")" ]] || ws_die 'serving pod restarted or changed during benchmark'
  after=$(jq -cS . "$output/after/runtime.json")
  if [[ $after != "$(jq -cS . "$evidence/runtime.json")" ]]; then
    jq '.status="failed-provenance-drift"' "$output/result.json" > "$output/result.failed.json"
    mv -- "$output/result.failed.json" "$output/result.json"
    ws_die 'model/runtime provenance changed during serving benchmark'
  fi
  jq '.status="measured-not-qualified"' "$output/result.json" > "$output/result.verified.json"
  mv -- "$output/result.verified.json" "$output/result.json"
)

ws_serving_startup() (
  set -euo pipefail
  local pod=$1 cache=$2 output=$3 before current deadline
  case $cache in cold|warm) ;; *) ws_die 'label the owner-prepared model/JIT cache state cold or warm' ;; esac
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new startup evidence directory'
  before=$(ws_serving_pod "$pod")
  jq -e '.ready == false and .started_at != null' <<< "$before" >/dev/null \
    || ws_die 'observe after container start but before readiness; already-ready is not a startup measurement'
  umask 077; mkdir -- "$output"
  printf '%s\n' "$before" > "$output/pod-before.json"
  deadline=$((SECONDS + ${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}))
  while :; do
    current=$(ws_serving_pod "$pod")
    [[ $(jq -r '[.uid,.started_at,.image_id]|join("/")' <<< "$current") == "$(jq -r '[.uid,.started_at,.image_id]|join("/")' <<< "$before")" ]] \
      || ws_die 'pod restarted during startup observation'
    [[ $(jq -r .ready <<< "$current") != true ]] || break
    ((SECONDS < deadline)) || ws_die 'startup timed out; no successful startup record'
    sleep 1
  done
  jq -n --argjson pod "$current" --arg cache "$cache" --arg at "$(date -u +%FT%TZ)" \
    '{schema:1,status:"observed-not-qualified",pod:$pod,cache_state:$cache,
      cache_state_evidence:"operator-declared; use an empty dedicated JIT cache for cold, retain it for warm",
      started_at:$pod.started_at,ready_observed_at:$at,
      elapsed_seconds:(($at|fromdateiso8601)-($pod.started_at|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)),
      scope:"container start through readiness (load/JIT/warmup combined); not image pull or pure model-load time",
      uncertainty:"API polling and node/client wall-clock skew; compare only synchronized clocks"}' > "$output/startup.json"
)

ws_rocm_validate_pod() (
  set -euo pipefail
  local pod=$1 output=$2 program before
  ws_serving_evidence "$pod" "$output"
  before=$(< "$output/pod.json")
  jq -e '.devices|length == 2' "$output/runtime.json" >/dev/null || ws_die 'IPC/collective comparison requires both GPUs allocated to this pod'
  for program in hip-peer-copy hip-ipc; do
    # Source enters stdin, not a mounted host tree. Only a newly-created private
    # /tmp binary is compiled/removed. No package installation or driver change.
    # shellcheck disable=SC2016 # Variables belong to the explicit container shell.
    ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- bash -c '
      set -euo pipefail
      task_dir=$(mktemp -d /tmp/workstation-hip-check.XXXXXXXX)
      trap '\''rm -f -- "$task_dir/check"; rmdir -- "$task_dir"'\'' EXIT
      hipcc -O2 --offload-arch=gfx1201 -x c++ - -o "$task_dir/check"
      timeout 180 "$task_dir/check"
    ' < "$(ws_repo_root)/tests/hardware/$program.cpp" > "$output/$program.txt" 2> "$output/$program-stderr.txt"
  done
  # Spawn-based PyTorch workers need a real file, not Python's stdin pseudo-path.
  # shellcheck disable=SC2016 # Variables belong to the explicit container shell.
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- bash -c '
    set -euo pipefail
    task_dir=$(mktemp -d /tmp/workstation-rccl-check.XXXXXXXX)
    trap '\''rm -f -- "$task_dir/check.py"; rmdir -- "$task_dir"'\'' EXIT
    cat > "$task_dir/check.py"
    PYTHONDONTWRITEBYTECODE=1 NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,GRAPH,P2P,SHM,NET NCCL_DEBUG_FILE=/dev/stderr \
      timeout 300 python3 "$task_dir/check.py" --count 2 --model R9700 --collective
  ' < "$(ws_repo_root)/tests/hardware/torch-rocm.py" > "$output/rccl.json" 2> "$output/rccl-transport.txt"
  jq -e '(.collectives|length == 2) and (.tests|length > 0)' "$output/rccl.json" >/dev/null
  [[ $before == "$(ws_serving_pod "$pod")" ]] || ws_die 'pod changed during GPU diagnostics'
  jq -n '{schema:1,status:"correctness-passed-transport-unqualified",scope:"actual selected K3s pod; not host execution"}' > "$output/pod-validation.json"
)

ws_performance_tuned_lock() {
  local directory=/run/workstation-performance
  [[ ! -L $directory ]] || ws_die 'TuneD lock directory must not be a symlink'
  if [[ ! -e $directory ]]; then (umask 077; mkdir -- "$directory"); fi
  [[ -d $directory && $(stat -c '%u:%a' "$directory") == 0:700 ]] || ws_die 'TuneD lock directory must be root-owned mode 0700'
  [[ ! -L $directory/tuned.lock ]] || ws_die 'TuneD lock must not be a symlink'
  exec 8>"$directory/tuned.lock"
  flock -n 8 || ws_die 'another TuneD comparison is running'
}

ws_performance_tuned() (
  set -euo pipefail
  local profile=$1 output=$2 previous measure_pid='' status=0
  shift 2
  [[ ${1:-} == -- && $# -ge 2 ]] || ws_die 'usage: performance tuned <profile> <new-output> -- <benchmark-command> [arguments]'
  shift
  ws_require_arch; ws_require_root
  case $profile in balanced|accelerator-performance|throughput-performance) ;; *) ws_die 'unsupported comparison TuneD profile' ;; esac
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new experiment directory'
  ws_performance_tuned_lock
  previous=$(tuned-adm active | sed -n 's/^Current active profile: //p')
  [[ $previous =~ ^[a-zA-Z0-9_-]+$ ]] || ws_die 'cannot identify a single active TuneD profile to restore'
  tuned-adm verify || ws_die 'current TuneD policy has drift; restore or document manual overrides first'
  (umask 077; mkdir -- "$output")
  printf '%s\n' "$previous" > "$output/previous-tuned-profile.txt"
  trap '[[ -z $measure_pid ]] || { kill -TERM "$measure_pid" 2>/dev/null || true; wait "$measure_pid" 2>/dev/null || true; }; tuned-adm profile "$previous" && tuned-adm verify || { printf "FAILED to restore TuneD profile %s\n" "$previous" >&2; exit 1; }' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  tuned-adm profile "$profile" || exit 1
  tuned-adm verify || exit 1
  ws_measure_command "$output/run" 86400 "$@" &
  measure_pid=$!
  wait "$measure_pid" || status=$?
  measure_pid=''
  exit "$status"
)

ws_performance_memory() (
  set -euo pipefail
  ws_require_arch; ws_require_user; ws_build_config_validate; ws_build_session_guard
  local workers=$1 mib=$2 output=$3 available
  [[ $workers =~ ^[1-9][0-9]?$ && $mib =~ ^[1-9][0-9]{1,3}$ ]] || ws_die 'workers and MiB must be bounded integers'
  ((workers <= 48 && workers <= $(nproc) && mib >= 16 && mib <= 1024)) || ws_die 'memory benchmark exceeds CPU or allocation limits'
  available=$(ws_available_memory_mib)
  ((3*mib + BUILD_RESERVE_MIB <= available)) || ws_die 'insufficient RAM after configured host/workload reserve'
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new memory benchmark directory'
  umask 077; mkdir -- "$output"
  c++ --version > "$output/compiler.txt"
  lscpu > "$output/cpu.txt"
  common::sha256_file "$(ws_repo_root)/tests/hardware/memory-bandwidth.cpp" > "$output/source.sha256"
  c++ -std=c++17 -O2 -march=native -pthread "$(ws_repo_root)/tests/hardware/memory-bandwidth.cpp" -o "$output/memory-bandwidth"
  common::sha256_file "$output/memory-bandwidth" > "$output/binary.sha256"
  ws_measure_command "$output/run" 300 "$output/memory-bandwidth" "$workers" "$mib"
  jq -e '.correctness == "passed" and (.seconds|length == 5)' "$output/run/stdout.txt" >/dev/null
)
