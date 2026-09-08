#!/usr/bin/env bash
# Explicit streaming profiles and offline/target diagnostics; no cluster mutation.

ws_sunshine_config_validate() {
  SUNSHINE_ENCODER=${SUNSHINE_ENCODER:-vaapi}
  SUNSHINE_CAPTURE=${SUNSHINE_CAPTURE:-x11}
  SUNSHINE_OUTPUT_NAME=${SUNSHINE_OUTPUT_NAME:-}
  SUNSHINE_HEVC_MODE=${SUNSHINE_HEVC_MODE:-0}
  SUNSHINE_AV1_MODE=${SUNSHINE_AV1_MODE:-0}
  SUNSHINE_VK_TUNE=${SUNSHINE_VK_TUNE:-2}
  SUNSHINE_VAAPI_STRICT_RC_BUFFER=${SUNSHINE_VAAPI_STRICT_RC_BUFFER:-disabled}
  case $SUNSHINE_ENCODER in vaapi|vulkan) ;; *) ws_die 'SUNSHINE_ENCODER must be vaapi or vulkan' ;; esac
  case $SUNSHINE_CAPTURE in x11|kms|wlr|kwin) ;; *) ws_die 'SUNSHINE_CAPTURE must explicitly select x11, kms, wlr or kwin' ;; esac
  [[ $SUNSHINE_ENCODER != vulkan || $SUNSHINE_CAPTURE != x11 ]] \
    || ws_die 'Vulkan Video requires a qualified DMA-BUF capture path, not x11'
  [[ -z $SUNSHINE_OUTPUT_NAME || $SUNSHINE_OUTPUT_NAME =~ ^[a-zA-Z][a-zA-Z0-9_.:-]*$ ]] \
    || ws_die 'SUNSHINE_OUTPUT_NAME must be empty or a discovered connector name, not a numeric ordinal'
  [[ $SUNSHINE_HEVC_MODE =~ ^[0-3]$ && $SUNSHINE_AV1_MODE =~ ^[0-3]$ ]] || ws_die 'Sunshine codec modes must be 0..3'
  [[ $SUNSHINE_VK_TUNE == 2 || $SUNSHINE_VK_TUNE == 3 ]] || ws_die 'SUNSHINE_VK_TUNE must be 2 (low latency) or 3 (experimental ultra-low latency)'
  case $SUNSHINE_VAAPI_STRICT_RC_BUFFER in enabled|disabled) ;; *) ws_die 'SUNSHINE_VAAPI_STRICT_RC_BUFFER must be enabled or disabled' ;; esac
}

ws_sunshine_new_output() {
  [[ -n $1 && ! -e $1 && ! -L $1 ]] || ws_die 'Sunshine output directory must not already exist'
  mkdir -p -- "$(dirname -- "$1")"
  mkdir -m 0700 -- "$1"
}

ws_sunshine_env() {
  local key
  ws_sunshine_config_validate
  for key in SUNSHINE_ENCODER SUNSHINE_CAPTURE SUNSHINE_OUTPUT_NAME SUNSHINE_HEVC_MODE SUNSHINE_AV1_MODE SUNSHINE_VK_TUNE SUNSHINE_VAAPI_STRICT_RC_BUFFER; do
    printf '%s=%s\n' "$key" "${!key}"
  done
}

ws_sunshine_profile() (
  set -euo pipefail
  umask 077
  ws_sunshine_config_validate
  ws_sunshine_new_output "$1"
  ws_sunshine_env > "$1/sunshine.env"
  ws_note "wrote $1/sunshine.env; generation does not discover hardware or change a session"
)

# Opening the node checks device-cgroup access as well as Unix permissions.
# No ioctl, encoding workload or device setting is performed here.
ws_sunshine_device_accessible() (
  [[ -c $1 && -r $1 && -w $1 ]] || return 1
  exec 3<> "$1"
)

ws_sunshine_device() (
  set -euo pipefail
  local sys=${1:-/sys} dev=${2:-/dev/dri} node selected='' device bdf driver card='' entry count=0
  for node in "$dev"/renderD*; do
    if ws_sunshine_device_accessible "$node" 2>/dev/null; then
      selected=$node
      count=$((count + 1))
    fi
  done
  [[ $count == 1 ]] || ws_die "expected exactly one accessible allocated render device; found $count (run inside the one-GPU allocation)"
  device=$(cd -- "$sys/class/drm/${selected##*/}/device" && pwd -P) || ws_die 'render device has no discoverable PCI identity'
  bdf=${device##*/}
  [[ $bdf =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] || ws_die 'render device PCI identity is unknown'
  [[ $(ws_hardware_read_value "$device/vendor") == 0x1002 ]] || ws_die 'the allocated GPU is not AMD'
  driver=$(cd -- "$device/driver" && pwd -P) || ws_die 'allocated GPU driver is unknown'
  [[ ${driver##*/} == amdgpu ]] || ws_die 'the allocated GPU must use the host amdgpu driver'
  for entry in "$device"/drm/card*; do
    [[ ${entry##*/} =~ ^card[0-9]+$ ]] || continue
    if ws_sunshine_device_accessible "$dev/${entry##*/}" 2>/dev/null; then
      [[ -z $card ]] || ws_die 'ambiguous primary DRM device'
      card="$dev/${entry##*/}"
    fi
  done
  jq -n --arg bdf "$bdf" --arg render "$selected" --arg card "$card" \
    --arg device_id "$(ws_hardware_read_value "$device/device")" \
    '{schema:1,bdf:$bdf,device_id:$device_id,driver:"amdgpu",render_node:$render,
      card_node:(if $card == "" then null else $card end),
      scope:"accessible devices, not proof of exclusive ownership or capture placement"}'
)

# CLI overrides take precedence without rewriting the user's config or credentials.
ws_sunshine_args() {
  local render=$1
  ws_sunshine_config_validate
  printf '%s\n' "encoder=$SUNSHINE_ENCODER" "capture=$SUNSHINE_CAPTURE" "adapter_name=$render" \
    "hevc_mode=$SUNSHINE_HEVC_MODE" "av1_mode=$SUNSHINE_AV1_MODE" \
    "vk_tune=$SUNSHINE_VK_TUNE" 'vk_rc_mode=2' 'vaapi_rc=auto' \
    "vaapi_strict_rc_buffer=$SUNSHINE_VAAPI_STRICT_RC_BUFFER" 'min_log_level=info'
  [[ -z $SUNSHINE_OUTPUT_NAME ]] || printf 'output_name=%s\n' "$SUNSHINE_OUTPUT_NAME"
  return 0
}

ws_sunshine_launch() (
  set -euo pipefail
  local binary=$1 config=$2 device render version expected arg
  local -a args=()
  ws_require_user
  ws_sunshine_config_validate
  [[ -x $binary && -f $config && -r $config ]] || ws_die 'Sunshine binary and existing readable config are required'
  [[ $config != *'='* && $config != *$'\n'* && $config != *$'\r'* ]] || ws_die 'Sunshine config path contains unsupported CLI characters'
  config="$(cd -- "$(dirname -- "$config")" && pwd -P)/$(basename -- "$config")"
  # --version itself can initialise Sunshine config; use disposable appdata.
  version=$(ws_sunshine_version "$binary") || ws_die 'cannot determine Sunshine version'
  expected=$(ws_read_lock SUNSHINE_VERSION)
  [[ $version == *"version: $expected"* ]] || ws_die "Sunshine must match locked version $expected"
  device=$(ws_sunshine_device) || exit 1
  render=$(jq -er .render_node <<< "$device")
  if [[ $SUNSHINE_CAPTURE == kms ]]; then
    jq -e '.card_node != null' <<< "$device" >/dev/null || ws_die 'KMS capture needs the allocated primary DRM node too'
    [[ -n $SUNSHINE_OUTPUT_NAME ]] || ws_die 'KMS capture requires an explicitly discovered connector name'
  fi
  while IFS= read -r arg; do args+=("$arg"); done < <(ws_sunshine_args "$render")
  printf 'Sunshine profile: encoder=%s capture=%s GPU=%s render=%s (verify capture/render placement)\n' \
    "$SUNSHINE_ENCODER" "$SUNSHINE_CAPTURE" "$(jq -r .bdf <<< "$device")" "$render" >&2
  exec "$binary" "$config" "${args[@]}"
)

ws_sunshine_version() (
  local scratch
  scratch=$(mktemp -d)
  trap 'rm -rf -- "$scratch"' EXIT
  XDG_CONFIG_HOME="$scratch" timeout --kill-after=2s 15s "$1" --version
)

ws_sunshine_diagnose() (
  set -euo pipefail
  umask 077
  local output=$1 device render binary
  ws_sunshine_config_validate
  command -v timeout >/dev/null || ws_die 'diagnostics require coreutils timeout'
  device=$(ws_sunshine_device) || exit 1
  ws_sunshine_new_output "$output"
  printf '%s\n' "$device" > "$output/device.json"
  ws_sunshine_env > "$output/sunshine.env"
  render=$(jq -er .render_node <<< "$device")
  ws_sunshine_args "$render" > "$output/overrides.txt"
  binary=$(command -v sunshine) || ws_die 'Sunshine is not installed in this environment'
  if [[ -x /usr/lib/workstation/sunshine.real ]]; then binary=/usr/lib/workstation/sunshine.real; fi
  ws_capture "$output" sunshine-version ws_sunshine_version "$binary"
  ws_capture "$output" kernel uname -srmo
  ws_capture_optional "$output" vaapi timeout --kill-after=2s 20s vainfo --display drm --device "$render"
  ws_capture_optional "$output" vulkan timeout --kill-after=2s 20s vulkaninfo --summary
  ws_capture_optional "$output" ffmpeg timeout --kill-after=2s 15s ffmpeg -version
  ws_capture_optional "$output" display timeout --kill-after=2s 15s xrandr --query
  ws_capture_optional "$output" renderer timeout --kill-after=2s 15s glxinfo -B
  ws_capture_optional "$output" audio timeout --kill-after=2s 15s pactl list short sinks
  if command -v dpkg-query >/dev/null; then
    # shellcheck disable=SC2016 # dpkg-query expands these fields, not the shell.
    ws_capture "$output" packages dpkg-query -W '-f=${binary:Package}\t${Version}\n' sunshine libgl1-mesa-dri mesa-libgallium mesa-vulkan-drivers libva2 libvulkan1 ffmpeg
  elif command -v pacman >/dev/null; then
    ws_capture "$output" packages pacman -Q sunshine mesa libva-mesa-driver vulkan-radeon libva ffmpeg
  fi
  jq -n --arg version "$(ws_read_lock SUNSHINE_VERSION)" \
    '{schema:1,status:"diagnostic-only-not-qualified",expected_sunshine:$version,
      not_measured:["exclusive ownership","capture GPU","hardware encode","input injection","stream quality","latency","frame times","power"]}' \
    > "$output/result.json"
  ws_note "wrote $output; inspect command exit codes, not just device discovery"
)

ws_sunshine_image_build() (
  set -euo pipefail
  umask 077
  local output=$1 root base version hash url mesa image_id
  root=$(ws_repo_root)
  base=$(ws_read_lock STEAM_HEADLESS_BASE_IMAGE)
  version=$(ws_read_lock SUNSHINE_VERSION)
  hash=$(ws_read_lock SUNSHINE_DEBIAN_SHA256)
  mesa=$(ws_read_lock SUNSHINE_MESA_DEBIAN_VERSION)
  [[ $base =~ ^docker\.io/josh5/steam-headless@sha256:[a-f0-9]{64}$ && $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $hash =~ ^[a-f0-9]{64}$ ]] \
    || ws_die 'invalid gaming image lock'
  [[ $mesa =~ ^[0-9][a-zA-Z0-9.+~:-]+$ ]] || ws_die 'invalid Mesa package version'
  command -v docker >/dev/null || ws_die 'image-build requires an existing Docker builder'
  ws_sunshine_new_output "$output"
  output=$(cd -- "$output" && pwd -P)
  case "$output/" in
    "$root/lib/"*|"$root/bin/"*|"$root/templates/sunshine/"*|"$root/infrastructure/gaming/"*)
      ws_die 'image output must be outside its source inputs; empty output directory retained' ;;
  esac
  mkdir -p "$output/context/bin" "$output/context/infrastructure/gaming" "$output/context/templates"
  cp "$root/bin/workstationctl" "$output/context/bin/"
  cp -R "$root/lib" "$output/context/"
  cp -R "$root/templates/sunshine" "$output/context/templates/"
  cp "$root/versions.lock" "$output/context/"
  cp "$root/infrastructure/gaming/"* "$output/context/infrastructure/gaming/"
  url="https://github.com/LizardByte/Sunshine/releases/download/v$version/sunshine_${version}-1%2Bdebiantrixie_amd64.deb"
  curl --fail --location --proto '=https' --proto-redir '=https' --max-time 180 --output "$output/context/sunshine.deb" "$url" \
    || ws_die 'Sunshine download failed; partial output retained'
  common::verify_sha256 "$output/context/sunshine.deb" "$hash"
  docker build --platform linux/amd64 --build-arg "BASE_IMAGE=$base" \
    --build-arg "SUNSHINE_SHA256=$hash" --build-arg "MESA_VERSION=$mesa" --iidfile "$output/image.id" \
    -f "$output/context/infrastructure/gaming/Dockerfile" "$output/context" \
    || ws_die 'gaming image build failed; partial output retained'
  image_id=$(< "$output/image.id")
  [[ $image_id =~ ^sha256:[a-f0-9]{64}$ ]] || ws_die 'builder did not return a local SHA-256 image ID'
  jq -n --arg base "$base" --arg sunshine "$version" --arg sha256 "$hash" --arg mesa "$mesa" --arg image_id "$image_id" \
    '{schema:1,status:"built-not-qualified",base:$base,sunshine:$sunshine,sunshine_deb_sha256:$sha256,
      mesa_debian_version:$mesa,local_image_id:$image_id,note:"local config ID is NOT a registry manifest digest; dependencies recorded inside image"}' > "$output/build-result.json"
  ws_note "built local candidate; no registry publication or cluster changes. Result: $output/build-result.json"
)

ws_sunshine_compare() (
  set -euo pipefail
  umask 077
  local baseline=$1 candidate=$2 output=$3 result
  result=$(jq -e -s -f "$(ws_repo_root)/templates/sunshine/compare.jq" "$baseline" "$candidate") \
    || ws_die 'invalid or incomparable streaming measurements; see docs/SUNSHINE.md'
  ws_sunshine_new_output "$output"
  printf '%s\n' "$result" > "$output/comparison.json"
  ws_note "wrote $output/comparison.json; measurements are operator supplied, not hardware qualification"
)
