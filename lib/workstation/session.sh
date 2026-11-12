#!/usr/bin/env bash
# Explicit, single-node maintenance command; not an always-running controller.
# Tests replace the OS and kubectl adapters. Never run this on a CI host.

ws_session_config_validate() {
  local key value
  for key in SESSION_CONTEXT SESSION_NODE SESSION_NAMESPACE SESSION_AI_DEPLOYMENT SESSION_GAME_DEPLOYMENT; do
    value="${!key:-}"
    [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$ ]] || ws_die "$key must explicitly identify the reviewed target"
  done
  [[ "$SESSION_AI_DEPLOYMENT" != "$SESSION_GAME_DEPLOYMENT" ]] || ws_die 'AI and gaming deployments must differ'
  [[ "${SESSION_TIMEOUT_SECONDS:-300}" =~ ^[1-9][0-9]*$ ]] || ws_die 'SESSION_TIMEOUT_SECONDS must be positive'
  [[ "${SESSION_AI_STARTUP_TIMEOUT_SECONDS:-2100}" =~ ^[1-9][0-9]{0,4}$ ]] || ws_die 'SESSION_AI_STARTUP_TIMEOUT_SECONDS must be a bounded positive integer'
  case "${SESSION_SERVICE_ENABLED:-false}" in true | false) ;; *) ws_die 'SESSION_SERVICE_ENABLED must be true or false' ;; esac
  if [[ ${SESSION_SERVICE_ENABLED:-false} == true ]]; then
    [[ ${SESSION_IMAGE_DEPLOYMENT:-} =~ ^[a-z0-9][a-z0-9-]*$ && "$SESSION_IMAGE_DEPLOYMENT" != "$SESSION_AI_DEPLOYMENT" && "$SESSION_IMAGE_DEPLOYMENT" != "$SESSION_GAME_DEPLOYMENT" ]] || ws_die 'distinct image Deployment required'
    [[ ${SESSION_SERVICE_CONTROL_FILE:-} == /* ]] || ws_die 'explicit protected gateway control file required'
  fi
  # Shared mode's second image generator (the other card of the two-GPU model).
  if [[ -n ${SESSION_IMAGE2_DEPLOYMENT:-} ]]; then
    [[ ${SESSION_SERVICE_ENABLED:-false} == true && $SESSION_IMAGE2_DEPLOYMENT =~ ^[a-z0-9][a-z0-9-]*$ &&
      "$SESSION_IMAGE2_DEPLOYMENT" != "$SESSION_AI_DEPLOYMENT" && "$SESSION_IMAGE2_DEPLOYMENT" != "$SESSION_GAME_DEPLOYMENT" &&
      "$SESSION_IMAGE2_DEPLOYMENT" != "$SESSION_IMAGE_DEPLOYMENT" ]] ||
      ws_die 'a distinct second image Deployment requires the shared service configuration'
  fi
  case "${SESSION_STREAMING_PATH:-sunshine}" in sunshine | steam) ;; *) ws_die 'SESSION_STREAMING_PATH must be sunshine or steam' ;; esac
}

# Optional service integration reuses this command's lock and qualification.
ws_session_service() {
  [[ ${SESSION_SERVICE_ENABLED:-false} == true ]] || return 0
  local phase="$1" model="${2:-none}" address
  address="$(ws_session_kubectl -n "$SESSION_NAMESPACE" get service litellm -o json | jq -er .spec.clusterIP)" || ws_die 'cannot resolve internal gateway'
  local -a extra=()
  [[ ${3:-false} != true ]] || extra=(--images)
  "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/service_session.py" "$address" "$SESSION_SERVICE_CONTROL_FILE" "$phase" "$model" "${extra[@]}"
}

ws_session_service_model() {
  local document config model
  document="$(ws_session_candidate "$SESSION_AI_DEPLOYMENT")"
  config="$(jq -r '[.spec.template.spec.containers[].envFrom[]?.configMapRef.name | select(startswith("sglang-profile"))] | unique | if length==1 then .[0] elif length==0 then "" else error("several") end' <<<"$document")" || ws_die 'one authoritative model ConfigMap required'
  if [[ -n $config ]]; then
    model="$(ws_session_kubectl -n "$SESSION_NAMESPACE" get configmap "$config" -o json | jq -er .data.SERVED_MODEL_NAME)" || ws_die 'cannot read served model identity'
  else
    # Serving-contract renders carry the launch argv on the Deployment itself.
    model="$(jq -er '[.spec.template.spec.containers[] | select(.name=="sglang") | .args // [] | to_entries[] | select(.value=="--served-model-name") | .key] as $i | [.spec.template.spec.containers[] | select(.name=="sglang") | .args][0][$i[0] + 1]' <<<"$document")" || ws_die 'cannot read served model identity'
  fi
  case "$model" in Qwen3.8-27B-FP8 | Qwen3.5-9B | Qwen3.8-27B-MXFP4) ;; *) ws_die 'unqualified service model identity' ;; esac
  printf '%s\n' "$model"
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
  case "$mode" in ai | split | shared | gaming | maintenance) ;; *) ws_die 'session mode must be ai, split, shared, gaming or maintenance' ;; esac
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

# GPUs are allocated through Kubernetes DRA: the operator-applied standalone
# ResourceClaims gpu-0/gpu-1 (infrastructure/gpu-operator/claims.yaml). A pod
# spec uses a GPU when it references any ResourceClaim (unknown claims are
# treated as GPU use, conservatively) or still requests the retired device-plugin
# resource amd.com/gpu. Managed workloads must use only gpu-N claims.
WS_SESSION_GPU_JQ='
  def gpu_claims: [(.resourceClaims // [])[] | (.resourceClaimName // "") | select(test("^gpu-[0-9]+$"))] | unique;
  def legacy_gpu: any(((.containers // []) + (.initContainers // []))[];
    ((.resources.requests["amd.com/gpu"] // 0)|tonumber) > 0 or ((.resources.limits["amd.com/gpu"] // 0)|tonumber) > 0);
  def uses_gpu: ((.resourceClaims // [])|length) > 0 or legacy_gpu;'

ws_session_target_check() {
  local hardware="$1" node slices boot container_status cpu_config
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
    any(.status.conditions[]; .type == "Ready" and .status == "True")' <<<"$node" >/dev/null ||
    ws_die 'selected node is not this boot or not Ready'
  # GPUs are published by the DRA driver (gpu.amd.com) as ResourceSlices.
  # Existing overlays use gfx labels rather than a node-name pin. Restrict the
  # command to their established single accelerator-node deployment model.
  slices="$(ws_session_kubectl get resourceslices -o json)" || ws_die 'cannot inspect published GPUs'
  jq -e --arg node "$SESSION_NODE" '[.items[] | select(.spec.driver == "gpu.amd.com") | .spec.nodeName] | unique == [$node]' \
    <<<"$slices" >/dev/null || ws_die 'session control requires exactly one accelerator node; pin and qualify multi-node placement separately'
  jq -e --arg node "$SESSION_NODE" '[.items[] | select(.spec.driver == "gpu.amd.com" and .spec.nodeName == $node) | .spec.devices[]] | length == 2' \
    <<<"$slices" >/dev/null || ws_die 'selected node does not publish exactly two GPUs through the DRA driver'
  printf '%s\n' "$node"
}

ws_session_deployment() {
  ws_session_kubectl -n "$SESSION_NAMESPACE" get deployment "$1" -o json
}

# The game Deployment may not be installed yet: gaming is qualified separately. Print it,
# or nothing when it does not exist; an API failure is still an error, so a running game
# is never mistaken for an absent one.
ws_session_game_document() {
  ws_session_kubectl -n "$SESSION_NAMESPACE" get deployment "$SESSION_GAME_DEPLOYMENT" --ignore-not-found -o json
}

ws_session_game_present() { [[ ${SESSION_GAME_PRESENT:-true} == true ]]; }

# The Deployment a switch will run: from the render being applied (--apply) when it
# carries one, otherwise the live object. Checks made before anything stops therefore
# judge the templates that will actually start.
ws_session_candidate() {
  local document=''
  if [[ -n ${WS_SESSION_APPLY:-} ]]; then
    # No match prints nothing (the live object is used); two matches are refused.
    document="$(jq -c --arg name "$1" '[.items[] | select(.kind == "Deployment" and .metadata.name == $name)] |
      if length <= 1 then .[0] // empty else error("duplicate Deployment") end' "$WS_SESSION_APPLY")" ||
      ws_die "the render holds more than one Deployment/$1"
  fi
  if [[ -n "$document" ]]; then printf '%s\n' "$document"; else ws_session_deployment "$1"; fi
}

# A render to apply between stopping and starting the GPU workloads: a JSON List,
# namespaced objects in the session namespace only, the only cluster-scoped kinds
# the service render emits, and every session-managed Deployment at zero replicas
# (the session starts them in order).
ws_session_apply_check() {
  local file="$1" managed="$2"
  [[ -f "$file" && ! -L "$file" ]] || ws_die 'the render to apply must be a regular file'
  jq -e --arg ns "$SESSION_NAMESPACE" --argjson managed "$managed" '
    .kind == "List" and (.items | type == "array" and length > 0) and
    all(.items[];
      if .kind == "Namespace" then .metadata.name == $ns
      elif (.kind == "StorageClass" or .kind == "PriorityClass") then true
      else .metadata.namespace == $ns end) and
    all(.items[] | select(.kind == "Deployment" and (.metadata.name as $n | $managed | index($n)));
      .spec.replicas == 0)' "$file" >/dev/null ||
    ws_die 'the render to apply is not a List for the session namespace with managed GPU Deployments at zero replicas'
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
  document="$(ws_session_candidate "$deployment")" || ws_die "cannot inspect Deployment/$deployment"
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
    --argjson managed "$managed" --argjson smt "$smt" "$WS_SESSION_GPU_JQ"'
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
      all(.initContainers[]?; .restartPolicy != "Always" and ((.resources.claims // [])|length) == 0 and
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
    ($spec|legacy_gpu|not) and
    (($spec.resourceClaims // [])|length) == ($spec|gpu_claims|length) and
    ($spec|gpu_claims|length) >= 1 and ($spec|gpu_claims|length) <= 2 and
    (($spec|footprint("cpu";cpu))+$usedcpu <= ($node.status.allocatable.cpu|cpu)) and
    (($spec|footprint("memory";mem))+$usedmem <= ($node.status.allocatable.memory|mem))
  ' >/dev/null || ws_die 'workload is not Guaranteed or cannot fit node CPU/RAM/GPU requests; unknown quantities also fail closed'
}

ws_session_managed_uids() {
  local ai game images='[]' images2='[]'
  [[ ${SESSION_SERVICE_ENABLED:-false} != true ]] || images="$(ws_session_managed_pods "$SESSION_IMAGE_DEPLOYMENT")" || return 1
  [[ -z ${SESSION_IMAGE2_DEPLOYMENT:-} ]] || images2="$(ws_session_managed_pods "$SESSION_IMAGE2_DEPLOYMENT")" || return 1
  ai="$(ws_session_managed_pods "$SESSION_AI_DEPLOYMENT")" || return 1
  game='[]'
  ! ws_session_game_present || game="$(ws_session_managed_pods "$SESSION_GAME_DEPLOYMENT")" || return 1
  jq -n --argjson ai "$ai" --argjson game "$game" --argjson images "$images" --argjson images2 "$images2" \
    '($ai+$game+$images+$images2)|map(.metadata.uid)'
}

ws_session_other_gpu_check() {
  local pods managed
  pods="$(ws_session_kubectl get pods -A -o json)" || ws_die 'cannot inspect pending and bound GPU consumers'
  managed="$(ws_session_managed_uids)" || ws_die 'cannot resolve managed Pod ownership'
  jq -e --argjson managed "$managed" "$WS_SESSION_GPU_JQ"'all(.items[];
    ((.status.phase == "Succeeded" or .status.phase == "Failed") and .metadata.deletionTimestamp == null) or
    (.metadata.uid as $uid | ($managed|index($uid)) != null) or
    (.spec|uses_gpu|not))' <<<"$pods" >/dev/null ||
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
  local hardware="$1" pods bdf bdfs path device number fd pid found=0 current expected
  pods="$(ws_session_kubectl get pods -A -o json)" || ws_die 'cannot verify GPU allocations, including pending consumers'
  jq -e "$WS_SESSION_GPU_JQ"'all(.items[];
    ((.status.phase == "Succeeded" or .status.phase == "Failed") and .metadata.deletionTimestamp == null) or
    (.spec|uses_gpu|not))' <<<"$pods" >/dev/null ||
    ws_die 'a Pod still requests GPUs on this node; do not start the next session'
  current="$(ws_pci_amd_gpus)"
  expected="$(jq -c '.pci_gpus|map({bdf,device_id,driver})|sort_by(.bdf)' "$hardware")"
  [[ "$expected" == "$(jq -c 'map({bdf,device_id,driver})|sort_by(.bdf)' <<<"$current")" ]] ||
    ws_die 'current PCI GPU identities differ from the hardware report'
  bdfs="$(jq -er '.[].bdf' <<<"$current")" || ws_die 'cannot enumerate live GPU identities'
  [[ -n "$bdfs" ]] || ws_die 'live GPU identity enumeration is empty'
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
  done <<<"$bdfs"
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
  [[ ${SESSION_SERVICE_ENABLED:-false} != true || "$deployment" != "$SESSION_IMAGE_DEPLOYMENT" ]] || expected="$SESSION_IMAGE_UID"
  [[ -z ${SESSION_IMAGE2_DEPLOYMENT:-} || "$deployment" != "$SESSION_IMAGE2_DEPLOYMENT" ]] || expected="$SESSION_IMAGE2_UID"
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
  local mode="$1" hardware="$2" dir="$3" execute="$4" apply="${5:-}" node state ai game desired previous_phase image=0 image2=0 want_image=false want_image2=false service_model=none managed
  ws_require_arch
  ws_require_root
  ws_session_config_validate
  ws_build_config_validate
  [[ "$execute" == --execute ]] || ws_die 'session changes require the explicit --execute argument'
  case "$mode" in ai | split | shared | gaming | maintenance | restore) ;; *) ws_die 'invalid session mode' ;; esac
  managed="$(jq -cn --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" --arg image "${SESSION_IMAGE_DEPLOYMENT:-}" \
    --arg image2 "${SESSION_IMAGE2_DEPLOYMENT:-}" '[$ai, $game, $image, $image2] | map(select(. != ""))')"
  WS_SESSION_APPLY=''
  if [[ -n "$apply" ]]; then
    case "$mode" in ai | split | shared) ;; *) ws_die 'a render can be applied only when switching to ai, split or shared' ;; esac
    ws_session_apply_check "$apply" "$managed"
    WS_SESSION_APPLY="$apply"
  fi
  [[ "$mode" != split || ${SESSION_SERVICE_ENABLED:-false} == true ]] || ws_die 'split requires the explicit shared service configuration'
  [[ "$mode" != shared || -n ${SESSION_IMAGE2_DEPLOYMENT:-} ]] || ws_die 'shared requires the second image Deployment (SESSION_IMAGE2_DEPLOYMENT)'
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
  local game_document
  game_document="$(ws_session_game_document)" || ws_die 'cannot inspect the game Deployment'
  SESSION_GAME_PRESENT=false
  [[ -z "$game_document" ]] || SESSION_GAME_PRESENT=true
  [[ "$mode" != gaming ]] || ws_session_game_present ||
    ws_die "gaming needs Deployment/$SESSION_GAME_DEPLOYMENT, which is not installed"
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
    game=0
    ! ws_session_game_present || game="$(jq -er '.spec.replicas' <<<"$game_document")"
    ((ai <= 1 && game <= 1 && ai + game <= 1)) || ws_die 'initial state must have at most one managed GPU deployment active; concurrent placement is not yet qualified'
    state="$(jq -n --arg context "$SESSION_CONTEXT" --arg node "$SESSION_NODE" --arg ns "$SESSION_NAMESPACE" \
      --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" --argjson ac "$ai" --argjson gc "$game" \
      --arg ai_uid "$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -er .metadata.uid)" \
      --arg game_uid "$(if ws_session_game_present; then jq -er .metadata.uid <<<"$game_document"; else printf absent; fi)" \
      '{schema_version:1,context:$context,node:$node,namespace:$ns,ai:$ai,game:$game,ai_uid:$ai_uid,game_uid:$game_uid,
        previous:{ai:$ac,game:$gc},mode:"baseline",phase:"ready"}')"
  fi
  # A game Deployment installed after the state was written is adopted only while stopped;
  # one the state records cannot silently disappear.
  if [[ "$(jq -r .game_uid <<<"$state")" == absent ]]; then
    if ws_session_game_present; then
      [[ "$(jq -r .spec.replicas <<<"$game_document")" == 0 ]] || ws_die 'adopt the game Deployment only while it is stopped'
      state="$(jq --arg uid "$(jq -er .metadata.uid <<<"$game_document")" '.game_uid = $uid | .previous.game = 0' <<<"$state")"
    fi
  else
    ws_session_game_present || ws_die "Deployment/$SESSION_GAME_DEPLOYMENT is recorded in the session state but no longer exists"
  fi
  SESSION_AI_UID="$(jq -r .ai_uid <<<"$state")"
  SESSION_GAME_UID="$(jq -r .game_uid <<<"$state")"
  if [[ ${SESSION_SERVICE_ENABLED:-false} == true ]]; then
    image="$(ws_session_deployment "$SESSION_IMAGE_DEPLOYMENT" | jq -er .spec.replicas)"
    [[ "$image" == 0 || "$image" == 1 ]] || ws_die 'unexpected image replica count'
    if [[ "$(jq -r '.image // empty' <<<"$state")" == "" ]]; then
      [[ "$image" == 0 ]] || ws_die 'adopt service ownership only with images stopped'
      state="$(jq --arg image "$SESSION_IMAGE_DEPLOYMENT" --arg uid "$(ws_session_deployment "$SESSION_IMAGE_DEPLOYMENT" | jq -er .metadata.uid)" '.image=$image | .image_uid=$uid | .previous.image=0' <<<"$state")"
    fi
    jq -e --arg image "$SESSION_IMAGE_DEPLOYMENT" '.image==$image and (.image_uid|type=="string") and (.previous.image==0 or .previous.image==1) and (if .previous.image==1 then .previous.ai==1 and .previous.game==0 else true end)' <<<"$state" >/dev/null || ws_die 'image state differs from selected target'
    SESSION_IMAGE_UID="$(jq -r .image_uid <<<"$state")"
    ws_session_checked_deployment "$SESSION_IMAGE_DEPLOYMENT" >/dev/null
    if [[ -n ${SESSION_IMAGE2_DEPLOYMENT:-} ]]; then
      image2="$(ws_session_deployment "$SESSION_IMAGE2_DEPLOYMENT" | jq -er .spec.replicas)"
      [[ "$image2" == 0 || "$image2" == 1 ]] || ws_die 'unexpected second image replica count'
      if [[ "$(jq -r '.image2 // empty' <<<"$state")" == "" ]]; then
        [[ "$image2" == 0 ]] || ws_die 'adopt the second image Deployment only while it is stopped'
        state="$(jq --arg image2 "$SESSION_IMAGE2_DEPLOYMENT" --arg uid "$(ws_session_deployment "$SESSION_IMAGE2_DEPLOYMENT" | jq -er .metadata.uid)" '.image2=$image2 | .image2_uid=$uid | .previous.image2=0' <<<"$state")"
      fi
      jq -e --arg image2 "$SESSION_IMAGE2_DEPLOYMENT" '.image2==$image2 and (.image2_uid|type=="string") and (.previous.image2==0 or .previous.image2==1) and (if .previous.image2==1 then .previous.image==1 else true end)' <<<"$state" >/dev/null || ws_die 'second image state differs from selected target'
      SESSION_IMAGE2_UID="$(jq -r .image2_uid <<<"$state")"
      ws_session_checked_deployment "$SESSION_IMAGE2_DEPLOYMENT" >/dev/null
    elif [[ "$(jq -r '.image2 // empty' <<<"$state")" != '' ]]; then
      ws_die 'second image ownership cannot be dropped while it is recorded'
    fi
  elif [[ "$(jq -r '.image // empty' <<<"$state")" != '' || "$mode" == split || "$mode" == shared ]]; then
    ws_die 'service image ownership cannot be dropped during a split session'
  fi
  SESSION_AI_UID="$(jq -r .ai_uid <<<"$state")"
  SESSION_GAME_UID="$(jq -r .game_uid <<<"$state")"
  ws_session_checked_deployment "$SESSION_AI_DEPLOYMENT" >/dev/null
  ! ws_session_game_present || ws_session_checked_deployment "$SESSION_GAME_DEPLOYMENT" >/dev/null
  ws_session_other_gpu_check
  desired=''
  case "$mode" in
    ai) desired="$SESSION_AI_DEPLOYMENT" ;;
    split)
      desired="$SESSION_AI_DEPLOYMENT"
      want_image=true
      ;;
    shared)
      desired="$SESSION_AI_DEPLOYMENT"
      want_image=true
      want_image2=true
      ;;
    gaming) desired="$SESSION_GAME_DEPLOYMENT" ;;
    restore)
      [[ "$(jq -r '.previous.image // 0' <<<"$state")" != 1 ]] || want_image=true
      [[ "$(jq -r '.previous.image2 // 0' <<<"$state")" != 1 ]] || want_image2=true
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
  if [[ ${SESSION_SERVICE_ENABLED:-false} == true && "$desired" == "$SESSION_AI_DEPLOYMENT" ]]; then
    service_model="$(ws_session_service_model)"
  fi
  if [[ "$want_image2" == true ]]; then
    # Shared: the two-GPU model plus one image generator on each of its cards.
    # SGLang sizes its KV pool on both cards from the tighter one, so the image
    # generators start only after it is ready (docs/HOME-LAB.md sharing rules).
    [[ "$service_model" == Qwen3.8-27B-MXFP4 ]] || ws_die 'shared requires the two-GPU Qwen3.8-27B-MXFP4 contract'
    ws_session_qualify "$SESSION_IMAGE_DEPLOYMENT" "$node" ai
    ws_session_qualify "$SESSION_IMAGE2_DEPLOYMENT" "$node" ai
    local combined
    combined="$(jq -n --argjson ai "$(ws_session_candidate "$SESSION_AI_DEPLOYMENT")" --argjson image "$(ws_session_candidate "$SESSION_IMAGE_DEPLOYMENT")" \
      --argjson image2 "$(ws_session_candidate "$SESSION_IMAGE2_DEPLOYMENT")" "$WS_SESSION_GPU_JQ"'
      ($ai.spec.template.spec|gpu_claims) as $model | ($image.spec.template.spec|gpu_claims) as $first |
      ($image2.spec.template.spec|gpu_claims) as $second |
      if ($model|length)==2 and ($first|length)==1 and ($second|length)==1 and (($first+$second)|sort)==$model then
        [$ai,$image,$image2] | map(.spec.template.spec) as $specs |
        $ai | .spec.template.spec.containers = [$specs[].containers[]] |
        .spec.template.spec.initContainers = [$specs[].initContainers[]?] |
        .spec.template.spec.resourceClaims = ([$specs[].resourceClaims[]?] | unique_by(.resourceClaimName // .name))
      else error("shared requires one image generator on each of the model cards") end')" ||
      ws_die 'invalid shared device allocation'
    ws_session_capacity_check "$combined" "$node"
  elif [[ "$want_image" == true ]]; then
    [[ "$service_model" == Qwen3.5-9B ]] || ws_die 'split requires the separately qualified one-GPU Qwen3.5-9B overlay'
    ws_session_qualify "$SESSION_IMAGE_DEPLOYMENT" "$node" ai
    local combined
    combined="$(jq -n --argjson ai "$(ws_session_candidate "$SESSION_AI_DEPLOYMENT")" --argjson image "$(ws_session_candidate "$SESSION_IMAGE_DEPLOYMENT")" "$WS_SESSION_GPU_JQ"'
      [$ai,$image] | if all(.[]; (.spec.template.spec|gpu_claims|length)==1) and
          ($ai.spec.template.spec|gpu_claims) != ($image.spec.template.spec|gpu_claims) then
        $ai | .spec.template.spec.containers += $image.spec.template.spec.containers |
        .spec.template.spec.resourceClaims = ((.spec.template.spec.resourceClaims // []) + ($image.spec.template.spec.resourceClaims // [])) |
        .spec.template.spec.initContainers=((.spec.template.spec.initContainers // [])+($image.spec.template.spec.initContainers // []))
      else error("split requires one device each") end')" || ws_die 'invalid split device allocation'
    ws_session_capacity_check "$combined" "$node"
  fi
  if [[ -n "$apply" ]]; then
    ws_session_kubectl apply --dry-run=server -f "$apply" >/dev/null ||
      ws_die 'the render failed a server-side dry run; nothing was stopped'
  fi
  previous_phase="$(jq -r .phase <<<"$state")"
  if [[ -z "$apply" && "$previous_phase" == ready && "$(jq -r .mode <<<"$state")" == "$mode" && ${SESSION_SERVICE_ENABLED:-false} == false ]]; then
    local actual_ai actual_game
    actual_ai="$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -r .spec.replicas)"
    actual_game=0
    ! ws_session_game_present || actual_game="$(ws_session_deployment "$SESSION_GAME_DEPLOYMENT" | jq -r .spec.replicas)"
    if [[ "$actual_ai" == "$([[ "$desired" == "$SESSION_AI_DEPLOYMENT" ]] && printf 1 || printf 0)" &&
    "$actual_game" == "$([[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] && printf 1 || printf 0)" ]]; then
      [[ "$desired" == "$SESSION_AI_DEPLOYMENT" ]] || ws_session_stop "$SESSION_AI_DEPLOYMENT"
      [[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] || ! ws_session_game_present || ws_session_stop "$SESSION_GAME_DEPLOYMENT"
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
  trap 'ws_session_service unavailable || :; ws_session_state_write "$dir" "$(jq '\''.phase="failed"'\'' <<< "$state")"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  ws_session_service draining
  ws_session_stop "$SESSION_AI_DEPLOYMENT"
  ! ws_session_game_present || ws_session_stop "$SESSION_GAME_DEPLOYMENT"
  [[ ${SESSION_SERVICE_ENABLED:-false} != true ]] || ws_session_stop "$SESSION_IMAGE_DEPLOYMENT"
  [[ -z ${SESSION_IMAGE2_DEPLOYMENT:-} ]] || ws_session_stop "$SESSION_IMAGE2_DEPLOYMENT"
  ws_session_gpu_free "$hardware"
  if [[ -n "$apply" ]]; then
    # The new arm and its consumers change while no GPU workload runs. Other
    # Deployments in the render (gateway, Open WebUI, tools) roll out first, so the
    # gateway that receives the admission calls below is the new one.
    ws_session_kubectl apply -f "$apply" >/dev/null || ws_die 'applying the render failed; the GPU workloads stay stopped'
    local rolled
    while IFS= read -r rolled; do
      ws_session_kubectl -n "$SESSION_NAMESPACE" rollout status "deployment/$rolled" --timeout="${SESSION_TIMEOUT_SECONDS}s" >/dev/null ||
        ws_die "Deployment/$rolled did not roll out after the apply"
    done < <(jq -r --argjson managed "$managed" '.items[] | select(.kind == "Deployment" and (.spec.replicas // 1) > 0) |
      .metadata.name | select(. as $n | $managed | index($n) | not)' "$apply")
  fi
  if [[ -n "$desired" ]]; then
    [[ "$service_model" == none ]] || ws_session_service starting "$service_model"
    ws_session_qualify "$desired" "$node" "$qualification_mode"
    local candidate
    candidate="$(ws_session_checked_deployment "$desired")"
    ws_session_gpu_free "$hardware"
    ws_session_kubectl -n "$SESSION_NAMESPACE" scale "deployment/$desired" --resource-version="$(jq -r .metadata.resourceVersion <<<"$candidate")" --current-replicas=0 --replicas=1 >/dev/null ||
      ws_die 'selected deployment changed concurrently or failed to start'
    ws_session_kubectl -n "$SESSION_NAMESPACE" rollout status "deployment/$desired" --timeout="$(ws_session_rollout_timeout "$desired")" >/dev/null ||
      ws_die 'selected deployment did not become ready; session remains failed until explicit restore or retry'
  fi
  local -a images=()
  [[ "$want_image" != true ]] || images+=("$SESSION_IMAGE_DEPLOYMENT")
  [[ "$want_image2" != true ]] || images+=("$SESSION_IMAGE2_DEPLOYMENT")
  local starting
  for starting in "${images[@]}"; do
    ws_session_qualify "$starting" "$node" ai
    candidate="$(ws_session_checked_deployment "$starting")"
    ws_session_kubectl -n "$SESSION_NAMESPACE" scale "deployment/$starting" --resource-version="$(jq -r .metadata.resourceVersion <<<"$candidate")" --current-replicas=0 --replicas=1 >/dev/null || ws_die 'image Deployment changed concurrently'
    ws_session_kubectl -n "$SESSION_NAMESPACE" rollout status "deployment/$starting" --timeout="$(ws_session_rollout_timeout "$starting")" >/dev/null || ws_die 'image backend did not become ready'
  done
  if [[ "$service_model" != none ]]; then ws_session_service ready "$service_model" "$want_image"; else ws_session_service maintenance; fi
  state="$(jq '.phase="ready"' <<<"$state")"
  ws_session_state_write "$dir" "$state"
  [[ "$desired" == "$SESSION_GAME_DEPLOYMENT" ]] || ws_session_build_gate release
  trap - EXIT INT TERM
  ws_note "session $mode ready; physical release was a point-in-time DRM observation, not protection from unrelated host processes"
)
