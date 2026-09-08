#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/common.sh"
source "$repo_root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/TheRock/.git"
locked_commit=$(ws_read_lock ROCM_THEROCK_COMMIT)

# Model Git's read-only responses. This checks policy/manifest construction,
# not actual TheRock source authenticity or local signature verification.
git() {
  [[ $1 == -C && $2 == "$work/TheRock" ]] || return 1
  shift 2
  case "$1 ${2:-}" in
    'rev-parse HEAD') printf '%s\n' "$locked_commit" ;;
    'remote get-url') printf '%s\n' "${source_origin:-https://github.com/ROCm/TheRock.git}" ;;
    'status --porcelain=v1') [[ ${source_case:-} != dirty-root ]] || printf ' M README.md\n' ;;
    'submodule status')
      if [[ ${source_case:-} == missing ]]; then printf '%s\n' '-1111111111111111111111111111111111111111 compiler';
      else printf ' 1111111111111111111111111111111111111111 compiler\n'; fi ;;
    'submodule foreach')
      [[ ${source_case:-} != dirty-submodule ]] || return 1
      [[ $* == *--ignore-submodules=all* ]] || return 1
      printf 'compiler\thttps://example.invalid/compiler.git\t%s\t%s\t%s\n' \
        1111111111111111111111111111111111111111 2222222222222222222222222222222222222222 3333333333333333333333333333333333333333 ;;
    'verify-commit '*) return 1 ;;
    *) return 1 ;;
  esac
}
ws_rocm_source_manifest "$work/TheRock" "$work/sources.json"
jq -e --arg commit "$locked_commit" '
  .source.commit==$commit and .source.signature=="unverified" and .requires_review and
  .status=="source-inventory-not-build-proof" and .submodules[0].patched and (.submodules[0].tree|length)==40
' "$work/sources.json" >/dev/null
for source_case in dirty-root missing dirty-submodule; do
  if (ws_rocm_source_manifest "$work/TheRock" "$work/$source_case.json") >/dev/null 2>&1; then
    echo "unsafe source inventory accepted: $source_case" >&2; exit 1
  fi
  [[ ! -e $work/$source_case.json ]]
done
source_case=''
source_origin=https://example.invalid/unreviewed.git
if (ws_rocm_source_manifest "$work/TheRock" "$work/wrong-origin.json") >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/wrong-origin.json ]]
echo 'ROCm source inventory policy tests passed (synthetic Git responses)'
