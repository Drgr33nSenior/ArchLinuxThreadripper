#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/workstation/build.sh
source "$repo_root/lib/workstation/build.sh"

ws_die() {
  printf 'test failure: %s\n' "$*" >&2
  exit 1
}
ws_available_memory_mib() { printf '%s\n' "$MOCK_AVAILABLE_MEMORY_MIB"; }
uname() { printf 'Linux\n'; }
nproc() { printf '48\n'; }

MOCK_AVAILABLE_MEMORY_MIB=65536
normal_args=$(ws_cmake_ninja_args normal)
expected_normal=$'-DCMAKE_JOB_POOLS=compile=20;link=1\n-DCMAKE_JOB_POOL_COMPILE=compile\n-DCMAKE_JOB_POOL_LINK=link'
[[ $normal_args == "$expected_normal" ]]

heavy_args=$(ws_cmake_ninja_args memory-heavy)
expected_heavy=$'-DCMAKE_JOB_POOLS=compile=10;link=1\n-DCMAKE_JOB_POOL_COMPILE=compile\n-DCMAKE_JOB_POOL_LINK=link'
[[ $heavy_args == "$expected_heavy" ]]

for kind in unsupported normal; do
  if [[ $kind == normal ]]; then
    MOCK_AVAILABLE_MEMORY_MIB=24000
  fi
  if output=$(ws_cmake_ninja_args "$kind" 2>/dev/null); then
    printf 'invalid CMake/Ninja pool request was accepted: %s\n' "$kind" >&2
    exit 1
  fi
  [[ -z $output ]] || {
    printf 'failed CMake/Ninja pool request emitted partial output\n' >&2
    exit 1
  }
done
MOCK_AVAILABLE_MEMORY_MIB=65536

BUILD_LINK_JOBS=0
if output=$(ws_cmake_ninja_args normal 2>/dev/null); then
  printf 'invalid link-job budget was accepted\n' >&2
  exit 1
fi
[[ -z $output ]] || {
  printf 'invalid configuration emitted partial CMake/Ninja arguments\n' >&2
  exit 1
}
BUILD_LINK_JOBS=1

if command -v cmake >/dev/null && command -v ninja >/dev/null; then
  work=$(mktemp -d)
  trap 'rm -rf -- "$work"' EXIT
  printf '%s\n' \
    'cmake_minimum_required(VERSION 3.15)' \
    'project(workstation_pool_fixture C)' \
    'add_executable(workstation_pool_fixture main.c)' >"$work/CMakeLists.txt"
  printf '%s\n' 'int main(void) { return 0; }' >"$work/main.c"

  cmake_args=()
  while IFS= read -r argument; do
    cmake_args+=("$argument")
  done <<<"$normal_args"
  cmake -S "$work" -B "$work/build" -G Ninja "${cmake_args[@]}" >/dev/null
  rg -q '^[[:space:]]*pool = compile$' "$work/build/build.ninja"
  rg -q '^[[:space:]]*pool = link$' "$work/build/build.ninja"
  ninja -C "$work/build" >/dev/null
else
  printf 'SKIP: generated CMake/Ninja pool fixture requires cmake and ninja\n'
fi

printf 'CMake/Ninja build pool tests passed\n'
