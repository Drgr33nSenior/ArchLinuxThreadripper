#!/usr/bin/env bash
# Move stack.lock to the current upstream heads and show what changed. Every
# component tracks main; the build image is the one TheRock's CI uses at the
# new TheRock commit, and Triton is the commit the new PyTorch pins. Review the
# diff, then run build-all.sh; unchanged stages are reused.
# Usage: refresh.sh SERVING_REPOSITORY_ROOT
set -euo pipefail
stop() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
lock=$here/stack.lock
[[ $# == 1 ]] || stop 'usage: refresh.sh SERVING_REPOSITORY_ROOT'
series=$1/serving/patches/sglang-main
get() { awk -F= -v key="$1" '$1 == key {print substr($0, index($0, "=") + 1)}' "$lock"; }
head_of() {
  local sha
  sha=$(git ls-remote "$1" refs/heads/main | cut -f1)
  [[ $sha =~ ^[a-f0-9]{40}$ ]] || stop "cannot resolve main of $1"
  printf '%s\n' "$sha"
}
raw() { curl -fsSL --max-time 60 "https://raw.githubusercontent.com/$1/$2/$3"; }
slug() { sed -E 's#^https://github\.com/([^/]+/[^/]+)\.git$#\1#' <<<"$1"; }

declare -A new
new[THEROCK_COMMIT]=$(head_of "$(get THEROCK_REPOSITORY)")
new[THEROCK_BUILD_IMAGE]=$(raw "$(slug "$(get THEROCK_REPOSITORY)")" "${new[THEROCK_COMMIT]}" \
  .github/workflows/multi_arch_build_portable_linux_artifacts.yml |
  grep -o -E 'ghcr\.io/rocm/therock_build_manylinux_x86_64@sha256:[a-f0-9]{64}' | sort -u)
[[ $(grep -c . <<<"${new[THEROCK_BUILD_IMAGE]}") == 1 ]] || stop 'TheRock CI does not name exactly one build image'
new[PYTORCH_COMMIT]=$(head_of "$(get PYTORCH_REPOSITORY)")
new[TORCHVISION_COMMIT]=$(head_of "$(get TORCHVISION_REPOSITORY)")
new[TRITON_COMMIT]=$(raw "$(slug "$(get PYTORCH_REPOSITORY)")" "${new[PYTORCH_COMMIT]}" .ci/docker/ci_commit_pins/triton.txt | tr -d '[:space:]')
[[ ${new[TRITON_COMMIT]} =~ ^[a-f0-9]{40}$ ]] || stop 'PyTorch does not pin a Triton commit'
new[SGLANG_COMMIT]=$(head_of "$(get SGLANG_REPOSITORY)")
new[SGLANG_PATCHSET_SHA256]=$(bash "$here/patchset-hash.sh" "$series")

# Resolve the runtime base tag to its current manifest-list digest (Docker Hub).
tag=$(get RUNTIME_BASE_TAG)
[[ $tag =~ ^docker\.io/([a-z0-9._/-]+):([A-Za-z0-9._-]+)$ ]] || stop 'RUNTIME_BASE_TAG must be a docker.io tag'
repository=${BASH_REMATCH[1]} reference=${BASH_REMATCH[2]}
token=$(curl -fsS --max-time 30 "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repository:pull" |
  python3 -I -c 'import json,sys; print(json.load(sys.stdin)["token"])')
digest=$(curl -fsSI --max-time 30 -H "Authorization: Bearer $token" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
  "https://registry-1.docker.io/v2/$repository/manifests/$reference" |
  awk 'tolower($1) == "docker-content-digest:" {gsub(/\r/, ""); print $2}')
[[ $digest =~ ^sha256:[a-f0-9]{64}$ ]] || stop "cannot resolve $tag"
new[RUNTIME_BASE_IMAGE]=docker.io/$repository@$digest

staged=$(mktemp "$lock.XXXXXX")
pairs=$(mktemp "$lock.XXXXXX")
trap 'rm -f -- "$staged" "$pairs"' EXIT
for key in "${!new[@]}"; do printf '%s=%s\n' "$key" "${new[$key]}"; done >"$pairs"
awk -v date="$(date -u +%Y-%m-%d)" '
  FNR == NR { j = index($0, "="); value[substr($0, 1, j - 1)] = substr($0, j + 1); next }
  /^# Refreshed [0-9-]+\.$/ { print "# Refreshed " date "."; next }
  { j = index($0, "="); key = substr($0, 1, j - 1) }
  j > 0 && key in value { print key "=" value[key]; seen[key] = 1; next }
  { print }
  END { for (k in value) if (!(k in seen)) { print "missing key " k > "/dev/stderr"; exit 1 } }
' "$pairs" "$lock" >"$staged"
if diff -u "$lock" "$staged"; then
  echo 'stack.lock is already at the current upstream heads'
else
  cat -- "$staged" >"$lock"
  echo 'stack.lock updated; review the diff above, commit it, then sync.sh and build-all.sh'
fi
