#!/usr/bin/env bash
# Copy this pipeline and the pinned SGLang patch series to a build host.
# The pipeline lands in a content-addressed directory, so a build that is
# already running keeps executing the exact scripts it started with.
# Usage: sync.sh SSH_DESTINATION SERVING_REPOSITORY_ROOT
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
[[ $# == 2 ]] || stop 'usage: sync.sh SSH_DESTINATION SERVING_REPOSITORY_ROOT'
destination=$1
series=$2/serving/patches/sglang-main
[[ -d $series ]] || stop "no patch series at $series"
pinned=$(awk -F= '$1 == "SGLANG_PATCHSET_SHA256" {print $2}' "$here/stack.lock")
actual=$(bash "$here/patchset-hash.sh" "$series")
[[ $actual == "$pinned" ]] || stop "patch series hash $actual does not match stack.lock $pinned"

# Hash the pipeline files themselves (names and contents) for the directory name.
pipeline=$(cd "$here" && find . -type f ! -name '._*' -print | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256 | cut -c1-12)
[[ $pipeline =~ ^[a-f0-9]{12}$ && $pinned =~ ^[a-f0-9]{64}$ ]] || stop 'could not compute content hashes'
# Extra ssh options (for example a host-key alias) may come from AI_STACK_SSH_OPTIONS.
read -r -a ssh_opts <<<"-o BatchMode=yes ${AI_STACK_SSH_OPTIONS:-}"
export COPYFILE_DISABLE=1
# The remote commands embed only the two validated hex hashes above.
# shellcheck disable=SC2029
tar --no-xattrs -C "$here" -cf - --exclude '._*' . |
  ssh "${ssh_opts[@]}" "$destination" "set -e; d=\$HOME/ai-stack/pipeline/$pipeline; if [ ! -d \"\$d\" ]; then rm -rf \"\$d.tmp\" && mkdir -p \"\$d.tmp\" && tar -xf - -C \"\$d.tmp\" && mv \"\$d.tmp\" \"\$d\"; else cat >/dev/null; fi; ln -sfn \"\$d\" \$HOME/ai-stack/pipeline/current"
# shellcheck disable=SC2029
tar --no-xattrs -C "$series" -cf - --exclude '._*' . |
  ssh "${ssh_opts[@]}" "$destination" "set -e; d=\$HOME/ai-stack/patches/$pinned; if [ ! -d \"\$d\" ]; then rm -rf \"\$d.tmp\" && mkdir -p \"\$d.tmp\" && tar -xf - -C \"\$d.tmp\" && mv \"\$d.tmp\" \"\$d\"; else cat >/dev/null; fi; [ \"\$(bash \$HOME/ai-stack/pipeline/$pipeline/patchset-hash.sh \"\$d\")\" = $pinned ]"
printf 'PIPELINE=~/ai-stack/pipeline/%s\nPATCHES=~/ai-stack/patches/%s\n' "$pipeline" "$pinned"
