#!/usr/bin/env bash
# Runtime actions for the installed workstation.  Source through workstationctl.

set -o errexit -o nounset -o pipefail

# shellcheck source=lib/workstation/build.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/build.sh"
# shellcheck source=lib/workstation/hardware.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/hardware.sh"
# shellcheck source=lib/workstation/rocm.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/rocm.sh"
# shellcheck source=lib/workstation/packages.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/packages.sh"
# shellcheck source=lib/workstation/resources.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/resources.sh"
# shellcheck source=lib/workstation/session.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/session.sh"
# shellcheck source=lib/workstation/sunshine.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/sunshine.sh"
# shellcheck source=lib/workstation/rag.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/rag.sh"
# shellcheck source=lib/workstation/agents.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/agents.sh"
# shellcheck source=lib/workstation/performance.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/performance.sh"
# shellcheck source=lib/workstation/telemetry.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/telemetry.sh"

ws_die() {
  printf 'workstationctl: %s\n' "$*" >&2
  exit 1
}
ws_note() { printf '%s\n' "$*"; }
ws_command() { command "$@"; }

ws_repo_root() {
  local here
  here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
  printf '%s\n' "$here"
}

ws_require_arch() {
  [[ -r /etc/arch-release ]] || ws_die 'this action is supported only on an installed Arch Linux system'
}

ws_require_root() {
  [[ "${EUID}" -eq 0 ]] || ws_die 'this action must be run as root'
}

ws_require_user() {
  [[ "${EUID}" -ne 0 ]] || ws_die 'run this action as the target non-root user'
}

ws_safe_dir() {
  local dir="$1"
  [[ -n "$dir" && "$dir" != / && "$dir" != . ]] || ws_die 'refusing an unsafe directory'
  mkdir -p -- "$dir"
}

ws_read_lock() {
  local key="$1" lock="${2:-$(ws_repo_root)/versions.lock}" line found=''
  [[ -r "$lock" ]] || ws_die "lock file is not readable: $lock"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" == "$key="* ]]; then
      [[ -z "$found" ]] || ws_die "duplicate lock key: $key"
      found="${line#*=}"
    fi
  done <"$lock"
  [[ -n "$found" ]] || ws_die "missing lock key: $key"
  printf '%s\n' "$found"
}

ws_load_config() {
  local config_file="$1"
  unset SESSION_AI_STARTUP_TIMEOUT_SECONDS WORKSTATION_PYTHON LLAMA_THREADS LLAMA_SPLIT_MODE LLAMA_KV_K LLAMA_KV_V LLAMA_BENCH_PAIRS GAMING_REFRESH_HZ LLAMA_HIP_EXPORT_METRICS
  unset LLM_DATA_DIR LLM_LISTEN LLM_SHM_SIZE DEFAULT_TUNED_PROFILE MAKE_JOBS MEMORY_HEAVY_JOBS
  unset BUILD_RESERVE_MIB BUILD_JOB_MIB BUILD_HEAVY_JOB_MIB BUILD_LINK_MIB BUILD_LINK_JOBS CCACHE_DIRECTORY CCACHE_MAX_SIZE EXPECTED_GPU_COUNT EXPECTED_GPU_MODEL ROCM_SDK_PROVIDER
  unset LLAMA_CONTEXT_SIZE LLAMA_BATCH_SIZE LLAMA_UBATCH_SIZE LLAMA_FLASH_ATTN LLAMA_BENCH_PROMPT_TOKENS LLAMA_BENCH_GENERATION_TOKENS LLAMA_BENCH_REPETITIONS
  unset RESOURCE_RESERVED_CORES RESOURCE_HOST_RESERVE_MIB RESOURCE_KUBE_RESERVE_MIB RESOURCE_EVICTION_MIB
  unset SESSION_CONTEXT SESSION_NODE SESSION_NAMESPACE SESSION_AI_DEPLOYMENT SESSION_GAME_DEPLOYMENT SESSION_ENVIRONMENT SESSION_TIMEOUT_SECONDS SESSION_STREAMING_PATH
  unset SUNSHINE_ENCODER SUNSHINE_CAPTURE SUNSHINE_OUTPUT_NAME SUNSHINE_HEVC_MODE SUNSHINE_AV1_MODE SUNSHINE_VK_TUNE SUNSHINE_VAAPI_STRICT_RC_BUFFER GAMING_WIDTH GAMING_HEIGHT
  unset AGENT_HARNESS AGENT_BASE_URL AGENT_MODEL AGENT_CONTEXT_TOKENS AGENT_MAX_OUTPUT_TOKENS
  unset TELEMETRY_ENABLED TELEMETRY_GPU_EXPORTER TELEMETRY_SGLANG_TRACE TELEMETRY_API_ADDRESS TELEMETRY_WORKSTATION_ADDRESS TELEMETRY_RESERVE_MIB TELEMETRY_WORKLOADS
  unset TELEMETRY_KUBELET TELEMETRY_NODE_NAME TELEMETRY_PROFILE TELEMETRY_MARGIN_MIB INFERENCE_CACHE_ROOT INFERENCE_CACHE_FREE_RESERVE_MIB
  common::load_config "$config_file" SESSION_AI_STARTUP_TIMEOUT_SECONDS WORKSTATION_PYTHON LLAMA_THREADS LLAMA_SPLIT_MODE LLAMA_KV_K LLAMA_KV_V LLAMA_BENCH_PAIRS GAMING_REFRESH_HZ LLAMA_HIP_EXPORT_METRICS LLM_DATA_DIR LLM_LISTEN LLM_SHM_SIZE DEFAULT_TUNED_PROFILE \
    MAKE_JOBS MEMORY_HEAVY_JOBS BUILD_RESERVE_MIB BUILD_JOB_MIB BUILD_HEAVY_JOB_MIB \
    BUILD_LINK_MIB BUILD_LINK_JOBS CCACHE_DIRECTORY CCACHE_MAX_SIZE EXPECTED_GPU_COUNT EXPECTED_GPU_MODEL ROCM_SDK_PROVIDER \
    LLAMA_CONTEXT_SIZE LLAMA_BATCH_SIZE LLAMA_UBATCH_SIZE LLAMA_FLASH_ATTN \
    LLAMA_BENCH_PROMPT_TOKENS LLAMA_BENCH_GENERATION_TOKENS LLAMA_BENCH_REPETITIONS \
    RESOURCE_RESERVED_CORES RESOURCE_HOST_RESERVE_MIB RESOURCE_KUBE_RESERVE_MIB RESOURCE_EVICTION_MIB \
    SESSION_CONTEXT SESSION_NODE SESSION_NAMESPACE SESSION_AI_DEPLOYMENT SESSION_GAME_DEPLOYMENT \
    SESSION_ENVIRONMENT SESSION_TIMEOUT_SECONDS SESSION_STREAMING_PATH \
    SUNSHINE_ENCODER SUNSHINE_CAPTURE SUNSHINE_OUTPUT_NAME SUNSHINE_HEVC_MODE SUNSHINE_AV1_MODE SUNSHINE_VK_TUNE SUNSHINE_VAAPI_STRICT_RC_BUFFER GAMING_WIDTH GAMING_HEIGHT \
    AGENT_HARNESS AGENT_BASE_URL AGENT_MODEL AGENT_CONTEXT_TOKENS AGENT_MAX_OUTPUT_TOKENS \
    TELEMETRY_ENABLED TELEMETRY_GPU_EXPORTER TELEMETRY_SGLANG_TRACE TELEMETRY_API_ADDRESS TELEMETRY_WORKSTATION_ADDRESS TELEMETRY_RESERVE_MIB TELEMETRY_WORKLOADS \
    TELEMETRY_KUBELET TELEMETRY_NODE_NAME TELEMETRY_PROFILE TELEMETRY_MARGIN_MIB INFERENCE_CACHE_ROOT INFERENCE_CACHE_FREE_RESERVE_MIB
  common::require_values LLM_DATA_DIR LLM_LISTEN LLM_SHM_SIZE DEFAULT_TUNED_PROFILE MAKE_JOBS MEMORY_HEAVY_JOBS
  [[ "$LLM_LISTEN" == 127.0.0.1:8000 ]] || ws_die 'LLM_LISTEN must remain 127.0.0.1:8000'
  [[ "$LLM_SHM_SIZE" =~ ^[1-9][0-9]*[mMgG]$ ]] || ws_die 'LLM_SHM_SIZE is invalid'
  [[ "$MAKE_JOBS" =~ ^[1-9][0-9]*$ && "$MEMORY_HEAVY_JOBS" =~ ^[1-9][0-9]*$ ]] || ws_die 'build job counts must be positive integers'
  case "$DEFAULT_TUNED_PROFILE" in server | desktop | build | ai | game | vm) ;; *) ws_die 'DEFAULT_TUNED_PROFILE is invalid' ;; esac
  ws_rocm_sdk_provider >/dev/null
  WORKSTATION_LLM_DATA_DIR=$LLM_DATA_DIR
  WORKSTATION_LLM_LISTEN=$LLM_LISTEN
  WORKSTATION_LLM_SHM_SIZE=$LLM_SHM_SIZE
  WORKSTATION_DEFAULT_TUNED_PROFILE=$DEFAULT_TUNED_PROFILE
  WORKSTATION_MAKE_JOBS=$MAKE_JOBS
  WORKSTATION_MEMORY_HEAVY_JOBS=$MEMORY_HEAVY_JOBS
  ws_build_config_validate
  ws_resource_config_validate
  ws_llama_runtime_config_validate
  ws_llama_benchmark_config_validate
  ws_sunshine_config_validate
  ws_agent_config_validate
  ws_telemetry_config_validate
}

ws_assert_clean_locked_checkout() {
  local source_dir="$1" expected_repo="$2" expected_commit="$3" actual_repo actual_commit status
  [[ -d "$source_dir/.git" && -f "$source_dir/PKGBUILD" ]] || ws_die "AUR checkout must contain .git and PKGBUILD: $source_dir"
  actual_repo="$(git -C "$source_dir" remote get-url origin)"
  actual_commit="$(git -C "$source_dir" rev-parse HEAD)"
  status="$(git -C "$source_dir" status --porcelain=v1 --untracked-files=all)"
  [[ "$actual_repo" == "$expected_repo" && "$actual_commit" == "$expected_commit" ]] || ws_die 'AUR checkout does not match the locked repository and commit'
  [[ -z "$status" ]] || ws_die 'AUR checkout is dirty or contains untracked files'
}

ws_validate_clean_chroot() {
  local requested="$1" resolved
  [[ "$requested" == /* ]] || ws_die 'clean-chroot directory must be an absolute path'
  resolved="$(realpath -e -- "$requested")" || ws_die 'clean-chroot directory does not exist'
  [[ "$resolved" == /var/lib/archbuild/* ]] ||
    ws_die 'clean-chroot directory must resolve below /var/lib/archbuild'
  [[ -d "$resolved/root/var/lib/pacman/local" && -x "$resolved/root/usr/bin/pacman" && -r "$resolved/root/etc/arch-release" ]] ||
    ws_die 'clean-chroot root is not an initialized mkarchroot environment'
  printf '%s\n' "$resolved"
}

ws_copy_built_packages() {
  local build_dir="$1" output_dir="$2" package found=0
  ws_safe_dir "$output_dir"
  shopt -s nullglob
  for package in "$build_dir"/*.pkg.tar.*; do
    [[ "$package" != *.sig ]] || continue
    install -m 0644 -- "$package" "$output_dir/"
    found=1
  done
  shopt -u nullglob
  ((found == 1)) || ws_die "build produced no package artifacts in $build_dir"
}

ws_makepkg_configure() {
  ws_makepkg_native_configure "$@"
}

ws_status() {
  ws_require_arch
  ws_note '== kernel =='
  uname -a
  ws_note '== TuneD =='
  ws_command tuned-adm active 2>&1 || true
  ws_note '== display/compute devices =='
  ws_command lspci -nnk 2>&1 || true
  ws_note '== DRM devices =='
  find /dev/dri -maxdepth 1 -type c -printf '%f\n' 2>/dev/null || true
  ws_note '== root storage =='
  findmnt -no SOURCE,FSTYPE,OPTIONS / 2>&1 || true
}

ws_boot_benchmark() {
  ws_require_arch
  local output="${1:-/var/lib/workstation/benchmarks/boot-$(date -u +%Y%m%dT%H%M%SZ)}"
  ws_safe_dir "$output"
  systemd-analyze time >"$output/time.txt"
  systemd-analyze critical-chain >"$output/critical-chain.txt"
  systemd-analyze blame >"$output/blame.txt"
  systemd-analyze plot >"$output/boot.svg"
  ws_note "wrote boot benchmark to $output"
}

ws_profile() {
  ws_require_arch
  ws_require_root
  local requested="${1:-${WORKSTATION_DEFAULT_TUNED_PROFILE:-server}}" tuned
  case "$requested" in
    server) tuned=balanced ;;
    desktop) tuned=desktop ;;
    build) tuned=throughput-performance ;;
    ai) tuned=accelerator-performance ;;
    game) tuned=throughput-performance ;;
    vm) tuned=virtual-host ;;
    *) ws_die 'profile must be server, desktop, build, ai, game, or vm' ;;
  esac
  tuned-adm profile "$tuned"
  ws_note "active TuneD profile: $tuned"
}

ws_build_environment() {
  ws_emit_build_environment "$@"
}

ws_kernel_stage_native_config() {
  local source_dir="$1" expected_commit="$2" build_dir="$3" template="$4" pristine
  [[ -r "$template" ]] || ws_die "native kernel configuration template is not readable: $template"
  [[ -f "$build_dir/config.user" && ! -L "$build_dir/config.user" ]] ||
    ws_die 'copied AUR tree has no regular config.user file; refusing configuration collision'
  pristine="$(mktemp "$build_dir/.config.user.pristine.XXXXXX")"
  git -C "$source_dir" show "$expected_commit:config.user" >"$pristine"
  cmp -s "$pristine" "$build_dir/config.user" ||
    ws_die 'copied AUR config.user differs from the locked checkout; refusing configuration collision'
  rm -- "$pristine"
  install -m 0644 -- "$template" "$build_dir/config.user"
}

ws_kernel_reject_chroot_overrides() {
  local chroot_dir="$1" relative path
  for relative in etc/linux-git/config etc/linux-git/remote etc/linux-git/patches/patches; do
    path="$chroot_dir/root/$relative"
    [[ ! -e "$path" && ! -L "$path" ]] ||
      ws_die "clean chroot contains a linux-git user override; refusing to build with $path"
  done
}

ws_kernel_extract_effective_config() {
  local headers_package="$1" destination="$2" config_member makefile_member
  config_member="$(bsdtar -tf "$headers_package" | awk '/\/build\/\.config$/ { n++; value=$0 } END { if (n == 1) print value; else exit 1 }')" ||
    ws_die 'linux-git-headers package does not contain one effective build/.config'
  makefile_member="$(bsdtar -tf "$headers_package" | awk '/\/build\/arch\/x86\/Makefile$/ { n++; value=$0 } END { if (n == 1) print value; else exit 1 }')" ||
    ws_die 'linux-git-headers package does not contain one arch/x86/Makefile'
  bsdtar -xOf "$headers_package" "$config_member" >"$destination"
  # The effective headers must preserve the native Kconfig mapping in both the
  # C and Rust compiler paths, rather than relying on caller CFLAGS/RUSTFLAGS.
  bsdtar -xOf "$headers_package" "$makefile_member" | grep -Eq '^[[:space:]]*KBUILD_CFLAGS[[:space:]]*\+=[[:space:]]*-march=native$' ||
    ws_die 'linux-git headers do not map CONFIG_X86_NATIVE_CPU to -march=native'
  bsdtar -xOf "$headers_package" "$makefile_member" | grep -Eq '^[[:space:]]*KBUILD_RUSTFLAGS[[:space:]]*\+=[[:space:]]*-Ctarget-cpu=native$' ||
    ws_die 'linux-git headers do not map CONFIG_X86_NATIVE_CPU to Rust target-cpu=native'
}

ws_kernel_validate_effective_config() {
  local config="$1" option
  [[ -s "$config" ]] || ws_die 'effective kernel configuration is empty'
  for option in \
    CONFIG_X86_NATIVE_CPU=y \
    CONFIG_SMP=y \
    CONFIG_CPU_MITIGATIONS=y \
    CONFIG_MODULE_SIG=y \
    CONFIG_IOMMU_SUPPORT=y \
    CONFIG_AMD_IOMMU=y; do
    grep -Fxq -- "$option" "$config" ||
      ws_die "effective kernel configuration is missing required option: $option"
  done
  for option in CONFIG_DRM_AMDGPU CONFIG_HSA_AMD; do
    grep -Eq "^${option}=[ym]$" "$config" ||
      ws_die "effective kernel configuration is missing required option: $option"
  done
}

ws_kernel_write_toolchain_metadata() {
  local headers_package="$1" destination="$2" buildinfo_member
  buildinfo_member="$(bsdtar -tf "$headers_package" | awk '/(^|\/)\.BUILDINFO$/ { n++; value=$0 } END { if (n == 1) print value; else exit 1 }')" ||
    ws_die 'linux-git-headers package does not contain one .BUILDINFO file'
  bsdtar -xOf "$headers_package" "$buildinfo_member" >"$destination"
  [[ -s "$destination" ]] || ws_die 'linux-git-headers .BUILDINFO is empty'
}

ws_kernel_write_toolchain_summary() {
  local buildinfo="$1" destination="$2" tool
  : >"$destination"
  for tool in gcc clang rust binutils make; do
    if grep -Eq "^installed = ${tool}-[0-9]" "$buildinfo"; then
      awk -v tool="$tool" '$0 ~ "^installed = " tool "-[0-9]" { sub("^installed = " tool "-", ""); print tool "=" $0; exit }' "$buildinfo" >>"$destination"
    else
      printf '%s=absent-from-package-buildinfo\n' "$tool" >>"$destination"
    fi
  done
}

ws_kernel_namcap_audit() {
  local report="$1"
  shift
  namcap "$@" >"$report" 2>&1 || ws_die "namcap could not audit kernel packages: $report"
  if grep -Eq '(^|[[:space:]])(E|ERROR|Error):' "$report"; then
    ws_die "namcap reported errors; review $report"
  fi
}

ws_kernel_build() {
  ws_require_arch
  ws_require_user
  local source_dir="$1" chroot_dir="$2" output_dir="$3" lock_file="${4:-$(ws_repo_root)/versions.lock}"
  local expected_repo expected_commit source_repo source_commit build_dir template jobs headers_package effective_config config_delta buildinfo toolchain_metadata namcap_report package package_hashes diff_status
  [[ $(uname -m) == x86_64 ]] || ws_die 'native kernel build is supported only on the x86_64 target host'
  [[ ! -e "$output_dir" && ! -L "$output_dir" ]] || ws_die "kernel output already exists; refusing to replace candidate: $output_dir"
  chroot_dir="$(ws_validate_clean_chroot "$chroot_dir")"
  ws_kernel_reject_chroot_overrides "$chroot_dir"
  [[ -r "$lock_file" ]] || ws_die 'kernel lock file is not readable'
  expected_repo="$(ws_read_lock LINUX_GIT_AUR_REPOSITORY "$lock_file")"
  expected_commit="$(ws_read_lock LINUX_GIT_AUR_COMMIT "$lock_file")"
  source_repo="$(ws_read_lock LINUX_GIT_SOURCE_REPOSITORY "$lock_file")"
  source_commit="$(ws_read_lock LINUX_GIT_SOURCE_COMMIT "$lock_file")"
  [[ "$source_repo" == torvalds/linux && "$source_commit" =~ ^[a-f0-9]{40}$ ]] || ws_die 'linux-git source lock is invalid'
  ws_assert_clean_locked_checkout "$source_dir" "$expected_repo" "$expected_commit"
  command -v makechrootpkg >/dev/null || ws_die 'makechrootpkg is required (devtools)'
  command -v namcap >/dev/null || ws_die 'namcap is required for the AUR package audit'
  command -v bsdtar >/dev/null || ws_die 'bsdtar is required to validate the packaged kernel configuration'
  build_dir="$(mktemp -d "${TMPDIR:-/tmp}/workstationctl-linux-git.XXXXXX")"
  git -C "$source_dir" archive --format=tar "$expected_commit" | tar -xf - -C "$build_dir"
  template="$(ws_repo_root)/templates/workstation/linux-git.config"
  ws_kernel_stage_native_config "$source_dir" "$expected_commit" "$build_dir" "$template"
  printf 'REMOTE=%q\nCOMMIT=%q\n' "$source_repo" "$source_commit" >"$build_dir/remote"
  (
    cd -- "$build_dir"
    # User-space flags and its ccache are not shared with the clean chroot.
    # The AUR kernel Kconfig, not these variables, supplies native C/Rust flags.
    jobs="$(ws_build_jobs memory-heavy)"
    env -u CFLAGS -u CXXFLAGS -u RUSTFLAGS -u KCFLAGS -u KCPPFLAGS \
      -u CCACHE_DIR -u CCACHE_CONFIGPATH -u CCACHE_BASEDIR -u CCACHE_NAMESPACE MAKEFLAGS="-j$jobs" \
      makechrootpkg -c -n -r "$chroot_dir" -- --syncdeps
    printf '%s\n' "$jobs" >"$build_dir/linux-git.measured-jobs"
  )
  shopt -s nullglob
  local packages=("$build_dir"/*.pkg.tar.*)
  shopt -u nullglob
  ((${#packages[@]} > 0)) || ws_die "kernel build produced no package artifacts in $build_dir"
  headers_package=''
  for package in "${packages[@]}"; do
    [[ $package == */linux-git-headers-*.pkg.tar.* ]] || continue
    [[ -z $headers_package ]] || ws_die 'kernel build produced multiple linux-git-headers packages'
    headers_package="$package"
  done
  [[ -n $headers_package ]] || ws_die 'kernel build did not produce linux-git-headers'
  effective_config="$build_dir/linux-git.effective.config"
  ws_kernel_extract_effective_config "$headers_package" "$effective_config"
  ws_kernel_validate_effective_config "$effective_config"
  jobs="$(<"$build_dir/linux-git.measured-jobs")"
  [[ $jobs =~ ^[1-9][0-9]*$ ]] || ws_die 'kernel build did not record a valid measured job count'
  config_delta="$build_dir/linux-git.config.delta"
  diff -u --label aur-base-config --label effective-config "$build_dir/config" "$effective_config" >"$config_delta" || {
    diff_status=$?
    ((diff_status == 1)) || ws_die 'could not generate effective kernel configuration delta'
  }
  buildinfo="$build_dir/linux-git.headers.BUILDINFO"
  ws_kernel_write_toolchain_metadata "$headers_package" "$buildinfo"
  toolchain_metadata="$build_dir/linux-git.chroot-toolchain.txt"
  ws_kernel_write_toolchain_summary "$buildinfo" "$toolchain_metadata"
  namcap_report="$build_dir/linux-git.namcap.txt"
  ws_kernel_namcap_audit "$namcap_report" "${packages[@]}"
  mkdir -p -- "$(dirname -- "$output_dir")"
  mkdir -- "$output_dir"
  ws_copy_built_packages "$build_dir" "$output_dir"
  install -m 0644 -- "$effective_config" "$config_delta" "$buildinfo" "$toolchain_metadata" "$namcap_report" "$output_dir/"
  package_hashes="$output_dir/linux-git.package-sha256"
  : >"$package_hashes"
  shopt -s nullglob
  for package in "$output_dir"/*.pkg.tar.*; do
    [[ $package != *.sig ]] || continue
    printf '%s  %s\n' "$(common::sha256_file "$package")" "$(basename -- "$package")" >>"$package_hashes"
  done
  shopt -u nullglob
  printf 'AUR_REPOSITORY=%s\nAUR_COMMIT=%s\nKERNEL_SOURCE_REPOSITORY=%s\nKERNEL_SOURCE_COMMIT=%s\nNATIVE_CFLAGS=-march=native\nNATIVE_RUSTFLAGS=-Ctarget-cpu=native\nMEASURED_BUILD_JOBS=%s\nMAKEFLAGS=-j%s\nNATIVE_CONFIG_USER_SHA256=%s\nEFFECTIVE_CONFIG_SHA256=%s\nCONFIG_DELTA_SHA256=%s\nHEADERS_BUILDINFO_SHA256=%s\nCHROOT_TOOLCHAIN_SHA256=%s\nNAMCAP_REPORT_SHA256=%s\nPACKAGE_SHA256_FILE=%s\nPACKAGE_SHA256_FILE_SHA256=%s\n' \
    "$expected_repo" "$expected_commit" "$source_repo" "$source_commit" \
    "$jobs" "$jobs" \
    "$(common::sha256_file "$template")" "$(common::sha256_file "$output_dir/linux-git.effective.config")" \
    "$(common::sha256_file "$output_dir/linux-git.config.delta")" "$(common::sha256_file "$output_dir/linux-git.headers.BUILDINFO")" "$(common::sha256_file "$output_dir/linux-git.chroot-toolchain.txt")" \
    "$(common::sha256_file "$output_dir/linux-git.namcap.txt")" "$(basename -- "$package_hashes")" \
    "$(common::sha256_file "$package_hashes")" >"$output_dir/linux-git.build-lock"
  if grep -Eq '(^|[[:space:]])(W|WARNING|Warning):' "$namcap_report"; then
    ws_note "namcap reported warnings; review $output_dir/linux-git.namcap.txt"
  fi
  ws_note "kernel build workspace retained for audit: $build_dir"
}

ws_kernel_build_uki() {
  ws_require_arch
  ws_require_root
  local root preset_source preset_target candidate cert="${1:-/var/lib/sbctl/keys/db/db.pem}"
  root="$(ws_repo_root)"
  preset_source="$root/templates/workstation/workstation-linux-git.preset"
  preset_target=/etc/mkinitcpio.d/workstation-linux-git.preset
  candidate=/var/lib/workstation/uki/arch-linux-git.efi
  [[ -r /boot/vmlinuz-linux-git ]] || ws_die 'install the reviewed linux-git package before building its UKI'
  [[ -r /etc/kernel/cmdline ]] || ws_die '/etc/kernel/cmdline is unavailable'
  [[ -r "$cert" ]] || ws_die "Secure Boot db certificate is not readable: $cert"
  command -v mkinitcpio >/dev/null || ws_die 'mkinitcpio is required'
  command -v sbctl >/dev/null || ws_die 'sbctl is required'
  command -v sbverify >/dev/null || ws_die 'sbverify is required'
  install -d -m 0755 -- /etc/mkinitcpio.d /var/lib/workstation/uki
  if [[ -e "$preset_target" ]]; then
    cmp -s "$preset_source" "$preset_target" || ws_die "existing git-kernel preset differs from the reviewed template: $preset_target"
  else
    install -m 0644 -- "$preset_source" "$preset_target"
  fi
  mkinitcpio -p workstation-linux-git
  [[ -s "$candidate" ]] || ws_die 'mkinitcpio did not create the git-kernel UKI'
  sbctl sign -s "$candidate"
  sbverify --cert "$cert" "$candidate" >/dev/null || ws_die 'git-kernel UKI is not signed by the configured Secure Boot db certificate'
  ws_note "signed git-kernel candidate: $candidate"
  ws_note 'benchmark it without changing BootOrder, then use kernel promote on both ESPs'
}

ws_candidate_filename() {
  case "$1" in
    linux-git) printf '%s\n' arch-linux-git.efi ;;
    stable) printf '%s\n' arch-linux.efi ;;
    lts) printf '%s\n' arch-linux-lts.efi ;;
    *) ws_die 'candidate must be linux-git, stable, or lts' ;;
  esac
}

ws_candidate_label() {
  case "$1" in
    linux-git) printf '%s\n' 'Arch Linux (git)' ;;
    stable) printf '%s\n' 'Arch Linux (stable)' ;;
    lts) printf '%s\n' 'Arch Linux (LTS)' ;;
    *) ws_die 'candidate must be linux-git, stable, or lts' ;;
  esac
}

ws_bootnum_for_label() {
  common::bootnum_for_label "$@"
}

ws_select_boot_label() {
  local label="$1" selected order entry new_order='' inventory filename
  case "$label" in
    'Arch Linux (git)') filename='arch-linux-git.efi' ;;
    'Arch Linux (stable)') filename='arch-linux.efi' ;;
    'Arch Linux (LTS)') filename='arch-linux-lts.efi' ;;
    *) ws_die 'unsupported workstation boot label' ;;
  esac
  common::validate_esp_pair / || ws_die 'ESP identity validation failed'
  selected="$(ws_bootnum_for_label "$label" "$COMMON_ESP_A_PARTUUID" "\\EFI\\Linux\\$filename")" ||
    ws_die "UEFI entry is absent, ambiguous or targets an unexpected ESP/loader: $label"
  inventory=$(efibootmgr) || ws_die 'cannot read firmware BootOrder'
  order=$(awk -F': ' '$1 == "BootOrder" { n++; value=$2 } END {if(n!=1)exit 1; print value}' <<<"$inventory") ||
    ws_die 'firmware did not report a unique BootOrder'
  [[ $order =~ ^[0-9A-Fa-f]{4}(,[0-9A-Fa-f]{4})*$ ]] || ws_die 'firmware reported an invalid BootOrder'
  new_order=$selected
  IFS=',' read -r -a _ws_boot_order <<<"$order"
  for entry in "${_ws_boot_order[@]}"; do
    [[ "$(printf '%s' "$entry" | tr '[:lower:]' '[:upper:]')" == "$(printf '%s' "$selected" | tr '[:lower:]' '[:upper:]')" ]] ||
      new_order="$new_order,$entry"
  done
  efibootmgr --bootorder "$new_order"
  ws_note "preferred UEFI entry: $label (Boot$selected)"
}

ws_create_git_boot_entry() {
  local esp="$1" label="$2" part_number
  [[ $esp == /efi && $label == 'Arch Linux (git)' ]] || ws_die 'unexpected git UKI boot target'
  common::validate_esp_pair / || ws_die 'ESP identity validation failed before firmware creation'
  part_number=$(lsblk --nodeps --noheadings --raw --output PARTN "$COMMON_ESP_A_DEVICE") ||
    ws_die 'cannot inspect the primary ESP partition number'
  [[ $part_number =~ ^[1-9][0-9]*$ ]] || ws_die 'invalid primary ESP partition number'
  efibootmgr --create --disk "$COMMON_ESP_A_DISK" --part "$part_number" --label "$label" --loader '\EFI\Linux\arch-linux-git.efi'
}

ws_kernel_promote() {
  ws_require_arch
  ws_require_root
  local candidate="$1" source="$2" esp_a="$3" esp_b="$4" cert="${5:-/var/lib/sbctl/keys/db/db.pem}"
  local relative='EFI/Linux/' filename label staged_a staged_b boot_status=0
  filename="$(ws_candidate_filename "$candidate")"
  label="$(ws_candidate_label "$candidate")"
  [[ -f "$source" && "$(basename -- "$source")" == "$filename" ]] ||
    ws_die "UKI source must be an existing file named $filename"
  command -v sbverify >/dev/null || ws_die 'sbverify is required to promote a UKI'
  command -v efibootmgr >/dev/null || ws_die 'efibootmgr is required to promote a UKI'
  [[ -r "$cert" ]] || ws_die "Secure Boot db certificate is not readable: $cert"
  sbverify --cert "$cert" "$source" >/dev/null || ws_die 'UKI is not signed by the configured Secure Boot db certificate'
  [[ $esp_a == /efi && $esp_b == /efi2 ]] || ws_die 'promotion requires the configured /efi and /efi2 ESP mountpoints'
  common::validate_esp_pair / || ws_die 'ESP identity validation failed'
  ws_bootnum_for_label "$label" "$COMMON_ESP_A_PARTUUID" "\\EFI\\Linux\\$filename" >/dev/null || boot_status=$?
  if ((boot_status != 0)); then
    [[ $candidate == linux-git && $boot_status == 1 ]] || ws_die 'cannot safely resolve the existing boot entry'
  fi
  for mountpoint in "$esp_a" "$esp_b"; do
    install -d -m 0755 -- "$mountpoint/$relative"
  done
  staged_a="$esp_a/$relative.$filename.new.$$"
  staged_b="$esp_b/$relative.$filename.new.$$"
  install -m 0644 -- "$source" "$staged_a"
  install -m 0644 -- "$source" "$staged_b"
  sbverify --cert "$cert" "$staged_a" >/dev/null
  sbverify --cert "$cert" "$staged_b" >/dev/null
  mv -- "$staged_a" "$esp_a/$relative$filename"
  mv -- "$staged_b" "$esp_b/$relative$filename"
  sync
  if ((boot_status == 1)); then
    ws_create_git_boot_entry "$esp_a" "$label"
  fi
  ws_select_boot_label "$label"
  ws_note "installed and selected $candidate UKI on both ESPs"
}

ws_kernel_rollback() {
  ws_require_arch
  ws_require_root
  local candidate="${1:-lts}"
  [[ "$candidate" == stable || "$candidate" == lts ]] || ws_die 'rollback target must be stable or lts'
  common::validate_esp_pair / || ws_die 'ESP identity validation failed'
  local filename cert=/var/lib/sbctl/keys/db/db.pem
  filename=$(ws_candidate_filename "$candidate")
  [[ -r $cert ]] || ws_die 'configured Secure Boot db certificate is unavailable'
  sbverify --cert "$cert" "/efi/EFI/Linux/$filename" >/dev/null || ws_die 'rollback UKI signature is invalid'
  cmp -s "/efi/EFI/Linux/$filename" "/efi2/EFI/Linux/$filename" || ws_die 'rollback UKI copies differ'
  ws_select_boot_label "$(ws_candidate_label "$candidate")"
}

ws_gpu_validate() {
  ws_require_arch
  local bdf="${1:-}" card render driver pci_details
  [[ "$bdf" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] || ws_die 'BDF must be DOMAIN:BUS:DEVICE.FUNCTION'
  [[ "$(lspci -n -s "$bdf")" == *'8086:e223'* ]] || ws_die "BDF $bdf is not an Intel Arc Pro B70 (8086:e223)"
  driver="$(lspci -k -s "$bdf" | awk -F': ' '/Kernel driver in use/{print $2}')"
  [[ "$driver" == xe ]] || ws_die "B70 at $bdf is not bound to xe (found: ${driver:-none})"
  pci_details="$(lspci -vv -s "$bdf")"
  grep -Eq 'Memory at .+\[size=32G\]' <<<"$pci_details" || ws_die 'B70 does not expose the expected 32 GiB large BAR; verify Above 4G Decoding and ReBAR'
  grep -Eq 'LnkSta:.*Speed 32GT/s.*Width x16' <<<"$pci_details" || ws_die 'B70 is not negotiated at PCIe 5.0 x16; verify slot choice and firmware settings'
  card="$(readlink -f -- "/dev/dri/by-path/pci-$bdf-card")"
  render="$(readlink -f -- "/dev/dri/by-path/pci-$bdf-render")"
  [[ "$card" == /dev/dri/card* && -c "$card" ]] || ws_die "no card node for $bdf"
  [[ "$render" == /dev/dri/renderD* && -c "$render" ]] || ws_die "no render node for $bdf"
  printf 'B70_PCI_BDF=%s\nB70_CARD_DEVICE=%s\nB70_RENDER_DEVICE=%s\n' "$bdf" "$card" "$render"
  printf '%s\n' "$pci_details" | grep -E 'LnkCap:|LnkSta:|Resizable BAR|Region [0-9]+: Memory' || true
  [[ -r "/sys/bus/pci/devices/$bdf/numa_node" ]] && printf 'B70_NUMA_NODE=%s\n' "$(<"/sys/bus/pci/devices/$bdf/numa_node")"
  command -v vulkaninfo >/dev/null && vulkaninfo --summary || true
  command -v clinfo >/dev/null && clinfo || true
}

ws_ai_validate() {
  ws_require_arch
  ws_require_user
  local bdf="$1"
  ws_gpu_validate "$bdf"
  python -c 'import torch; torch.manual_seed(0); assert torch.xpu.is_available(); print(torch.xpu.get_device_name(0)); a=torch.randn(256,256); expected=a@a.T; x=a.to("xpu"); actual=(x@x.T).cpu(); torch.xpu.synchronize(); torch.testing.assert_close(actual,expected,rtol=1e-3,atol=1e-3); print("PyTorch XPU FP32 matmul: PASS")'
  python -c 'from openvino import Core; devices=Core().available_devices; print("OpenVINO devices:", devices); assert any(device.startswith("GPU") for device in devices)'
  ws_note 'AI enumeration and deterministic FP32 smoke checks passed; inspect the kernel journal for xe resets or firmware errors'
}

ws_llm_devices() {
  local bdf="$1" card render
  [[ "$bdf" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] || ws_die 'BDF must be DOMAIN:BUS:DEVICE.FUNCTION'
  card="$(readlink -f -- "/dev/dri/by-path/pci-$bdf-card")"
  render="$(readlink -f -- "/dev/dri/by-path/pci-$bdf-render")"
  [[ "$card" == /dev/dri/card* && -c "$card" ]] || ws_die "no card node for $bdf"
  [[ "$render" == /dev/dri/renderD* && -c "$render" ]] || ws_die "no render node for $bdf"
  printf '%s\n%s\n' "$card" "$render"
}

ws_llm_up() {
  ws_require_arch
  ws_require_user
  local bdf="$1" model_dir="${2:-${WORKSTATION_LLM_DATA_DIR:-}}" lock_file="${3:-$(ws_repo_root)/versions.lock}"
  local image card render devices shm_size="${WORKSTATION_LLM_SHM_SIZE:-8g}" listen host port
  image="$(ws_read_lock LLM_SCALER_IMAGE "$lock_file")"
  [[ "$image" == *@sha256:* ]] || ws_die 'LLM image must be pinned by immutable digest'
  [[ -n "$model_dir" ]] || ws_die 'model directory is required unless LLM_DATA_DIR is loaded from --config'
  [[ -d "$model_dir" ]] || ws_die "model directory does not exist: $model_dir"
  ws_gpu_validate "$bdf" >/dev/null
  devices="$(ws_llm_devices "$bdf")"
  card=${devices%%$'\n'*}
  render=${devices#*$'\n'}
  [[ "$card" != "$render" ]] || ws_die 'GPU device discovery did not return distinct card and render nodes'
  [[ "$shm_size" =~ ^[1-9][0-9]*[mMgG]$ ]] || ws_die 'WORKSTATION_LLM_SHM_SIZE must be a positive MiB or GiB value (for example 8g)'
  listen=${WORKSTATION_LLM_LISTEN:-127.0.0.1:8000}
  [[ "$listen" == 127.0.0.1:8000 ]] || ws_die 'LLM listener must remain 127.0.0.1:8000'
  host=${listen%:*}
  port=${listen##*:}
  podman run -d --name workstation-b70-llm --replace --rm --pull=never \
    --network=slirp4netns:allow_host_loopback=false \
    -p "$listen:$port" \
    --device "$card:$card:rwm" --device "$render:$render:rwm" --group-add keep-groups \
    --userns=keep-id --cap-drop=all --security-opt=no-new-privileges --read-only --pids-limit 4096 \
    --shm-size "$shm_size" \
    --tmpfs /tmp:rw,noexec,nosuid,size=4g --tmpfs /run:rw,noexec,nosuid,size=64m \
    --env HOME=/tmp --env HF_HOME=/tmp/hf \
    --mount "type=bind,src=$model_dir,dst=/models,ro,rbind=false" \
    --workdir /llm --entrypoint /bin/bash \
    "$image" -lc 'source /opt/intel/oneapi/setvars.sh --force >/dev/null && exec "$@"' bash \
    vllm serve /models --host 0.0.0.0 --port "$port" --gpu-memory-utilization 0.80 --max-model-len 8192 --max-num-seqs 1 --enforce-eager
  ws_note "LLM started on $host:$port; no systemd unit or credential file was created"
}

ws_llm_pull() {
  ws_require_arch
  ws_require_user
  local lock_file="${1:-$(ws_repo_root)/versions.lock}" image
  image="$(ws_read_lock LLM_SCALER_IMAGE "$lock_file")"
  [[ "$image" == *@sha256:* ]] || ws_die 'LLM image must be pinned by immutable digest'
  podman pull "$image"
}

ws_llm_down() {
  ws_require_user
  if ! podman container exists workstation-b70-llm; then
    ws_note 'LLM container is absent'
    return 0
  fi
  podman stop --time 30 workstation-b70-llm
}

ws_llm_status() {
  ws_require_user
  if ! podman container exists workstation-b70-llm; then
    ws_note 'LLM container is absent'
    return 0
  fi
  podman inspect --format 'name={{.Name}} status={{.State.Status}} ports={{.NetworkSettings.Ports}}' workstation-b70-llm
}

ws_llm_bench() {
  ws_require_arch
  ws_require_user
  local concurrency="${1:-1}" output="${2:-$PWD/llm-benchmark-$(date -u +%Y%m%dT%H%M%SZ).txt}"
  [[ "$concurrency" =~ ^[1-9][0-9]*$ ]] || ws_die 'concurrency must be a positive integer'
  podman container exists workstation-b70-llm || ws_die 'LLM container is not running'
  [[ "$(podman inspect --format '{{.State.Running}}' workstation-b70-llm)" == true ]] || ws_die 'LLM container is not running'
  [[ ! -e "$output" ]] || ws_die "refusing to overwrite benchmark output: $output"
  ws_safe_dir "$(dirname -- "$output")"
  {
    printf 'timestamp=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    uname -a
    podman inspect --format 'image={{.ImageDigest}}' workstation-b70-llm
    podman exec --workdir /llm workstation-b70-llm /bin/bash -lc \
      'source /opt/intel/oneapi/setvars.sh --force >/dev/null && exec vllm bench serve --backend openai --base-url http://127.0.0.1:8000 --model /models --dataset-name random --num-prompts 50 --random-input-len 1024 --random-output-len 512 --max-concurrency "$1"' bash "$concurrency"
  } | tee "$output"
}

ws_zsh_setup() {
  ws_require_arch
  ws_require_user
  local lock_file="${1:-$(ws_repo_root)/versions.lock}" root="${2:-$HOME/.local/share/oh-my-zsh}" commit remote
  commit="$(ws_read_lock OMZ_COMMIT "$lock_file")"
  remote="$(ws_read_lock OMZ_REPOSITORY "$lock_file")"
  [[ "$commit" =~ ^[a-fA-F0-9]{40}$ ]] || ws_die 'Oh My Zsh commit must be a full 40-character commit ID'
  [[ ! -e "$root" ]] || ws_die "refusing to replace existing Oh My Zsh directory: $root"
  for required in /usr/share/zsh/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh \
    /usr/share/zsh/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh; do
    [[ -r "$required" ]] || ws_die "required packaged Zsh plugin is unavailable: $required"
  done
  command -v zsh >/dev/null || ws_die 'zsh is required'
  zsh -n "$(ws_repo_root)/templates/workstation/zshrc"
  mkdir -p -- "$(dirname -- "$root")"
  git clone --filter=blob:none --no-checkout "$remote" "$root"
  git -C "$root" checkout --detach "$commit"
  [[ "$(git -C "$root" rev-parse HEAD)" == "$commit" ]] || ws_die 'Oh My Zsh checkout did not resolve to requested commit'
  [[ "$(git -C "$root" remote get-url origin)" == "$remote" ]] || ws_die 'Oh My Zsh remote differs from versions.lock'
  [[ -z "$(git -C "$root" status --porcelain=v1 --untracked-files=all)" ]] || ws_die 'Oh My Zsh checkout is not clean'
  if [[ ! -e "$HOME/.zshrc" ]]; then
    install -m 0644 -- "$(ws_repo_root)/templates/workstation/zshrc" "$HOME/.zshrc"
    ws_note 'installed the reviewed Zsh configuration; change the login shell separately with chsh'
  else
    ws_note 'existing ~/.zshrc was preserved; merge the reviewed template manually'
  fi
  ws_note "Oh My Zsh installed at $root; updater remains disabled"
}

ws_toolbox_archive_root() {
  local archive=$1 version=$2 entries top
  entries=$(tar -tzf "$archive") || ws_die 'cannot enumerate JetBrains Toolbox archive'
  if ! awk '/^\// || /(^|\/)\.\.($|\/)/ { bad=1 } END { exit bad }' <<<"$entries"; then
    ws_die 'JetBrains Toolbox archive contains an unsafe path'
  fi
  top=$(awk -F/ 'NF {if(root=="")root=$1; if($1!=root)bad=1} END {if(bad)exit 1; print root}' <<<"$entries") ||
    ws_die 'JetBrains Toolbox archive contains more than one top-level path'
  [[ $top == "jetbrains-toolbox-$version" ]] || ws_die 'JetBrains Toolbox archive root does not match the locked version'
  printf '%s\n' "$top"
}

ws_toolbox_setup() {
  ws_require_arch
  ws_require_user
  local lock_file="${1:-$(ws_repo_root)/versions.lock}" version url expected temp archive top target
  version="$(ws_read_lock JETBRAINS_TOOLBOX_VERSION "$lock_file")"
  url="$(ws_read_lock JETBRAINS_TOOLBOX_URL "$lock_file")"
  expected="$(ws_read_lock JETBRAINS_TOOLBOX_SHA256 "$lock_file")"
  [[ "$version" =~ ^[0-9][0-9A-Za-z.-]+$ && "$expected" =~ ^[a-f0-9]{64}$ ]] || ws_die 'JetBrains Toolbox lock values are invalid'
  for command_name in curl tar sha256sum; do command -v "$command_name" >/dev/null || ws_die "required command is unavailable: $command_name"; done
  temp="$(mktemp -d "${TMPDIR:-/tmp}/workstationctl-toolbox.XXXXXX")"
  archive="$temp/toolbox.tar.gz"
  curl --fail --location --proto '=https' --tlsv1.2 -o "$archive" "$url"
  printf '%s  %s\n' "$expected" "$archive" | sha256sum --check --status || ws_die 'JetBrains Toolbox checksum mismatch'
  top=$(ws_toolbox_archive_root "$archive" "$version") || return 1
  mkdir -p -- "$temp/extract"
  tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$temp/extract"
  while IFS= read -r link; do
    [[ "$(readlink -f -- "$link")" == "$temp/extract/$top/"* ]] ||
      ws_die "JetBrains Toolbox archive contains an escaping or broken symlink: $link"
  done < <(find "$temp/extract/$top" -type l -print)
  [[ -x "$temp/extract/$top/bin/jetbrains-toolbox" ]] || ws_die 'JetBrains Toolbox executable is missing from the archive'
  target="$HOME/.local/opt/$top"
  [[ ! -e "$target" && ! -e "$HOME/.local/bin/jetbrains-toolbox" ]] || ws_die 'JetBrains Toolbox target already exists'
  mkdir -p -- "$HOME/.local/opt" "$HOME/.local/bin"
  mv -- "$temp/extract/$top" "$target"
  ln -s -- "$target/bin/jetbrains-toolbox" "$HOME/.local/bin/jetbrains-toolbox"
  rm -rf -- "$temp"
  ws_note "installed JetBrains Toolbox $version without enabling login autostart"
}

ws_backup_credential_path() {
  case "$1" in
    repository) printf '%s\n' /etc/credstore.encrypted/workstation-restic-repository.cred ;;
    password) printf '%s\n' /etc/credstore.encrypted/workstation-restic-password.cred ;;
    aws) printf '%s\n' /etc/credstore.encrypted/workstation-restic-aws.cred ;;
    *) ws_die 'backup credential must be repository, password, or aws' ;;
  esac
}

ws_backup_credential() {
  ws_require_arch
  ws_require_root
  local kind="$1" target temporary value confirmation='' access_key secret_key session_token=''
  command -v systemd-creds >/dev/null || ws_die 'systemd-creds is required'
  [[ -t 0 && -t 1 ]] || ws_die 'backup credential entry requires an interactive terminal'
  target="$(ws_backup_credential_path "$kind")"
  [[ ! -e "$target" ]] || ws_die "refusing to replace encrypted credential: $target"
  install -d -m 0700 -- /etc/credstore.encrypted
  temporary="$target.new.$$"
  umask 077
  case "$kind" in
    repository)
      printf 'Restic repository URL (input hidden): ' >&2
      IFS= read -r -s value
      printf '\n' >&2
      [[ -n "$value" ]] || ws_die 'repository URL cannot be empty'
      printf '%s\n' "$value" | systemd-creds encrypt --name=restic_repository - "$temporary"
      ;;
    password)
      printf 'Restic repository password (input hidden): ' >&2
      IFS= read -r -s value
      printf '\n' >&2
      [[ -n "$value" ]] || ws_die 'Restic password cannot be empty'
      printf 'Confirm Restic repository password (input hidden): ' >&2
      IFS= read -r -s confirmation
      printf '\n' >&2
      [[ "$value" == "$confirmation" ]] || ws_die 'Restic password confirmation did not match'
      printf '%s\n' "$value" | systemd-creds encrypt --name=restic_password - "$temporary"
      ;;
    aws)
      printf 'Least-privilege AWS access key ID (input hidden): ' >&2
      IFS= read -r -s access_key
      printf '\n' >&2
      printf 'AWS secret access key (input hidden): ' >&2
      IFS= read -r -s secret_key
      printf '\n' >&2
      printf 'Optional AWS session token (input hidden; press Enter to omit): ' >&2
      IFS= read -r -s session_token
      printf '\n' >&2
      [[ -n "$access_key" && -n "$secret_key" ]] || ws_die 'AWS access key ID and secret are required'
      {
        printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' "$access_key" "$secret_key"
        [[ -z "$session_token" ]] || printf 'aws_session_token=%s\n' "$session_token"
      } | systemd-creds encrypt --name=aws_credentials - "$temporary"
      ;;
  esac
  chmod 0600 "$temporary"
  mv -- "$temporary" "$target"
  value='' confirmation='' access_key='' secret_key='' session_token=''
  ws_note "stored encrypted $kind credential at $target"
}

ws_backup_require_package() {
  local destination
  pacman -Q arch-workstation-backup >/dev/null || ws_die 'install the reviewed signed arch-workstation-backup package with pacman first'
  for destination in /usr/local/libexec/workstation-restic \
    /etc/systemd/system/workstation-restic@.service \
    /etc/systemd/system/workstation-restic-backup.timer \
    /etc/systemd/system/workstation-restic-retention.timer; do
    [[ ! -e $destination && ! -L $destination ]] || ws_die "legacy backup file requires an explicit package migration: $destination"
  done
  for destination in /usr/lib/arch-workstation-backup/run \
    /usr/lib/systemd/system/workstation-restic@.service \
    /usr/lib/systemd/system/workstation-restic-backup.timer \
    /usr/lib/systemd/system/workstation-restic-retention.timer \
    /etc/restic/workstation.include /etc/restic/workstation.exclude; do
    [[ $(pacman -Qqo -- "$destination") == arch-workstation-backup ]] || ws_die "backup path is not owned by arch-workstation-backup: $destination"
  done
  pacman -Qk arch-workstation-backup >/dev/null || ws_die 'backup package files are incomplete'
}

ws_backup_install() {
  ws_require_arch
  ws_require_root
  ws_backup_require_package
  systemctl daemon-reload
  ws_note 'verified packaged Restic units; review policy, create credentials, run @init once and @check, then enable the timers'
}

ws_backup_enable() {
  ws_require_arch
  ws_require_root
  local kind path
  for kind in repository password aws; do
    path="$(ws_backup_credential_path "$kind")"
    [[ -r "$path" ]] || ws_die "encrypted backup credential is missing: $path"
  done
  ws_backup_require_package
  systemctl start --wait workstation-restic@check.service
  systemctl enable --now workstation-restic-backup.timer workstation-restic-retention.timer
  ws_note 'Restic repository check passed and backup timers are enabled'
}

ws_backup_run() {
  ws_require_arch
  ws_require_root
  local action="${1:-}"
  case "$action" in init | backup | check | retention | snapshots) ;; *) ws_die 'backup run action must be init, backup, check, retention, or snapshots' ;; esac
  ws_backup_require_package
  systemctl start --wait "workstation-restic@$action.service"
}

ws_codex_configure() {
  ws_require_arch
  ws_require_user
  local output="${1:-$HOME/.codex/config.toml}" template
  template="$(ws_repo_root)/templates/workstation/codex-config.toml"
  [[ ! -e "$output" ]] || ws_die "refusing to overwrite existing Codex configuration: $output"
  ws_safe_dir "$(dirname -- "$output")"
  install -m 0600 -- "$template" "$output"
  ws_note 'configured Codex to use the desktop keyring; authenticate interactively'
}

ws_aur_checkout() {
  ws_require_arch
  ws_require_user
  local name="$1" destination="$2" lock_file="${3:-$(ws_repo_root)/versions.lock}" repo_key commit_key repository commit
  case "$name" in
    paru)
      repo_key=PARU_AUR_REPOSITORY
      commit_key=PARU_AUR_COMMIT
      ;;
    linux-git)
      repo_key=LINUX_GIT_AUR_REPOSITORY
      commit_key=LINUX_GIT_AUR_COMMIT
      ;;
    aws-ssm)
      repo_key=AWS_SSM_AUR_REPOSITORY
      commit_key=AWS_SSM_AUR_COMMIT
      ;;
    *) ws_die 'AUR checkout name must be paru, linux-git, or aws-ssm' ;;
  esac
  [[ ! -e "$destination" ]] || ws_die "refusing to replace destination: $destination"
  repository="$(ws_read_lock "$repo_key" "$lock_file")"
  commit="$(ws_read_lock "$commit_key" "$lock_file")"
  [[ "$commit" =~ ^[a-f0-9]{40}$ ]] || ws_die 'AUR commit lock is invalid'
  git clone --no-checkout -- "$repository" "$destination"
  git -C "$destination" checkout --detach "$commit"
  ws_assert_clean_locked_checkout "$destination" "$repository" "$commit"
  ws_note "checked out reviewed $name source at $commit"
}

ws_paru_bootstrap() {
  ws_require_arch
  ws_require_user
  local source_dir="$1" chroot_dir="$2" output_dir="$3" lock_file="${4:-$(ws_repo_root)/versions.lock}"
  local expected_repo expected_commit build_dir
  chroot_dir="$(ws_validate_clean_chroot "$chroot_dir")"
  expected_repo="$(ws_read_lock PARU_AUR_REPOSITORY "$lock_file")"
  expected_commit="$(ws_read_lock PARU_AUR_COMMIT "$lock_file")"
  ws_assert_clean_locked_checkout "$source_dir" "$expected_repo" "$expected_commit"
  command -v makechrootpkg >/dev/null || ws_die 'makechrootpkg is required (devtools)'
  command -v namcap >/dev/null || ws_die 'namcap is required for the AUR package audit'
  build_dir="$(mktemp -d "${TMPDIR:-/tmp}/workstationctl-paru.XXXXXX")"
  git -C "$source_dir" archive --format=tar "$expected_commit" | tar -xf - -C "$build_dir"
  (
    cd -- "$build_dir"
    makechrootpkg -c -n -r "$chroot_dir" -- --syncdeps
  )
  ws_copy_built_packages "$build_dir" "$output_dir"
  printf 'AUR_COMMIT=%s\n' "$expected_commit" >"$output_dir/paru.build-lock"
  ws_note "Paru was built but not installed; review $output_dir and add it to the signed local repository"
}

ws_dev_setup() {
  ws_require_arch
  ws_require_root
  local profile="$1" manifest="$2" package line packages=()
  case "$profile" in server | workstation | gpu | ai | gaming | shell | cloud | virtualization | backup) ;; *) ws_die 'unknown package profile' ;; esac
  [[ -r "$manifest" ]] || ws_die "package manifest is not readable: $manifest"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    for package in $line; do
      [[ "$package" =~ ^[a-z0-9@._+:-]+$ ]] || ws_die "unsafe package name in manifest: $package"
      if [[ $profile == server ]]; then
        case $package in
          gnome | gnome-* | gdm | tuned-ppd | steam | gamescope | gamemode | mangohud | lib32-* | virt-manager | virt-viewer | qemu-desktop)
            ws_die "desktop/local-gaming package is not part of the server profile: $package"
            ;;
        esac
      fi
      case "$profile" in
        server | workstation) packages+=("$package") ;;
        gpu) [[ "$package" == *intel* || "$package" == linux-firmware-amdgpu || "$package" == mesa* || "$package" == lib32-mesa || "$package" == vulkan* || "$package" == lib32-vulkan-* || "$package" == level-zero* || "$package" == ocl-* || "$package" == clinfo || "$package" == libva-* || "$package" == hwloc || "$package" == numactl || "$package" == perf ]] && packages+=("$package") ;;
        ai) [[ "$package" == rocm-* || "$package" == rocminfo || "$package" == rccl || "$package" == python-pytorch-opt-rocm || "$package" == python-pytorch-xpu || "$package" == python-pytorch-opt-xpu || "$package" == openvino || "$package" == python-openvino || "$package" == openvino-intel-gpu-plugin || "$package" == ggml-sycl || "$package" == intel-compute-runtime || "$package" == level-zero-loader || "$package" == clinfo || "$package" == hwloc || "$package" == numactl ]] && packages+=("$package") ;;
        gaming) [[ "$package" == steam || "$package" == gamescope || "$package" == gamemode || "$package" == lib32-gamemode || "$package" == mangohud || "$package" == lib32-mangohud || "$package" == mesa || "$package" == lib32-mesa || "$package" == vulkan-radeon || "$package" == lib32-vulkan-radeon || "$package" == vulkan-intel || "$package" == lib32-vulkan-intel ]] && packages+=("$package") ;;
        shell) [[ "$package" == zsh* || "$package" == bash-completion || "$package" == fzf || "$package" == pkgfile || "$package" == git || "$package" == git-lfs || "$package" == git-delta || "$package" == git-zsh-completion || "$package" == openssh || "$package" == gnupg || "$package" == libsecret || "$package" == libfido2 || "$package" == yubikey-manager ]] && packages+=("$package") ;;
        cloud) [[ "$package" == aws-cli-v2 || "$package" == kubectl || "$package" == helm || "$package" == kustomize || "$package" == k9s || "$package" == kubectx || "$package" == stern || "$package" == eksctl || "$package" == openai-codex ]] && packages+=("$package") ;;
        virtualization) [[ "$package" == qemu-* || "$package" == libvirt || "$package" == virt-install || "$package" == virt-manager || "$package" == virt-viewer || "$package" == edk2-ovmf || "$package" == dnsmasq || "$package" == nftables || "$package" == cloud-image-utils || "$package" == ansible-core || "$package" == python || "$package" == ipcalc || "$package" == jq || "$package" == libxml2 ]] && packages+=("$package") ;;
        backup) [[ "$package" == restic || "$package" == pacman-contrib || "$package" == arch-audit || "$package" == rebuild-detector ]] && packages+=("$package") ;;
      esac
    done
  done <"$manifest"
  ((${#packages[@]} > 0)) || ws_die "no packages selected for profile: $profile"
  pacman -Syu --needed -- "${packages[@]}"
}

ws_virtualization_configure() {
  ws_require_arch
  ws_require_root
  local username="$1"
  [[ "$username" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || ws_die 'virtualization user name is invalid'
  id "$username" >/dev/null 2>&1 || ws_die "user does not exist: $username"
  getent group libvirt >/dev/null || ws_die 'libvirt group is unavailable; install the virtualization package profile first'
  systemctl list-unit-files libvirtd.socket >/dev/null 2>&1 || ws_die 'libvirtd.socket is unavailable on this libvirt installation'
  usermod --append --groups libvirt "$username"
  systemctl enable --now libvirtd.socket
  ws_note 'enabled libvirt socket activation without enabling any VM or virtual-network autostart'
  ws_note "log out and back in before using qemu:///system as $username"
}

ws_virtualization_validate() {
  ws_require_arch
  command -v virsh >/dev/null || ws_die 'virsh is required'
  command -v virt-host-validate >/dev/null || ws_die 'virt-host-validate is required'
  grep -qw svm /proc/cpuinfo || ws_die 'AMD SVM is not exposed by firmware'
  [[ -d /sys/kernel/iommu_groups ]] || ws_die 'IOMMU groups are unavailable; verify firmware IOMMU and the signed kernel command line'
  find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 -type d -print -quit | grep -q . ||
    ws_die 'the kernel exposed no IOMMU groups'
  [[ "$(virsh -c qemu:///system uri)" == qemu:///system ]] || ws_die 'qemu:///system is unavailable'
  virt-host-validate qemu
}
