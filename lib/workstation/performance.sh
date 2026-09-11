#!/usr/bin/env bash
# Explicit benchmarks only; no install-time load or automatic cluster changes.

ws_measure_python() { printf '%s\n' "${WORKSTATION_PYTHON:-python3}"; }

ws_measure_command() (
  local output=$1 seconds=$2
  shift 2
  exec "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/measurement.py" --output "$output" --timeout "$seconds" run -- "$@"
)

ws_serving_pod() {
  local pod=$1 document launch_spec launch_spec_sha256
  ws_session_config_validate
  [[ $pod =~ ^[a-z0-9][a-z0-9.-]*$ ]] || ws_die 'provide the exact SGLang pod name'
  document=$(ws_session_kubectl -n "$SESSION_NAMESPACE" get pod "$pod" -o json) || return 1
  # Bind rendered launch settings without retaining environment values or
  # arguments: only the canonical JSON hash leaves this local function.
  launch_spec=$(jq -ceS '[.spec.containers[] | select(.name == "sglang")] | select(length == 1) | .[0] |
    {command:(.command//[]),args:(.args//[]),env:(.env//[]),envFrom:(.envFrom//[])}' <<<"$document") || return 1
  launch_spec_sha256=$(common::sha256_file <(printf '%s\n' "$launch_spec")) || return 1
  jq -e --arg launch_spec_sha256 "$launch_spec_sha256" '
    select(.metadata.deletionTimestamp == null and .status.phase == "Running") |
    . as $p | [.spec.containers[] | select(.name == "sglang")] | select(length == 1) | .[0] as $c |
    {name:$p.metadata.name,uid:$p.metadata.uid,node:$p.spec.nodeName,
     image:$c.image,image_id:([$p.status.containerStatuses[] | select(.name == "sglang") | .imageID][0]),
     container_id:([$p.status.containerStatuses[] | select(.name == "sglang") | .containerID][0]),
     restart_count:([$p.status.containerStatuses[] | select(.name == "sglang") | .restartCount][0]),
     launch_spec_sha256:$launch_spec_sha256,
     started_at:([$p.status.containerStatuses[] | select(.name == "sglang") | .state.running.startedAt][0]),
     resources:$c.resources,shm:[$p.spec.volumes[]? | select(.name == "shm") | .emptyDir],
     ready:([$p.status.containerStatuses[] | select(.name == "sglang") | .ready][0])}' <<<"$document"
}

ws_serving_evidence() (
  set -euo pipefail
  local pod=$1 output=$2 before after
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new evidence directory'
  before=$(ws_serving_pod "$pod") || ws_die 'cannot identify running SGLang pod'
  jq -e --arg node "$SESSION_NODE" '.node == $node and .ready and (.image|test("@sha256:[a-f0-9]{64}$"))' <<<"$before" >/dev/null ||
    ws_die 'pod must be Ready on the reviewed node with an immutable image'
  umask 077
  mkdir -- "$output"
  printf '%s\n' "$before" >"$output/pod.json"
  # No environment dump, credentials, Secret read, shell or host-path access.
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - \
    <"$(ws_repo_root)/tests/hardware/sglang-evidence.py" >"$output/runtime.json"
  jq -e '.schema == 1 and (.model_files|length > 0) and (.devices|length > 0)' "$output/runtime.json" >/dev/null
  jq -e --argjson pod "$before" '(.devices|length) == ($pod.resources.limits["amd.com/gpu"]|tonumber)
    and $pod.resources.requests == $pod.resources.limits' "$output/runtime.json" >/dev/null ||
    ws_die 'visible GPU count or Guaranteed resources do not match the declared pod allocation'
  after=$(ws_serving_pod "$pod")
  [[ $before == "$after" ]] || ws_die 'pod identity, resources or readiness changed during hashing'
  ws_note "private serving evidence retained in $output; no model was downloaded or server changed"
)

ws_serving_warm_status() (
  # This is a read-only status probe. It deliberately does not run the optional
  # representative warmup: the existing owner-run harness needs a non-root
  # tunnel, while the canonical session lock is root-owned. Do not trust an
  # environment FD or weaken the installed lock policy to bridge that gap.
  set -euo pipefail
  local pod=$1 warmup=$2 output=$3 current temporary evidence_dir warm_probe_status=0
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new warm-status output path'
  current="$(ws_serving_pod "$pod")" || ws_die 'cannot inspect the exact SGLang Pod'
  jq -e --arg node "$SESSION_NODE" '.node == $node and (.uid|type == "string") and
    (.image|test("@sha256:[a-f0-9]{64}$")) and (.image_id|type == "string" and length > 0) and
    (.container_id|type == "string" and length > 0) and (.restart_count|type == "number" and . >= 0)' \
    <<<"$current" >/dev/null || ws_die 'current SGLang Pod is outside the reviewed node scope or lacks process identity'
  temporary="$(mktemp -d)" || ws_die 'cannot create private warm-status workspace'
  trap 'warm_probe_status=$?; trap - EXIT; set +e
    rm -f -- "$temporary/current-pod.json" "$temporary/evidence/pod.json" "$temporary/evidence/runtime.json" "$temporary/evidence/compiler.json"
    rmdir -- "$temporary/evidence" 2>/dev/null || true
    rmdir -- "$temporary" 2>/dev/null || true
    exit "$warm_probe_status"' EXIT
  printf '%s\n' "$current" >"$temporary/current-pod.json"
  if [[ $(jq -r .ready <<<"$current") != true ]]; then
    "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/serving_runtime.py" pod-status \
      "$temporary/current-pod.json" "$output"
    exit 0
  fi
  evidence_dir="$temporary/evidence"
  if ! ws_kernel_evidence "$pod" "$evidence_dir"; then
    # A live Ready bit is not sufficient to reuse an old model/runtime/device
    # identity. Retain an explicit unknown state without exposing helper text.
    jq -n --argjson pod "$current" '{schema:1,kind:"sglang-warm-status",status:"unknown",
      kubernetes_readiness:"healthy",representative_warmup:"unknown",pod:{uid:$pod.uid,started_at:$pod.started_at,
      container_id:$pod.container_id,restart_count:$pod.restart_count,image:$pod.image,image_id:$pod.image_id},identity:null,
      reason:"fresh current model, runtime or allocated-device evidence is unavailable",
      scope:"read-only status; no representative warmup was started"}' >"$output"
    chmod 0600 "$output"
    exit 0
  fi
  [[ $(jq -cS . "$evidence_dir/pod.json") == "$(jq -cS . <<<"$current")" ]] || {
    jq -n --argjson pod "$current" '{schema:1,kind:"sglang-warm-status",status:"stale",
      kubernetes_readiness:"unknown",representative_warmup:"unknown",pod:{uid:$pod.uid,started_at:$pod.started_at,
      container_id:$pod.container_id,restart_count:$pod.restart_count,image:$pod.image,image_id:$pod.image_id},identity:null,
      reason:"Pod changed during current evidence collection",scope:"read-only status; no representative warmup was started"}' >"$output"
    chmod 0600 "$output"
    exit 0
  }
  "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/serving_runtime.py" warm-status \
    "$evidence_dir" "$warmup" "$output"
)

ws_performance_bundle_config() {
  # Older owner configuration files predate this optional cache planner. Keep
  # the conservative documented reserve unless policy deliberately overrides it.
  INFERENCE_CACHE_FREE_RESERVE_MIB=${INFERENCE_CACHE_FREE_RESERVE_MIB:-20480}
  [[ $INFERENCE_CACHE_FREE_RESERVE_MIB =~ ^[1-9][0-9]*$ ]] ||
    ws_die 'INFERENCE_CACHE_FREE_RESERVE_MIB must be a positive integer'
  [[ -z ${INFERENCE_CACHE_ROOT:-} ]] && return 0
  [[ ${INFERENCE_CACHE_ROOT:-} == /* && "$INFERENCE_CACHE_ROOT" != / && -d "$INFERENCE_CACHE_ROOT" && ! -L "$INFERENCE_CACHE_ROOT" ]] ||
    ws_die 'INFERENCE_CACHE_ROOT must be an existing absolute non-symlink directory from administrator configuration'
}

ws_performance_bundle() (
  set -euo pipefail
  local action=$1
  shift
  case "$action" in
    seal)
      (($# == 4)) || ws_die 'bundle seal requires directory, kind, target and source SHA-256'
      "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/performance_bundle.py" seal "$1" \
        --kind "$2" --target "$3" --source-revision "$4"
      ;;
    inspect)
      (($# == 2)) || ws_die 'bundle inspect requires directory and manifest SHA-256'
      "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/performance_bundle.py" inspect "$1" "$2"
      ;;
    export)
      (($# == 5)) || ws_die 'bundle export requires directory, manifest SHA-256, new output, target and source SHA-256'
      ws_performance_bundle_config
      local -a bundle_cache_args=()
      if [[ -n ${INFERENCE_CACHE_ROOT:-} ]]; then bundle_cache_args=(--cache-root "$INFERENCE_CACHE_ROOT"); fi
      "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/performance_bundle.py" run "$1" "$2" "$3" \
        --target "$4" --source-revision "$5" --owner "$(id -un)" \
        --cache-free-reserve-mib "$INFERENCE_CACHE_FREE_RESERVE_MIB" "${bundle_cache_args[@]}"
      ;;
    *) ws_die 'performance bundle action must be seal, inspect or export' ;;
  esac
)

ws_performance_export() (
  # The typed Bridge helper supplies only the sealed bundle identity and its
  # actor. Cache authority remains exclusively in administrator configuration.
  set -euo pipefail
  (($# == 9)) ||
    ws_die 'performance export requires input, manifest SHA-256, new output, --target, --source-revision and --owner'
  [[ $4 == --target && $6 == --source-revision && $8 == --owner ]] ||
    ws_die 'performance export requires input, manifest SHA-256, new output, --target, --source-revision and --owner'
  ws_performance_bundle_config
  local -a bundle_cache_args=()
  if [[ -n ${INFERENCE_CACHE_ROOT:-} ]]; then bundle_cache_args=(--cache-root "$INFERENCE_CACHE_ROOT"); fi
  "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/performance_bundle.py" run "$1" "$2" "$3" \
    --target "$5" --source-revision "$7" --owner "$9" \
    --cache-free-reserve-mib "$INFERENCE_CACHE_FREE_RESERVE_MIB" "${bundle_cache_args[@]}"
)

ws_serving_benchmark() (
  set -euo pipefail
  local workload=$1 evidence=$2 output=$3 mode=${4:-serving} pod before after port_dir port_pid='' measure_pid='' port attempt boot
  local -a identity=()
  case $mode in serving | kernel-warmup | kernel-profile) ;; *) ws_die 'unsupported serving measurement mode' ;; esac
  ws_require_arch
  ws_require_user
  [[ -f $evidence/pod.json ]] || ws_die 'collect serving-evidence first'
  pod=$(jq -er .name "$evidence/pod.json")
  before=$(ws_serving_pod "$pod")
  [[ $(jq -cS . <<<"$before") == "$(jq -cS . "$evidence/pod.json")" ]] || ws_die 'serving evidence no longer describes the live pod'
  boot=$(ws_rocm_boot_id_read /proc/sys/kernel/random/boot_id)
  ws_session_kubectl get node "$SESSION_NODE" -o json | jq -e --arg boot "$boot" \
    '.status.nodeInfo.bootID == $boot and (.status.allocatable["amd.com/gpu"] == "2")' >/dev/null ||
    ws_die 'run serving measurements on the selected two-GPU node so host telemetry describes the workload'
  # Bind a fresh loopback port to this exact Pod, never an existing public route
  # or an operator tunnel whose destination cannot be verified here.
  port_dir=$(mktemp -d)
  # Retain the primary exit/signal status. Remove only named files we created;
  # unexpected contents are retained and reported, never recursively removed.
  trap 'status=$?; trap - EXIT INT TERM; set +e
    if [[ -n $measure_pid ]]; then
      kill -TERM -- "-$measure_pid" 2>/dev/null
      wait "$measure_pid" 2>/dev/null
    fi
    if [[ -n $port_pid ]]; then
      kill -TERM -- "-$port_pid" 2>/dev/null
      wait "$port_pid" 2>/dev/null
    fi
    rm -f -- "$port_dir/forward.txt" "$port_dir/pod-before.json" "$port_dir/compiler-before.json"
    rmdir -- "$port_dir" || printf "WARNING: temporary directory retained: %s\n" "$port_dir" >&2
    exit "$status"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  [[ -z ${SESSION_KUBECONFIG:-} ]] || identity=(--kubeconfig "$SESSION_KUBECONFIG")
  setsid kubectl "${identity[@]}" --context "$SESSION_CONTEXT" -n "$SESSION_NAMESPACE" \
    port-forward --address=127.0.0.1 "pod/$pod" :30000 >"$port_dir/forward.txt" 2>&1 &
  port_pid=$!
  port=''
  for ((attempt = 0; attempt < 30; attempt++)); do
    kill -0 "$port_pid" 2>/dev/null || ws_die 'private pod port-forward failed'
    port=$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\) -> 30000$/\1/p' "$port_dir/forward.txt")
    [[ -z $port ]] || break
    sleep 1
  done
  [[ $port =~ ^[1-9][0-9]{0,4}$ ]] || ws_die 'private pod port-forward did not become ready'
  export AGENT_BASE_URL="http://127.0.0.1:$port/v1"
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - snapshot \
    <"$(ws_repo_root)/lib/workstation/measurement.py" >"$port_dir/pod-before.json"
  if [[ $mode == serving ]]; then
    setsid timeout --kill-after=5s 21600 "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/serving.py" "$workload" "$evidence" "$output" &
  else
    ws_kernel_probe "$pod" >"$port_dir/compiler-before.json"
    if [[ $mode == kernel-profile ]]; then
      ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - contract \
        <"$(ws_repo_root)/tests/hardware/sglang-profile-evidence.py" >/dev/null
    fi
    setsid timeout --kill-after=5s 21600 "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/kernel_run.py" \
      "$mode" "$workload" "$evidence" "$port_dir/compiler-before.json" "$output" &
  fi
  measure_pid=$!
  wait "$measure_pid" || exit "$?"
  measure_pid=''
  cp -- "$port_dir/pod-before.json" "$output/pod-resources-before.json"
  ws_hardware_collect "$output/hardware"
  ws_detected_gpu_target "$output/hardware/hardware.json" >/dev/null
  [[ $boot == "$(ws_rocm_boot_id_read "$output/hardware/boot-id.txt")" ]] || ws_die 'host boot changed during serving benchmark'
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - snapshot \
    <"$(ws_repo_root)/lib/workstation/measurement.py" >"$output/pod-resources-after.json"
  # Re-hash the actual serving mount after the run, not just the old local record.
  ws_serving_evidence "$pod" "$output/after"
  [[ $(jq -cS . "$output/after/pod.json") == "$(jq -cS . "$evidence/pod.json")" ]] || ws_die 'serving pod restarted or changed during benchmark'
  after=$(jq -cS . "$output/after/runtime.json")
  if [[ $after != "$(jq -cS . "$evidence/runtime.json")" ]]; then
    jq '.status="failed-provenance-drift"' "$output/result.json" >"$output/result.failed.json"
    mv -- "$output/result.failed.json" "$output/result.json"
    ws_die 'model/runtime provenance changed during serving benchmark'
  fi
  if [[ $mode != serving ]]; then
    ws_kernel_probe "$pod" >"$output/compiler-after.json"
    if [[ $mode == kernel-profile ]]; then
      ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$pod" -c sglang -- python3 - \
        "$(jq -er .profile_path "$output/result.json")" "$(jq -er '.settings.TENSOR_PARALLEL' "$evidence/runtime.json")" \
        <"$(ws_repo_root)/tests/hardware/sglang-profile-evidence.py" >"$output/profile-traces.json"
      if [[ $(jq -r .profile_stop "$output/result.json") == http-500-awaiting-verification ]]; then
        ws_session_kubectl -n "$SESSION_NAMESPACE" logs "$pod" -c sglang \
          --since-time="$(jq -er .profile_started_at "$output/result.json")" --tail=2000 --limit-bytes=262144 |
          "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/kernel_run.py" profile-log "$output"
      fi
    fi
    "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/kernel_run.py" finish "$output" ||
      ws_die 'kernel experiment memory/runtime check failed'
    [[ $(ws_serving_pod "$pod" | jq -cS .) == "$(jq -cS . "$evidence/pod.json")" ]] ||
      ws_die 'pod changed during final compiler observation; result remains unverified'
  fi
  jq '.status="measured-not-qualified"' "$output/result.json" >"$output/result.verified.json"
  mv -- "$output/result.verified.json" "$output/result.json"
)

ws_kernel_probe() {
  ws_session_kubectl -n "$SESSION_NAMESPACE" exec -i "$1" -c sglang -- python3 - \
    <"$(ws_repo_root)/tests/hardware/sglang-kernel-evidence.py"
}

ws_kernel_evidence() (
  set -euo pipefail
  local pod=$1 output=$2
  ws_serving_evidence "$pod" "$output"
  ws_kernel_probe "$pod" >"$output/compiler.json"
  [[ $(jq -cS . "$output/pod.json") == "$(ws_serving_pod "$pod" | jq -cS .)" ]] || ws_die 'pod changed during compiler evidence'
  ws_note 'Compiler/cache evidence retained; no compilation, restart or tuning performed'
)

ws_serving_startup() (
  set -euo pipefail
  local pod=$1 cache=$2 output=$3 mode=${4:-} before current deadline
  case $cache in cold | warm) ;; *) ws_die 'label the owner-prepared model/JIT cache state cold or warm' ;; esac
  case $mode in '' | --memory) ;; *) ws_die 'the optional startup mode is --memory' ;; esac
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new startup evidence directory'
  if [[ $mode == --memory ]]; then
    ws_serving_startup_memory "$pod" "$cache" "$output"
    exit "$?"
  fi
  before=$(ws_serving_pod "$pod")
  jq -e '.ready == false and .started_at != null' <<<"$before" >/dev/null ||
    ws_die 'observe after container start but before readiness; already-ready is not a startup measurement'
  umask 077
  mkdir -- "$output"
  printf '%s\n' "$before" >"$output/pod-before.json"
  deadline=$((SECONDS + ${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}))
  while :; do
    current=$(ws_serving_pod "$pod")
    [[ $(jq -cS '[.uid,.started_at,.image_id,.restart_count,.container_id]' <<<"$current") == "$(jq -cS '[.uid,.started_at,.image_id,.restart_count,.container_id]' <<<"$before")" ]] ||
      ws_die 'pod restarted during startup observation'
    [[ $(jq -r .ready <<<"$current") != true ]] || break
    ((SECONDS < deadline)) || ws_die 'startup timed out; no successful startup record'
    sleep 1
  done
  jq -n --argjson pod "$current" --arg cache "$cache" --arg at "$(date -u +%FT%TZ)" \
    '{schema:1,status:"observed-not-qualified",pod:$pod,cache_state:$cache,
      cache_state_evidence:"operator-declared; use an empty dedicated JIT cache for cold, retain it for warm",
      started_at:$pod.started_at,ready_observed_at:$at,
      elapsed_seconds:(($at|fromdateiso8601)-($pod.started_at|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)),
      scope:"container start through readiness (load/JIT/warmup combined); not image pull or pure model-load time",
      uncertainty:"API polling and node/client wall-clock skew; compare only synchronized clocks"}' >"$output/startup.json"
)

ws_serving_startup_memory() (
  set -euo pipefail
  local pod=$1 cache=$2 output=$3 before=null current=null category=probe complete=0
  local boot uid sample cgroup_id='' sample_id previous_tick=0 deadline observed_at ready_at='' interrupted_signal=null
  ws_require_arch
  ws_require_user
  umask 077
  mkdir -- "$output" || ws_die 'cannot create startup memory evidence directory'
  # Keep a failed record even when a probe or signal ends observation. Only
  # categories are retained; kubectl/Python error text may contain private data.
  # shellcheck disable=SC2329 # Invoked by the EXIT trap below.
  ws_serving_startup_memory_finish() {
    local exit_status=$? status=failed
    trap - EXIT INT TERM
    set +e
    if ((complete == 1 && exit_status == 0)); then
      status=observed-not-qualified
      category=""
    elif ((exit_status == 0)); then exit_status=1; fi
    jq -n --arg status "$status" --arg category "$category" --arg cache "$cache" \
      --argjson before "${before:-null}" --argjson pod "${current:-null}" \
      --arg at "$ready_at" --argjson signal "$interrupted_signal" \
      '{schema:1,status:$status,error_category:(if $category == "" then null else $category end),
        interrupted_signal:$signal,pod:$pod,pod_before:$before,cache_state:$cache,
        cache_state_evidence:"operator-declared; use an empty dedicated JIT cache for cold, retain it for warm",
        memory_telemetry:"memory.jsonl",started_at:$before.started_at,
        ready_observed_at:(if $at == "" then null else $at end),
        elapsed_seconds:(if $at == "" then null else
          (($at|fromdateiso8601)-($before.started_at|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)) end),
        scope:"container start through readiness (load/JIT/warmup combined); not image pull or pure model-load time",
        memory_scope:"samples from observer attachment before Ready through Ready; memory.peak is lifetime and is never reset",
        uncertainty:"API polling, sampled peaks and node/client wall-clock skew; compare only synchronized clocks"}' \
      >"$output/startup.json" || exit_status=1
    exit "$exit_status"
  }
  trap ws_serving_startup_memory_finish EXIT
  trap 'category=interrupted; interrupted_signal=2; exit 130' INT
  trap 'category=interrupted; interrupted_signal=15; exit 143' TERM
  before=$(ws_serving_pod "$pod" 2>/dev/null) || ws_die 'cannot probe startup pod'
  current=$before
  jq -e --arg node "$SESSION_NODE" '
    .ready == false and (.started_at|type == "string") and .node == $node
    and (.uid|type == "string") and (.image|test("@sha256:[a-f0-9]{64}$"))
    and (.container_id|type == "string" and length > 0)
    and (.image_id|type == "string" and length > 0)
    and (.restart_count|type == "number" and . >= 0 and . == floor)
    and (.started_at|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601|type == "number")' <<<"$before" >/dev/null ||
    ws_die 'observe a running immutable SGLang container on the selected node before Ready'
  printf '%s\n' "$before" >"$output/pod-before.json" || ws_die 'cannot retain startup pod identity'
  uid=$(jq -er .uid <<<"$before") || ws_die 'cannot identify startup pod UID'
  category=host-identity
  boot=$(ws_rocm_boot_id_read /proc/sys/kernel/random/boot_id) || ws_die 'cannot identify local boot'
  ws_session_kubectl get node "$SESSION_NODE" -o json 2>/dev/null | jq -e --arg boot "$boot" \
    '.status.nodeInfo.bootID == $boot and (.status.allocatable["amd.com/gpu"] == "2")' >/dev/null ||
    ws_die 'run startup memory observation on the selected two-GPU node'
  deadline=$((SECONDS + ${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}))
  while :; do
    category=telemetry
    sample=$("$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/measurement.py" pod-memory "$uid" 2>/dev/null) ||
      ws_die 'startup memory probe failed'
    jq -e --argjson previous "$previous_tick" '(.pod_cgroup.id|type == "string" and test("^[0-9]+:[0-9]+$"))
        and (.pod_cgroup.path|type == "string" and length > 0)
        and (.pod_cgroup.values["memory.current"]|type == "string" and test("^[0-9]+$"))
        and (.host_memory|type == "string" and length > 0)
        and (.monotonic_ns|type == "number" and . > $previous)
        and (.unix_ns|type == "number" and . > 0)' <<<"$sample" >/dev/null ||
      ws_die 'startup memory sample is incomplete'
    sample_id=$(jq -er .pod_cgroup.id <<<"$sample") || ws_die 'startup cgroup identity is missing'
    [[ -z $cgroup_id || $sample_id == "$cgroup_id" ]] || ws_die 'startup cgroup identity changed'
    cgroup_id=$sample_id
    previous_tick=$(jq -er .monotonic_ns <<<"$sample") || ws_die 'startup sample timestamp is missing'
    jq -c . <<<"$sample" >>"$output/memory.jsonl" || ws_die 'cannot retain startup memory sample'
    ((SECONDS < deadline)) || {
      category=timeout
      ws_die 'startup timed out'
    }
    [[ $(jq -r .ready <<<"$current") != true ]] || break
    category=probe
    current=$(ws_serving_pod "$pod" 2>/dev/null) || ws_die 'cannot probe startup pod'
    category='pod-identity'
    [[ $(jq -cS 'del(.ready)' <<<"$current") == "$(jq -cS 'del(.ready)' <<<"$before")" ]] ||
      ws_die 'pod restarted or changed during startup observation'
    if [[ $(jq -r .ready <<<"$current") != true ]]; then sleep 1; fi
  done
  category=host-identity
  [[ $boot == "$(ws_rocm_boot_id_read /proc/sys/kernel/random/boot_id)" ]] || ws_die 'local boot changed during startup'
  ws_session_kubectl get node "$SESSION_NODE" -o json 2>/dev/null | jq -e --arg boot "$boot" \
    '.status.nodeInfo.bootID == $boot' >/dev/null || ws_die 'selected node boot changed during startup'
  category=probe
  current=$(ws_serving_pod "$pod" 2>/dev/null) || ws_die 'cannot verify final startup pod'
  [[ $(jq -cS 'del(.ready)' <<<"$current") == "$(jq -cS 'del(.ready)' <<<"$before")" && $(jq -r .ready <<<"$current") == true ]] ||
    {
      category='pod-identity'
      ws_die 'pod changed after final startup memory sample'
    }
  category=clock
  observed_at=$(date -u +%FT%TZ) || ws_die 'cannot timestamp startup readiness'
  jq -en --arg at "$observed_at" --argjson before "$before" \
    '($at|fromdateiso8601) >= ($before.started_at|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)' >/dev/null ||
    ws_die 'startup timestamps are invalid or clocks are not synchronized'
  ((SECONDS < deadline)) || {
    category=timeout
    ws_die 'startup timed out during final verification'
  }
  ready_at=$observed_at
  complete=1
)

ws_rocm_validate_pod() (
  set -euo pipefail
  local pod=$1 output=$2 program before
  ws_serving_evidence "$pod" "$output"
  before=$(<"$output/pod.json")
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
    ' <"$(ws_repo_root)/tests/hardware/$program.cpp" >"$output/$program.txt" 2>"$output/$program-stderr.txt"
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
  ' <"$(ws_repo_root)/tests/hardware/torch-rocm.py" >"$output/rccl.json" 2>"$output/rccl-transport.txt"
  jq -e '(.collectives|length == 2) and (.tests|length > 0)' "$output/rccl.json" >/dev/null
  [[ $before == "$(ws_serving_pod "$pod")" ]] || ws_die 'pod changed during GPU diagnostics'
  jq -n '{schema:1,status:"correctness-passed-transport-unqualified",scope:"actual selected K3s pod; not host execution"}' >"$output/pod-validation.json"
)

ws_performance_tuned_lock() {
  local directory=/run/workstation-performance
  [[ ! -L $directory ]] || ws_die 'TuneD lock directory must not be a symlink'
  if [[ ! -e $directory ]]; then (
    umask 077
    mkdir -- "$directory"
  ); fi
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
  ws_require_arch
  ws_require_root
  case $profile in balanced | accelerator-performance | throughput-performance) ;; *) ws_die 'unsupported comparison TuneD profile' ;; esac
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new experiment directory'
  ws_performance_tuned_lock
  previous=$(tuned-adm active | sed -n 's/^Current active profile: //p')
  [[ $previous =~ ^[a-zA-Z0-9_-]+$ ]] || ws_die 'cannot identify a single active TuneD profile to restore'
  tuned-adm verify || ws_die 'current TuneD policy has drift; restore or document manual overrides first'
  (
    umask 077
    mkdir -- "$output"
  )
  printf '%s\n' "$previous" >"$output/previous-tuned-profile.txt"
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
  ws_require_arch
  ws_require_user
  ws_build_config_validate
  ws_build_session_guard
  local workers=$1 mib=$2 output=$3 available
  [[ $workers =~ ^[1-9][0-9]?$ && $mib =~ ^[1-9][0-9]{1,3}$ ]] || ws_die 'workers and MiB must be bounded integers'
  ((workers <= 48 && workers <= $(nproc) && mib >= 16 && mib <= 1024)) || ws_die 'memory benchmark exceeds CPU or allocation limits'
  available=$(ws_available_memory_mib)
  ((3 * mib + BUILD_RESERVE_MIB <= available)) || ws_die 'insufficient RAM after configured host/workload reserve'
  [[ ! -e $output && ! -L $output ]] || ws_die 'use a new memory benchmark directory'
  umask 077
  mkdir -- "$output"
  c++ --version >"$output/compiler.txt"
  lscpu >"$output/cpu.txt"
  common::sha256_file "$(ws_repo_root)/tests/hardware/memory-bandwidth.cpp" >"$output/source.sha256"
  c++ -std=c++17 -O2 -march=native -pthread "$(ws_repo_root)/tests/hardware/memory-bandwidth.cpp" -o "$output/memory-bandwidth"
  common::sha256_file "$output/memory-bandwidth" >"$output/binary.sha256"
  ws_measure_command "$output/run" 300 "$output/memory-bandwidth" "$workers" "$mib"
  jq -e '.correctness == "passed" and (.seconds|length == 5)' "$output/run/stdout.txt" >/dev/null
)
