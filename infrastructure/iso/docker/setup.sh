#!/usr/bin/env bash
# Runs only in the Docker build. No host package or trust changes.
set -euo pipefail
export LC_ALL=C
lock=/opt/arch-workstation-builder/versions.lock
snapshot=$(awk -F= '$1=="ARCH_SNAPSHOT" {print $2}' "$lock")
expected=$(awk -F= '$1=="ARCHISO_PACKAGE_VERSION" {print $2}' "$lock")
[[ $snapshot =~ ^[0-9]{4}/[0-9]{2}/[0-9]{2}$ && $expected =~ ^[0-9]+-[0-9]+$ ]]
[[ $(uname -m) == x86_64 && -f /etc/arch-release ]]
# Literal pacman variables, not shell variables.
# shellcheck disable=SC2016
printf 'Server = https://archive.archlinux.org/repos/%s/$repo/os/$arch\n' "$snapshot" >/etc/pacman.d/mirrorlist
pacman-key --init
pacman-key --populate archlinux
# Restore shared documentation directories omitted by the slim base image.
# Its NoExtract rules skip their contents, but pacman -Qkk checks these parent
# directories. Create them before installation and keep integrity failures fatal.
install -d -m0755 /usr/share/doc /usr/share/man
# A coherent upgrade against the snapshot, never a partial rolling upgrade.
# Include both split packages' dependencies so makepkg needs no network or sudo.
pacman -Syu --needed --noconfirm --disable-sandbox-syscalls base-devel archiso gnupg cryptsetup mdadm \
  xfsprogs gptfdisk pciutils jq sbctl sbsigntools efibootmgr curl iproute2 ripgrep python python-yaml \
  "openai-codex=$(awk -F= '$1=="CODEX_PACKAGE_VERSION" {print $2}' "$lock")"
codex_version=$(awk -F= '$1=="CODEX_PACKAGE_VERSION" {print $2}' "$lock")
codex_hash=$(awk -F= '$1=="CODEX_PACKAGE_SHA256" {print $2}' "$lock")
[[ $(pacman -Q openai-codex) == "openai-codex $codex_version" ]]
printf '%s  /var/cache/pacman/pkg/openai-codex-%s-x86_64.pkg.tar.zst\n' "$codex_hash" "$codex_version" | sha256sum --check --strict
[[ $(codex --version) == "codex-cli $(awk -F= '$1=="CODEX_CLI_VERSION" {print $2}' "$lock")" ]]
[[ $(pacman -Q archiso) == "archiso $expected" ]] || {
  printf 'ERROR: archiso %s is required\n' "$expected" >&2
  exit 1
}
pacman -Qkk archiso
useradd --uid 1000 --user-group --create-home --shell /bin/bash builder
install -d -m0755 -o1000 -g1000 /work
pacman -Q >/opt/arch-workstation-builder/packages.txt
# Only this image's package cache is cleared; never prune Docker or host state.
pacman -Scc --noconfirm
