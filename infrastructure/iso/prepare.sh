#!/usr/bin/env bash
# Stage a new Archiso profile from verified local packages. Never build, install
# packages, mutate the host keyring, contact a cluster or touch block devices.
set -euo pipefail
export LC_ALL=C
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"
(($# == 5)) || common::die 'usage: prepare.sh <signed-package-dir> <public-key.asc> <primary-fingerprint> <builder-keyring-dir> <new-output-dir>'
common::require_linux
[[ $(uname -m) == x86_64 && -f /etc/arch-release ]] || common::die 'prepare on an isolated x86_64 Arch builder'
lock="$root/infrastructure/iso/versions.lock"
expected=$(common::lock_get "$lock" ARCHISO_PACKAGE_VERSION)
[[ $(pacman -Q archiso) == "archiso $expected" ]] || common::die "archiso $expected is required"
pacman -Qkk archiso >/dev/null || common::die 'installed archiso files differ; inspect the builder'
packages=$(cd -- "$1" && pwd -P)
public_key=$2 fingerprint=$3
keyring=$(cd -- "$4" && pwd -P)
[[ $fingerprint =~ ^[A-F0-9]{40}$ ]] || common::die 'supply the full uppercase primary signing fingerprint'
[[ -f $public_key && ! -L $public_key ]] || common::die 'public key must be a regular file'
grep -q '^-----BEGIN PGP PUBLIC KEY BLOCK-----$' "$public_key" || common::die 'only an armored public key is accepted'
if grep -q 'PRIVATE KEY' "$public_key"; then common::die 'private material is forbidden'; fi
[[ ! -e $5 && ! -L $5 ]] || common::die 'output exists; choose a fresh profile directory'
mkdir -- "$5"
output=$(cd -- "$5" && pwd -P)
# Paths enter pacman configuration, not a shell expression. Keep them unambiguous.
for path in "$output" "$keyring"; do
  [[ $path =~ ^/[A-Za-z0-9_./-]+$ ]] || common::die 'build paths cannot contain whitespace or config delimiters'
done
mkdir -m700 "$output/verification"
gpg --batch --homedir "$output/verification" --import "$public_key" >/dev/null 2>&1
actual=$(gpg --batch --homedir "$output/verification" --with-colons --list-keys | awk -F: '$1=="fpr" {print $10; exit}')
[[ $actual == "$fingerprint" ]] || common::die 'public key fingerprint mismatch'
verify() {
  local status
  [[ -f $1 && ! -L $1 && -f $1.sig && ! -L $1.sig ]] || common::die "missing regular signed artifact: $1"
  status=$(gpg --batch --homedir "$output/verification" --status-fd 1 --verify -- "$1.sig" "$1" 2>/dev/null) \
    || common::die "signature failed: $1"
  awk -v f="$fingerprint" '$2=="VALIDSIG" && ($3==f || $NF==f) {ok=1} END {exit !ok}' <<< "$status" \
    || common::die 'artifact signer differs from the reviewed primary key'
}
shopt -s nullglob
bootstrap=("$packages"/arch-workstation-bootstrap-*.pkg.tar.zst)
boot=("$packages"/arch-workstation-boot-*.pkg.tar.zst)
[[ ${#bootstrap[@]} == 1 && ${#boot[@]} == 1 ]] || common::die 'supply exactly one package of each split-package name'
verify "${bootstrap[0]}"
verify "${boot[0]}"
verify "$packages/arch-workstation.db.tar.gz"
package_field() {
  bsdtar -xOf "$1" .PKGINFO | awk -F ' = ' -v key="$2" '$1==key {n++; value=$2} END {if(n!=1) exit 1; print value}'
}
[[ $(package_field "${bootstrap[0]}" pkgname) == arch-workstation-bootstrap \
  && $(package_field "${boot[0]}" pkgname) == arch-workstation-boot ]] || common::die 'package metadata name mismatch'
bootstrap_version=$(package_field "${bootstrap[0]}" pkgver) || common::die 'invalid installer package version metadata'
boot_version=$(package_field "${boot[0]}" pkgver) || common::die 'invalid runtime package version metadata'
[[ -n $bootstrap_version && $bootstrap_version == "$boot_version" ]] || common::die 'split-package versions differ'
cp -R /usr/share/archiso/configs/releng "$output/profile"
profile="$output/profile"
cp "$root/infrastructure/iso/profile/profiledef.sh" "$profile/profiledef.sh"
cp "$root/infrastructure/iso/profile/zlogin" "$profile/airootfs/root/.zlogin"
printf 'AUTO -all\n' > "$profile/airootfs/etc/mdadm.conf"
install -Dm644 "$root/templates/arch/no-hibernation.conf" "$profile/airootfs/etc/systemd/sleep.conf.d/10-no-hibernation.conf"
# These are exact files in our newly created staging tree, never the installed
# profile. Remove releng's automatic remote-script and mirror-change entry points.
rm -- "$profile/airootfs/root/.automated_script.sh"
rm -- "$profile/airootfs/etc/systemd/system/multi-user.target.wants/sshd.service"
rm -- "$profile/airootfs/etc/systemd/system/multi-user.target.wants/choose-mirror.service"
for unit in sshd.service choose-mirror.service cloud-init.target cloud-init-local.service cloud-init-main.service cloud-init-network.service cloud-config.service cloud-final.service; do
  ln -sf /dev/null "$profile/airootfs/etc/systemd/system/$unit"
done
{ sed '/^archinstall$/d; /^cloud-init$/d' "$profile/packages.x86_64"; sed '/^#/d; /^$/d' "$root/infrastructure/iso/profile/packages.x86_64"; } | sort -u > "$output/packages.x86_64"
cp "$output/packages.x86_64" "$profile/packages.x86_64"
live_repo=/var/cache/arch-workstation/repo
mkdir -p "$profile/local-repo" "$profile/airootfs$live_repo" \
  "$profile/airootfs/var/cache/arch-workstation" "$profile/airootfs/etc/arch-workstation-iso"
for artifact in "${bootstrap[0]}" "${boot[0]}" "$packages/arch-workstation.db.tar.gz"; do
  cp "$artifact" "$artifact.sig" "$profile/local-repo/"
done
cp "$profile/local-repo/arch-workstation.db.tar.gz" "$profile/local-repo/arch-workstation.db"
cp "$profile/local-repo/arch-workstation.db.tar.gz.sig" "$profile/local-repo/arch-workstation.db.sig"
cp "$profile/local-repo/"* "$profile/airootfs$live_repo/"
cp "${boot[0]}" "$profile/airootfs/var/cache/arch-workstation/boot.pkg.tar.zst"
cp "${boot[0]}.sig" "$profile/airootfs/var/cache/arch-workstation/boot.pkg.tar.zst.sig"
# Public seed only; no private signing key or builder keyring enters the ISO.
gpg --batch --homedir "$output/verification" --armor --export "$fingerprint" > "$profile/airootfs/etc/arch-workstation-iso/signing-key.asc"
sed "s/@FINGERPRINT@/$fingerprint/g" "$root/infrastructure/iso/profile/arch-workstation-keyring.service.in" \
  > "$profile/airootfs/etc/systemd/system/arch-workstation-keyring.service"
ln -s ../arch-workstation-keyring.service "$profile/airootfs/etc/systemd/system/multi-user.target.wants/arch-workstation-keyring.service"
snapshot=$(common::lock_get "$lock" ARCH_SNAPSHOT)
sed -e "s|@SNAPSHOT@|$snapshot|g" -e "s|@KEYRING@|$keyring|g" -e "s|@REPO@|$profile/local-repo|g" \
  "$root/infrastructure/iso/profile/pacman.conf.in" > "$profile/pacman.conf"
sed -e "s|@SNAPSHOT@|$snapshot|g" -e 's|@KEYRING@|/etc/pacman.d/gnupg|g' -e "s|@REPO@|$live_repo|g" \
  "$root/infrastructure/iso/profile/pacman.conf.in" > "$profile/airootfs/etc/pacman.conf"
# Pacman, not this shell, expands $repo and $arch.
# shellcheck disable=SC2016
printf 'Server = https://archive.archlinux.org/repos/%s/$repo/os/$arch\n' "$snapshot" > "$profile/airootfs/etc/pacman.d/mirrorlist"
cp "$lock" "$profile/airootfs/etc/arch-workstation-iso/release.lock"
cp "$lock" "$output/release.lock"
pacman -Q > "$output/builder-packages.txt"
printf 'SIGNING_FINGERPRINT=%s\nBOOTSTRAP_SHA256=%s\nBOOT_SHA256=%s\n' "$fingerprint" \
  "$(common::sha256_file "${bootstrap[0]}")" "$(common::sha256_file "${boot[0]}")" > "$output/artifacts.lock"
common::info "Prepared $profile; ISO build, signing and VM boot have NOT run."
