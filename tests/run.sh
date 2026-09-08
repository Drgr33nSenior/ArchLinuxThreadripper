#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

test_count=0
while IFS= read -r test_file; do
  printf 'TEST: %s\n' "$test_file"
  bash "$test_file"
  test_count=$((test_count + 1))
done < <(rg --files tests -g 'test_*.sh' | sort)

printf 'PASS: %d test file(s)\n' "$test_count"

