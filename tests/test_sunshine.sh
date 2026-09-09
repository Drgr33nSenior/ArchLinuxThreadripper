#!/usr/bin/env bash
set -euo pipefail
repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

reject() {
  if ("$@") >"$work/rejected.txt" 2>&1; then
    printf 'unexpected success: %s\n' "$*" >&2
    exit 1
  fi
}

ws_load_config "$repo_root/config/workstation.conf.example"
[[ $SUNSHINE_ENCODER == vulkan && $SUNSHINE_CAPTURE == kwin && $SUNSHINE_VK_TUNE == 2 && $GAMING_WIDTH == 1920 && $GAMING_HEIGHT == 1080 ]]
ws_sunshine_profile "$work/profile" >/dev/null
grep -Fxq 'SUNSHINE_ENCODER=vulkan' "$work/profile/sunshine.env"
grep -Fxq 'SUNSHINE_CAPTURE=kwin' "$work/profile/sunshine.env"
grep -Fxq 'GAMING_WIDTH=1920' "$work/profile/sunshine.env"
grep -Fxq 'GAMING_HEIGHT=1080' "$work/profile/sunshine.env"
reject ws_sunshine_profile "$work/profile"
ln -s "$work/nonexistent" "$work/symlink"
reject ws_sunshine_profile "$work/symlink"
reject env SUNSHINE_ENCODER=software "$repo_root/bin/workstationctl" sunshine profile "$work/software"
reject env SUNSHINE_ENCODER=vulkan SUNSHINE_CAPTURE=x11 "$repo_root/bin/workstationctl" sunshine profile "$work/cpu-capture"
reject env SUNSHINE_ENCODER=vulkan SUNSHINE_CAPTURE=wlr "$repo_root/bin/workstationctl" sunshine profile "$work/wlr-capture"
reject env SUNSHINE_OUTPUT_NAME=0 "$repo_root/bin/workstationctl" sunshine profile "$work/ordinal"
reject env SUNSHINE_VK_TUNE=0 "$repo_root/bin/workstationctl" sunshine profile "$work/tune"
reject env SUNSHINE_AV1_MODE=9 "$repo_root/bin/workstationctl" sunshine profile "$work/codec"
reject env GAMING_WIDTH=18446744073709553536 "$repo_root/bin/workstationctl" sunshine profile "$work/overflow-width"
reject env GAMING_WIDTH=1919 "$repo_root/bin/workstationctl" sunshine profile "$work/odd-width"
reject env GAMING_HEIGHT=479 "$repo_root/bin/workstationctl" sunshine profile "$work/small-height"
reject env GAMING_WIDTH=7682 "$repo_root/bin/workstationctl" sunshine profile "$work/wide-width"
SUNSHINE_ENCODER=vulkan SUNSHINE_CAPTURE=portal GAMING_WIDTH=2560 GAMING_HEIGHT=1440 "$repo_root/bin/workstationctl" sunshine profile "$work/vulkan" >/dev/null
grep -Fxq 'SUNSHINE_CAPTURE=portal' "$work/vulkan/sunshine.env"
grep -Fxq 'GAMING_WIDTH=2560' "$work/vulkan/sunshine.env"
# The X11 rollback remains explicit and uses VAAPI rather than pretending that
# Vulkan Video works with CPU-copy X11 capture.
SUNSHINE_ENCODER=vaapi SUNSHINE_CAPTURE=x11 "$repo_root/bin/workstationctl" sunshine profile "$work/x11-rollback" >/dev/null
grep -Fxq 'SUNSHINE_CAPTURE=x11' "$work/x11-rollback/sunshine.env"

# Two identical synthetic AMD GPUs. Only the allocated node is accessible.
mkdir -p "$work/sys/class/drm" "$work/sys/drivers/amdgpu" "$work/dev"
for item in 128 129; do
  bdf="0000:$((item - 127))1:00.0"
  gpu="$work/sys/devices/$bdf"
  mkdir -p "$gpu/drm/card$((item - 128))" "$work/sys/class/drm/renderD$item"
  printf '0x1002\n' >"$gpu/vendor"
  printf '0x1234\n' >"$gpu/device"
  ln -s "$work/sys/drivers/amdgpu" "$gpu/driver"
  ln -s "$gpu" "$work/sys/class/drm/renderD$item/device"
  touch "$work/dev/renderD$item" "$work/dev/card$((item - 128))"
done
allocated=renderD129
accessible_mode=single
ws_sunshine_device_accessible() {
  case $accessible_mode in
    all) return 0 ;;
    none) return 1 ;;
    single) [[ ${1##*/} == "$allocated" || ${1##*/} == card1 ]] ;;
  esac
}
# shellcheck disable=SC2218 # Imported real function; replaced for launch fixtures below.
ws_sunshine_device "$work/sys" "$work/dev" >"$work/device.json"
jq -e '.bdf == "0000:21:00.0" and (.render_node|endswith("renderD129")) and (.card_node|endswith("card1"))' "$work/device.json" >/dev/null
mv "$work/dev/renderD129" "$work/dev/renderD135"
mv "$work/sys/class/drm/renderD129" "$work/sys/class/drm/renderD135"
allocated=renderD135
# shellcheck disable=SC2218 # Imported real function; replaced for launch fixtures below.
ws_sunshine_device "$work/sys" "$work/dev" >"$work/renumbered.json"
jq -e '.bdf == "0000:21:00.0" and (.render_node|endswith("renderD135"))' "$work/renumbered.json" >/dev/null
accessible_mode=all
reject ws_sunshine_device "$work/sys" "$work/dev"
accessible_mode=none
reject ws_sunshine_device "$work/sys" "$work/dev"
accessible_mode=single
printf '0x8086\n' >"$work/sys/devices/0000:21:00.0/vendor"
reject ws_sunshine_device "$work/sys" "$work/dev"
printf '0x1002\n' >"$work/sys/devices/0000:21:00.0/vendor"
mv "$work/sys/devices/0000:21:00.0/driver" "$work/sys/devices/0000:21:00.0/driver-recorded"
reject ws_sunshine_device "$work/sys" "$work/dev"

# Launch is fixture-only: no graphics process, existing config remains byte-identical.
printf 'paired configuration fixture\n' >"$work/sunshine.conf"
cp "$work/sunshine.conf" "$work/original.conf"
cat >"$work/sunshine-fixture" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$SUNSHINE_TEST_ARGS"
STUB
chmod +x "$work/sunshine-fixture"
export SUNSHINE_TEST_ARGS="$work/args.txt"
ws_require_user() { :; }
ws_sunshine_version() { printf 'Sunshine version: 2026.906.222525\n'; }
ws_sunshine_device() { jq -n '{bdf:"0000:21:00.0",render_node:"/dev/dri/renderD135",card_node:"/dev/dri/card1"}'; }
ws_sunshine_launch "$work/sunshine-fixture" "$work/sunshine.conf" 2>/dev/null
grep -Fxq 'encoder=vulkan' "$work/args.txt"
grep -Fxq 'capture=kwin' "$work/args.txt"
grep -Fxq 'adapter_name=/dev/dri/renderD135' "$work/args.txt"
cmp "$work/original.conf" "$work/sunshine.conf"
SUNSHINE_ENCODER=vulkan SUNSHINE_CAPTURE=portal ws_sunshine_launch "$work/sunshine-fixture" "$work/sunshine.conf" 2>/dev/null
grep -Fxq 'encoder=vulkan' "$work/args.txt"
grep -Fxq 'vk_rc_mode=2' "$work/args.txt"
reject env SUNSHINE_CAPTURE=kms "$repo_root/bin/workstationctl" sunshine profile "$work/kms-profile-extra-argument" ignored-argument
reject env SUNSHINE_OUTPUT_NAME='DP-1;echo injected' "$repo_root/bin/workstationctl" sunshine profile "$work/injected"
ws_sunshine_version() { printf 'Sunshine version: 2026.516.143833\n'; }
reject ws_sunshine_launch "$work/sunshine-fixture" "$work/sunshine.conf"

# Bounded diagnostic failures remain visible; no target tools execute here.
cp "$work/sunshine-fixture" "$work/sunshine"
timeout() {
  printf '%s\n' "$*" >>"$work/diagnostic-commands.txt"
  [[ $1 == --kill-after=2s ]] || return 99
  [[ $3 != vainfo ]] || return 124
}
PATH="$work:$PATH" ws_sunshine_diagnose "$work/diagnostics" >/dev/null
jq -e '.status == "diagnostic-only-not-qualified"' "$work/diagnostics/result.json" >/dev/null
jq -e -s 'any(.[]; .command == "vaapi" and .exit_code == 124 and .required == false)' "$work/diagnostics/optional-commands.jsonl" >/dev/null
grep -Fxq -- '--kill-after=2s 20s vainfo --display drm --device /dev/dri/renderD135' "$work/diagnostic-commands.txt"
grep -Fxq -- '--kill-after=2s 15s wayland-info' "$work/diagnostic-commands.txt"
grep -Fxq -- '--kill-after=2s 15s gdbus call --session --dest org.kde.KWin --object-path /KWin --method org.kde.KWin.supportInformation' "$work/diagnostic-commands.txt"

# Paired operator-entered measurements: fixtures, NOT measured performance.
jq '.provenance |= with_entries(if (.value|type) == "string" then .value="fixture" else . end) |
  .provenance.image_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" |
  .provenance.cache="warm" | .runs[0].frame_p99_ms=10 | .runs[1].frame_p99_ms=14 | .runs[2].frame_p99_ms=12' \
  "$repo_root/templates/sunshine/measurement.example.json" >"$work/baseline.json"
jq '.profile.encoder="vulkan" | .runs[0].frame_p99_ms=9 | .runs[1].frame_p99_ms=11 | .runs[2].frame_p99_ms=10' \
  "$work/baseline.json" >"$work/candidate.json"
ws_sunshine_compare "$work/baseline.json" "$work/candidate.json" "$work/comparison" >/dev/null
jq -e '.baseline.metrics.frame_p99_ms == {n:3,median:12,min:10,max:14} and
  .candidate.metrics.gpu_power_w == {n:0,median:null,min:null,max:null}' "$work/comparison/comparison.json" >/dev/null
reject ws_sunshine_compare "$work/baseline.json" "$work/candidate.json" "$work/comparison"
reject ws_sunshine_compare "$repo_root/templates/sunshine/measurement.example.json" "$work/candidate.json" "$work/placeholders"
reject ws_sunshine_compare "$work/baseline.json" "$work/baseline.json" "$work/identical"
for change in '.provenance.fps=120' '.provenance.cache="cold"' '.profile.vk_tune=3' '.runs[0].frame_p99_ms=-1' '.runs=[]'; do
  jq "$change" "$work/candidate.json" >"$work/bad.json"
  reject ws_sunshine_compare "$work/baseline.json" "$work/bad.json" "$work/bad-comparison"
done

# Builder failure paths: synthetic download and Docker, no network/build daemon.
curl() {
  local destination=''
  while (($#)); do
    if [[ $1 == --output ]]; then
      destination=$2
      shift 2
    else shift; fi
  done
  printf 'synthetic Sunshine package\n' >"$destination"
}
# shellcheck disable=SC2329 # Called indirectly by the image-build fixture.
docker() {
  printf 'called\n' >>"$work/docker-called.txt"
  return 90
}
reject ws_sunshine_image_build "$work/hash-failure"
[[ ! -e $work/docker-called.txt && ! -e $work/hash-failure/build-result.json ]]
ws_read_lock() {
  if [[ $1 == SUNSHINE_DEBIAN_SHA256 ]]; then
    common::sha256_file "$work/hash-failure/context/sunshine.deb"
  else
    common::lock_get "$repo_root/versions.lock" "$1"
  fi
}
reject ws_sunshine_image_build "$work/build-failure"
[[ -s $work/docker-called.txt && ! -e $work/build-failure/build-result.json ]]

# Target selection is explicit. The local ID is only a build receipt; it never
# authorizes a registry or Kubernetes promotion.
# shellcheck disable=SC2329 # Called indirectly by the image-build fixture.
docker() {
  local iid=''
  printf '%s\n' "$@" >"$work/docker-success-args.txt"
  while (($#)); do
    if [[ $1 == --iidfile ]]; then
      iid=$2
      shift 2
    else shift; fi
  done
  [[ -n $iid ]] || return 91
  printf '%s\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' >"$iid"
}
ws_sunshine_image_build "$work/build-wayland" >/dev/null
jq -e '.session == "wayland" and .build_target == "wayland"' "$work/build-wayland/build-result.json" >/dev/null
grep -Fxq -- '--target' "$work/docker-success-args.txt"
grep -Fxq 'wayland' "$work/docker-success-args.txt"
ws_sunshine_image_build "$work/build-x11" x11 >/dev/null
jq -e '.session == "x11" and .build_target == "x11"' "$work/build-x11/build-result.json" >/dev/null
grep -Fxq 'x11' "$work/docker-success-args.txt"
reject ws_sunshine_image_build "$work/build-invalid" legacy
[[ ! -e $work/build-invalid ]]
printf 'Sunshine profile, GPU mapping, launch and comparison fixtures passed (no GPU exercised)\n'
