#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/common.sh"
source "$repo_root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

ws_load_config "$repo_root/config/workstation.conf.example"
[[ $(ws_calculate_jobs 65536 48 24 2048 16384 8192 1) == 20 ]]
[[ $(ws_calculate_jobs 65536 48 16 4096 16384 8192 1) == 10 ]]
[[ $(ws_calculate_jobs 65536 4 24 2048 16384 8192 1) == 4 ]]
if (ws_calculate_jobs 24000 48 24 2048 16384 8192 1) >/dev/null 2>&1; then
  echo 'insufficient RAM was accepted' >&2; exit 1
fi
if (ws_calculate_jobs 65536 48 24 0 16384 8192 1) >/dev/null 2>&1; then
  echo 'zero per-job memory was accepted' >&2; exit 1
fi
flags='-march=x86-64 -mtune=generic -O2 -pipe -fstack-clash-protection -fcf-protection -Wp,-D_FORTIFY_SOURCE=3 -fno-omit-frame-pointer'
native=$(ws_native_flags "$flags")
[[ $native == "${flags/-march=x86-64 -mtune=generic/-march=native -mtune=native}" ]]
[[ $(ws_native_flags "$flags -Wp,-D_GLIBCXX_ASSERTIONS") == *'-Wp,-D_GLIBCXX_ASSERTIONS' ]]
for bad in "$flags -O3" "$flags -ffast-math" "$flags -march=native" "${flags/-O2/-Os}"; do
  if (ws_native_flags "$bad") >/dev/null 2>&1; then echo 'unsafe or ambiguous CPU flags accepted' >&2; exit 1; fi
done

printf 'original\n' > "$work/destination"
printf 'original\n' > "$work/same"
ws_write_new_or_identical "$work/same" "$work/destination"
printf 'new\n' > "$work/different"
if (ws_write_new_or_identical "$work/different" "$work/destination") >/dev/null 2>&1; then exit 1; fi
[[ $(<"$work/destination") == original ]]

agents=$(ws_rocminfo_agents "$repo_root/tests/fixtures/rocminfo-two.txt")
[[ $(jq length <<< "$agents") == 2 ]]
pci='[{"bdf":"0000:01:00.0","driver":"amdgpu","device_id":"0xffff"},{"bdf":"0000:02:00.0","driver":"amdgpu","device_id":"0xffff"}]'
ws_hardware_assert_json "$pci" "$agents" 2
if ws_hardware_assert_json "$pci" "$(jq '.[0:1]' <<< "$agents")" 2; then echo 'one visible GPU accepted' >&2; exit 1; fi
if ws_hardware_assert_json "$(jq '.[1].driver="vfio-pci"' <<< "$pci")" "$agents" 2; then exit 1; fi
if ws_hardware_assert_json "$pci" "$(jq '.[1].gfx="gfx9999"' <<< "$agents")" 2; then exit 1; fi
if ws_hardware_assert_json "$(jq '.[1].bdf=.[0].bdf' <<< "$pci")" "$agents" 2; then exit 1; fi

jq -n --argjson pci "$pci" --argjson agents "$agents" \
  '{schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2},pci_gpus:$pci,rocm_agents:$agents,gpu_target:"gfx9abc"}' > "$work/hardware.json"
[[ $(ws_detected_gpu_target "$work/hardware.json") == gfx9abc ]]
ws_rocm_plan "$work/hardware.json" "$work/plan" >/dev/null
jq -e '.status=="plan-not-built" and .gpu_targets==["gfx9abc"] and .hip_compiler_launcher==null and (.cmake_args|index("-DTHEROCK_BACKGROUND_BUILD_JOBS=1")!=null)' "$work/plan/build-plan.json" >/dev/null
jq '.status="pending"' "$work/hardware.json" > "$work/pending.json"
if (ws_rocm_plan "$work/pending.json" "$work/bad-plan") >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/bad-plan ]]
if (export HSA_OVERRIDE_GFX_VERSION=99.0.0; ws_rocm_unfiltered) >/dev/null 2>&1; then exit 1; fi

mkdir -p "$work/proc" "$work/etc" "$work/sys/block"
printf 'Filename Type Size Used Priority\n' > "$work/proc/swaps"
printf 'quiet root=UUID=synthetic\n' > "$work/proc/cmdline"
printf 'UUID=synthetic / xfs defaults 0 0\n' > "$work/etc/fstab"
ws_no_swap_check "$work/proc" "$work/etc" "$work/sys" "$work/usr"
printf '/swapfile file 1024 0 -2\n' >> "$work/proc/swaps"
if (ws_no_swap_check "$work/proc" "$work/etc" "$work/sys" "$work/usr") >/dev/null 2>&1; then exit 1; fi
printf 'Filename Type Size Used Priority\n' > "$work/proc/swaps"
printf 'quiet resume=UUID=synthetic\n' > "$work/proc/cmdline"
if (ws_no_swap_check "$work/proc" "$work/etc" "$work/sys" "$work/usr") >/dev/null 2>&1; then exit 1; fi
printf 'quiet\n' > "$work/proc/cmdline"
mkdir "$work/sys/block/zram0"
if (ws_no_swap_check "$work/proc" "$work/etc" "$work/sys" "$work/usr") >/dev/null 2>&1; then exit 1; fi
rmdir "$work/sys/block/zram0"
mkdir -p "$work/usr/lib/systemd"
printf '[zram0]\n' > "$work/usr/lib/systemd/zram-generator.conf"
if (ws_no_swap_check "$work/proc" "$work/etc" "$work/sys" "$work/usr") >/dev/null 2>&1; then exit 1; fi

# Real compiler/cache execution is conditional on the local tool availability.
# It proves a hit and executable result; no mocked hit is reported as success.
if command -v ccache >/dev/null && command -v cc >/dev/null; then
  CCACHE_DIRECTORY="$work/ccache"
  ws_ccache_configure
  ws_ccache_configure
  ws_ccache_test
else
  echo 'SKIP: real ccache repeat-build test requires ccache and a C compiler'
fi
echo 'AMD hardware/build policy tests passed'
