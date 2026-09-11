#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"
(($# == 1 || $# == 2)) || common::die 'usage: build.sh <prepared-directory> [--execute]'
[[ ${2:---dry-run} == --dry-run || ${2:-} == --execute ]] || common::die 'unsupported mode'
prepared=$(cd -- "$1" && pwd -P)
[[ -f $prepared/release.lock && -f $prepared/artifacts.lock && -f $prepared/profile/profiledef.sh ]] || common::die 'not a prepared profile'
[[ ! -e $prepared/work && ! -L $prepared/work && ! -e $prepared/out && ! -L $prepared/out ]] ||
  common::die 'build state exists; prepare a new directory, never clean a partial build automatically'
common::print_command bash "$root/infrastructure/iso/mkarchiso.sh" -v -w "$prepared/work" -o "$prepared/out" "$prepared/profile"
[[ ${2:-} == --execute ]] || exit 0
common::require_linux
common::require_root
[[ $(uname -m) == x86_64 && -f /etc/arch-release ]] || common::die 'an isolated x86_64 Arch builder is required'
[[ $(pacman -Q archiso) == "archiso $(common::lock_get "$prepared/release.lock" ARCHISO_PACKAGE_VERSION)" ]] || common::die 'archiso version changed'
pacman -Qkk archiso >/dev/null || common::die 'installed archiso files differ; inspect the builder'
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(common::lock_get "$prepared/release.lock" SOURCE_DATE_EPOCH)
bash "$root/infrastructure/iso/mkarchiso.sh" -v -w "$prepared/work" -o "$prepared/out" "$prepared/profile"
shopt -s nullglob
images=("$prepared/out/"*.iso)
[[ ${#images[@]} == 1 ]] || common::die 'expected exactly one ISO artifact'
printf '%s  %s\n' "$(common::sha256_file "${images[0]}")" "${images[0]##*/}" >"$prepared/out/SHA256SUMS"
common::info 'ISO built, not signed or hardware-qualified. Review and sign it separately.'
