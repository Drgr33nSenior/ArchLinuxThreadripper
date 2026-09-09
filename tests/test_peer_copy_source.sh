#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source_file="$repo_root/tests/hardware/hip-peer-copy.cpp"
mock_include="$repo_root/tests/fixtures/hardware-tranche"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
compiler=$(printenv CXX || true)
[[ -n $compiler ]] || compiler=c++

[[ -f $source_file ]]
grep -Fq 'constexpr std::size_t kCopyBytes = 16U * 1024U * 1024U;' "$source_file"
grep -Fq 'hipDeviceCanAccessPeer(&capability, destination.ordinal,' "$source_file"
grep -Fq 'hipDeviceEnablePeerAccess(source.ordinal, 0)' "$source_file"
grep -Fq 'hipMemcpyPeer(destination_memory, destination.ordinal,' "$source_file"
grep -Fq 'hipMemcpy(actual.data(), destination_memory, kCopyBytes,' "$source_file"
grep -Fq 'std::memcmp(expected.data(), actual.data(), kCopyBytes)' "$source_file"
grep -Fq 'hipDeviceGetPCIBusId(info.pci_bus_id' "$source_file"
grep -Fq 'hipDeviceGetUuid(&uuid, ordinal)' "$source_file"
grep -Fq 'return kExitSkipped;' "$source_file"
grep -Fq 'hipDeviceDisablePeerAccess(source.ordinal)' "$source_file"

if grep -Fq '\\n' "$source_file"; then
  echo 'peer-copy diagnostic contains double-escaped output newlines' >&2
  exit 1
fi
if grep -Eq 'kCopyBytes = [1-9][0-9]{8,}' "$source_file"; then
  echo 'peer-copy diagnostic exceeds its bounded allocation' >&2
  exit 1
fi

command -v "$compiler" >/dev/null || {
  echo "C++ compiler is required for the mock HIP diagnostic test: $compiler" >&2
  exit 1
}
"$compiler" -std=c++17 -O0 -I "$mock_include" "$source_file" -o "$work/hip-peer-copy-mock"

"$work/hip-peer-copy-mock" >"$work/success.out"
[[ $(wc -l <"$work/success.out") -eq 5 ]]
grep -Fq 'PAIR source=0 ' "$work/success.out"
grep -Fq 'PAIR source=1 ' "$work/success.out"
grep -Fq 'result=PASS correctness=verified' "$work/success.out"
grep -Fq 'SUMMARY passed=2 skipped=0 failed=0' "$work/success.out"
if grep -Fq '\n' "$work/success.out"; then
  echo 'mock HIP success output contains literal newline escapes' >&2
  exit 1
fi

run_mode() {
  local mode expected_status output actual_status
  mode=$1
  expected_status=$2
  output="$work/$mode.out"
  set +e
  MOCK_HIP_MODE="$mode" "$work/hip-peer-copy-mock" >"$output" 2>&1
  actual_status=$?
  set -e
  [[ $actual_status == "$expected_status" ]]
}

run_mode no-capability 77
grep -Fq 'result=SKIP correctness=not-run' "$work/no-capability.out"
grep -Fq 'SKIP result=SKIP reason=no-functional-peer-copy-pair' "$work/no-capability.out"
run_mode copy-failure 1
grep -Fq 'result=ERROR correctness=failed' "$work/copy-failure.out"
grep -Fq 'detail=peer-copy-failed' "$work/copy-failure.out"
run_mode corrupt-readback 1
grep -Fq 'result=ERROR correctness=failed' "$work/corrupt-readback.out"
grep -Fq 'detail=readback-mismatch' "$work/corrupt-readback.out"

echo 'HIP peer-copy source and bounded mock-HIP control-flow tests passed (no HIP compiler or GPU execution performed)'
