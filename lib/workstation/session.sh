#!/usr/bin/env bash
# Explicit, single-node maintenance command; not an always-running controller.
# Tests replace the OS and kubectl adapters. Never run this on a CI host.

ws_session_config_validate() {
  local key value
  for key in SESSION_CONTEXT SESSION_NODE SESSION_NAMESPACE SESSION_AI_DEPLOYMENT SESSION_GAME_DEPLOYMENT; do
    value="${!key:-}"
    [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$ ]] || ws_die "$key must explicitly identify the reviewed target"
  done
  case "${SESSION_ENVIRONMENT:-}" in dev | tst | int) ;; *) ws_die 'SESSION_ENVIRONMENT must explicitly classify a non-production target: dev, tst or int' ;; esac
  [[ "$SESSION_AI_DEPLOYMENT" != "$SESSION_GAME_DEPLOYMENT" ]] || ws_die 'AI and gaming deployments must differ'
  [[ "${SESSION_TIMEOUT_SECONDS:-300}" =~ ^[1-9][0-9]*$ ]] || ws_die 'SESSION_TIMEOUT_SECONDS must be positive'
  [[ "${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}" =~ ^[1-9][0-9]{0,4}$ ]] || ws_die 'SESSION_AI_STARTUP_TIMEOUT_SECONDS must be a bounded positive integer'
  case "${SESSION_STREAMING_PATH:-sunshine}" in sunshine | steam) ;; *) ws_die 'SESSION_STREAMING_PATH must be sunshine or steam' ;; esac
}

ws_session_rollout_timeout() {
  if [[ $1 == "$SESSION_AI_DEPLOYMENT" ]]; then
    printf '%ss\n' "${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}"
  else
    printf '%ss\n' "${SESSION_TIMEOUT_SECONDS:-300}"
  fi
}

ws_session_kubectl() {
  local -a identity=()
  [[ -z ${SESSION_KUBECONFIG:-} ]] || identity=(--kubeconfig "$SESSION_KUBECONFIG")
  command kubectl "${identity[@]}" --context "$SESSION_CONTEXT" --request-timeout=15s "$@"
}

# Optional installed Bridge contract v1. Without this root-owned policy the
# existing explicit state-directory interface remains available. With it, all
# callers must share the administrator's canonical lock and cluster identity.
ws_session_installed_policy() {
  local policy=/etc/workstation/session-policy.conf
  [[ -e "$policy" || -L "$policy" ]] || return 0
  [[ -f "$policy" && ! -L "$policy" && "$(stat -c '%u:%a' "$policy")" == 0:600 ]] ||
    ws_die 'installed session policy must be root-owned mode 0600'
  unset SESSION_STATE_DIRECTORY SESSION_KUBECONFIG
  common::load_config "$policy" SESSION_STATE_DIRECTORY SESSION_KUBECONFIG
  [[ ${SESSION_STATE_DIRECTORY:-} == /* && "$SESSION_STATE_DIRECTORY" != / && ! -L "$SESSION_STATE_DIRECTORY" ]] ||
    ws_die 'installed session policy requires an absolute canonical state directory'
  [[ ${SESSION_KUBECONFIG:-} == /* && -f "$SESSION_KUBECONFIG" && ! -L "$SESSION_KUBECONFIG" &&
    "$(stat -c '%u:%a' "$SESSION_KUBECONFIG")" == 0:600 ]] ||
    ws_die 'installed session policy requires an explicit root-owned mode 0600 Kubernetes identity'
}

ws_session_plan() {
  local mode="$1"
  ws_session_config_validate
  case "$mode" in ai | gaming | maintenance) ;; *) ws_die 'session mode must be ai, gaming or maintenance' ;; esac
  jq -n --arg mode "$mode" --arg context "$SESSION_CONTEXT" --arg node "$SESSION_NODE" \
    --arg namespace "$SESSION_NAMESPACE" --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" \
    '{status:"plan-only-no-cluster-contact",mode:$mode,context:$context,node:$node,namespace:$namespace,
      managed_deployments:[$ai,$game],
      policy:"Stop managed GPU workloads, verify pod exit and live DRM holders, then start only the selected deployment.",
      gates:["same-host boot identity","qualified immutable image and compatible security","resource budget and Guaranteed QoS",
        "no unmanaged GPU consumers","exclusive count allocation; no physical-card selector"],
      build_policy:"Cooperative managed-build inhibition during gaming and transitions; existing or unrelated builds are not stopped.",
      power_policy:"No temporary power changes; existing TuneD state remains unchanged."}'
}

ws_session_target_check() {
  local hardware="$1" node nodes boot container_status cpu_config
  if systemd-detect-virt --container --quiet; then container_status=0; else container_status=$?; fi
  [[ "$container_status" == 1 ]] || ws_die 'run session control on the host, not inside a container or an unknown process namespace'
  ws_detected_gpu_target "$hardware" >/dev/null
  jq -e '.policyName == "static"' /var/lib/kubelet/cpu_manager_state >/dev/null ||
    ws_die 'qualify the static CPU Manager setup before using exclusive workstation sessions'
  cpu_config=/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/90-workstation-cpu.conf
  [[ -f "$cpu_config" && ! -L "$cpu_config" && "$(stat -c '%u:%a' "$cpu_config")" == 0:600 ]] ||
    ws_die 'the reviewed root-owned CPU Manager drop-in is required'
  local setting
  for setting in 'cpuManagerPolicy: static' '  full-pcpus-only: "true"' '  strict-cpu-reservation: "true"' \
    'topologyManagerPolicy: restricted' 'topologyManagerScope: pod'; do
    grep -Fxq "$setting" "$cpu_config" || ws_die "CPU Manager configuration drift: missing $setting"
  done
  boot="$(ws_rocm_boot_id_read /proc/sys/kernel/random/boot_id)"
  [[ "$boot" == "$(ws_rocm_boot_id_read "$(dirname -- "$hardware")/boot-id.txt")" ]] || ws_die 'recollect hardware after reboot before session control'
  node="$(ws_session_kubectl get node "$SESSION_NODE" -o json)" || ws_die 'cannot inspect the explicit node'
  jq -e --arg boot "$boot" '.status.nodeInfo.bootID == $boot and
    any(.status.conditions[]; .type == "Ready" and .status == "True") and
    ((.status.allocatable["amd.com/gpu"]|tonumber) == 2)' <<<"$node" >/dev/null ||
    ws_die 'selected node is not this boot, Ready, or advertising exactly two GPUs'
  # Existing overlays use gfx labels rather than a node-name pin. Restrict the
  # command to their established single accelerator-node deployment model.
  nodes="$(ws_session_kubectl get nodes -o json)" || ws_die 'cannot check accelerator node scope'
  jq -e --arg node "$SESSION_NODE" '[.items[] | select(((.status.allocatable["amd.com/gpu"] // "0")|tonumber)>0) | .metadata.name] == [$node]' \
    <<<"$nodes" >/dev/null || ws_die 'session control requires exactly one accelerator node; pin and qualify multi-node placement separately'
  printf '%s\n' "$node"
}

ws_session_deployment() {
  ws_session_kubectl -n "$SESSION_NAMESPACE" get deployment "$1" -o json
}

# A Bridge lock owner can bind the exact root-qualified desired Pod template.
# Check it again on every candidate read; the final scale also uses that read's
# resourceVersion. Empty expectations retain the standalone CLI contract.
ws_session_template_check() {
  local deployment="$1" document="$2" expected='' canonical digest
  if [[ "$deployment" == "$SESSION_AI_DEPLOYMENT" ]]; then
    expected="${WORKSTATION_SESSION_AI_TEMPLATE_SHA256:-}"
  elif [[ "$deployment" == "$SESSION_GAME_DEPLOYMENT" ]]; then expected="${WORKSTATION_SESSION_GAME_TEMPLATE_SHA256:-}"; fi
  [[ -n "$expected" ]] || return 0
  [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || ws_die 'invalid inherited template identity'
  canonical="$(jq -cS '.spec.template' <<<"$document")" || ws_die 'cannot canonicalize deployment template'
  digest="$(printf '%s' "$canonical" | sha256sum | awk '{print $1}')"
  [[ "$digest" == "$expected" ]] || ws_die 'deployment template differs from the root-qualified Bridge operation'
}

ws_session_qualify() {
  local deployment="$1" node="$2" mode="$3" document namespace
  document="$(ws_session_deployment "$deployment")" || ws_die "cannot inspect Deployment/$deployment"
  ws_session_template_check "$deployment" "$document"
  # A digest alone is not qualification. Require a deliberate reviewed marker;
  # existing pending images fail here BEFORE stopping any running workload.
  jq -e '.metadata.annotations["workstation.ai/qualification"] == "qualified" and
    .spec.strategy.type == "Recreate" and
    all((.spec.template.spec.containers + (.spec.template.spec.initContainers // []))[];
      (.image | test("@sha256:[a-f0-9]{64}$")) and
      ((.securityContext.privileged // false) == false) and
      ((.securityContext.allowPrivilegeEscalation // false) == false) and
      ((.securityContext.capabilities.add // [])|length == 0)) and
    ((.spec.template.spec.hostPID // false) == false) and
    ((.spec.template.spec.hostIPC // false) == false) and
    ((.spec.template.spec.hostNetwork // false) == false) and
    all(.spec.template.spec.volumes[]?; has("hostPath")|not)' <<<"$document" >/dev/null ||
    ws_die "Deployment/$deployment is not qualified: require digest-pinned containers, reviewed qualification and baseline-compatible device/security access"
  namespace="$(ws_session_kubectl get namespace "$SESSION_NAMESPACE" -o json)" || ws_die 'cannot inspect Pod Security policy'
  jq -e '.metadata.labels["pod-security.kubernetes.io/enforce"] == "baseline" or .metadata.labels["pod-security.kubernetes.io/enforce"] == "restricted"' \
    <<<"$namespace" >/dev/null || ws_die 'session namespace must retain baseline or restricted Pod Security enforcement'
  ws_session_capacity_check "$document" "$node"
  if [[ "$mode" == gaming ]]; then
    jq -e --arg path "${SESSION_STREAMING_PATH:-sunshine}" '
      [.spec.template.spec.containers[].env[]? | select(has("value")) | {key:.name,value:.value}] | from_entries |
      .ENABLE_STEAM == "true" and .ENABLE_SUNSHINE == (if $path == "sunshine" then "true" else "false" end)
    ' <<<"$document" >/dev/null || ws_die 'gaming deployment does not select the configured alternative streaming path'
  fi
}

ws_session_capacity_check() {
  local document="$1" node="$2" pods managed smt
  pods="$(ws_session_kubectl get pods -A --field-selector "spec.nodeName=$SESSION_NODE" -o json)" || ws_die 'cannot inspect node workload budgets'
  managed="$(ws_session_managed_uids)" || ws_die 'cannot resolve managed Pod ownership'
  smt="$(ws_hardware_cpu_topology | jq -er '[.cpus[].thread_siblings|length]|unique|select(length==1 and .[0]>0)|.[0]')" ||
    ws_die 'uniform live SMT width is unknown; cannot validate whole-core requests'
  # Kubernetes scheduling requests, not apparent free RAM, determine fit.
  # Only this repository's explicit integer CPU and Mi/Gi memory quantities
  # are accepted for the selected Guaranteed workload; unknown units fail.
  jq -en --argjson d "$document" --argjson node "$node" --argjson pods "$pods" \
    --argjson managed "$managed" --argjson smt "$smt" '
    def cpu: tostring | if test("^[0-9]+m$") then rtrimstr("m")|tonumber/1000 else tonumber end;
    def mem: tostring | if test("^[0-9]+(Ki|Mi|Gi|Ti)?$") then . else error("unhandled memory quantity") end |
      capture("^(?<n>[0-9]+)(?<u>Ki|Mi|Gi|Ti)?$") as $q |
      ($q.n|tonumber) * ({Ki:1024,Mi:1048576,Gi:1073741824,Ti:1099511627776}[($q.u // "")] // 1);
    def request($key): .resources.requests[$key] // "0";
    def footprint($key;f):
      ([.containers[] | request($key)|f]|add // 0) as $app |
      ([.initContainers[]? | request($key)|f]|max // 0) as $init |
      ([$app,$init]|max) + ((.overhead[$key] // "0")|f);
    def managed: .metadata.uid as $uid | ($managed|index($uid)) != null;
    def supported: (has("resources")|not) and
      all(.initContainers[]?; .restartPolicy != "Always" and
        ((.resources.requests["amd.com/gpu"] // 0)|tonumber) == 0 and
        ((.resources.limits["amd.com/gpu"] // 0)|tonumber) == 0);
    $d.spec.template.spec as $spec |
    ([ $pods.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed") |
      select(managed|not) | .spec | footprint("cpu";cpu)]|add // 0) as $usedcpu |
    ([ $pods.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed") |
      select(managed|not) | .spec | footprint("memory";mem)]|add // 0) as $usedmem |
    ($spec|supported) and all($pods.items[] | select(.status.phase != "Succeeded" and .status.phase != "Failed"); .spec|supported) and
    all(($spec.containers + ($spec.initContainers // []))[];
      .resources.requests.cpu == .resources.limits.cpu and
      .resources.requests.memory == .resources.limits.memory and
      (.resources.requests.cpu|test("^[1-9][0-9]*$")) and
      ((.resources.requests.cpu|tonumber) % $smt == 0) and
      (.resources.requests.memory|test("^[1-9][0-9]*(Mi|Gi)$"))) and
    ([ $spec.containers[] | .resources.requests["amd.com/gpu"] // 0 | tonumber ]|add) >= 1 and
    ([ $spec.containers[] | .resources.requests["amd.com/gpu"] // 0 | tonumber ]|add) <= 2 and
    all($spec.containers[];
      (.resources.requests["amd.com/gpu"] // 0) == (.resources.limits["amd.com/gpu"] // 0)) and
    (($spec|footprint("cpu";cpu))+$usedcpu <= ($node.status.allocatable.cpu|cpu)) and
    (($spec|footprint("memory";mem))+$usedmem <= ($node.status.allocatable.memory|mem))
  ' >/dev/null || ws_die 'workload is not Guaranteed or cannot fit node CPU/RAM/GPU requests; unknown quantities also fail closed'
}

ws_session_managed_uids() {
  local ai game
  ai="$(ws_session_managed_pods "$SESSION_AI_DEPLOYMENT")" || return 1
  game="$(ws_session_managed_pods "$SESSION_GAME_DEPLOYMENT")" || return 1
  jq -n --argjson ai "$ai" --argjson game "$game" '($ai+$game)|map(.metadata.uid)'
}

ws_session_other_gpu_check() {
  local pods managed
  pods="$(ws_session_kubectl get pods -A -o json)" || ws_die 'cannot inspect pending and bound GPU consumers'
  managed="$(ws_session_managed_uids)" || ws_die 'cannot resolve managed Pod ownership'
  jq -e --argjson managed "$managed" 'all(.items[];
    ((.status.phase == "Succeeded" or .status.phase == "Failed") and .metadata.deletionTimestamp == null) or
    (.metadata.uid as $uid | ($managed|index($uid)) != null) or
    all((.spec.containers + (.spec.initContainers // []))[];
      ((.resources.requests["amd.com/gpu"] // 0)|tonumber) == 0 and
      ((.resources.limits["amd.com/gpu"] // 0)|tonumber) == 0))' <<<"$pods" >/dev/null ||
    ws_die 'an unmanaged pending or bound Pod requests GPUs; leave it untouched and resolve ownership before switching'
}

ws_session_managed_pods() {
  # Deployment names are operator-configured. Resolve the actual ReplicaSet
  # ownership UIDs instead of trusting a label or a generated-name prefix.
  local deployment="$1" uid replicasets pods
  uid="$(ws_session_deployment "$deployment" | jq -er '.metadata.uid')" || return 1
  replicasets="$(ws_session_kubectl -n "$SESSION_NAMESPACE" get replicasets -o json)" || return 1
  pods="$(ws_session_kubectl -n "$SESSION_NAMESPACE" get pods -o json)" || return 1
  jq -n --arg uid "$uid" --argjson rs "$replicasets" --argjson pods "$pods" '
    [$rs.items[] | select(any(.metadata.ownerReferences[]?; .uid == $uid and .kind == "Deployment")) | .metadata.uid] as $ids |
    [$pods.items[] | select(any(.metadata.ownerReferences[]?; .kind == "ReplicaSet" and (.uid as $id | $ids | index($id)) != null))]'
}

ws_session_stop() {
  local deployment="$1" count deadline pods document
  document="$(ws_session_checked_deployment "$deployment")"
  count="$(jq -er '.spec.replicas' <<<"$document")" || ws_die 'cannot read current replica count'
  if ((count != 0)); then
    ws_session_kubectl -n "$SESSION_NAMESPACE" scale "deployment/$deployment" --resource-version="$(jq -r .metadata.resourceVersion <<<"$document")" --current-replicas="$count" --replicas=0 >/dev/null ||
      ws_die "concurrent change or failure stopping $deployment"
  fi
  deadline=$((SECONDS + ${SESSION_TIMEOUT_SECONDS:-300}))
  while :; do
    pods="$(ws_session_managed_pods "$deployment")" || ws_die 'cannot verify managed Pod termination'
    [[ "$(jq length <<<"$pods")" == 0 ]] && break
    ((SECONDS < deadline)) || ws_die "timed out waiting for $deployment Pods to terminate; GPUs are not handed over"
    sleep 1
  done
}

ws_session_gpu_free() {
  local hardware="$1" pods bdf path device number fd pid found=0 current expected
  pods="$(ws_session_kubectl get pods -A -o json)" || ws_die 'cannot verify GPU allocations, including pending consumers'
  jq -e 'all(.items[];
    ((.status.phase == "Succeeded" or .status.phase == "Failed") and .metadata.deletionTimestamp == null) or
    all((.spec.containers + (.spec.initContainers // []))[];
      ((.resources.requests["amd.com/gpu"] // 0)|tonumber) == 0 and
      ((.resources.limits["amd.com/gpu"] // 0)|tonumber) == 0))' <<<"$pods" >/dev/null ||
    ws_die 'a Pod still requests GPUs on this node; do not start the next session'
  current="$(ws_pci_amd_gpus)"
  expected="$(jq -c '.pci_gpus|map({bdf,device_id,driver})|sort_by(.bdf)' "$hardware")"
  [[ "$expected" == "$(jq -c 'map({bdf,device_id,driver})|sort_by(.bdf)' <<<"$current")" ]] ||
    ws_die 'current PCI GPU identities differ from the hardware report'
  local -a device_numbers=()
  while IFS= read -r bdf; do
    path="/sys/bus/pci/devices/$bdf/drm"
    [[ -d "$path" ]] || ws_die 'GPU DRM mapping is unavailable'
    found=0
    for device in "$path"/card[0-9]* "$path"/renderD[0-9]*; do
      [[ -e "$device" ]] || continue
      device="/dev/dri/${device##*/}"
      [[ -c "$device" ]] || ws_die 'resolved DRM device is not a character device'
      device_numbers+=("$(stat -Lc '%t:%T' -- "$device")")
      found=1
    done
    ((found == 1)) || ws_die 'GPU has no resolvable DRM device'
  done < <(jq -r '.[].bdf' <<<"$current")
  # Root plus a complete procfs view is required. Compare device numbers so
  # container paths and renamed render nodes cannot evade the holder check.
  local proc_options
  proc_options="$(findmnt -n -o OPTIONS /proc)" || ws_die 'cannot inspect procfs visibility'
  [[ ! "$proc_options" =~ hidepid=([^0,]|0[^,]) ]] || ws_die 'procfs hidepid prevents a complete GPU holder observation'
  for pid in /proc/[0-9]*; do
    [[ -d "$pid" ]] || continue
    [[ -r "$pid/fd" && -x "$pid/fd" ]] || ws_die 'cannot enumerate process descriptors; GPU release is unknown'
    for fd in "$pid"/fd/*; do
      [[ -L "$fd" ]] || continue
      number="$(stat -Lc '%F:%t:%T' -- "$fd" 2>/dev/null)" || {
        [[ ! -L "$fd" ]] && continue
        ws_die 'cannot inspect a live process descriptor'
      }
      [[ "$number" == 'character special file:'* ]] || continue
      number="${number#character special file:}"
      for device in "${device_numbers[@]}"; do
        [[ "$number" != "$device" ]] || ws_die "GPU still has an open DRM descriptor (PID ${pid##*/}); handover refused"
      done
    done
  done
}

ws_session_checked_deployment() {
  local deployment="$1" document expected
  document="$(ws_session_deployment "$deployment")" || ws_die 'cannot inspect deployment identity'
  ws_session_template_check "$deployment" "$document"
  expected="$SESSION_AI_UID"
  [[ "$deployment" != "$SESSION_GAME_DEPLOYMENT" ]] || expected="$SESSION_GAME_UID"
  [[ "$(jq -r .metadata.uid <<<"$document")" == "$expected" ]] || ws_die 'deployment was replaced; refuse to mutate a different UID'
  printf '%s\n' "$document"
}

ws_session_build_gate() {
  local action="$1" directory=/run/workstation
  # The inherited lock owner commits its own durable result before releasing
  # inhibition. It can perform several fixed actions in one transition.
  [[ "$action" != release || -z ${WORKSTATION_SESSION_LOCK_FD:-} ]] || return 0
  [[ ! -L "$directory" && ! -L "$directory/build-inhibit" ]] || ws_die 'refusing symlinked build gate'
  if [[ ! -d "$directory" ]]; then mkdir -m 0755 -- "$directory"; fi
  [[ "$(stat -c '%u:%a' "$directory")" == 0:755 ]] || ws_die 'build gate directory must be root-owned mode 0755'
  case "$action" in
    inhibit) (
      umask 022
      printf 'managed builds inhibited during gaming or an incomplete transition\n' >"$directory/build-inhibit"
    ) ;;
    release) if [[ -f "$directory/build-inhibit" ]]; then rm -- "$directory/build-inhibit"; fi ;;
    *) ws_die 'invalid build gate action' ;;
  esac
}

ws_session_state_write() {
  local dir="$1" json="$2" temporary
  temporary="$(mktemp "$dir/.state.XXXXXX")"
  printf '%s\n' "$json" >"$temporary"
  chmod 0600 "$temporary"
  sync -f "$temporary"
  mv -f -- "$temporary" "$dir/state.json"
  sync -f "$dir"
}

ws_session_switch() (
  local mode="$1" hardware="$2" dir="$3" execute="$4" node state ai game desired previous_phase
  ws_require_arch
  ws_require_root
  ws_session_config_validate
  ws_build_config_validate
  [[ "$execute" == --execute ]] || ws_die 'session changes require the explicit --execute argument'
  case "$mode" in ai | gaming | maintenance | restore) ;; *) ws_die 'invalid session mode' ;; esac
  ws_session_installed_policy
  [[ -z ${SESSION_STATE_DIRECTORY:-} || "$dir" == "$SESSION_STATE_DIRECTORY" ]] ||
    ws_die 'state directory differs from the installed canonical session policy'
  command -v flock >/dev/null || ws_die 'util-linux flock is required to serialize transitions'
  [[ "$dir" == /* && "$dir" != / && ! -L "$dir" ]] || ws_die 'state directory must be an absolute non-symlink path'
  (
    umask 077
    mkdir -p -- "$dir"
  )
  [[ "$(stat -c '%u:%a' "$dir")" == 0:700 ]] || ws_die 'state directory must be root-owned mode 0700'
  [[ ! -L "$dir/lock" && ! -L "$dir/state.json" ]] || ws_die 'refusing symlinked session state'
  if [[ -n ${WORKSTATION_SESSION_LOCK_FD:-} ]]; then
    [[ "$WORKSTATION_SESSION_LOCK_FD" == 3 && -n ${SESSION_STATE_DIRECTORY:-} &&
      "$(stat -Lc '%d:%i' /proc/self/fd/3)" == "$(stat -Lc '%d:%i' "$dir/lock")" ]] ||
      ws_die 'inherited session lock does not identify the canonical lock'
    exec 9<&3
  else
    exec 9>"$dir/lock"
  fi
  flock -n 9 || ws_die 'another session transition is in progress'
  node="$(ws_session_target_check "$hardware")"
  if [[ -f "$dir/state.json" ]]; then
    state="$(<"$dir/state.json")"
    jq -e --arg context "$SESSION_CONTEXT" --arg node "$SESSION_NODE" --arg ns "$SESSION_NAMESPACE" \
      --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" '
      .schema_version == 1 and .context == $context and .node == $node and .namespace == $ns and
      .ai == $ai and .game == $game and (.ai_uid|type == "string") and (.game_uid|type == "string") and
      ([.previous.ai,.previous.game]|all(. == 0 or . == 1)) and (.previous.ai+.previous.game <= 1)' \
      <<<"$state" >/dev/null || ws_die 'session state belongs to a different target or is invalid'
  else
    [[ "$mode" != restore ]] || ws_die 'no saved session to restore'
    ai="$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -er '.spec.replicas')"
    game="$(ws_session_deployment "$SESSION_GAME_DEPLOYMENT" | jq -er '.spec.replicas')"
    ((ai <= 1 && game <= 1 && ai + game <= 1)) || ws_die 'initial state must have at most one managed GPU deployment active; concurrent placement is not yet qualified'
    state="$(jq -n --arg context "$SESSION_CONTEXT" --arg node "$SESSION_NODE" --arg ns "$SESSION_NAMESPACE" \
      --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" --argjson ac "$ai" --argjson gc "$game" \
      --arg ai_uid "$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -er .metadata.uid)" \
      --arg game_uid "$(ws_session_deployment "$SESSION_GAME_DEPLOYMENT" | jq -er .metadata.uid)" \
      '{schema_version:1,context:$context,node:$node,namespace:$ns,ai:$ai,game:$game,ai_uid:$ai_uid,game_uid:$game_uid,
        previous:{ai:$ac,game:$gc},mode:"baseline",phase:"ready"}')"
  fi
  SESSION_AI_UID="$(jq -r .ai_uid <<<"$state")"
  SESSION_GAME_UID="$(jq -r .game_uid <<<"$state")"
  ws_session_checked_deployment "$SESSION_AI_DEPLOYMENT" >/dev/null
  ws_session_checked_deployment "$SESSION_GAME_DEPLOYMENT" >/dev/null
  ws_session_other_gpu_check
  desired=''
  case "$mode" in
    ai) desired="$SESSION_AI_DEPLOYMENT" ;;
    gaming) desired="$SESSION_GAME_DEPLOYMENT" ;;
    restore)
      if [[ "$(jq -r .previous.ai <<<"$state")" == 1 ]]; then
        desired="$SESSION_AI_DEPLOYMENT"
      elif [[ "$(jq -r .previous.game <<<"$state")" == 1 ]]; then desired="$SESSION_GAME_DEPLOYMENT"; fi
      ;;
  esac
  if [[ -n "$desired" ]]; then
    local qualification_mode=ai
    [[ "$desired" != "$SESSION_GAME_DEPLOYMENT" ]] || qualification_mode=gaming
    ws_session_qualify "$desired" "$node" "$qualification_mode"
  fi
  previous_phase="$(jq -r .phase <<<"$state")"
  if [[ "$previous_phase" == ready && "$(jq -r .mode <<<"$state")" == "$mode" ]]; then
    local actual_ai actual_game
    actual_ai="$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -r .spec.replicas)"
    actual_game="$(ws_session_deployment "$SESSION_GAME_DEPLOYMENT" | jq -r .spec.replicas)"
    if [[ "$actual_ai" == "$([[ "$desired" == "$SESSION_AI_DEPLOYMENT" ]] && printf 1 || printf 0)" &&
    "$actual_game" == "$([[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] && printf 1 || printf 0)" ]]; then
      [[ "$desired" == "$SESSION_AI_DEPLOYMENT" ]] || ws_session_stop "$SESSION_AI_DEPLOYMENT"
      [[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] || ws_session_stop "$SESSION_GAME_DEPLOYMENT"
      [[ -n "$desired" ]] || ws_session_gpu_free "$hardware"
      [[ -z "$desired" ]] || ws_session_kubectl -n "$SESSION_NAMESPACE" rollout status "deployment/$desired" --timeout="$(ws_session_rollout_timeout "$desired")" >/dev/null
      if [[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]]; then ws_session_build_gate inhibit; else ws_session_build_gate release; fi
      ws_note "session already $mode; no replica changes"
      exit 0
    fi
  fi
  state="$(jq --arg mode "$mode" '.mode=$mode | .phase="transition"' <<<"$state")"
  ws_session_build_gate inhibit
  ws_session_state_write "$dir" "$state"
  # On failure/cancellation retain the original snapshot and inhibit new
  # managed builds. Recovery is explicit; never restart AI over an uncertain GPU.
  trap 'ws_session_state_write "$dir" "$(jq '\''.phase="failed"'\'' <<< "$state")"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  ws_session_stop "$SESSION_AI_DEPLOYMENT"
  ws_session_stop "$SESSION_GAME_DEPLOYMENT"
  ws_session_gpu_free "$hardware"
  if [[ -n "$desired" ]]; then
    ws_session_qualify "$desired" "$node" "$qualification_mode"
    local candidate
    candidate="$(ws_session_checked_deployment "$desired")"
    ws_session_gpu_free "$hardware"
    ws_session_kubectl -n "$SESSION_NAMESPACE" scale "deployment/$desired" --resource-version="$(jq -r .metadata.resourceVersion <<<"$candidate")" --current-replicas=0 --replicas=1 >/dev/null ||
      ws_die 'selected deployment changed concurrently or failed to start'
    ws_session_kubectl -n "$SESSION_NAMESPACE" rollout status "deployment/$desired" --timeout="$(ws_session_rollout_timeout "$desired")" >/dev/null ||
      ws_die 'selected deployment did not become ready; session remains failed until explicit restore or retry'
  fi
  state="$(jq '.phase="ready"' <<<"$state")"
  ws_session_state_write "$dir" "$state"
  [[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] || ws_session_build_gate release
  trap - EXIT INT TERM
  ws_note "session $mode ready; physical release was a point-in-time DRM observation, not protection from unrelated host processes"
)
