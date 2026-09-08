#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

status=0
while IFS= read -r script; do
  if ! bash -n "$script"; then
    status=1
  fi
done < <({ rg --files -g '*.sh' -g 'bin/*' -g 'PKGBUILD' -g 'launch-*'; printf '%s\n' templates/arch/uki-sync templates/workstation/restic/workstation-restic; } | sort -u)

exit "$status"
