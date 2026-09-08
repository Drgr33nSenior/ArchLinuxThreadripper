#!/usr/bin/env bash
# The runtime package must be available and trusted before erasing any disk.
bootstrap_verify_boot_package() {
  local metadata name status entries
  [[ -f $BOOTSTRAP_BOOT_PACKAGE && ! -L $BOOTSTRAP_BOOT_PACKAGE && -f $BOOTSTRAP_BOOT_PACKAGE.sig && ! -L $BOOTSTRAP_BOOT_PACKAGE.sig ]] \
    || { bootstrap_die 'provide the signed arch-workstation-boot package with --boot-package'; return 1; }
  status=$(gpg --batch --homedir /etc/pacman.d/gnupg --status-fd 1 \
    --verify -- "$BOOTSTRAP_BOOT_PACKAGE.sig" "$BOOTSTRAP_BOOT_PACKAGE" 2>/dev/null) \
    || { bootstrap_die 'boot package signature is not trusted by the live keyring'; return 1; }
  awk '$2=="VALIDSIG" {valid=1} $2=="TRUST_FULLY" || $2=="TRUST_ULTIMATE" {trusted=1} END {exit !(valid && trusted)}' <<< "$status" \
    || { bootstrap_die 'runtime signer is not fully trusted; wait for live keyring initialization or review the key manually'; return 1; }
  metadata=$(bsdtar -xOf "$BOOTSTRAP_BOOT_PACKAGE" .PKGINFO) || return 1
  name=$(awk -F ' = ' '$1 == "pkgname" {print $2}' <<< "$metadata")
  [[ $name == arch-workstation-boot ]] || { bootstrap_die 'wrong runtime package'; return 1; }
  # This package is intentionally data-only: never accept a runtime scriptlet.
  entries=$(bsdtar -tf "$BOOTSTRAP_BOOT_PACKAGE") || return 1
  if grep -E '(^|/)\.INSTALL$' <<< "$entries" >/dev/null; then
    bootstrap_die 'unexpected runtime package install scriptlet'; return 1
  fi
  BOOTSTRAP_BOOT_PACKAGE_SHA256=$(common::sha256_file "$BOOTSTRAP_BOOT_PACKAGE") || return 1
  [[ $BOOTSTRAP_BOOT_PACKAGE_SHA256 =~ ^[a-f0-9]{64}$ ]] \
    || { bootstrap_die 'could not record a valid runtime package checksum'; return 1; }
}

bootstrap_install_boot_package() {
  [[ $(common::sha256_file "$BOOTSTRAP_BOOT_PACKAGE") == "$BOOTSTRAP_BOOT_PACKAGE_SHA256" ]] \
    || { bootstrap_die 'runtime package changed during installation'; return 1; }
  # Explicit live keyring; do not weaken LocalFileSigLevel or trust an unsigned file.
  bootstrap_verify_boot_package || return 1
  bootstrap_run pacman --root "$BOOTSTRAP_TARGET" --gpgdir /etc/pacman.d/gnupg \
    --noconfirm -U -- "$BOOTSTRAP_BOOT_PACKAGE" || return 1
}
