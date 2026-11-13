#!/usr/bin/env bash
# Move stack.lock to the current upstream heads and show what changed. Every
# component tracks main; the build image is the one TheRock's CI uses at the
# new TheRock commit, and Triton is the commit the new PyTorch pins. The
# game-streaming pins in the repository's versions.lock move too: the newest
# stable Sunshine release, the current archlinux:latest digest and the newest
# Arch archive snapshot. Review the diff, then run build-all.sh; unchanged
# stages are reused.
# Usage: refresh.sh SERVING_REPOSITORY_ROOT
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
lock=$here/stack.lock
[[ $# == 1 ]] || stop 'usage: refresh.sh SERVING_REPOSITORY_ROOT'
series=$1/serving/patches/sglang-main
get() { awk -F= -v key="$1" '$1 == key {print substr($0, index($0, "=") + 1)}' "$lock"; }
head_of() {
  local sha branch=${2:-main}
  sha=$(git ls-remote "$1" "refs/heads/$branch" | cut -f1)
  [[ $sha =~ ^[a-f0-9]{40}$ ]] || stop "cannot resolve $branch of $1"
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
new[SWARMUI_COMMIT]=$(head_of "$(get SWARMUI_REPOSITORY)" master)
new[COMFYUI_COMMIT]=$(head_of "$(get COMFYUI_REPOSITORY)" master)
new[OPEN_WEBUI_FORK_COMMIT]=$(head_of "$(get OPEN_WEBUI_FORK_REPOSITORY)" "$(get OPEN_WEBUI_FORK_BRANCH)")

# Resolve a docker.io tag to its current manifest-list digest (Docker Hub).
hub_image() {
  local tag=$1 repository reference token digest
  [[ $tag =~ ^docker\.io/([a-z0-9._/-]+):([A-Za-z0-9._-]+)$ ]] || stop "$tag must be a docker.io tag"
  repository=${BASH_REMATCH[1]} reference=${BASH_REMATCH[2]}
  token=$(curl -fsS --max-time 30 "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$repository:pull" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin)["token"])')
  digest=$(curl -fsSI --max-time 30 -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://registry-1.docker.io/v2/$repository/manifests/$reference" |
    awk 'tolower($1) == "docker-content-digest:" {gsub(/\r/, ""); print $2}')
  [[ $digest =~ ^sha256:[a-f0-9]{64}$ ]] || stop "cannot resolve $tag"
  printf 'docker.io/%s@%s\n' "$repository" "$digest"
}
new[RUNTIME_BASE_IMAGE]=$(hub_image "$(get RUNTIME_BASE_TAG)")

# Game streaming (the repository's versions.lock): the newest stable Sunshine
# release, its commit and the SHA-256 digests GitHub publishes for both assets.
declare -A game
release=$(curl -fsSL --max-time 30 https://api.github.com/repos/LizardByte/Sunshine/releases/latest)
read -r version arch_sha deb_sha < <(python3 -I -c '
import json, sys
release = json.load(sys.stdin)
version = release["tag_name"].removeprefix("v")
digests = {a["name"]: (a.get("digest") or "").removeprefix("sha256:") for a in release["assets"]}
print(version, digests.get(f"sunshine-{version}-1-x86_64.pkg.tar.zst", "-"),
      digests.get(f"sunshine_{version}-1+debiantrixie_amd64.deb", "-"))' <<<"$release")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $arch_sha =~ ^[a-f0-9]{64}$ && $deb_sha =~ ^[a-f0-9]{64}$ ]] ||
  stop 'cannot resolve the newest stable Sunshine release and its asset digests'
game[SUNSHINE_VERSION]=$version
# An annotated tag's peeled (^{}) line names the commit; a lightweight tag has only one line.
game[SUNSHINE_COMMIT]=$(git ls-remote https://github.com/LizardByte/Sunshine.git "refs/tags/v$version" "refs/tags/v$version^{}" |
  sort -k2 -r | head -1 | cut -f1)
[[ ${game[SUNSHINE_COMMIT]} =~ ^[a-f0-9]{40}$ ]] || stop "cannot resolve Sunshine v$version"
game[SUNSHINE_ARCH_SHA256]=$arch_sha
game[SUNSHINE_DEBIAN_SHA256]=$deb_sha
game[SUNSHINE_ARCH_BASE_IMAGE]=$(hub_image docker.io/library/archlinux:latest)
# The newest dated Arch archive snapshot already published (today or yesterday, UTC).
for day in 0 1; do
  snapshot=$(date -u -d "-$day day" +%Y/%m/%d 2>/dev/null || date -u -v-"$day"d +%Y/%m/%d)
  if curl -fsI --max-time 30 "https://archive.archlinux.org/repos/$snapshot/core/os/x86_64/core.db" >/dev/null; then
    game[SUNSHINE_ARCH_SNAPSHOT]=$snapshot
    break
  fi
done
[[ -n ${game[SUNSHINE_ARCH_SNAPSHOT]:-} ]] || stop 'no Arch archive snapshot for today or yesterday'

versions=$here/../../versions.lock
[[ -f $versions ]] || stop "run refresh.sh from a full repository checkout ($versions is missing)"
work=$(mktemp -d "$lock.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
for key in "${!new[@]}"; do printf '%s=%s\n' "$key" "${new[$key]}"; done >"$work/stack.pairs"
for key in "${!game[@]}"; do printf '%s=%s\n' "$key" "${game[$key]}"; done >"$work/versions.pairs"
# Usage: replace_keys PAIRS LOCK OUTPUT; every key in PAIRS must already exist in LOCK.
replace_keys() {
  awk -v date="$(date -u +%Y-%m-%d)" '
    FNR == NR { j = index($0, "="); value[substr($0, 1, j - 1)] = substr($0, j + 1); next }
    /^# Refreshed [0-9-]+\.$/ { print "# Refreshed " date "."; next }
    { j = index($0, "="); key = substr($0, 1, j - 1) }
    j > 0 && key in value { print key "=" value[key]; seen[key] = 1; next }
    { print }
    END { for (k in value) if (!(k in seen)) { print "missing key " k > "/dev/stderr"; exit 1 } }
  ' "$1" "$2" >"$3"
}
replace_keys "$work/stack.pairs" "$lock" "$work/stack.lock"
replace_keys "$work/versions.pairs" "$versions" "$work/versions.lock"
changed=0
for name in stack versions; do
  target=$lock
  [[ $name == stack ]] || target=$versions
  if ! diff -u "$target" "$work/$name.lock"; then
    cat -- "$work/$name.lock" >"$target"
    changed=1
  fi
done
if ((changed)); then
  echo 'locks updated; review the diff above, commit it, then sync.sh and build-all.sh'
else
  echo 'stack.lock and versions.lock are already at the current upstream releases'
fi
