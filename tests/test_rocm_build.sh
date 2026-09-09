#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

lock="$work/versions.lock"
repo=https://github.com/ggml-org/llama.cpp.git
source_dir="$work/llama.cpp"
git init -q "$source_dir"
printf '%s\n' 'cmake_minimum_required(VERSION 3.21)' > "$source_dir/CMakeLists.txt"
git -C "$source_dir" add CMakeLists.txt
git -C "$source_dir" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$source_dir" remote add origin "$repo"
commit=$(git -C "$source_dir" rev-parse HEAD)
printf '%s\n' \
  "ROCM_LLAMA_CPP_REPOSITORY=$repo" \
  "ROCM_LLAMA_CPP_COMMIT=$commit" \
  'ROCM_AUR_REPOSITORY=https://aur.archlinux.org/rocm-gfx120x-bin.git' \
  'ROCM_AUR_PACKAGE=rocm-gfx120x-bin' \
  'ROCM_AUR_PACKAGE_VERSION=10.0.0-2' \
  'ROCM_AUR_COMMIT=ccac18259575a393b402ea90cd9ef3552081721e' \
  'ROCM_AUR_PKGBUILD_SHA256=578d394a3f82f4006fca1707a2bed85ffc9774b4ca24e75e723399ae52dbb4d8' \
  'ROCM_AUR_SOURCE_URL=https://stable.repo.amd.com/rocm/core/tarball/therock-dist-linux-gfx120X-all-10.0.0.tar.gz' \
  'ROCM_AUR_SOURCE_SHA256=eb99db434a1738fd83b0c3b933146cdb76418f35fcf4647743fbdfef76e8c71f' > "$lock"

boot_id=11111111-2222-3333-4444-555555555555
make_report() {
  local output=$1 current_boot=${2:-$boot_id} target=${3:-gfx1201} bdf=${4:-0000:01:00.0}
  mkdir -p -- "$output"
  printf '%s\n' "$current_boot" > "$output/boot-id.txt"
  jq -n --arg target "$target" --arg bdf "$bdf" \
    '{schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2},gpu_target:$target,
      pci_gpus:[{bdf:$bdf,device_id:"0x1234",driver:"amdgpu"},{bdf:"0000:02:00.0",device_id:"0x1234",driver:"amdgpu"}],
      rocm_agents:[{agent:"1",gfx:$target,uuid:"GPU-111"},{agent:"2",gfx:$target,uuid:"GPU-222"}]}' \
    > "$output/hardware.json"
}
make_report "$work/recorded"

fake_bin="$work/bin"
mkdir -p -- "$fake_bin"
cat > "$fake_bin/cmake" <<'STUB'
#!/usr/bin/env bash
printf 'CCACHE_DIR=%s CCACHE_CONFIGPATH=%s\n' "${CCACHE_DIR:-}" "${CCACHE_CONFIGPATH:-}" >> "$CMAKE_LOG"
printf '%s\n' "$*" >> "$CMAKE_LOG"
while (($#)); do
  if [[ $1 == -S ]]; then source_dir=$2; shift 2; continue; fi
  if [[ $1 == -B ]]; then build=$2; shift 2; continue; fi
  shift
done
mkdir -p -- "$build/bin"
hip_dir=/opt/rocm/lib/cmake/hip
host_c=/usr/bin/cc
host_cxx=/usr/bin/c++
if [[ ${BAD_ROCM_CACHE:-0} == 1 ]]; then hip_dir=/tmp/foreign-rocm; fi
if [[ ${BAD_HOST_COMPILER_CACHE:-0} == 1 ]]; then host_c=/tmp/foreign-cc; fi
printf '%s\n' \
  'CMAKE_HIP_ARCHITECTURES:STRING=gfx1201' \
  'GGML_HIP:BOOL=ON' \
  'GGML_HIP_GRAPHS:BOOL=ON' \
  'GGML_HIP_RCCL:BOOL=OFF' \
  'GGML_NATIVE:BOOL=ON' \
  "hip_DIR:PATH=$hip_dir" \
  'hipblas_DIR:PATH=/opt/rocm/lib/cmake/hipblas' \
  'rocblas_DIR:PATH=/opt/rocm/lib/cmake/rocblas' \
  "CMAKE_C_COMPILER:FILEPATH=$host_c" \
  "CMAKE_CXX_COMPILER:FILEPATH=$host_cxx" > "$build/CMakeCache.txt"
printf '#!/usr/bin/env bash\nexit 0\n' > "$build/bin/llama-cli"
printf '#!/usr/bin/env bash\nexit 0\n' > "$build/bin/llama-bench"
printf '%s\n' 'LLAMA_BUILD_TESTS:BOOL=ON' >> "$build/CMakeCache.txt"
printf '%s\n' 'DEFINES = -DGGML_HIP_GRAPHS' > "$build/build.ninja"
cp "$build/bin/llama-bench" "$build/bin/llama-perplexity"
cp "$build/bin/llama-bench" "$build/bin/test-backend-ops"
chmod +x "$build/bin/llama-perplexity" "$build/bin/test-backend-ops"
chmod +x "$build/bin/llama-cli" "$build/bin/llama-bench"
if [[ ${MUTATE_SOURCE_AFTER_CONFIGURE:-0} == 1 ]]; then
  printf 'unexpected source change\n' > "$source_dir/untracked-after-configure"
fi
STUB
cat > "$fake_bin/ninja" <<'STUB'
#!/usr/bin/env bash
printf 'CCACHE_DIR=%s CCACHE_CONFIGPATH=%s\n' "${CCACHE_DIR:-}" "${CCACHE_CONFIGPATH:-}" >> "$NINJA_LOG"
printf '%s\n' "$*" >> "$NINJA_LOG"
STUB
cat > "$fake_bin/ccache" <<'STUB'
#!/usr/bin/env bash
if [[ ${1:-} == --show-config ]]; then
  printf 'cache_dir = %s\n' "${CCACHE_DIR:-}"
else
  printf 'ccache test\n'
fi
STUB
chmod +x "$fake_bin/cmake" "$fake_bin/ninja" "$fake_bin/ccache"

export PATH="$fake_bin:$PATH" CMAKE_LOG="$work/cmake.log" NINJA_LOG="$work/ninja.log"
CCACHE_DIRECTORY="$work/ccache"
mkdir -p -- "$CCACHE_DIRECTORY"
printf 'cache_dir = %s\nmax_size = 100G\n' "$CCACHE_DIRECTORY" > "$CCACHE_DIRECTORY/ccache.conf"
ws_require_arch() { :; }
ws_require_user() { :; }
uname() { [[ ${1:-} == -m ]] && printf 'x86_64\n' || printf 'Linux\n'; }
nproc() { printf '48\n'; }
ws_available_memory_mib() { printf '65536\n'; }
ws_rocm_llama_rocm_validate() { printf 'rocm-core\nhip-runtime-amd\n'; }
ws_rocm_llama_cmake_rocm_validate() {
  [[ ${BAD_ROCM_CACHE:-0} != 1 ]] || return 1
}
ws_rocm_llama_host_compilers_validate() { printf 'gcc\n'; }
ws_rocm_llama_cmake_host_compilers_validate() {
  [[ ${BAD_HOST_COMPILER_CACHE:-0} != 1 ]] || return 1
}
ws_rocm_llama_record_toolchain() {
  local output=$1
  printf 'stub toolchain\n' > "$output/toolchain.txt"
  printf 'stub official packages\n' > "$output/rocm-packages.txt"
}
ws_hardware_collect() {
  local output=$1
  make_report "$output" "${FRESH_BOOT_ID:-$boot_id}" "${FRESH_TARGET:-gfx1201}" "${FRESH_BDF:-0000:01:00.0}"
}

pacman() {
  [[ $1 == -Qqo ]] || return 1
  printf 'rocm-core %s\n' "$2"
  return 1
}
if owner_output=$(ws_rocm_llama_rocm_owner_output /opt/rocm/bin/amdclang++ /opt/rocm/lib/cmake/hip/hip-config.cmake 2>/dev/null); then
  printf 'partial pacman ownership output was accepted\n' >&2
  exit 1
fi
[[ -z $owner_output ]] || { printf 'partial pacman ownership output escaped validation\n' >&2; exit 1; }
unset -f pacman

ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/output" "$lock" >/dev/null
grep -Fq -- "CCACHE_DIR=$CCACHE_DIRECTORY CCACHE_CONFIGPATH=$CCACHE_DIRECTORY/ccache.conf" "$CMAKE_LOG"
grep -Fq -- "CCACHE_DIR=$CCACHE_DIRECTORY CCACHE_CONFIGPATH=$CCACHE_DIRECTORY/ccache.conf" "$NINJA_LOG"
[[ $(<"$work/output/ccache-config.sha256") =~ ^[a-f0-9]{64}$ ]]
grep -Fq -- "CCACHE_DIR=$CCACHE_DIRECTORY" "$work/output/ccache-selection.txt"
grep -Fq -- "CCACHE_CONFIGPATH=$CCACHE_DIRECTORY/ccache.conf" "$work/output/ccache-selection.txt"
grep -Fxq 'provider=arch' "$work/output/rocm-sdk-provider.txt"
grep -Fxq 'prefix=/opt/rocm' "$work/output/rocm-sdk-provider.txt"
grep -Fq -- '-DGGML_HIP=ON' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_HIP_ARCHITECTURES=gfx1201' "$CMAKE_LOG"
grep -Fq -- '-DGGML_NATIVE=ON' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_PREFIX_PATH=/opt/rocm' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_PACKAGE_ROOT_PATH=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_HIP_COMPILER=/opt/rocm/bin/amdclang++' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_C_COMPILER=/usr/bin/cc' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_CXX_COMPILER=/usr/bin/c++' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_C_COMPILER_LAUNCHER=ccache' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_JOB_POOLS=compile=10;link=1' "$CMAKE_LOG"
grep -Fq -- '-j 10 llama-cli llama-bench' "$NINJA_LOG"
if grep -Eq 'fast-math|ROCWMMA|VMM|CMAKE_HIP_COMPILER_LAUNCHER' "$CMAKE_LOG"; then
  printf 'unreviewed HIP optimization or HIP caching option was passed to CMake\n' >&2
  exit 1
fi
[[ -x $work/output/build/bin/llama-cli && -x $work/output/build/bin/llama-bench ]]
jq -e '.status == "built-not-qualified" and .installed == false and .gpu_target == "gfx1201" and .rocm_sdk_provider == "arch" and .rocm_prefix == "/opt/rocm" and
  (.outputs.llama_cli_sha256|length == 64) and (.cmake_args|index("-DGGML_HIP=ON") != null)' \
  "$work/output/build-result.json" >/dev/null

ROCM_SDK_PROVIDER=aur-gfx120x-bin
pacman() {
  [[ ${1:-} == -Q && ${2:-} == rocm-gfx120x-bin ]] || return 1
  printf 'rocm-gfx120x-bin 10.0.0-2\n'
}
ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/aur-output" "$lock" >/dev/null
grep -Fq -- '-DCMAKE_PREFIX_PATH=/opt/rocm/core' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_HIP_COMPILER=/opt/rocm/core/bin/amdclang++' "$CMAKE_LOG"
grep -Fxq 'provider=aur-gfx120x-bin' "$work/aur-output/rocm-sdk-provider.txt"
grep -Fxq 'prefix=/opt/rocm/core' "$work/aur-output/rocm-sdk-provider.txt"
grep -Fxq 'observed_package_identity=rocm-gfx120x-bin 10.0.0-2' "$work/aur-output/rocm-sdk-provider.txt"
grep -Fxq 'declared_recipe_repository=https://aur.archlinux.org/rocm-gfx120x-bin.git' "$work/aur-output/rocm-sdk-provider.txt"
jq -e '.rocm_sdk_provider == "aur-gfx120x-bin" and .rocm_prefix == "/opt/rocm/core"' \
  "$work/aur-output/build-result.json" >/dev/null
unset ROCM_SDK_PROVIDER
unset -f pacman

for failure in stale-boot changed-gpu existing-output dirty-source; do
  output="$work/$failure"
  case $failure in
    stale-boot)
      FRESH_BOOT_ID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
      if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$output" "$lock" >/dev/null 2>&1; then
        printf 'stale boot evidence was accepted\n' >&2; exit 1
      fi
      [[ ! -e $output/build ]]
      unset FRESH_BOOT_ID
      ;;
    changed-gpu)
      FRESH_BDF=0000:03:00.0
      if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$output" "$lock" >/dev/null 2>&1; then
        printf 'changed GPU identity was accepted\n' >&2; exit 1
      fi
      [[ ! -e $output/build ]]
      unset FRESH_BDF
      ;;
    existing-output)
      mkdir -p -- "$output"
      if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$output" "$lock" >/dev/null 2>&1; then
        printf 'existing output directory was accepted\n' >&2; exit 1
      fi
      ;;
    dirty-source)
      printf 'dirty\n' > "$source_dir/untracked"
      if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$output" "$lock" >/dev/null 2>&1; then
        printf 'dirty llama.cpp source was accepted\n' >&2; exit 1
      fi
      rm -- "$source_dir/untracked"
      ;;
  esac
done

MUTATE_SOURCE_AFTER_CONFIGURE=1
export MUTATE_SOURCE_AFTER_CONFIGURE
if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/source-mutated-during-build" "$lock" >/dev/null 2>&1; then
  printf 'source mutation during the build was accepted\n' >&2
  exit 1
fi
unset MUTATE_SOURCE_AFTER_CONFIGURE
rm -- "$source_dir/untracked-after-configure"
[[ ! -e $work/source-mutated-during-build/build-result.json ]]

BAD_ROCM_CACHE=1
export BAD_ROCM_CACHE
if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/foreign-rocm-cache" "$lock" >/dev/null 2>&1; then
  printf 'foreign ROCm CMake package path was accepted\n' >&2
  exit 1
fi
unset BAD_ROCM_CACHE
[[ ! -e $work/foreign-rocm-cache/build-result.json ]]

BAD_HOST_COMPILER_CACHE=1
export BAD_HOST_COMPILER_CACHE
if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/foreign-host-compiler" "$lock" >/dev/null 2>&1; then
  printf 'foreign host C/C++ compiler was accepted\n' >&2
  exit 1
fi
unset BAD_HOST_COMPILER_CACHE
[[ ! -e $work/foreign-host-compiler/build-result.json ]]

CMAKE_TOOLCHAIN_FILE="$work/foreign-toolchain.cmake"
export CMAKE_TOOLCHAIN_FILE
if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/foreign-toolchain" "$lock" >/dev/null 2>&1; then
  printf 'CMake toolchain-file environment override was accepted\n' >&2
  exit 1
fi
unset CMAKE_TOOLCHAIN_FILE

ROCM_PATH=/tmp/other-rocm
export ROCM_PATH
if ws_rocm_build_llama "$source_dir" "$work/recorded/hardware.json" "$work/mixed-environment" "$lock" >/dev/null 2>&1; then
  printf 'mixed ROCm environment was accepted\n' >&2
  exit 1
fi
unset ROCM_PATH

printf 'ROCm llama.cpp build tests passed\n'
