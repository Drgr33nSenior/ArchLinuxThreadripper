#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

write_hardware() {
  local path=$1 memory_bytes=$2 dimm_count=$3 topology=$4 dimms='[]' number
  for ((number = 1; number <= dimm_count; number++)); do
    dimms=$(jq --arg locator "DIMM_${number}" '. + [{locator:$locator,size:"32 GiB"}]' <<< "$dimms")
  done
  jq -n --argjson memory "$memory_bytes" --argjson dimms "$dimms" --argjson topology "$topology" '
    {schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2,gpu_model:"R9700"},gpu_target:"gfx1201",
     pci_gpus:[{bdf:"0000:01:00.0",device_id:"0x1234",driver:"amdgpu",render_nodes:["renderD128"]},{bdf:"0000:02:00.0",device_id:"0x1234",driver:"amdgpu",render_nodes:["renderD129"]}],
     rocm_agents:[{agent:"1",gfx:"gfx1201",uuid:"GPU-1"},{agent:"2",gfx:"gfx1201",uuid:"GPU-2"}],
     cpu_topology:$topology,memory:{total_bytes:$memory,available_bytes:($memory - 1073741824),dimms:$dimms}}' > "$path"
}

contiguous_topology=$(jq -cn '{
  online_cpus:[0,1,2,3,4,5,6,7],
  cpus:[
    {cpu:0,core_id:0,socket_id:0,thread_siblings:[0,1]},{cpu:1,core_id:0,socket_id:0,thread_siblings:[0,1]},
    {cpu:2,core_id:1,socket_id:0,thread_siblings:[2,3]},{cpu:3,core_id:1,socket_id:0,thread_siblings:[2,3]},
    {cpu:4,core_id:2,socket_id:0,thread_siblings:[4,5]},{cpu:5,core_id:2,socket_id:0,thread_siblings:[4,5]},
    {cpu:6,core_id:3,socket_id:0,thread_siblings:[6,7]},{cpu:7,core_id:3,socket_id:0,thread_siblings:[6,7]}]}')
noncontiguous_topology=$(jq -cn '{
  online_cpus:[0,1,2,3,4,5,6,7],
  cpus:[
    {cpu:0,core_id:0,socket_id:0,thread_siblings:[0,4]},{cpu:4,core_id:0,socket_id:0,thread_siblings:[0,4]},
    {cpu:1,core_id:1,socket_id:0,thread_siblings:[1,5]},{cpu:5,core_id:1,socket_id:0,thread_siblings:[1,5]},
    {cpu:2,core_id:2,socket_id:0,thread_siblings:[2,6]},{cpu:6,core_id:2,socket_id:0,thread_siblings:[2,6]},
    {cpu:3,core_id:3,socket_id:0,thread_siblings:[3,7]},{cpu:7,core_id:3,socket_id:0,thread_siblings:[3,7]}]}')

write_hardware "$work/two-dimm.json" $((64 * 1024 * 1024 * 1024)) 2 "$contiguous_topology"
write_hardware "$work/four-dimm.json" $((128 * 1024 * 1024 * 1024)) 4 "$contiguous_topology"
ws_resources_plan "$work/two-dimm.json" "$work/two-plan" >/dev/null
ws_resources_plan "$work/four-dimm.json" "$work/four-plan" >/dev/null
jq -e '(.memory.total_mib == 65536 and .memory.allocatable_mib == 47104 and (.memory.dimms|length) == 2 and .reserved_cpus == [0,1,2,3,4,5])' "$work/two-plan/resource-plan.json" >/dev/null
jq -e '(.memory.total_mib == 131072 and .memory.allocatable_mib == 112640 and (.memory.dimms|length) == 4 and .reserved_cpus == [0,1,2,3,4,5])' "$work/four-plan/resource-plan.json" >/dev/null

write_hardware "$work/noncontiguous.json" $((64 * 1024 * 1024 * 1024)) 2 "$noncontiguous_topology"
ws_resources_plan "$work/noncontiguous.json" "$work/noncontiguous-plan" >/dev/null
jq -e '.reserved_cpus == [0,1,2,4,5,6] and .smt_widths == [2]' "$work/noncontiguous-plan/resource-plan.json" >/dev/null

RESOURCE_RESERVED_CORES=2 RESOURCE_HOST_RESERVE_MIB=16384 RESOURCE_KUBE_RESERVE_MIB=8192 RESOURCE_EVICTION_MIB=4096 ws_resources_plan "$work/two-dimm.json" "$work/override-plan" >/dev/null
jq -e '.reserved_cpus == [0,1,2,3] and .reserved_physical_cores == 2 and .memory.allocatable_mib == 36864' "$work/override-plan/resource-plan.json" >/dev/null

write_hardware "$work/unknown-topology.json" $((64 * 1024 * 1024 * 1024)) 2 null
if (ws_resources_plan "$work/unknown-topology.json" "$work/unknown-plan") >/dev/null 2>&1; then
  printf 'unknown CPU topology was accepted\n' >&2
  exit 1
fi
[[ ! -e $work/unknown-plan ]]

asymmetric_topology=$(jq '.cpus[1].thread_siblings = [1]' <<< "$contiguous_topology")
write_hardware "$work/asymmetric-topology.json" $((64 * 1024 * 1024 * 1024)) 2 "$asymmetric_topology"
if (ws_resources_plan "$work/asymmetric-topology.json" "$work/asymmetric-plan") >/dev/null 2>&1; then
  printf 'asymmetric SMT topology was accepted\n' >&2
  exit 1
fi
[[ ! -e $work/asymmetric-plan ]]

printf 'K3S_VERSION=v1.36.0+k3s1\n' > "$work/unsupported.lock"
if (ws_resources_plan "$work/two-dimm.json" "$work/unsupported-plan" "$work/unsupported.lock") >/dev/null 2>&1; then
  printf 'unsupported K3s minor was accepted\n' >&2
  exit 1
fi
[[ ! -e $work/unsupported-plan ]]

write_hardware "$work/insufficient-ram.json" $((16 * 1024 * 1024 * 1024)) 2 "$contiguous_topology"
if (ws_resources_plan "$work/insufficient-ram.json" "$work/insufficient-plan") >/dev/null 2>&1; then
  printf 'insufficient resource-plan RAM was accepted\n' >&2
  exit 1
fi
[[ ! -e $work/insufficient-plan ]]

ws_build_session_inhibit_path() { printf '%s\n' "$work/build-inhibit"; }
uname() { [[ ${1:-} == -s ]] && printf 'Linux\n'; }
nproc() { printf '48\n'; }
ws_available_memory_mib() { printf '65536\n'; }
[[ $(ws_build_jobs normal) == 20 ]]
printf 'gaming transition\n' > "$work/build-inhibit"
if (ws_build_jobs normal) >/dev/null 2>&1; then
  printf 'active managed-build inhibit marker was ignored\n' >&2
  exit 1
fi
chmod 000 "$work/build-inhibit"
if [[ ! -r $work/build-inhibit ]]; then
  if (ws_build_jobs normal) > "$work/unreadable-marker.txt" 2>&1; then
    printf 'unreadable managed-build inhibit marker was ignored\n' >&2
    exit 1
  fi
  grep -Fq -- 'unreadable or unsafe' "$work/unreadable-marker.txt"
fi
chmod 600 "$work/build-inhibit"
rm -- "$work/build-inhibit"
ln -s "$work/no-readable-marker" "$work/build-inhibit"
if (ws_build_jobs normal) >/dev/null 2>&1; then
  printf 'unsafe or unreadable managed-build inhibit marker was ignored\n' >&2
  exit 1
fi

printf 'Resource plan and managed-build inhibit tests passed\n'
