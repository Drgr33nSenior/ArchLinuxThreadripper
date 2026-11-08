#!/usr/bin/env bash
# Print the content hash of a patch series: sha256 over "sha256  name" lines
# of every *.patch in name order. stack.lock pins this value.
set -euo pipefail
dir=${1:?usage: patchset-hash.sh DIR}
cd -- "$dir"
shopt -s nullglob
patches=(*.patch)
((${#patches[@]})) || {
  echo 'STOP: no patches in series' >&2
  exit 1
}
sha256sum -- "${patches[@]}" | sha256sum | cut -d' ' -f1
