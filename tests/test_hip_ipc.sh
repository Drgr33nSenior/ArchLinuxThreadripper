#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if ! command -v c++ >/dev/null; then printf 'SKIP: IPC API simulation needs a C++ compiler\n'; exit 0; fi
work=$(mktemp -d)
trap 'rm -f -- "$work/ipc" "$work/result.txt"; rmdir -- "$work"' EXIT
c++ -std=c++17 -Wall -Wextra -Werror -I "$root/tests/fixtures/hip-ipc" "$root/tests/hardware/hip-ipc.cpp" -o "$work/ipc"
"$work/ipc" > "$work/result.txt"
[[ $(grep -c 'correctness=passed' "$work/result.txt") == 2 ]]
for failure in MOCK_IPC_EXPORT_FAIL MOCK_IPC_IMPORT_FAIL MOCK_IPC_CORRUPT; do
  if env "$failure=1" "$work/ipc" >/dev/null 2>&1; then
    printf 'IPC fixture accepted %s\n' "$failure" >&2
    exit 1
  fi
done
printf 'HIP IPC fork/exec, error and corruption simulation passed; real HIP IPC NOT RUN\n'
