#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
source_dir="$work/llama.cpp"
repository=https://github.com/ggml-org/llama.cpp.git
git init -q "$source_dir"
printf '%s\n' 'cmake_minimum_required(VERSION 3.21)' >"$source_dir/CMakeLists.txt"
git -C "$source_dir" add CMakeLists.txt
git -C "$source_dir" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$source_dir" remote add origin "$repository"
commit=$(git -C "$source_dir" rev-parse HEAD)
lock="$work/versions.lock"
printf 'ROCM_LLAMA_CPP_REPOSITORY=%s\nROCM_LLAMA_CPP_COMMIT=%s\n' "$repository" "$commit" >"$lock"

make_report() {
  local output=$1
  mkdir -p -- "$output"
  printf '%s\n' 11111111-2222-3333-4444-555555555555 >"$output/boot-id.txt"
  jq -n '{schema:1,status:"observed",os:"Linux",architecture:"x86_64",expected:{gpu_count:2},gpu_target:"gfx1201",
    pci_gpus:[{bdf:"0000:01:00.0",device_id:"0x1234",driver:"amdgpu"},{bdf:"0000:02:00.0",device_id:"0x1234",driver:"amdgpu"}],
    rocm_agents:[{agent:"1",gfx:"gfx1201",uuid:"GPU-111"},{agent:"2",gfx:"gfx1201",uuid:"GPU-222"}]}' >"$output/hardware.json"
}
make_report "$work/recorded"

fake_bin="$work/bin"
mkdir -p -- "$fake_bin"
cat >"$fake_bin/cmake" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CMAKE_LOG"
[[ ${1:-} != --version ]] || { printf 'cmake fixture\n'; exit 0; }
previous=''
for argument in "$@"; do
  if [[ $previous == -B ]]; then
    build=$argument
    break
  fi
  previous=$argument
done
: "${build:?missing CMake build directory}"
mkdir -p -- "$build/bin"
printf '%s\n' 'GGML_VULKAN:BOOL=ON' 'GGML_NATIVE:BOOL=ON' \
  'Vulkan_GLSLC_EXECUTABLE:FILEPATH=/usr/bin/glslc' \
  'CMAKE_C_COMPILER:FILEPATH=/usr/bin/cc' 'CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++' > "$build/CMakeCache.txt"
printf '#!/usr/bin/env bash\nexit 0\n' > "$build/bin/llama-cli"
printf '#!/usr/bin/env bash\nexit 0\n' > "$build/bin/llama-bench"
printf '%s\n' 'LLAMA_BUILD_TESTS:BOOL=ON' >> "$build/CMakeCache.txt"
cp "$build/bin/llama-bench" "$build/bin/llama-perplexity"
cp "$build/bin/llama-bench" "$build/bin/test-backend-ops"
chmod +x "$build/bin/llama-perplexity" "$build/bin/test-backend-ops"
chmod +x "$build/bin/llama-cli" "$build/bin/llama-bench"
STUB
cat >"$fake_bin/ninja" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$NINJA_LOG"
STUB
cat >"$fake_bin/ccache" <<'STUB'
#!/usr/bin/env bash
printf 'ccache test\n'
STUB
chmod +x "$fake_bin/cmake" "$fake_bin/ninja" "$fake_bin/ccache"

export PATH="$fake_bin:$PATH" CMAKE_LOG="$work/cmake.log" NINJA_LOG="$work/ninja.log"
CCACHE_DIRECTORY="$work/ccache"
mkdir -p -- "$CCACHE_DIRECTORY"
printf 'cache_dir = %s\n' "$CCACHE_DIRECTORY" >"$CCACHE_DIRECTORY/ccache.conf"
ws_require_arch() { :; }
ws_require_user() { :; }
uname() { [[ ${1:-} == -m ]] && printf 'x86_64\n' || printf 'Linux\n'; }
nproc() { printf '48\n'; }
ws_available_memory_mib() { printf '65536\n'; }
ws_hardware_collect() { make_report "$1"; }
ws_rocm_llama_host_compilers_validate() { printf 'gcc\n'; }
ws_rocm_llama_vulkan_glslc_validate() { printf 'shaderc\n'; }
ws_rocm_llama_cmake_host_compilers_validate() { :; }
ws_rocm_llama_vulkan_record_toolchain() {
  printf 'stub Vulkan toolchain\n' >"$1/toolchain.txt"
  printf 'stub Vulkan packages\n' >"$1/packages.txt"
}
ws_rocm_build_llama_vulkan "$source_dir" "$work/recorded/hardware.json" "$work/output" "$lock" >/dev/null

grep -Fq -- '-DGGML_VULKAN=ON' "$CMAKE_LOG"
grep -Fq -- '-DGGML_NATIVE=ON' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_PACKAGE_ROOT_PATH=FALSE' "$CMAKE_LOG"
grep -Fq -- '-DCMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH=FALSE' "$CMAKE_LOG"
if grep -Eq 'GGML_HIP|fast-math|ROCWMMA|VMM|CMAKE_HIP_COMPILER_LAUNCHER' "$CMAKE_LOG"; then
  printf 'Vulkan build accepted HIP-only or speculative optimization settings\n' >&2
  exit 1
fi
grep -Fq -- '-j 10 llama-cli llama-bench' "$NINJA_LOG"
jq -e '.status == "built-not-qualified" and .backend == "vulkan" and .installed == false and
  (.outputs.llama_bench_sha256|length == 64)' "$work/output/build-result.json" >/dev/null

VULKAN_SDK="$work/foreign-sdk"
export VULKAN_SDK
if ws_rocm_build_llama_vulkan "$source_dir" "$work/recorded/hardware.json" "$work/foreign-sdk" "$lock" >/dev/null 2>&1; then
  printf 'VULKAN_SDK override was accepted\n' >&2
  exit 1
fi
unset VULKAN_SDK

printf 'ROCm Vulkan llama.cpp build tests passed\n'
