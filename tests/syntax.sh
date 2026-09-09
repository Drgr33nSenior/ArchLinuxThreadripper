#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

command -v rg >/dev/null 2>&1 || {
  printf 'FAIL: rg is required for syntax discovery\n' >&2
  exit 1
}
scripts=$(rg --files -g '*.sh' -g 'bin/*' -g 'PKGBUILD' -g 'launch-*') || {
  printf 'FAIL: syntax discovery failed\n' >&2
  exit 1
}
[[ -n $scripts ]] || {
  printf 'FAIL: syntax discovery returned no files\n' >&2
  exit 1
}
scripts=$(printf '%s\n' "$scripts" templates/arch/uki-sync templates/workstation/restic/workstation-restic | sort -u)
status=0
while IFS= read -r script; do
  if ! bash -n "$script"; then
    status=1
  fi
done <<<"$scripts"

exit "$status"
