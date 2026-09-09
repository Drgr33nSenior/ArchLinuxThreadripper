#!/usr/bin/env bash
# Read-only hardware evidence and no-swap policy validation.

ws_no_swap_check() {
  local proc=${1:-/proc} etc=${2:-/etc} sys=${3:-/sys} usr=${4:-/usr} units
  [[ -r $proc/swaps && -r $proc/cmdline && -r $etc/fstab ]] || ws_die 'swap policy evidence is unavailable'
  [[ $(awk 'NR > 1 {n++} END {print n+0}' "$proc/swaps") == 0 ]] || ws_die 'active swap violates the workstation policy'
  if awk '$0 !~ /^[[:space:]]*#/ && $3 == "swap" {found=1} END {exit !found}' "$etc/fstab"; then
    ws_die 'fstab configures swap'
  fi
  if grep -Eq '(^|[[:space:]])(resume|resume_offset)=' "$proc/cmdline"; then
    ws_die 'kernel command line configures hibernation resume'
  fi
  local device
  for device in "$sys"/block/zram*; do
    [[ ! -e $device ]] || ws_die 'a zram block device is present'
  done
  for device in "$etc"/systemd/zram-generator.conf "$etc"/systemd/zram-generator.conf.d/*.conf \
    "$usr"/lib/systemd/zram-generator.conf "$usr"/lib/systemd/zram-generator.conf.d/*.conf; do
    [[ ! -s $device ]] || ws_die "zram configuration is present: $device"
  done
  # Target-only service checks supplement the fixture-testable file checks.
  if [[ $proc == /proc ]]; then
    units=$(systemctl list-unit-files --type=swap --no-legend --no-pager) || ws_die 'cannot inspect configured swap units'
    [[ -z $units ]] || ws_die 'configured swap unit found; inspect systemctl list-unit-files --type=swap'
  fi
}

ws_rocminfo_agents() {
  # Only top-level Agent Name fields count. ISA Name lines contain target
  # triples and must not inflate the GPU count.
  awk '
    function emit() {if (gfx != "") printf "%s\t%s\t%s\n", agent,gfx,uuid}
    /^[[:space:]]*Agent[[:space:]]+[0-9]+/ {emit(); agent=$2; gfx=""; uuid=""}
    /^[[:space:]]*Name:[[:space:]]+gfx[0-9a-f]+[[:space:]]*$/ {gfx=$2}
    /^[[:space:]]*Uuid:/ {uuid=$2}
    END {emit()}
  ' "$1" | jq -Rn '[inputs | split("\t") | {agent:.[0],gfx:.[1],uuid:.[2]}]'
}

ws_capture() {
  local output=$1 name=$2 status=0
  shift 2
  if command -v "$1" >/dev/null 2>&1; then
    "$@" >"$output/$name.txt" 2>&1 || status=$?
  else
    printf 'unavailable: %s\n' "$1" >"$output/$name.txt"
    status=127
  fi
  jq -n --arg name "$name" --argjson status "$status" '{command:$name,exit_code:$status}' >>"$output/commands.jsonl"
}

ws_capture_optional() {
  local output=$1 name=$2 status=0
  shift 2
  if command -v "$1" >/dev/null 2>&1; then
    "$@" >"$output/$name.txt" 2>&1 || status=$?
  else
    printf 'unavailable: %s\n' "$1" >"$output/$name.txt"
    status=127
  fi
  jq -n --arg name "$name" --argjson status "$status" '{command:$name,exit_code:$status,required:false}' >>"$output/optional-commands.jsonl"
}

ws_hardware_cpu_list_json() {
  local list=${1:-} item first last cpu
  [[ $list =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]] || {
    jq -n 'null'
    return
  }
  {
    IFS=',' read -r -a _ws_hardware_cpu_items <<<"$list"
    for item in "${_ws_hardware_cpu_items[@]}"; do
      if [[ $item == *-* ]]; then
        first=${item%-*}
        last=${item#*-}
        ((first <= last)) || {
          jq -n 'null'
          return
        }
        for ((cpu = first; cpu <= last; cpu++)); do printf '%s\n' "$cpu"; done
      else
        printf '%s\n' "$item"
      fi
    done
  } | sort -nu | jq -Rsc '[split("\n")[] | select(length > 0) | tonumber]'
}

ws_hardware_number_or_null() {
  local value=${1:-}
  if [[ $value =~ ^-?[0-9]+$ ]]; then
    jq -n --argjson value "$value" '$value'
  else
    jq -n 'null'
  fi
}

ws_hardware_read_value() {
  local path=$1
  [[ -r $path ]] && tr -d '\n' <"$path" || true
}

ws_hardware_numa_nodes_json() {
  local sys=${1:-/sys} node_path node cpulist memory_total
  [[ -d $sys/devices/system/node ]] || {
    jq -n 'null'
    return
  }
  for node_path in "$sys"/devices/system/node/node[0-9]*; do
    [[ -d $node_path ]] || continue
    node=${node_path##*/node}
    cpulist="$(ws_hardware_read_value "$node_path/cpulist")"
    memory_total=$(awk -v node="$node" '$1 == "Node" && $2 == node && $3 == "MemTotal:" {printf "%.0f\n", $4 * 1024}' "$node_path/meminfo" 2>/dev/null || true)
    jq -n --argjson node "$(ws_hardware_number_or_null "$node")" --argjson cpus "$(ws_hardware_cpu_list_json "$cpulist")" \
      --argjson memory_total "$(ws_hardware_number_or_null "$memory_total")" '{node:$node,cpus:$cpus,memory_total_bytes:$memory_total}'
  done | jq -s .
}

ws_hardware_cpu_topology() {
  local sys=${1:-/sys} online_path online cpu_path cpu core socket siblings numa='' caches cache level type id shared
  online_path="$sys/devices/system/cpu/online"
  [[ -r $online_path ]] || {
    jq -n 'null'
    return
  }
  online="$(<"$online_path")"
  ws_hardware_cpu_list_json "$online" | jq -e 'type == "array"' >/dev/null || {
    jq -n 'null'
    return
  }
  {
    while IFS= read -r cpu; do
      cpu_path="$sys/devices/system/cpu/cpu$cpu"
      [[ -d $cpu_path ]] || continue
      core="$(ws_hardware_read_value "$cpu_path/topology/core_id")"
      socket="$(ws_hardware_read_value "$cpu_path/topology/physical_package_id")"
      siblings="$(ws_hardware_read_value "$cpu_path/topology/thread_siblings_list")"
      numa=''
      local node_path
      for node_path in "$cpu_path"/node[0-9]*; do
        [[ -e $node_path || -L $node_path ]] || continue
        numa=${node_path##*/node}
        break
      done
      caches=$(
        for cache in "$cpu_path"/cache/index[0-9]*; do
          [[ -d $cache ]] || continue
          level="$(ws_hardware_read_value "$cache/level")"
          type="$(ws_hardware_read_value "$cache/type")"
          id="$(ws_hardware_read_value "$cache/id")"
          shared="$(ws_hardware_read_value "$cache/shared_cpu_list")"
          jq -n --argjson level "$(ws_hardware_number_or_null "$level")" --arg type "$type" \
            --argjson id "$(ws_hardware_number_or_null "$id")" --argjson shared "$(ws_hardware_cpu_list_json "$shared")" \
            '{level:$level,type:(if $type == "" then null else $type end),id:$id,shared_cpus:$shared}'
        done | jq -s .
      )
      jq -n --argjson cpu "$cpu" --argjson core "$(ws_hardware_number_or_null "$core")" \
        --argjson socket "$(ws_hardware_number_or_null "$socket")" --argjson numa "$(ws_hardware_number_or_null "$numa")" \
        --argjson siblings "$(ws_hardware_cpu_list_json "$siblings")" --argjson caches "$caches" \
        '{cpu:$cpu,core_id:$core,socket_id:$socket,numa_node:$numa,thread_siblings:$siblings,caches:$caches}'
    done < <(ws_hardware_cpu_list_json "$online" | jq -r '.[]')
  } | jq -s --argjson online "$(ws_hardware_cpu_list_json "$online")" --argjson nodes "$(ws_hardware_numa_nodes_json "$sys")" \
    '{online_cpus:$online,cpus:.,numa_nodes:$nodes}'
}

ws_hardware_memory_json() {
  local proc=${1:-/proc} total available
  [[ -r $proc/meminfo ]] || {
    jq -n 'null'
    return
  }
  total=$(awk '$1 == "MemTotal:" {printf "%.0f\n", $2 * 1024}' "$proc/meminfo")
  available=$(awk '$1 == "MemAvailable:" {printf "%.0f\n", $2 * 1024}' "$proc/meminfo")
  jq -n --argjson total "$(ws_hardware_number_or_null "$total")" --argjson available "$(ws_hardware_number_or_null "$available")" \
    '{total_bytes:$total,available_bytes:$available,dimms:null}'
}

ws_hardware_dimms_json() {
  local report=$1 records
  [[ -r $report ]] || {
    jq -n 'null'
    return
  }
  records=$(awk '
    BEGIN { RS=""; FS="\n" }
    /Memory Device/ {
      locator=""; bank=""; size=""; speed=""; configured=""
      for (i=1; i<=NF; i++) {
        if ($i ~ /^[[:space:]]*Locator:/) { sub(/^[[:space:]]*Locator:[[:space:]]*/, "", $i); locator=$i }
        else if ($i ~ /^[[:space:]]*Bank Locator:/) { sub(/^[[:space:]]*Bank Locator:[[:space:]]*/, "", $i); bank=$i }
        else if ($i ~ /^[[:space:]]*Size:/) { sub(/^[[:space:]]*Size:[[:space:]]*/, "", $i); size=$i }
        else if ($i ~ /^[[:space:]]*Speed:/) { sub(/^[[:space:]]*Speed:[[:space:]]*/, "", $i); speed=$i }
        else if ($i ~ /^[[:space:]]*Configured Memory Speed:/) { sub(/^[[:space:]]*Configured Memory Speed:[[:space:]]*/, "", $i); configured=$i }
      }
      if (locator != "" || size != "") print locator "\t" bank "\t" size "\t" speed "\t" configured
    }
  ' "$report")
  [[ -n $records ]] || {
    jq -n 'null'
    return
  }
  printf '%s\n' "$records" | jq -Rn '
    [inputs | split("\t") |
      {locator:(if .[0] == "" then null else .[0] end),bank_locator:(if .[1] == "" then null else .[1] end),
       size:(if .[2] == "" or .[2] == "No Module Installed" or .[2] == "Unknown" then null else .[2] end),
       speed:(if .[3] == "" or .[3] == "Unknown" then null else .[3] end),
       configured_speed:(if .[4] == "" or .[4] == "Unknown" then null else .[4] end)}]'
}

ws_hardware_platform_json() {
  local sys=${1:-/sys} dmi board_vendor board_name board_version bios_vendor bios_version bios_date
  dmi="$sys/class/dmi/id"
  [[ -d $dmi ]] || {
    jq -n 'null'
    return
  }
  board_vendor="$(ws_hardware_read_value "$dmi/board_vendor")"
  board_name="$(ws_hardware_read_value "$dmi/board_name")"
  board_version="$(ws_hardware_read_value "$dmi/board_version")"
  bios_vendor="$(ws_hardware_read_value "$dmi/bios_vendor")"
  bios_version="$(ws_hardware_read_value "$dmi/bios_version")"
  bios_date="$(ws_hardware_read_value "$dmi/bios_date")"
  jq -n --arg board_vendor "$board_vendor" --arg board_name "$board_name" --arg board_version "$board_version" \
    --arg bios_vendor "$bios_vendor" --arg bios_version "$bios_version" --arg bios_date "$bios_date" \
    '{board_vendor:(if $board_vendor == "" then null else $board_vendor end),board_name:(if $board_name == "" then null else $board_name end),board_version:(if $board_version == "" then null else $board_version end),bios_vendor:(if $bios_vendor == "" then null else $bios_vendor end),bios_version:(if $bios_version == "" then null else $bios_version end),bios_date:(if $bios_date == "" then null else $bios_date end)}'
}

ws_hardware_json_report_or_null() {
  local report=$1
  if [[ -r $report ]] && jq -e . "$report" >/dev/null 2>&1; then
    jq -c . "$report"
  else
    jq -n 'null'
  fi
}

ws_hardware_capture_nvme_smart() {
  local output=$1 sys=${2:-/sys} controller name
  for controller in "$sys"/class/nvme/nvme[0-9]*; do
    [[ -d $controller ]] || continue
    name=${controller##*/}
    ws_capture_optional "$output" "$name-smart" nvme smart-log -o json "/dev/$name"
  done
}

ws_hardware_nvme_json() {
  local sys=${1:-/sys} output=${2:-} controller name namespace sectors sector_size capacity model serial firmware state namespaces smart thermal
  [[ -d $sys/class/nvme ]] || {
    jq -n 'null'
    return
  }
  for controller in "$sys"/class/nvme/nvme[0-9]*; do
    [[ -d $controller ]] || continue
    name=${controller##*/}
    model="$(ws_hardware_read_value "$controller/model")"
    serial="$(ws_hardware_read_value "$controller/serial")"
    firmware="$(ws_hardware_read_value "$controller/firmware_rev")"
    state="$(ws_hardware_read_value "$controller/state")"
    smart=null
    thermal=null
    if [[ -n $output ]]; then
      smart="$(ws_hardware_json_report_or_null "$output/$name-smart.txt")"
      thermal=$(jq -c '(.temperature // .temperature_sensor_1 // null)' <<<"$smart")
    fi
    namespaces=$(
      for namespace in "$sys"/block/"$name"n*; do
        [[ -d $namespace ]] || continue
        sectors="$(ws_hardware_read_value "$namespace/size")"
        sector_size="$(ws_hardware_read_value "$namespace/queue/logical_block_size")"
        capacity=''
        # Linux block sysfs `size` is always a count of 512-byte sectors,
        # including namespaces formatted with 4096-byte logical blocks.
        [[ $sectors =~ ^[0-9]+$ ]] && capacity=$((sectors * 512))
        jq -n --arg name "${namespace##*/}" --argjson sectors "$(ws_hardware_number_or_null "$sectors")" \
          --argjson sector_size "$(ws_hardware_number_or_null "$sector_size")" --argjson capacity "$(ws_hardware_number_or_null "$capacity")" \
          '{name:$name,sectors:$sectors,logical_block_size:$sector_size,capacity_bytes:$capacity}'
      done | jq -s .
    )
    jq -n --arg controller "$name" --arg model "$model" --arg serial "$serial" --arg firmware "$firmware" --arg state "$state" --argjson namespaces "$namespaces" --argjson smart "$smart" --argjson thermal "$thermal" \
      '{controller:$controller,model:(if $model == "" then null else $model end),serial:(if $serial == "" then null else $serial end),firmware:(if $firmware == "" then null else $firmware end),state:(if $state == "" then null else $state end),namespaces:$namespaces,thermal:$thermal,smart:$smart}'
  done | jq -s .
}

ws_hardware_trim_json() {
  local discard_report=$1 timer_report=$2 timer=''
  [[ -r $timer_report ]] && timer="$(head -n 1 "$timer_report")"
  jq -n --rawfile discard "$discard_report" --arg timer "$timer" '
    {block_discard:(try ($discard | fromjson) catch null),
     fstrim_timer:(if $timer == "enabled" then "enabled" elif $timer == "disabled" then "disabled" else null end)}'
}

ws_pci_amd_gpus() {
  local sys=${1:-/sys} path vendor class driver render renders render_mappings bars link_speed link_width max_link_speed max_link_width numa
  for path in "$sys"/bus/pci/devices/*; do
    [[ -r $path/vendor && -r $path/class ]] || continue
    read -r vendor <"$path/vendor"
    read -r class <"$path/class"
    [[ $vendor == 0x1002 && $class == 0x03* ]] || continue
    driver=''
    [[ ! -L $path/driver ]] || driver=$(basename -- "$(readlink "$path/driver")")
    renders=$(for render in "$path"/drm/renderD*; do if [[ -e $render || -L $render ]]; then basename -- "$render"; fi; done | jq -Rsc '[split("\n")[] | select(length > 0)]')
    render_mappings=$(for render in "$path"/drm/renderD*; do
      [[ -e $render || -L $render ]] || continue
      jq -n --arg node "$(basename -- "$render")" --arg path "$render" '{node:$node,sysfs_path:$path}'
    done | jq -s .)
    bars=null
    if [[ -r "$path/resource" ]]; then
      bars=$(awk '$1 !~ /^0x0+$/ {print $1 "\t" $2 "\t" $3}' "$path/resource" | jq -Rn '[inputs | split("\t") | {start:.[0],end:.[1],flags:.[2]}]')
    fi
    link_speed="$(ws_hardware_read_value "$path/current_link_speed")"
    link_width="$(ws_hardware_read_value "$path/current_link_width")"
    max_link_speed="$(ws_hardware_read_value "$path/max_link_speed")"
    max_link_width="$(ws_hardware_read_value "$path/max_link_width")"
    numa="$(ws_hardware_read_value "$path/numa_node")"
    jq -n --arg bdf "${path##*/}" --arg driver "$driver" --arg device "$(<"$path/device")" --argjson renders "$renders" --argjson render_mappings "$render_mappings" --argjson bars "$bars" \
      --arg speed "$link_speed" --arg width "$link_width" --arg max_speed "$max_link_speed" --arg max_width "$max_link_width" --argjson numa "$(ws_hardware_number_or_null "$numa")" \
      '{bdf:$bdf,driver:$driver,device_id:$device,render_nodes:$renders,render_mappings:$render_mappings,numa_node:$numa,bars:$bars,
        link:{current_speed:(if $speed == "" then null else $speed end),current_width:(if $width == "" then null else $width end),max_speed:(if $max_speed == "" then null else $max_speed end),max_width:(if $max_width == "" then null else $max_width end)}}'
  done | jq -s .
}

ws_hardware_collect() (
  set -euo pipefail
  ws_build_config_validate
  local output=$1 os arch pci='[]' agents='[]' cpu_topology=null memory=null platform=null nvme_devices=null trim_evidence=null dimms=null status=pending reason='target Linux x86_64 validation has not run' checks
  command -v jq >/dev/null || ws_die 'jq is required for hardware evidence'
  [[ ! -e $output ]] || ws_die 'hardware output directory already exists; select a new observation directory'
  umask 077
  mkdir -p -- "$output"
  os=$(uname -s)
  arch=$(uname -m)
  if [[ $os == Linux && $arch == x86_64 ]]; then
    ws_capture "$output" lscpu lscpu
    ws_capture "$output" lspci lspci -nnk
    ws_capture "$output" memory free -h
    ws_capture "$output" lsblk lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINTS,MODEL
    ws_capture "$output" gcc gcc --version
    ws_capture "$output" clang clang --version
    ws_capture "$output" gcc-native gcc -march=native -Q --help=target
    ws_capture "$output" rocminfo rocminfo
    ws_capture "$output" rocm-smi rocm-smi
    ws_capture "$output" packages pacman -Q
    ws_capture "$output" topology lspci -tv
    # Read-only PCI capability evidence (links, ACS and Resizable BAR where
    # exposed). This is not a functional GPU peer-transfer result.
    ws_capture_optional "$output" pci-capabilities lspci -Dvvnn
    ws_capture_optional "$output" dmidecode-memory dmidecode -t memory
    ws_capture_optional "$output" lsblk-discard lsblk -J -b -o NAME,KNAME,TYPE,MOUNTPOINTS,DISC-GRAN,DISC-MAX,DISC-ZERO
    ws_capture_optional "$output" fstrim-timer systemctl is-enabled fstrim.timer
    ws_capture_optional "$output" nvme-list nvme list -o json
    ws_capture_optional "$output" cpu-power "${WORKSTATION_PYTHON:-python3}" "$(ws_repo_root)/lib/workstation/measurement.py" cpu-power
    ws_capture_optional "$output" smartctl-scan smartctl --scan-open -j
    ws_hardware_capture_nvme_smart "$output"
    cp /proc/meminfo "$output/meminfo.txt"
    cp /proc/sys/kernel/random/boot_id "$output/boot-id.txt"
    pci=$(ws_pci_amd_gpus)
    agents=$(ws_rocminfo_agents "$output/rocminfo.txt")
    cpu_topology=$(ws_hardware_cpu_topology)
    memory=$(ws_hardware_memory_json)
    dimms=$(ws_hardware_dimms_json "$output/dmidecode-memory.txt")
    memory=$(jq --argjson dimms "$dimms" 'if . == null then null else .dimms = $dimms end' <<<"$memory")
    platform=$(ws_hardware_platform_json)
    nvme_devices=$(ws_hardware_nvme_json /sys "$output")
    trim_evidence=$(ws_hardware_trim_json "$output/lsblk-discard.txt" "$output/fstrim-timer.txt")
    if (ws_no_swap_check) >"$output/no-swap.txt" 2>&1; then
      if ws_hardware_assert_json "$pci" "$agents" "$EXPECTED_GPU_COUNT"; then
        checks=$(jq -s '[.[] | select(.exit_code != 0)] | length' "$output/commands.jsonl")
        if ((checks == 0)); then
          status=observed
          reason='GPU count, matching ROCm targets, driver binding and no-swap policy passed; workload validation still required'
        else
          reason='one or more required collection commands failed; see commands.json'
        fi
      else
        status=failed
        reason='GPU count, driver or ROCm agent validation failed'
      fi
    else
      status=failed
      reason='no-swap policy validation failed'
    fi
    jq -s . "$output/commands.jsonl" >"$output/commands.json"
    jq -s . "$output/optional-commands.jsonl" >"$output/optional-commands.json"
  fi
  jq -n --arg status "$status" --arg reason "$reason" --arg os "$os" --arg arch "$arch" \
    --arg date "$(date -u +%FT%TZ)" --arg model "$EXPECTED_GPU_MODEL" --argjson count "$EXPECTED_GPU_COUNT" \
    --argjson pci "$pci" --argjson agents "$agents" --argjson cpu_topology "$cpu_topology" --argjson memory "$memory" \
    --argjson platform "$platform" --argjson nvme_devices "$nvme_devices" --argjson trim_evidence "$trim_evidence" \
    --argjson cpu_power "$(ws_hardware_json_report_or_null "$output/cpu-power.txt")" \
    '{schema:1,status:$status,reason:$reason,collected_at:$date,os:$os,architecture:$arch,
      expected:{gpu_count:$count,gpu_model:$model},pci_gpus:$pci,rocm_agents:$agents,
      gpu_target:(if ($agents|length)>0 then $agents[0].gfx else null end),hardware_workloads_validated:false,
      cpu_topology:$cpu_topology,cpu_power:$cpu_power,memory:$memory,platform:$platform,nvme_devices:$nvme_devices,trim_evidence:$trim_evidence}' >"$output/hardware.json"
  ws_note "hardware evidence: $output/hardware.json ($status)"
  [[ $status != failed ]]
)

ws_hardware_assert_json() {
  local pci=$1 agents=$2 expected=$3
  jq -en --argjson pci "$pci" --argjson agents "$agents" --argjson n "$expected" '
    ($pci|length) == $n and ($pci|map(.bdf)|unique|length) == $n and
    all($pci[]; .driver == "amdgpu") and ($pci|map(.device_id)|unique|length) == 1 and
    ($agents|length) == $n and ($agents|map(.agent)|unique|length) == $n and
    ($agents|map(.gfx)|unique|length) == 1 and all($agents[]; .gfx|test("^gfx[0-9a-f]+$"))
  ' >/dev/null
}

ws_detected_gpu_target() {
  local report=$1
  ws_build_config_validate
  jq -e --argjson count "$EXPECTED_GPU_COUNT" '
    .schema == 1 and .status == "observed" and .os == "Linux" and .architecture == "x86_64" and
    .expected.gpu_count == $count and (.pci_gpus|length) == $count and
    (.rocm_agents|length) == $count and (.gpu_target|test("^gfx[0-9a-f]+$"))
  ' "$report" >/dev/null || ws_die 'an observed target-workstation report with every GPU is required'
  ws_hardware_assert_json "$(jq -c .pci_gpus "$report")" "$(jq -c .rocm_agents "$report")" "$EXPECTED_GPU_COUNT" ||
    ws_die 'hardware report contains inconsistent GPU evidence'
  jq -er '.gpu_target as $target | select(all(.rocm_agents[]; .gfx == $target)) | .gpu_target' "$report"
}

ws_argon2_calibrate() (
  ws_require_arch
  local config=$1 output=$2 raw memory iterations parallel
  # Reuse the installer's strict non-secret schema. This operation calls only
  # cryptsetup benchmark: no device, passphrase or header is opened.
  # shellcheck disable=SC1091
  source "$(ws_repo_root)/lib/bootstrap/common.sh"
  # shellcheck disable=SC1091
  source "$(ws_repo_root)/lib/bootstrap/config.sh"
  bootstrap_load_config "$config" || exit 1
  [[ ! -e $output ]] || ws_die 'calibration output already exists'
  mkdir -p -- "$output"
  raw="$output/argon2id.txt"
  cryptsetup benchmark --pbkdf argon2id --iter-time "$LUKS_ITER_TIME_MS" \
    --pbkdf-memory "$LUKS_MEMORY_KIB" --pbkdf-parallel "$LUKS_PARALLEL" >"$raw" || ws_die 'Argon2id benchmark failed'
  read -r iterations memory parallel < <(awk '$1 == "argon2id" {print $2,$4,$6}' "$raw") || ws_die 'Argon2id benchmark produced no parameters'
  local parameter
  for parameter in "$iterations" "$memory" "$parallel"; do
    ws_positive_integer "$parameter" || ws_die 'could not parse Argon2id benchmark'
  done
  ((memory >= 262144 && memory <= LUKS_MEMORY_KIB && parallel <= LUKS_PARALLEL)) ||
    ws_die 'calibrated Argon2id parameters are outside the installation policy; review the raw benchmark'
  jq -n --argjson time "$LUKS_ITER_TIME_MS" --argjson memory "$memory" --argjson iterations "$iterations" \
    --argjson parallel "$parallel" --arg version "$(cryptsetup --version)" --arg date "$(date -u +%FT%TZ)" \
    '{schema:1,pbkdf:"argon2id",requested_unlock_ms:$time,memory_kib:$memory,iterations:$iterations,parallelism:$parallel,
      cryptsetup:$version,measured_at:$date,scope:"in-memory benchmark; installation recalibrates and records actual keyslot parameters"}' >"$output/argon2id.json"
  printf 'LUKS_ITER_TIME_MS=%s\nLUKS_MEMORY_KIB=%s\nLUKS_PARALLEL=%s\n' "$LUKS_ITER_TIME_MS" "$memory" "$parallel" >"$output/install-values.conf"
  ws_note "Argon2id calibration: $output/argon2id.json; no storage was changed"
)
