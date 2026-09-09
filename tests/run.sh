#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

command -v rg >/dev/null 2>&1 || {
  printf 'FAIL: rg is required for test discovery\n' >&2
  exit 1
}
test_files=$(rg --files tests -g 'test_*.sh' | sort) || {
  printf 'FAIL: test discovery failed\n' >&2
  exit 1
}
[[ -n $test_files ]] || {
  printf 'FAIL: test discovery returned no files\n' >&2
  exit 1
}
test_count=0
while IFS= read -r test_file; do
  printf 'TEST: %s\n' "$test_file"
  bash "$test_file"
  test_count=$((test_count + 1))
done <<<"$test_files"

printf 'PASS: %d test file(s)\n' "$test_count"
