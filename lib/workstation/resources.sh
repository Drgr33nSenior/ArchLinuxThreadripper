#!/usr/bin/env bash
# Offline target planning. No cluster access or CPU affinity changes.

ws_resource_config_validate() {
  local key value
  for key in RESOURCE_RESERVED_CORES RESOURCE_HOST_RESERVE_MIB RESOURCE_KUBE_RESERVE_MIB RESOURCE_EVICTION_MIB; do
    value="${!key:-}"
    [[ -z "$value" || "$value" =~ ^[1-9][0-9]*$ ]] || ws_die "$key must be a positive integer"
  done
}

ws_resources_plan() {
  local hardware="$1" output="$2" lock="${3:-$(ws_repo_root)/versions.lock}" version
  command -v jq >/dev/null || ws_die 'jq is required for resource planning'
  ws_resource_config_validate
  ws_build_config_validate
  ws_detected_gpu_target "$hardware" >/dev/null
  version="$(ws_read_lock K3S_VERSION "$lock")"
  [[ "$version" =~ ^v1\.35\.[0-9]+\+k3s[0-9]+$ ]] || ws_die 'CPU policy options are qualified only for locked K3s 1.35; reverify other minors'
  [[ ! -e "$output" && ! -L "$output" ]] || ws_die 'resource plan output must not already exist'
  # Validate before creating output: unknown or asymmetric topology cannot
  # safely produce a reserved CPU list. Keep the existing `none` policy instead.
  jq -e '
    .cpu_topology as $t | .memory.total_bytes as $ram |
    ($ram | type == "number" and . > 0) and
    ($t.online_cpus | type == "array" and length > 0) and
    ($t.cpus | type == "array" and length > 0) and
    (($t.cpus | map(.cpu) | sort) == ($t.online_cpus | sort)) and
    (($t.online_cpus | unique | length) == ($t.online_cpus | length)) and
    all($t.cpus[]; . as $c |
      (.cpu | type == "number" and . >= 0 and floor == .) and
      (.core_id | type == "number" and . >= 0) and
      (.socket_id | type == "number" and . >= 0) and
      (.thread_siblings | type == "array" and length > 0) and
      ((.thread_siblings | sort) ==
       ([$t.cpus[] | select(.socket_id == $c.socket_id and .core_id == $c.core_id) | .cpu] | sort)))
  ' "$hardware" >/dev/null || ws_die 'RAM or full online SMT topology is unknown/inconsistent; retain CPU Manager none and recollect on target'
  local plan
  plan="$(jq --arg version "$version" --arg hardware_sha256 "$(common::sha256_file "$hardware")" \
    --argjson cores "${RESOURCE_RESERVED_CORES:-3}" \
    --argjson host "${RESOURCE_HOST_RESERVE_MIB:-12288}" \
    --argjson kube "${RESOURCE_KUBE_RESERVE_MIB:-4096}" \
    --argjson eviction "${RESOURCE_EVICTION_MIB:-2048}" '
      (.cpu_topology.cpus | sort_by(.socket_id,.core_id,.cpu) |
        group_by([.socket_id,.core_id])) as $groups |
      ($groups[0:$cores] | map(map(.cpu)) | flatten | sort) as $reserved |
      (.memory.total_bytes / 1048576 | floor) as $ram |
      if ($groups|length) <= $cores then error("reserved cores leave no workload CPUs")
      elif ($reserved|length) < 3 then error("reserve at least three logical CPUs for host and K3s")
      elif $ram <= ($host+$kube+$eviction) then error("insufficient RAM after host/K3s/eviction reserves")
      else {
        schema_version:1, status:"offline-plan-not-applied", k3s_version:$version,
        hardware_sha256:$hardware_sha256, cpu_manager_policy:"static",
        online_logical_cpus:(.cpu_topology.online_cpus|length),
        reserved_cpus:$reserved, reserved_physical_cores:$cores,
        allocatable_logical_cpus:((.cpu_topology.online_cpus|length)-($reserved|length)),
        smt_widths:($groups|map(length)|unique),
        memory:{total_mib:$ram, available_mib:(if .memory.available_bytes then (.memory.available_bytes/1048576|floor) else null end),
          host_reserve_mib:$host,kube_reserve_mib:$kube,eviction_mib:$eviction,
          allocatable_mib:($ram-$host-$kube-$eviction),dimms:.memory.dimms},
        gpu_count:(.pci_gpus|length), gpu_identity:(.pci_gpus|map({bdf,device_id,render_nodes})),
        limitations:["Capacity is not current free capacity; admission must also count other workloads.",
          "DIMM population does not establish active channel count or bandwidth.",
          "Reserved CPUs do not move host processes or interrupts.",
          "GPU count allocation does not select a physical card or combine VRAM."]
      } end
    ' "$hardware")" || ws_die 'resource budget cannot fit the discovered hardware'
  (
    umask 077
    mkdir -p -- "$output"
  )
  printf '%s\n' "$plan" >"$output/resource-plan.json"
  jq '{lab_cpu_manager_policy:"static",lab_reserved_system_cpus:(.reserved_cpus|map(tostring)|join(",")),
    lab_system_reserved_cpu:((.reserved_cpus|length)-2|tostring),lab_kube_reserved_cpu:"2",
    lab_system_reserved_memory:((.memory.host_reserve_mib|tostring)+"Mi"),
    lab_kube_reserved_memory:((.memory.kube_reserve_mib|tostring)+"Mi"),
    lab_eviction_memory:((.memory.eviction_mib|tostring)+"Mi"),
    lab_cpu_manager_cache_alignment:false}' "$output/resource-plan.json" >"$output/ansible-vars.json"
  ws_note "offline resource plan written to $output; review before passing ansible-vars.json as Ansible extra vars"
}
