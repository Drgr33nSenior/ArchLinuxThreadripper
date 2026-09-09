#!/usr/bin/env bash
# Fixture-only topology checks; no target workload, sudo, or hardware write.
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/hardware.sh
source "$root/lib/workstation/hardware.sh"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
sys="$work/sys"
proc="$work/proc"
mkdir -p "$sys/devices/system/cpu" "$sys/devices/system/node/node0" "$sys/devices/system/node/node1" "$sys/bus/pci/devices" "$sys/class/dmi/id" "$sys/class/nvme" "$sys/block" "$proc"
printf '0-1,4-5\n' >"$sys/devices/system/cpu/online"
printf '0-1\n' >"$sys/devices/system/node/node0/cpulist"
printf '4-5\n' >"$sys/devices/system/node/node1/cpulist"

for cpu in 0 1 4 5; do
  mkdir -p "$sys/devices/system/cpu/cpu$cpu/topology" "$sys/devices/system/cpu/cpu$cpu/cache/index0"
  printf '%s\n' "$((cpu / 4))" >"$sys/devices/system/cpu/cpu$cpu/topology/physical_package_id"
  case "$cpu" in
    0 | 1)
      core=0
      siblings=0-1
      ;;
    4 | 5)
      core=2
      siblings=4-5
      ;;
  esac
  printf '%s\n' "$core" >"$sys/devices/system/cpu/cpu$cpu/topology/core_id"
  printf '%s\n' "$siblings" >"$sys/devices/system/cpu/cpu$cpu/topology/thread_siblings_list"
  if ((cpu < 4)); then mkdir "$sys/devices/system/cpu/cpu$cpu/node0"; else mkdir "$sys/devices/system/cpu/cpu$cpu/node1"; fi
  printf '1\n' >"$sys/devices/system/cpu/cpu$cpu/cache/index0/level"
  # The cache fixture needs one value per ABI file.
  printf 'Data\n' >"$sys/devices/system/cpu/cpu$cpu/cache/index0/type"
  printf '0\n' >"$sys/devices/system/cpu/cpu$cpu/cache/index0/id"
  printf '%s\n' "$siblings" >"$sys/devices/system/cpu/cpu$cpu/cache/index0/shared_cpu_list"
done

printf 'Gigabyte\n' >"$sys/class/dmi/id/board_vendor"
printf 'TRX50 AI TOP\n' >"$sys/class/dmi/id/board_name"
printf '1.0\n' >"$sys/class/dmi/id/board_version"
printf 'American Megatrends\n' >"$sys/class/dmi/id/bios_vendor"
printf 'F3\n' >"$sys/class/dmi/id/bios_version"
printf '01/02/2026\n' >"$sys/class/dmi/id/bios_date"
cp "$root/tests/fixtures/hardware-tranche/meminfo" "$proc/meminfo"

for bdf in 0000:41:00.0 0000:81:00.0; do
  gpu="$sys/bus/pci/devices/$bdf"
  mkdir -p "$gpu/drm"
  printf '0x1002\n0x03' >"$gpu/vendor"
  printf '0x03%02d\n' 0 >"$gpu/class"
  printf '0x7550\n' >"$gpu/device"
  printf '16.0 GT/s\n' >"$gpu/current_link_speed"
  printf '16\n' >"$gpu/current_link_width"
  printf '32.0 GT/s\n' >"$gpu/max_link_speed"
  printf '16\n' >"$gpu/max_link_width"
  printf '0\n' >"$gpu/numa_node"
  printf '0x0000001000000000 0x00000010ffffffff 0x0000000000040200\n' >"$gpu/resource"
done
touch "$sys/bus/pci/devices/0000:41:00.0/drm/renderD128" "$sys/bus/pci/devices/0000:81:00.0/drm/renderD129"

nvme="$sys/class/nvme/nvme0"
mkdir -p "$nvme" "$sys/block/nvme0n1/queue"
printf 'Synthetic NVMe\n' >"$nvme/model"
printf 'SN123\n' >"$nvme/serial"
printf '1.2.3\n' >"$nvme/firmware_rev"
printf 'live\n' >"$nvme/state"
printf '2000000\n' >"$sys/block/nvme0n1/size"
printf '512\n' >"$sys/block/nvme0n1/queue/logical_block_size"

topology="$(ws_hardware_cpu_topology "$sys")"
jq -e '.online_cpus == [0,1,4,5] and (.cpus|length) == 4 and .cpus[0].thread_siblings == [0,1] and .cpus[2].core_id == 2 and .cpus[0].numa_node == 0 and .cpus[0].caches[0].shared_cpus == [0,1] and .numa_nodes == [{node:0,cpus:[0,1],memory_total_bytes:null},{node:1,cpus:[4,5],memory_total_bytes:null}]' <<<"$topology" >/dev/null
[[ "$(ws_hardware_cpu_topology "$work/absent")" == null ]]

memory="$(ws_hardware_memory_json "$proc")"
jq -e '.total_bytes == 134217728000 and .available_bytes == 98304000000 and .dimms == null' <<<"$memory" >/dev/null
jq -e 'length == 2 and .[0].locator == "DIMM_A1" and .[1].configured_speed == "4800 MT/s"' "$root/tests/fixtures/hardware-tranche/dmidecode-two.txt" >/dev/null 2>&1 && exit 1 || true
two_dimms="$(ws_hardware_dimms_json "$root/tests/fixtures/hardware-tranche/dmidecode-two.txt")"
four_dimms="$(ws_hardware_dimms_json "$root/tests/fixtures/hardware-tranche/dmidecode-four.txt")"
jq -e 'length == 2 and .[0].locator == "DIMM_A1" and .[1].configured_speed == "4800 MT/s"' <<<"$two_dimms" >/dev/null
jq -e 'length == 4 and .[3].size == null and .[3].configured_speed == null' <<<"$four_dimms" >/dev/null
[[ "$(ws_hardware_dimms_json "$work/missing-dmidecode")" == null ]]

platform="$(ws_hardware_platform_json "$sys")"
jq -e '.board_name == "TRX50 AI TOP" and .board_version == "1.0" and .bios_version == "F3"' <<<"$platform" >/dev/null
[[ "$(ws_hardware_platform_json "$work/absent")" == null ]]

gpus="$(ws_pci_amd_gpus "$sys")"
jq -e 'length == 2 and ((map(.device_id) | unique) == ["0x7550"]) and .[0].bdf == "0000:41:00.0" and .[1].bdf == "0000:81:00.0" and .[0].render_nodes == ["renderD128"] and .[1].render_nodes == ["renderD129"] and (.[0].render_mappings[0].sysfs_path | endswith("0000:41:00.0/drm/renderD128")) and .[0].link.current_width == "16" and all(.[]; (.bars | length) == 1)' <<<"$gpus" >/dev/null
rm -- "$sys/bus/pci/devices/0000:41:00.0/drm/renderD128"
gpus_changed="$(ws_pci_amd_gpus "$sys")"
jq -e 'length == 2 and .[0].bdf == "0000:41:00.0" and .[0].render_nodes == [] and .[1].render_nodes == ["renderD129"]' <<<"$gpus_changed" >/dev/null
rm -- "$sys/bus/pci/devices/0000:41:00.0/resource"
ws_pci_amd_gpus "$sys" | jq -e '.[0].bars == null and .[0].render_nodes == []' >/dev/null

nvmes="$(ws_hardware_nvme_json "$sys")"
jq -e 'length == 1 and .[0].serial == "SN123" and .[0].namespaces[0].capacity_bytes == 1024000000 and .[0].thermal == null and .[0].smart == null' <<<"$nvmes" >/dev/null
printf '4096\n' >"$sys/block/nvme0n1/queue/logical_block_size"
ws_hardware_nvme_json "$sys" | jq -e '.[0].namespaces[0].logical_block_size == 4096 and .[0].namespaces[0].capacity_bytes == 1024000000' >/dev/null
mkdir "$work/smart"
printf '{"temperature":42,"percentage_used":1}\n' >"$work/smart/nvme0-smart.txt"
nvmes_smart="$(ws_hardware_nvme_json "$sys" "$work/smart")"
jq -e '.[0].thermal == 42 and .[0].smart.percentage_used == 1' <<<"$nvmes_smart" >/dev/null
[[ "$(ws_hardware_nvme_json "$work/absent")" == null ]]

printf 'hardware topology fixture tests passed\n'
