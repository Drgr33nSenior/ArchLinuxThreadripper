#!/usr/bin/env bash
# AMD source planning and explicit hardware workloads. No installation occurs.

ws_rocm_unfiltered() {
  local name
  for name in HSA_OVERRIDE_GFX_VERSION HIP_VISIBLE_DEVICES ROCR_VISIBLE_DEVICES CUDA_VISIBLE_DEVICES GPU_DEVICE_ORDINAL GGML_CUDA_DEVICES; do
    [[ -z ${!name+x} ]] || ws_die "unset $name before validating the physical two-GPU workstation"
  done
}

ws_rocm_plan() {
  local report=$1 output=$2 target commit repository
  target=$(ws_detected_gpu_target "$report") || return 1
  repository=$(ws_read_lock ROCM_THEROCK_REPOSITORY)
  commit=$(ws_read_lock ROCM_THEROCK_COMMIT)
  [[ $repository == https://github.com/ROCm/TheRock.git && $commit =~ ^[a-f0-9]{40}$ ]] || ws_die 'TheRock source lock is invalid'
  [[ ! -e $output ]] || ws_die 'plan directory already exists; use a new revision directory'
  mkdir -p -- "$output"
  # A plan can be reviewed away from the workstation. Parallelism is deliberately
  # not frozen from stale observations; build environment measures it at launch.
  jq -n --arg source "$repository" --arg commit "$commit" --arg target "$target" \
    --arg report_hash "$(common::sha256_file "$report")" \
    '{schema:1,status:"plan-not-built",profile:"experimental",source:{repository:$source,commit:$commit},
      hardware_report_sha256:$report_hash,gpu_targets:[$target],cpu_profile:"native-O2-preserve-Arch-hardening",
      cmake_args:["-GNinja",("-DTHEROCK_AMDGPU_TARGETS="+$target),
        ("-DTHEROCK_DIST_AMDGPU_TARGETS="+$target),("-DTHEROCK_TEST_AMDGPU_TARGETS="+$target),
        "-DTHEROCK_AMDGPU_FAMILIES=",
        "-DTHEROCK_ENABLE_ALL=OFF","-DTHEROCK_ENABLE_COMPILER=ON","-DTHEROCK_ENABLE_CORE_RUNTIME=ON",
        "-DTHEROCK_ENABLE_HIP_RUNTIME=ON","-DTHEROCK_ENABLE_RCCL=ON","-DTHEROCK_ENABLE_BLAS=ON",
        "-DTHEROCK_ENABLE_PRIM=ON","-DTHEROCK_ENABLE_RAND=ON","-DTHEROCK_ENABLE_MIOPEN=ON",
        "-DTHEROCK_ENABLE_CORE_AMDSMI=ON","-DTHEROCK_BACKGROUND_BUILD_JOBS=1",
        "-DLLVM_PARALLEL_LINK_JOBS=1","-DFLANG_PARALLEL_COMPILE_JOBS=1",
        "-DTHEROCK_BUILD_TESTING=ON",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache","-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DCMAKE_BUILD_TYPE=Release","-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG","-DCMAKE_CXX_FLAGS_RELEASE=-O2 -DNDEBUG"],
      hip_compiler_launcher:null,system_install:false,
      prerequisites:["Capture all prepared source/submodule commits and patch tree hashes",
        "Lock Python build dependencies with hashes and the Arch build-root package set",
        "Package/verify the upstream-required patched patchelf; do not install it untracked",
        "Use pinned TheRock compiler_check for bootstrapped compilers; scope any sloppiness to this build",
        "Recalculate available-memory concurrency and verify nested Ninja limits before compilation",
        "Record effective per-component compiler flags, CMake caches and package hashes"],
      release_gate:"HIP, PyTorch, RCCL, inference and reboot tests on both physical GPUs; retain last-known-good packages"}' > "$output/build-plan.json"
  cp -- "$report" "$output/hardware.json"
  ws_note "generated reviewable TheRock plan: $output/build-plan.json; source build and dependency sealing remain pending"
}

ws_rocm_source_manifest() (
  set -euo pipefail
  local source=$1 output=$2 commit repository records signature=unverified top_status status
  commit=$(ws_read_lock ROCM_THEROCK_COMMIT)
  repository=$(ws_read_lock ROCM_THEROCK_REPOSITORY)
  [[ -d $source/.git && ! -e $output ]] || ws_die 'require an existing TheRock checkout and a new output filename'
  [[ $(git -C "$source" rev-parse HEAD) == "$commit" && $(git -C "$source" remote get-url origin) == "$repository" ]] \
    || ws_die 'TheRock checkout differs from versions.lock'
  top_status=$(git -C "$source" status --porcelain=v1 --untracked-files=all --ignore-submodules=all) || ws_die 'cannot inspect TheRock checkout status'
  [[ -z $top_status ]] || ws_die 'top-level TheRock checkout contains modifications or untracked files'
  status=$(git -C "$source" submodule status --recursive) || ws_die 'cannot inspect source submodules'
  [[ -n $status ]] || ws_die 'TheRock has no initialized source submodules'
  if grep -Eq '^[-U]' <<< "$status"; then
    ws_die 'source submodules are missing or conflicted'
  fi
  records=$(mktemp)
  trap 'rm -- "$records"' EXIT
  # foreach provides the expected gitlink SHA and the nested path. Reject dirty
  # sources; patched commits are recorded separately from expected gitlinks.
  # Variables here are provided by git in each submodule, not by this shell.
  # shellcheck disable=SC2016
  git -C "$source" submodule foreach --quiet --recursive '
    state=$(git status --porcelain=v1 --untracked-files=all --ignore-submodules=all) || exit 1
    test -z "$state" || exit 1
    origin=$(git remote get-url origin) || exit 1
    actual=$(git rev-parse HEAD) || exit 1
    tree=$(git rev-parse HEAD^{tree}) || exit 1
    printf "%s\t%s\t%s\t%s\t%s\n" "$displaypath" "$origin" "$sha1" "$actual" "$tree"
  ' > "$records" || ws_die 'a source submodule is dirty or has no origin'
  if git -C "$source" verify-commit "$commit" >/dev/null 2>&1; then signature=verified-local-keyring; fi
  jq -Rn --arg repository "$repository" --arg commit "$commit" --arg signature "$signature" \
    '[inputs | split("\t") | {path:.[0],repository:.[1],gitlink:.[2],commit:.[3],tree:.[4],patched:(.[2]!=.[3])}] |
      {schema:1,source:{repository:$repository,commit:$commit,signature:$signature},submodules:.,
       status:"source-inventory-not-build-proof",requires_review:true,
       limitations:["External downloads/Python wheels/build-root packages must be sealed separately",
                    "Patched submodule commits must be reviewed against the pinned upstream patch set"]}' < "$records" > "$output"
)

ws_rocm_llama_checkout_validate() {
  local source=$1 lock=${2:-$(ws_repo_root)/versions.lock} repository commit status
  repository=$(ws_read_lock ROCM_LLAMA_CPP_REPOSITORY "$lock")
  commit=$(ws_read_lock ROCM_LLAMA_CPP_COMMIT "$lock")
  [[ $repository == https://github.com/ggml-org/llama.cpp.git && $commit =~ ^[a-f0-9]{40}$ ]] \
    || ws_die 'llama.cpp source lock is invalid'
  [[ -d $source/.git ]] || ws_die 'llama.cpp checkout must contain .git'
  [[ $(git -C "$source" rev-parse HEAD) == "$commit" && $(git -C "$source" remote get-url origin) == "$repository" ]] \
    || ws_die 'llama.cpp checkout differs from versions.lock'
  status=$(git -C "$source" status --porcelain=v1 --untracked-files=all --ignore-submodules=all) \
    || ws_die 'cannot inspect llama.cpp checkout status'
  [[ -z $status ]] || ws_die 'llama.cpp checkout contains modifications or untracked files'
}

ws_rocm_boot_id_read() {
  local file=$1 boot_id
  [[ -r $file ]] || ws_die "hardware boot ID is unavailable: $file"
  boot_id=$(<"$file")
  [[ $boot_id =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] \
    || ws_die "hardware boot ID is invalid: $file"
  printf '%s\n' "$boot_id"
}

ws_rocm_hardware_identities_match() {
  local recorded=$1 fresh=$2
  jq -en --slurpfile recorded "$recorded" --slurpfile fresh "$fresh" '
    def pci_ids: [.pci_gpus[] | {bdf,device_id,driver}] | sort_by(.bdf);
    def rocm_ids: [.rocm_agents[] | {uuid,gfx}] | sort_by(.uuid);
    ($recorded[0].gpu_target == $fresh[0].gpu_target) and
    (all($recorded[0].rocm_agents[]; (.uuid | (type == "string" and length > 0)))) and
    (all($fresh[0].rocm_agents[]; (.uuid | (type == "string" and length > 0)))) and
    (($recorded[0] | pci_ids) == ($fresh[0] | pci_ids)) and
    (($recorded[0] | rocm_ids) == ($fresh[0] | rocm_ids))
  ' >/dev/null
}

ws_rocm_llama_environment_validate() {
  local name
  # These variables can make CMake or its compiler discovery consume another
  # ROCm installation. The build sets the sole accepted prefix explicitly.
  for name in ROCM_PATH ROCM_HOME HIP_PATH HIP_PLATFORM CMAKE_PREFIX_PATH CMAKE_LIBRARY_PATH CMAKE_INCLUDE_PATH \
    CMAKE_TOOLCHAIN_FILE CMAKE_HIP_COMPILER CMAKE_HIP_COMPILER_ROCM_ROOT CMAKE_C_COMPILER CMAKE_CXX_COMPILER CC CXX \
    hip_ROOT hipblas_ROOT rocblas_ROOT \
    LD_LIBRARY_PATH LIBRARY_PATH CPATH PKG_CONFIG_PATH; do
    [[ -z ${!name+x} ]] || ws_die "unset $name before building against /opt/rocm"
  done
}

ws_rocm_sdk_provider() {
  local provider=${ROCM_SDK_PROVIDER:-arch}
  case $provider in
    arch|aur-gfx120x-bin) ;;
    *) ws_die 'ROCM_SDK_PROVIDER must be arch or aur-gfx120x-bin'; return 1 ;;
  esac
  printf '%s\n' "$provider"
}

ws_rocm_sdk_aur_lock_validate() {
  local lock=$1 repository package version release_version commit recipe_hash source_url source_hash
  repository=$(ws_read_lock ROCM_AUR_REPOSITORY "$lock")
  package=$(ws_read_lock ROCM_AUR_PACKAGE "$lock")
  version=$(ws_read_lock ROCM_AUR_PACKAGE_VERSION "$lock")
  release_version=${version%-*}
  commit=$(ws_read_lock ROCM_AUR_COMMIT "$lock")
  recipe_hash=$(ws_read_lock ROCM_AUR_PKGBUILD_SHA256 "$lock")
  source_url=$(ws_read_lock ROCM_AUR_SOURCE_URL "$lock")
  source_hash=$(ws_read_lock ROCM_AUR_SOURCE_SHA256 "$lock")
  [[ $repository == https://aur.archlinux.org/rocm-gfx120x-bin.git \
    && $package == rocm-gfx120x-bin && $version == 10.0.0-2 \
    && $commit =~ ^[a-f0-9]{40}$ && $recipe_hash =~ ^[a-f0-9]{64}$ \
    && $source_url == "https://stable.repo.amd.com/rocm/core/tarball/therock-dist-linux-gfx120X-all-$release_version.tar.gz" \
    && $source_hash =~ ^[a-f0-9]{64}$ ]] \
    || { ws_die 'the reviewed ROCm 10 AUR provider lock does not match rocm-gfx120x-bin 10.0.0-2'; return 1; }
}

ws_rocm_sdk_prefix() {
  local provider=${1:-$(ws_rocm_sdk_provider)}
  case $provider in
    arch) printf '%s\n' /opt/rocm ;;
    aur-gfx120x-bin) printf '%s\n' /opt/rocm/core ;;
    *) ws_die 'unknown ROCm SDK provider'; return 1 ;;
  esac
}

ws_rocm_realpath_existing() {
  local path=$1
  [[ -e $path || -L $path ]] || { ws_die "cannot resolve missing ROCm path: $path"; return 1; }
  realpath -e -- "$path" 2>/dev/null || realpath "$path"
}

ws_rocm_sdk_aur_layout_validate() {
  local root=$1 rocm=$2 path resolved directory link_target
  root=$(ws_rocm_realpath_existing "$root") || return 1
  rocm=$(ws_rocm_realpath_existing "$rocm") || return 1
  [[ $root == /* && $rocm == "$root/core" && -d $rocm \
    && -x $rocm/bin/amdclang++ && -x $rocm/bin/hipcc \
    && -r $rocm/lib/cmake/hip/hip-config.cmake \
    && -r $rocm/lib/cmake/hipblas/hipblas-config.cmake \
    && -r $rocm/lib/cmake/rocblas/rocblas-config.cmake ]] \
    || { ws_die 'ROCm 10 AUR core layout is incomplete'; return 1; }
  for directory in bin lib include; do
    link_target=$(readlink -- "$root/$directory") || return 1
    [[ -L $root/$directory && $link_target == /* \
      && $(ws_rocm_realpath_existing "$root/$directory") == "$rocm/$directory" ]] \
      || { ws_die "ROCm 10 AUR compatibility path is not the reviewed core symlink: $root/$directory"; return 1; }
  done
  for path in "$rocm/bin/amdclang++" "$rocm/bin/hipcc" \
    "$rocm/lib/cmake/hip/hip-config.cmake" "$rocm/lib/cmake/hipblas/hipblas-config.cmake" \
    "$rocm/lib/cmake/rocblas/rocblas-config.cmake"; do
    resolved=$(ws_rocm_realpath_existing "$path") || return 1
    [[ $resolved == "$rocm/"* ]] || { ws_die "ROCm 10 AUR path resolves outside the reviewed core: $path"; return 1; }
  done
}

ws_rocm_llama_rocm_owner_output() {
  local ownership
  ownership=$(pacman -Qqo "$@") || { ws_die 'could not identify every installed ROCm path owner'; return 1; }
  printf '%s\n' "$ownership"
}

ws_rocm_llama_official_owner_output() {
  local ownership package owners=()
  ownership=$(ws_rocm_llama_rocm_owner_output "$@") || return 1
  while IFS= read -r package; do
    [[ -n $package ]] && owners+=("$package")
  done < <(printf '%s\n' "$ownership" | awk '{print $1}' | sort -u)
  ((${#owners[@]} > 0)) || ws_die 'could not identify installed package owners'
  for package in "${owners[@]}"; do
    if pacman -Qm "$package" >/dev/null 2>&1; then
      ws_die "path owner is a foreign package: $package"
    fi
    pacman -Si "$package" | awk -F: '$1 ~ /^[[:space:]]*Repository[[:space:]]*$/ && $2 ~ /^[[:space:]]*(core|extra|multilib)[[:space:]]*$/ {found=1} END {exit !found}' \
      || ws_die "path owner is not present in an official Arch sync repository: $package"
  done
  printf '%s\n' "${owners[@]}"
}

ws_rocm_llama_aur_owner_output() {
  local package=$1 version=$2
  shift 2
  local ownership owner path resolved owners=()
  local -a ownership_paths=()
  for path in "$@"; do
    resolved=$(ws_rocm_realpath_existing "$path") || return 1
    ownership_paths+=("$path")
    [[ $resolved == "$path" ]] || ownership_paths+=("$resolved")
  done
  ownership=$(ws_rocm_llama_rocm_owner_output "${ownership_paths[@]}") || return 1
  while IFS= read -r owner; do
    [[ -n $owner ]] && owners+=("${owner%% *}")
  done <<< "$ownership"
  ((${#owners[@]} > 0)) || { ws_die 'could not identify installed ROCm package owners'; return 1; }
  for owner in "${owners[@]}"; do
    [[ $owner == "$package" ]] || { ws_die "ROCm 10 AUR path owner is not the reviewed package: $owner"; return 1; }
  done
  [[ $(pacman -Q "$package") == "$package $version" ]] \
    || { ws_die "ROCm 10 AUR package version does not match the reviewed lock: $package"; return 1; }
  printf '%s\n' "$package"
}

ws_rocm_llama_rocm_validate() {
  local lock=${1:-$(ws_repo_root)/versions.lock} provider rocm ownership package version
  provider=$(ws_rocm_sdk_provider) || return 1
  rocm=$(ws_rocm_sdk_prefix "$provider") || return 1
  [[ -d $rocm && -x $rocm/bin/amdclang++ && -x $rocm/bin/hipcc \
    && -r $rocm/lib/cmake/hip/hip-config.cmake \
    && -r $rocm/lib/cmake/hipblas/hipblas-config.cmake \
    && -r $rocm/lib/cmake/rocblas/rocblas-config.cmake ]] \
    || { ws_die "a coherent $provider ROCm installation is required at $rocm"; return 1; }
  command -v pacman >/dev/null || ws_die 'pacman is required to inspect the installed ROCm ownership'
  case $provider in
    arch)
      ownership=$(ws_rocm_llama_official_owner_output "$rocm/bin/amdclang++" "$rocm/lib/cmake/hip/hip-config.cmake" \
        "$rocm/lib/cmake/hipblas/hipblas-config.cmake" "$rocm/lib/cmake/rocblas/rocblas-config.cmake") \
        || ws_die 'could not identify every installed ROCm path owner'
      ;;
    aur-gfx120x-bin)
      ws_rocm_sdk_aur_lock_validate "$lock" >/dev/null || return 1
      package=$(ws_read_lock ROCM_AUR_PACKAGE "$lock")
      version=$(ws_read_lock ROCM_AUR_PACKAGE_VERSION "$lock")
      ws_rocm_sdk_aur_layout_validate /opt/rocm "$rocm" || return 1
      ownership=$(ws_rocm_llama_aur_owner_output "$package" "$version" "$rocm/bin/amdclang++" \
        "$rocm/bin/hipcc" "$rocm/lib/cmake/hip/hip-config.cmake" \
        "$rocm/lib/cmake/hipblas/hipblas-config.cmake" "$rocm/lib/cmake/rocblas/rocblas-config.cmake" \
        /opt/rocm/bin/amdclang++ /opt/rocm/bin/hipcc \
        /opt/rocm/lib/cmake/hip/hip-config.cmake /opt/rocm/lib/cmake/hipblas/hipblas-config.cmake \
        /opt/rocm/lib/cmake/rocblas/rocblas-config.cmake) \
        || ws_die 'could not identify every reviewed ROCm 10 AUR path owner'
      ;;
  esac
  printf '%s\n' "$ownership"
}

ws_rocm_llama_host_compilers_validate() {
  [[ -x /usr/bin/cc && -x /usr/bin/c++ ]] || ws_die 'the installed Arch host compilers /usr/bin/cc and /usr/bin/c++ are required'
  ws_rocm_llama_official_owner_output /usr/bin/cc /usr/bin/c++
}

ws_rocm_llama_cmake_cache_value() {
  local cache=$1 key=$2 value
  value=$(awk -F= -v key="$key" '$1 ~ "^" key ":[^=]+$" {if (found++) exit 1; print $2} END {exit found == 1 ? 0 : 1}' "$cache") \
    || ws_die "CMake cache has no unique $key value"
  [[ -n $value ]] || ws_die "CMake cache has an empty $key value"
  printf '%s\n' "$value"
}

ws_rocm_llama_cmake_rocm_validate() {
  local cache=$1 lock=${2:-$(ws_repo_root)/versions.lock} provider rocm key directory config package='' version=''
  provider=$(ws_rocm_sdk_provider) || return 1
  rocm=$(ws_rocm_sdk_prefix "$provider") || return 1
  if [[ $provider == aur-gfx120x-bin ]]; then
    ws_rocm_sdk_aur_lock_validate "$lock" >/dev/null || return 1
    package=$(ws_read_lock ROCM_AUR_PACKAGE "$lock")
    version=$(ws_read_lock ROCM_AUR_PACKAGE_VERSION "$lock")
  fi
  for key in hip_DIR hipblas_DIR rocblas_DIR; do
    directory=$(ws_rocm_llama_cmake_cache_value "$cache" "$key") || return 1
    case $key in
      hip_DIR) config='hip-config.cmake' ;;
      hipblas_DIR) config='hipblas-config.cmake' ;;
      rocblas_DIR) config='rocblas-config.cmake' ;;
    esac
    [[ $directory == "$rocm/lib/cmake/${key%_DIR}" && -d $directory && -r $directory/$config ]] \
      || { ws_die "CMake resolved $key outside the canonical $provider ROCm installation"; return 1; }
    case $provider in
      arch) ws_rocm_llama_official_owner_output "$directory/$config" >/dev/null ;;
      aur-gfx120x-bin)
        ws_rocm_llama_aur_owner_output "$package" "$version" "$directory/$config" >/dev/null
        ;;
    esac
  done
}

ws_rocm_llama_cmake_host_compilers_validate() {
  local cache=$1 c_compiler cxx_compiler
  c_compiler=$(ws_rocm_llama_cmake_cache_value "$cache" CMAKE_C_COMPILER) || return 1
  cxx_compiler=$(ws_rocm_llama_cmake_cache_value "$cache" CMAKE_CXX_COMPILER) || return 1
  [[ $c_compiler == /usr/bin/cc && $cxx_compiler == /usr/bin/c++ ]] \
    || ws_die 'CMake did not retain the installed Arch host compiler paths'
}

ws_rocm_llama_ccache_validate() {
  local config="$CCACHE_DIRECTORY/ccache.conf"
  [[ -d $CCACHE_DIRECTORY && ! -L $CCACHE_DIRECTORY && -O $CCACHE_DIRECTORY ]] \
    || ws_die 'ccache directory must be an existing, non-symlink directory owned by the build user'
  [[ -f $config && ! -L $config && -r $config && -O $config ]] \
    || ws_die 'run ccache configure before building llama.cpp'
  printf '%s\n' "$config"
}

ws_rocm_llama_record_toolchain() {
  local output=$1 rocm=$2 package
  shift 2
  {
    cmake --version
    ninja --version
    ccache --version
    /usr/bin/cc --version
    /usr/bin/c++ --version
    "$rocm/bin/amdclang++" --version
    "$rocm/bin/hipcc" --version
  } > "$output/toolchain.txt"
  {
    printf '== installed package metadata ==\n'
    pacman -Qi "$@"
    printf '== sync repository metadata when available ==\n'
    for package in "$@"; do
      pacman -Qm "$package" >/dev/null 2>&1 || pacman -Si "$package"
    done
  } > "$output/rocm-packages.txt"
  while IFS= read -r package; do
    pacman -Qi "$package"
  done < "$output/host-compiler-owners.txt" > "$output/host-compiler-packages.txt"
}

ws_rocm_build_llama() (
  set -euo pipefail
  ws_require_arch
  ws_require_user
  [[ $(uname -m) == x86_64 ]] || ws_die 'llama.cpp ROCm builds require the native x86_64 workstation'
  ws_rocm_unfiltered
  ws_build_config_validate
  (($# == 3 || $# == 4)) || ws_die 'usage: build-llama <checkout> <hardware.json> <new-output-directory> [versions.lock]'
  local source=$1 report=$2 output=$3 lock=${4:-$(ws_repo_root)/versions.lock}
  local report_dir input_boot fresh_boot target fresh_target owners_raw host_compiler_owners pool_args_raw jobs source_commit source_tree provider rocm
  local build_dir binary command_file ccache_config package args_json
  local -a pool_args cmake_args owners
  [[ -f $report && ! -e $output && ! -L $output ]] || ws_die 'provide an existing hardware report and a new non-symlink output directory'
  ws_rocm_llama_checkout_validate "$source" "$lock"
  target=$(ws_detected_gpu_target "$report") || exit 1
  report_dir=$(cd -- "$(dirname -- "$report")" && pwd -P)
  input_boot=$(ws_rocm_boot_id_read "$report_dir/boot-id.txt") || exit 1
  ws_rocm_llama_environment_validate
  provider=$(ws_rocm_sdk_provider) || exit 1
  rocm=$(ws_rocm_sdk_prefix "$provider") || exit 1
  command -v cmake >/dev/null || ws_die 'cmake is required for the llama.cpp ROCm build'
  command -v ninja >/dev/null || ws_die 'ninja is required for the llama.cpp ROCm build'
  command -v ccache >/dev/null || ws_die 'ccache is required for host C/C++ compilation'
  ccache_config=$(ws_rocm_llama_ccache_validate) || exit 1
  owners_raw=$(ws_rocm_llama_rocm_validate "$lock") || ws_die 'could not verify the installed ROCm package ownership'
  host_compiler_owners=$(ws_rocm_llama_host_compilers_validate) || ws_die 'could not verify the installed host compiler ownership'
  while IFS= read -r package; do
    [[ -n $package ]] && owners+=("$package")
  done <<< "$owners_raw"
  ((${#owners[@]} > 0)) || ws_die 'ROCm package ownership verification produced no packages'

  umask 077
  mkdir -p -- "$output"
  # shellcheck disable=SC2030,SC2031 # intentionally scoped to this retained build subprocess
  export CCACHE_DIR="$CCACHE_DIRECTORY" CCACHE_CONFIGPATH="$ccache_config"
  common::sha256_file "$CCACHE_CONFIGPATH" > "$output/ccache-config.sha256"
  # Do not record `ccache --show-config`: its remote-storage configuration can
  # contain credentials. The selected path and content hash are sufficient
  # provenance for this bounded local build.
  printf 'CCACHE_DIR=%s\nCCACHE_CONFIGPATH=%s\n' "$CCACHE_DIR" "$CCACHE_CONFIGPATH" > "$output/ccache-selection.txt"
  printf '%s\n' "$host_compiler_owners" > "$output/host-compiler-owners.txt"
  {
    printf 'provider=%s\nprefix=%s\n' "$provider" "$rocm"
    if [[ $provider == aur-gfx120x-bin ]]; then
      ws_rocm_sdk_aur_lock_validate "$lock"
      printf 'declared_recipe_repository=%s\ndeclared_recipe_commit=%s\ndeclared_PKGBUILD_SHA256=%s\n' \
        "$(ws_read_lock ROCM_AUR_REPOSITORY "$lock")" "$(ws_read_lock ROCM_AUR_COMMIT "$lock")" \
        "$(ws_read_lock ROCM_AUR_PKGBUILD_SHA256 "$lock")"
      printf 'declared_source_url=%s\ndeclared_source_SHA256=%s\nobserved_package_identity=%s\n' \
        "$(ws_read_lock ROCM_AUR_SOURCE_URL "$lock")" "$(ws_read_lock ROCM_AUR_SOURCE_SHA256 "$lock")" \
        "$(pacman -Q "$(ws_read_lock ROCM_AUR_PACKAGE "$lock")")"
    fi
  } > "$output/rocm-sdk-provider.txt"
  cp -- "$report" "$output/input-hardware.json"
  printf '%s\n' "$input_boot" > "$output/input-boot-id.txt"
  ws_hardware_collect "$output/hardware"
  fresh_target=$(ws_detected_gpu_target "$output/hardware/hardware.json") || exit 1
  fresh_boot=$(ws_rocm_boot_id_read "$output/hardware/boot-id.txt") || exit 1
  [[ $input_boot == "$fresh_boot" ]] || ws_die 'hardware report is from a different boot; collect a fresh report before building'
  [[ $target == "$fresh_target" ]] || ws_die 'fresh hardware observation has a different GPU target'
  ws_rocm_hardware_identities_match "$report" "$output/hardware/hardware.json" \
    || ws_die 'fresh hardware observation does not match the recorded dual-GPU identities'

  pool_args_raw=$(ws_cmake_ninja_args memory-heavy) || ws_die 'could not calculate safe CMake/Ninja resource pools'
  # shellcheck disable=SC2030,SC2031 # array is consumed only inside this build subprocess
  while IFS= read -r package; do
    [[ -n $package ]] && pool_args+=("$package")
  done <<< "$pool_args_raw"
  ((${#pool_args[@]} == 3)) || ws_die 'CMake/Ninja resource pool helper returned an unexpected argument count'
  jobs=$(ws_build_jobs memory-heavy) || ws_die 'could not calculate the memory-heavy Ninja concurrency'
  [[ ${pool_args[0]} == "-DCMAKE_JOB_POOLS=compile=$jobs;link=$BUILD_LINK_JOBS" ]] \
    || ws_die 'CMake/Ninja pool measurement changed unexpectedly; rerun the build'
  build_dir="$output/build"
  cmake_args=(
    -S "$source" -B "$build_dir" -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DGGML_HIP=ON
    "-DCMAKE_HIP_ARCHITECTURES=$target"
    -DGGML_NATIVE=ON
    "-DCMAKE_PREFIX_PATH=$rocm"
    -DCMAKE_FIND_USE_PACKAGE_REGISTRY=FALSE
    -DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=FALSE
    -DCMAKE_FIND_USE_PACKAGE_ROOT_PATH=FALSE
    -DCMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH=FALSE
    "-DCMAKE_HIP_COMPILER=$rocm/bin/amdclang++"
    -DCMAKE_C_COMPILER=/usr/bin/cc
    -DCMAKE_CXX_COMPILER=/usr/bin/c++
    -DCMAKE_C_COMPILER_LAUNCHER=ccache
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
    "${pool_args[@]}"
  )
  command_file="$output/cmake-command.txt"
  printf '%q ' env "ROCM_PATH=$rocm" cmake "${cmake_args[@]}" > "$command_file"
  printf '\n' >> "$command_file"
  env "ROCM_PATH=$rocm" cmake "${cmake_args[@]}"
  [[ -r $build_dir/CMakeCache.txt ]] || ws_die 'CMake did not produce CMakeCache.txt'
  grep -Eq '^GGML_HIP:(BOOL|UNINITIALIZED)=ON$' "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain GGML_HIP=ON'
  grep -Eq '^GGML_NATIVE:(BOOL|UNINITIALIZED)=ON$' "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain GGML_NATIVE=ON'
  grep -Eq "^CMAKE_HIP_ARCHITECTURES:[^=]+=$target$" "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain the observed HIP architecture'
  ws_rocm_llama_cmake_rocm_validate "$build_dir/CMakeCache.txt" "$lock" || exit 1
  ws_rocm_llama_cmake_host_compilers_validate "$build_dir/CMakeCache.txt" || exit 1
  cp -- "$build_dir/CMakeCache.txt" "$output/CMakeCache.txt"
  printf '%q ' ninja -C "$build_dir" -j "$jobs" llama-cli llama-bench > "$output/ninja-command.txt"
  printf '\n' >> "$output/ninja-command.txt"
  ninja -C "$build_dir" -j "$jobs" llama-cli llama-bench
  for binary in "$build_dir/bin/llama-cli" "$build_dir/bin/llama-bench"; do
    [[ -x $binary ]] || ws_die "expected llama.cpp build output is missing or non-executable: $binary"
  done

  ws_rocm_llama_checkout_validate "$source" "$lock"
  source_commit=$(git -C "$source" rev-parse HEAD)
  source_tree=$(git -C "$source" rev-parse 'HEAD^{tree}')
  {
    printf 'repository=%s\n' "$(git -C "$source" remote get-url origin)"
    printf 'commit=%s\ntree=%s\n' "$source_commit" "$source_tree"
    git -C "$source" status --porcelain=v1 --untracked-files=all --ignore-submodules=all
  } > "$output/source.txt"
  ws_rocm_llama_record_toolchain "$output" "$rocm" "${owners[@]}"
  args_json=$(printf '%s\n' "${cmake_args[@]}" | jq -Rn '[inputs]')
  jq -n --arg source_commit "$source_commit" --arg source_tree "$source_tree" --arg target "$target" --arg provider "$provider" --arg rocm "$rocm" \
    --arg report_hash "$(common::sha256_file "$report")" \
    --arg fresh_report_hash "$(common::sha256_file "$output/hardware/hardware.json")" \
    --arg cache_hash "$(common::sha256_file "$output/CMakeCache.txt")" \
    --arg cli_hash "$(common::sha256_file "$build_dir/bin/llama-cli")" \
    --arg bench_hash "$(common::sha256_file "$build_dir/bin/llama-bench")" \
    --argjson cmake_args "$args_json" \
    --argjson packages "$(printf '%s\n' "${owners[@]}" | jq -Rn '[inputs]')" \
    '{schema:1,status:"built-not-qualified",profile:"experimental",backend:"hip",source:{commit:$source_commit,tree:$source_tree},
      gpu_target:$target,hardware_report_sha256:$report_hash,fresh_hardware_report_sha256:$fresh_report_hash,
      cmake_cache_sha256:$cache_hash,outputs:{llama_cli_sha256:$cli_hash,llama_bench_sha256:$bench_hash},
      cmake_args:$cmake_args,rocm_sdk_provider:$provider,rocm_prefix:$rocm,rocm_packages:$packages,
      installed:false,qualification_required:["dual-GPU inference","llama-bench review","reboot repeat","sustained soak"]}' \
    > "$output/build-result.json"
  ws_note "llama.cpp ROCm candidate built in $build_dir; it is retained for direct experimental execution and is not pacman-installed or qualified"
)

ws_rocm_llama_vulkan_environment_validate() {
  ws_rocm_llama_environment_validate
  [[ -z ${VULKAN_SDK+x} ]] || ws_die 'unset VULKAN_SDK before building against the installed Arch Vulkan stack'
}

ws_rocm_llama_vulkan_glslc_validate() {
  [[ -x /usr/bin/glslc ]] || ws_die 'the installed Arch glslc executable is required for the Vulkan build'
  ws_rocm_llama_official_owner_output /usr/bin/glslc
}

ws_rocm_llama_vulkan_record_toolchain() {
  local output=$1
  shift
  {
    cmake --version
    ninja --version
    ccache --version
    /usr/bin/cc --version
    /usr/bin/c++ --version
    /usr/bin/glslc --version
  } > "$output/toolchain.txt"
  pacman -Qi "$@" > "$output/packages.txt"
}

ws_rocm_build_llama_vulkan() (
  set -euo pipefail
  ws_require_arch
  ws_require_user
  [[ $(uname -m) == x86_64 ]] || ws_die 'llama.cpp Vulkan builds require the native x86_64 workstation'
  ws_rocm_unfiltered
  ws_build_config_validate
  (($# == 3 || $# == 4)) || ws_die 'usage: build-llama-vulkan <checkout> <hardware.json> <new-output-directory> [versions.lock]'
  local source=$1 report=$2 output=$3 lock=${4:-$(ws_repo_root)/versions.lock}
  local report_dir input_boot fresh_boot target fresh_target ccache_config host_compiler_owners glslc_owners
  local pool_args_raw jobs source_commit source_tree args_json package build_dir binary
  local -a pool_args packages cmake_args
  [[ -f $report && ! -e $output && ! -L $output ]] || ws_die 'provide an existing hardware report and a new non-symlink output directory'
  ws_rocm_llama_checkout_validate "$source" "$lock"
  target=$(ws_detected_gpu_target "$report") || exit 1
  report_dir=$(cd -- "$(dirname -- "$report")" && pwd -P)
  input_boot=$(ws_rocm_boot_id_read "$report_dir/boot-id.txt") || exit 1
  ws_rocm_llama_vulkan_environment_validate
  command -v cmake >/dev/null || ws_die 'cmake is required for the llama.cpp Vulkan build'
  command -v ninja >/dev/null || ws_die 'ninja is required for the llama.cpp Vulkan build'
  command -v ccache >/dev/null || ws_die 'ccache is required for host C/C++ compilation'
  ccache_config=$(ws_rocm_llama_ccache_validate) || exit 1
  host_compiler_owners=$(ws_rocm_llama_host_compilers_validate) || ws_die 'could not verify the installed host compiler ownership'
  glslc_owners=$(ws_rocm_llama_vulkan_glslc_validate) || ws_die 'could not verify installed glslc ownership'
  while IFS= read -r package; do
    [[ -n $package ]] && packages+=("$package")
  done <<< "$host_compiler_owners"
  while IFS= read -r package; do
    [[ -n $package ]] && packages+=("$package")
  done <<< "$glslc_owners"

  umask 077
  mkdir -p -- "$output"
  # shellcheck disable=SC2030,SC2031 # intentionally scoped to this retained build subprocess
  export CCACHE_DIR="$CCACHE_DIRECTORY" CCACHE_CONFIGPATH="$ccache_config"
  common::sha256_file "$CCACHE_CONFIGPATH" > "$output/ccache-config.sha256"
  printf 'CCACHE_DIR=%s\nCCACHE_CONFIGPATH=%s\n' "$CCACHE_DIR" "$CCACHE_CONFIGPATH" > "$output/ccache-selection.txt"
  printf '%s\n' "$host_compiler_owners" > "$output/host-compiler-owners.txt"
  printf '%s\n' "$glslc_owners" > "$output/glslc-owners.txt"
  cp -- "$report" "$output/input-hardware.json"
  printf '%s\n' "$input_boot" > "$output/input-boot-id.txt"
  ws_hardware_collect "$output/hardware"
  fresh_target=$(ws_detected_gpu_target "$output/hardware/hardware.json") || exit 1
  fresh_boot=$(ws_rocm_boot_id_read "$output/hardware/boot-id.txt") || exit 1
  [[ $input_boot == "$fresh_boot" ]] || ws_die 'hardware report is from a different boot; collect a fresh report before building'
  [[ $target == "$fresh_target" ]] || ws_die 'fresh hardware observation has a different GPU target'
  ws_rocm_hardware_identities_match "$report" "$output/hardware/hardware.json" \
    || ws_die 'fresh hardware observation does not match the recorded dual-GPU identities'

  pool_args_raw=$(ws_cmake_ninja_args memory-heavy) || ws_die 'could not calculate safe CMake/Ninja resource pools'
  # shellcheck disable=SC2030,SC2031 # array is consumed only inside this build subprocess
  while IFS= read -r package; do
    [[ -n $package ]] && pool_args+=("$package")
  done <<< "$pool_args_raw"
  ((${#pool_args[@]} == 3)) || ws_die 'CMake/Ninja resource pool helper returned an unexpected argument count'
  jobs=$(ws_build_jobs memory-heavy) || ws_die 'could not calculate the memory-heavy Ninja concurrency'
  [[ ${pool_args[0]} == "-DCMAKE_JOB_POOLS=compile=$jobs;link=$BUILD_LINK_JOBS" ]] \
    || ws_die 'CMake/Ninja pool measurement changed unexpectedly; rerun the build'
  build_dir="$output/build"
  cmake_args=(
    -S "$source" -B "$build_dir" -G Ninja
    -DCMAKE_BUILD_TYPE=Release
    -DGGML_VULKAN=ON
    -DGGML_NATIVE=ON
    -DCMAKE_FIND_USE_PACKAGE_REGISTRY=FALSE
    -DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=FALSE
    -DCMAKE_FIND_USE_PACKAGE_ROOT_PATH=FALSE
    -DCMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH=FALSE
    -DCMAKE_C_COMPILER=/usr/bin/cc
    -DCMAKE_CXX_COMPILER=/usr/bin/c++
    -DCMAKE_C_COMPILER_LAUNCHER=ccache
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
    "${pool_args[@]}"
  )
  printf '%q ' cmake "${cmake_args[@]}" > "$output/cmake-command.txt"
  printf '\n' >> "$output/cmake-command.txt"
  cmake "${cmake_args[@]}"
  [[ -r $build_dir/CMakeCache.txt ]] || ws_die 'CMake did not produce CMakeCache.txt'
  grep -Eq '^GGML_VULKAN:(BOOL|UNINITIALIZED)=ON$' "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain GGML_VULKAN=ON'
  grep -Eq '^GGML_NATIVE:(BOOL|UNINITIALIZED)=ON$' "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain GGML_NATIVE=ON'
  grep -Eq '^Vulkan_GLSLC_EXECUTABLE:[^=]+=/usr/bin/glslc$' "$build_dir/CMakeCache.txt" \
    || ws_die 'CMake did not retain the installed Arch glslc path'
  ws_rocm_llama_cmake_host_compilers_validate "$build_dir/CMakeCache.txt" || exit 1
  cp -- "$build_dir/CMakeCache.txt" "$output/CMakeCache.txt"
  printf '%q ' ninja -C "$build_dir" -j "$jobs" llama-cli llama-bench > "$output/ninja-command.txt"
  printf '\n' >> "$output/ninja-command.txt"
  ninja -C "$build_dir" -j "$jobs" llama-cli llama-bench
  for binary in "$build_dir/bin/llama-cli" "$build_dir/bin/llama-bench"; do
    [[ -x $binary ]] || ws_die "expected llama.cpp build output is missing or non-executable: $binary"
  done

  ws_rocm_llama_checkout_validate "$source" "$lock"
  source_commit=$(git -C "$source" rev-parse HEAD)
  source_tree=$(git -C "$source" rev-parse 'HEAD^{tree}')
  {
    printf 'repository=%s\n' "$(git -C "$source" remote get-url origin)"
    printf 'commit=%s\ntree=%s\n' "$source_commit" "$source_tree"
    git -C "$source" status --porcelain=v1 --untracked-files=all --ignore-submodules=all
  } > "$output/source.txt"
  ws_rocm_llama_vulkan_record_toolchain "$output" "${packages[@]}"
  args_json=$(printf '%s\n' "${cmake_args[@]}" | jq -Rn '[inputs]')
  jq -n --arg source_commit "$source_commit" --arg source_tree "$source_tree" --arg target "$target" \
    --arg report_hash "$(common::sha256_file "$report")" \
    --arg fresh_report_hash "$(common::sha256_file "$output/hardware/hardware.json")" \
    --arg cache_hash "$(common::sha256_file "$output/CMakeCache.txt")" \
    --arg cli_hash "$(common::sha256_file "$build_dir/bin/llama-cli")" \
    --arg bench_hash "$(common::sha256_file "$build_dir/bin/llama-bench")" \
    --argjson cmake_args "$args_json" \
    --argjson packages "$(printf '%s\n' "${packages[@]}" | sort -u | jq -Rn '[inputs]')" \
    '{schema:1,status:"built-not-qualified",profile:"experimental",backend:"vulkan",source:{commit:$source_commit,tree:$source_tree},
      gpu_target:$target,hardware_report_sha256:$report_hash,fresh_hardware_report_sha256:$fresh_report_hash,
      cmake_cache_sha256:$cache_hash,outputs:{llama_cli_sha256:$cli_hash,llama_bench_sha256:$bench_hash},
      cmake_args:$cmake_args,packages:$packages,installed:false,
      qualification_required:["explicit HIP/Vulkan device mapping","paired benchmark review","reboot repeat","sustained soak"]}' \
    > "$output/build-result.json"
  ws_note "llama.cpp Vulkan candidate built in $build_dir; it is retained for direct experimental execution and is not pacman-installed or qualified"
)

ws_llama_defaults() {
  : "${LLAMA_CONTEXT_SIZE:=2048}" "${LLAMA_BATCH_SIZE:=2048}" "${LLAMA_UBATCH_SIZE:=512}"
  : "${LLAMA_FLASH_ATTN:=auto}" "${LLAMA_BENCH_PROMPT_TOKENS:=512}" "${LLAMA_BENCH_GENERATION_TOKENS:=128}" "${LLAMA_BENCH_REPETITIONS:=3}"
}

ws_llama_runtime_config_validate() {
  local key
  ws_llama_defaults
  for key in LLAMA_CONTEXT_SIZE LLAMA_BATCH_SIZE LLAMA_UBATCH_SIZE; do
    ws_positive_integer "${!key:-}" || { ws_die "$key must be a positive integer"; return 1; }
  done
  case ${LLAMA_FLASH_ATTN:-} in on|off|auto) ;; *) ws_die 'LLAMA_FLASH_ATTN must be on, off or auto'; return 1 ;; esac
}

ws_llama_benchmark_config_validate() {
  local key
  ws_llama_runtime_config_validate
  for key in LLAMA_BENCH_PROMPT_TOKENS LLAMA_BENCH_GENERATION_TOKENS LLAMA_BENCH_REPETITIONS; do
    ws_positive_integer "${!key:-}" || { ws_die "$key must be a positive integer"; return 1; }
  done
  ((LLAMA_BENCH_REPETITIONS >= 3)) || { ws_die 'LLAMA_BENCH_REPETITIONS must be at least 3 to report spread'; return 1; }
}

ws_llama_bench_result_validate() {
  local binary=$1 result=$2 backend=$3 hash
  [[ -x $binary && -r $result ]] || ws_die 'each llama-bench binary must be executable and retain its build-result.json'
  hash=$(common::sha256_file "$binary")
  jq -e --arg hash "$hash" --arg backend "$backend" '.status == "built-not-qualified" and .backend == $backend and .source.commit and .source.tree and .outputs.llama_bench_sha256 == $hash' "$result" >/dev/null \
    || ws_die "llama-bench provenance does not match its retained binary: $binary"
}

ws_llama_bench_device_map_validate() {
  local binary=$1 devices=$2 output=$3 name count=0
  [[ $devices =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] \
    || ws_die 'device mappings must contain explicit slash-delimited llama device names for both GPUs'
  "$binary" --list-devices > "$output"
  IFS=/ read -r -a _ws_llama_devices <<< "$devices"
  for name in "${_ws_llama_devices[@]}"; do
    grep -Fq -- "$name:" "$output" || ws_die "requested llama device is absent from $binary: $name"
    count=$((count + 1))
  done
  [[ $count == "$EXPECTED_GPU_COUNT" ]] || ws_die 'device mapping must explicitly cover every expected physical GPU'
  [[ $(printf '%s\n' "${_ws_llama_devices[@]}" | sort -u | awk 'END {print NR}') == "$EXPECTED_GPU_COUNT" ]] \
    || ws_die 'device mapping repeats a device instead of covering every GPU'
}

ws_rocm_benchmark_llama() (
  set -euo pipefail
  ws_require_arch
  ws_require_user
  ws_rocm_unfiltered
  ws_build_config_validate
  ws_llama_runtime_config_validate
  ws_llama_benchmark_config_validate
  (($# == 7)) || ws_die 'usage: benchmark-llama <hardware.json> <hip-bench> <vulkan-bench> <local-model.gguf> <new-output-directory> <hip-devices> <vulkan-devices>'
  local report=$1 hip=$2 vulkan=$3 model=$4 output=$5 hip_devices=$6 vulkan_devices=$7
  local report_dir input_boot fresh_boot target fresh_target hip_result vulkan_result hip_commit vulkan_commit hip_tree vulkan_tree locked_commit
  local -a common_args
  [[ -f $report && -x $hip && -x $vulkan && -f $model && ! -e $output && ! -L $output ]] \
    || ws_die 'provide observed hardware, retained executable benchmarks, one local model and a new non-symlink output directory'
  target=$(ws_detected_gpu_target "$report") || exit 1
  report_dir=$(cd -- "$(dirname -- "$report")" && pwd -P)
  input_boot=$(ws_rocm_boot_id_read "$report_dir/boot-id.txt") || exit 1
  hip_result=$(cd -- "$(dirname -- "$hip")/../.." && pwd -P)/build-result.json
  vulkan_result=$(cd -- "$(dirname -- "$vulkan")/../.." && pwd -P)/build-result.json
  ws_llama_bench_result_validate "$hip" "$hip_result" hip
  ws_llama_bench_result_validate "$vulkan" "$vulkan_result" vulkan
  hip_commit=$(jq -er '.source.commit' "$hip_result")
  vulkan_commit=$(jq -er '.source.commit' "$vulkan_result")
  hip_tree=$(jq -er '.source.tree' "$hip_result")
  vulkan_tree=$(jq -er '.source.tree' "$vulkan_result")
  [[ $hip_commit == "$vulkan_commit" && $hip_tree == "$vulkan_tree" ]] \
    || ws_die 'HIP and Vulkan benchmarks must come from the same pinned llama.cpp source revision'
  locked_commit=$(ws_read_lock ROCM_LLAMA_CPP_COMMIT)
  [[ $hip_commit == "$locked_commit" ]] \
    || ws_die 'HIP and Vulkan benchmarks must match the locked llama.cpp source commit'

  umask 077
  mkdir -p -- "$output"
  cp -- "$report" "$output/input-hardware.json"
  printf '%s\n' "$input_boot" > "$output/input-boot-id.txt"
  ws_hardware_collect "$output/hardware"
  fresh_target=$(ws_detected_gpu_target "$output/hardware/hardware.json") || exit 1
  fresh_boot=$(ws_rocm_boot_id_read "$output/hardware/boot-id.txt") || exit 1
  [[ $input_boot == "$fresh_boot" && $target == "$fresh_target" ]] \
    || ws_die 'benchmark hardware evidence is not from the same boot and GPU target'
  ws_rocm_hardware_identities_match "$report" "$output/hardware/hardware.json" \
    || ws_die 'benchmark hardware observation does not match recorded dual-GPU identities'
  ws_llama_bench_device_map_validate "$hip" "$hip_devices" "$output/hip-devices.txt"
  ws_llama_bench_device_map_validate "$vulkan" "$vulkan_devices" "$output/vulkan-devices.txt"

  common_args=(
    --model "$model" --n-gpu-layers 999
    --batch-size "$LLAMA_BATCH_SIZE" --ubatch-size "$LLAMA_UBATCH_SIZE"
    --flash-attn "$LLAMA_FLASH_ATTN"
    --n-prompt "$LLAMA_BENCH_PROMPT_TOKENS" --n-gen "$LLAMA_BENCH_GENERATION_TOKENS"
    --repetitions "$LLAMA_BENCH_REPETITIONS" --output json
  )
  printf '%q ' "$hip" "${common_args[@]}" --device "$hip_devices" > "$output/hip-command.txt"
  printf '\n' >> "$output/hip-command.txt"
  timeout 1800 "$hip" "${common_args[@]}" --device "$hip_devices" > "$output/hip-metrics.json" 2> "$output/hip-stderr.txt"
  printf '%q ' "$vulkan" "${common_args[@]}" --device "$vulkan_devices" > "$output/vulkan-command.txt"
  printf '\n' >> "$output/vulkan-command.txt"
  timeout 1800 "$vulkan" "${common_args[@]}" --device "$vulkan_devices" > "$output/vulkan-metrics.json" 2> "$output/vulkan-stderr.txt"
  jq -e 'type == "array" and length > 0 and all(.[]; (.avg_ts|type) == "number" and (.stddev_ts|type) == "number")' \
    "$output/hip-metrics.json" "$output/vulkan-metrics.json" >/dev/null \
    || ws_die 'llama-bench did not return JSON throughput and spread metrics'
  jq -n --arg target "$target" --arg model_hash "$(common::sha256_file "$model")" \
    --arg hip_hash "$(common::sha256_file "$hip")" --arg vulkan_hash "$(common::sha256_file "$vulkan")" \
    --arg source_commit "$hip_commit" --arg source_tree "$hip_tree" \
    --arg hip_devices "$hip_devices" --arg vulkan_devices "$vulkan_devices" \
    --argjson hip_metrics "$(<"$output/hip-metrics.json")" --argjson vulkan_metrics "$(<"$output/vulkan-metrics.json")" \
    --argjson context "$LLAMA_CONTEXT_SIZE" --argjson batch "$LLAMA_BATCH_SIZE" --argjson ubatch "$LLAMA_UBATCH_SIZE" \
    --argjson prompt "$LLAMA_BENCH_PROMPT_TOKENS" --argjson generation "$LLAMA_BENCH_GENERATION_TOKENS" \
    --argjson repetitions "$LLAMA_BENCH_REPETITIONS" --arg flash "$LLAMA_FLASH_ATTN" \
    '{schema:1,status:"measured-not-qualified",gpu_target:$target,
      provenance:{model_sha256:$model_hash,source_commit:$source_commit,source_tree:$source_tree,
        hip_binary_sha256:$hip_hash,vulkan_binary_sha256:$vulkan_hash},
      configuration:{context_size:{value:$context,scope:"recorded only; pinned llama-bench has no context option"},
        batch_size:$batch,ubatch_size:$ubatch,prompt_tokens:$prompt,generation_tokens:$generation,
        repetitions:$repetitions,flash_attention:$flash,warmup:"native llama-bench warmup retained",
        cache_policy:"warm compute throughput; cold file-cache state uncontrolled"},
      device_mapping:{hip:$hip_devices,vulkan:$vulkan_devices,assertion:"operator-supplied full two-GPU mapping; backend names do not independently prove PCI identity"},
      metrics:{hip:$hip_metrics,vulkan:$vulkan_metrics},
      qualification_required:["review JSON throughput and spread","validate backend device mapping against PCI/UUID evidence","repeat after reboot","sustained soak"]}' \
    > "$output/benchmark-result.json"
  ws_note "paired HIP/Vulkan benchmark recorded in $output; results are measured evidence, not a qualification or promotion"
)

ws_rocm_validate() (
  set -euo pipefail
  ws_require_arch
  ws_require_user
  ws_rocm_unfiltered
  ws_build_config_validate
  local output=$1 python=${2:-python} target hipcc
  [[ ! -e $output ]] || ws_die 'validation output already exists'
  mkdir -p -- "$output"
  output=$(cd "$output" && pwd -P)
  ws_hardware_collect "$output/hardware"
  target=$(ws_detected_gpu_target "$output/hardware/hardware.json") || exit 1
  ws_build_session_guard
  hipcc=$(command -v hipcc) || ws_die 'hipcc is unavailable in the selected ROCm environment'
  command -v "$python" >/dev/null || ws_die 'selected PyTorch Python executable is unavailable'
  "$hipcc" --version > "$output/hipcc.txt"
  "$hipcc" -O2 --offload-arch="$target" "$(ws_repo_root)/tests/hardware/hip-smoke.cpp" -o "$output/hip-smoke"
  "$output/hip-smoke" "$EXPECTED_GPU_COUNT" "$target" "$EXPECTED_GPU_MODEL" | tee "$output/hip.txt"
  "$python" "$(ws_repo_root)/tests/hardware/torch-rocm.py" --count "$EXPECTED_GPU_COUNT" --model "$EXPECTED_GPU_MODEL" \
    > "$output/pytorch.json"
  timeout 180 "$python" "$(ws_repo_root)/tests/hardware/torch-rocm.py" --count "$EXPECTED_GPU_COUNT" \
    --model "$EXPECTED_GPU_MODEL" --collective > "$output/rccl.json"
  rocm-smi > "$output/rocm-smi-after.txt"
  jq -n --arg target "$target" --argjson count "$EXPECTED_GPU_COUNT" \
    '{schema:1,status:"passed",gpu_target:$target,gpu_count:$count,tests:["HIP-each-device","FP32","FP16","BF16-if-supported","RCCL-all-reduce"],
      pending:["multi-GPU-inference","reboot-repeat","sustained-soak"]}' > "$output/result.json"
  ws_note "GPU validation passed: $output/result.json; inference, reboot and soak remain separate gates"
)

ws_rocm_inference() (
  set -euo pipefail
  ws_require_arch
  ws_require_user
  ws_rocm_unfiltered
  ws_build_config_validate
  local binary=$1 model=$2 output=$3 commit devices shares device target
  [[ -x $binary && -f $model && ! -e $output ]] || ws_die 'provide a llama-cli executable, local GGUF file and new output directory'
  commit=$(ws_read_lock ROCM_LLAMA_CPP_COMMIT)
  mkdir -p -- "$output"
  ws_hardware_collect "$output/hardware"
  target=$(ws_detected_gpu_target "$output/hardware/hardware.json") || exit 1
  "$binary" --version > "$output/version.txt" 2>&1
  grep -Fq "${commit:0:7}" "$output/version.txt" || ws_die 'llama-cli version does not match the source commit in versions.lock'
  "$binary" --list-devices > "$output/devices.txt" 2>&1
  devices=$(awk '$1 ~ /^ROCm[0-9]+:$/ {sub(/:$/, "", $1); print $1}' "$output/devices.txt")
  [[ $(printf '%s\n' "$devices" | awk 'NF {n++} END {print n+0}') == "$EXPECTED_GPU_COUNT" ]] || ws_die 'llama.cpp did not expose every ROCm GPU'
  shares=$(printf '%s\n' "$devices" | awk '{printf "%s1", sep;sep=","}')
  devices=$(printf '%s\n' "$devices" | paste -sd, -)
  timeout 600 "$binary" --model "$model" --device "$devices" --split-mode layer --tensor-split "$shares" \
    --n-gpu-layers 999 --ctx-size "$LLAMA_CONTEXT_SIZE" --batch-size "$LLAMA_BATCH_SIZE" --ubatch-size "$LLAMA_UBATCH_SIZE" \
    --flash-attn "$LLAMA_FLASH_ATTN" --seed 42 --temp 0 --n-predict 64 --prompt 'Explain why reproducible builds matter.' \
    > "$output/inference.txt" 2>&1
  # A successful process is only evidence; verify log allocations and output
  # quality before recording this workload as accepted.
  common::sha256_file "$model" > "$output/model.sha256"
  IFS=',' read -r -a _ws_llama_devices <<< "$devices"
  for device in "${_ws_llama_devices[@]}"; do
    grep -Eq "$device.*model buffer size[[:space:]]*=[[:space:]]*[1-9]" "$output/inference.txt" \
      || ws_die "no positive model allocation recorded for $device; do not accept a one-GPU/CPU fallback"
  done
  printf '%s\n' "$target" > "$output/gpu-target.txt"
  ws_note "inference completed; review per-GPU allocations and output in $output/inference.txt before promotion"
)
