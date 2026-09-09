#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C
fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}
lock_get() {
  awk -F= -v key="$2" '$1==key {n++; v=$2} END {if(n!=1 || v=="") exit 1; print v}' "$1"
}
builder=/opt/arch-workstation-builder
[[ $(uname -m) == x86_64 && -f /etc/arch-release ]] || fail 'amd64 Arch userspace is required'
[[ $(pacman -Q archiso) == "archiso $(lock_get "$builder/versions.lock" ARCHISO_PACKAGE_VERSION)" ]] ||
  fail 'builder Archiso version differs from the release lock'
case ${1:-check} in
  check)
    (($# <= 1)) || fail 'check accepts no arguments'
    printf 'Arch ISO builder: uid=%s architecture=%s\n' "$(id -u)" "$(uname -m)"
    pacman -Q archiso arch-install-scripts pacman gcc
    mkarchiso -h >/dev/null
    "$builder/bin/pacstrap" -h >/dev/null
    printf 'Userspace check passed; mount permissions and UEFI boot are NOT tested.\n'
    ;;
  packages)
    (($# == 1)) || fail 'packages accepts no arguments'
    [[ $(id -u) != 0 ]] || fail 'makepkg must not run as root'
    [[ ! -e /work/source && ! -e /work/output ]] || fail 'package work exists; select a new job'
    mkdir /work/source /work/output
    for file in bootstrap-source.tar.gz source.lock PKGBUILD; do
      [[ -f /input/$file && ! -L /input/$file ]] || fail "missing regular source input: $file"
      cp -- "/input/$file" /work/source/
    done
    cd /work/source
    export SOURCE_DATE_EPOCH PKGDEST=/work/output
    SOURCE_DATE_EPOCH=$(lock_get source.lock SOURCE_DATE_EPOCH)
    [[ $SOURCE_DATE_EPOCH =~ ^[0-9]{10}$ ]] || fail 'invalid source epoch'
    makepkg --cleanbuild --noconfirm
    shopt -s nullglob
    packages=(/work/output/*.pkg.tar.zst)
    ((${#packages[@]} == 4)) || fail 'expected four workstation split packages'
    for name in bootstrap boot backup bridge-runtime; do
      matching=(/work/output/arch-workstation-"$name"-*.pkg.tar.zst)
      ((${#matching[@]} == 1)) || fail "expected exactly one $name package"
    done
    # The operator signs this database and packages outside the builder. Backup
    # tooling is opt-in; ISO assembly consumes only the bootstrap and boot pair.
    repo-add /work/output/arch-workstation.db.tar.gz "${packages[@]}"
    cp "$builder/versions.lock" "$builder/packages.txt" source.lock PKGBUILD /work/output/
    printf 'BUILDER_IMAGE_ID=%s\n' "${BUILDER_IMAGE_ID:?}" >/work/output/builder.lock
    cd /work/output
    sha256sum ./*.pkg.tar.zst arch-workstation.db.tar.gz >SHA256SUMS
    printf 'Unsigned packages and repository database are ready for review and signing.\n'
    ;;
  bridge)
    (($# == 1)) || fail 'bridge accepts no arguments'
    [[ $(id -u) != 0 ]] || fail 'bundle as the unprivileged builder'
    bash /project/infrastructure/iso/bridge-bundle.sh /input /candidate /work/output
    ;;
  iso)
    (($# == 2)) || fail 'iso requires the reviewed primary public fingerprint'
    [[ $(id -u) == 0 && $2 =~ ^[A-F0-9]{40}$ ]] || fail 'ISO assembly requires container root and a full fingerprint'
    cmp "$builder/versions.lock" /project/infrastructure/iso/versions.lock ||
      fail 'image and project locks differ; rebuild the builder'
    [[ ! -e /work/prepared && ! -e /work/output ]] || fail 'ISO work exists; select a new job'
    # Fail before key setup/downloads if Docker or emulation rejects namespaces.
    unshare --mount --propagation private true ||
      fail 'mount namespace unavailable; do not disable Docker security controls automatically'
    # Check adapter compatibility before initializing trust or downloading.
    export PATH="$builder/bin:$PATH"
    pacstrap -h >/dev/null
    keyring=/run/arch-workstation/gnupg
    install -d -m0700 "$keyring"
    grep -qx -- '-----BEGIN PGP PUBLIC KEY BLOCK-----' /release/signing-key.asc ||
      fail 'an armored public key is required'
    if grep -q 'PRIVATE KEY' /release/signing-key.asc; then fail 'private signing material is forbidden'; fi
    pacman-key --gpgdir "$keyring" --init
    pacman-key --gpgdir "$keyring" --populate archlinux
    pacman-key --gpgdir "$keyring" --add /release/signing-key.asc
    pacman-key --gpgdir "$keyring" --lsign-key "$2"
    bash /project/infrastructure/iso/prepare.sh /release /release/signing-key.asc "$2" "$keyring" /work/prepared
    bash /project/infrastructure/iso/build.sh /work/prepared --execute
    mkdir /work/output
    cp /work/prepared/out/* /work/output/
    cp /work/prepared/release.lock /work/prepared/artifacts.lock /work/prepared/builder-packages.txt /work/output/
    printf 'BUILDER_IMAGE_ID=%s\n' "${BUILDER_IMAGE_ID:?}" >/work/output/builder.lock
    ;;
  *) fail 'supported actions: check, packages, iso' ;;
esac
