#!/usr/bin/env bash

bootstrap_verify_file() {
  local path=$1
  [[ -s "$BOOTSTRAP_TARGET$path" ]] || {
    bootstrap_die "required file is absent or empty: $path"
    return 1
  }
}

bootstrap_verify_uki_signature() {
  local uki=$1 cert=$2
  sbverify --cert "$cert" "$uki" >/dev/null ||
    {
      bootstrap_die "UKI is not signed by the configured Secure Boot db certificate: $uki"
      return 1
    }
}

bootstrap_verify_bootnum_for_label() {
  bootstrap_bootnum_for_label "$1"
}

bootstrap_verify_boot_order() {
  local labels=(
    'Arch Linux (stable)' 'Arch Linux (LTS)' 'Arch Linux (recovery)'
    'Arch Linux (stable backup)' 'Arch Linux (LTS backup)' 'Arch Linux (recovery backup)'
  ) label number expected='' actual inventory
  command -v efibootmgr >/dev/null 2>&1 || {
    bootstrap_die 'efibootmgr is required for UEFI BootOrder verification'
    return 1
  }
  for label in "${labels[@]}"; do
    number=$(bootstrap_verify_bootnum_for_label "$label") || return 1
    expected+="${expected:+,}$number"
  done
  inventory=$(efibootmgr -v) || {
    bootstrap_die 'firmware BootOrder inspection failed'
    return 1
  }
  actual=$(awk -F': ' '$1 == "BootOrder" {n++; value=toupper($2)} END {if(n != 1) exit 1; print value}' <<<"$inventory") || return 1
  [[ $actual == "$expected" || $actual == "$expected,"* ]] ||
    {
      bootstrap_die "UEFI BootOrder does not begin $expected"
      return 1
    }
}

bootstrap_verify() {
  local cert uki helper hook state
  BOOTSTRAP_TARGET=${BOOTSTRAP_TARGET:-/}
  cert="$BOOTSTRAP_TARGET/var/lib/sbctl/keys/db/db.pem"
  bootstrap_require_root || return 1
  bootstrap_require_uefi || return 1
  command -v sbverify >/dev/null 2>&1 || {
    bootstrap_die 'sbverify is required for Secure Boot verification'
    return 1
  }
  [[ -r $cert ]] || {
    bootstrap_die "configured Secure Boot db certificate is not readable: $cert"
    return 1
  }
  bootstrap_verify_file /etc/mkinitcpio.conf || return 1
  bootstrap_verify_file /etc/crypttab || return 1
  bootstrap_verify_file /etc/crypttab.initramfs || return 1
  bootstrap_verify_file /etc/mdadm.conf || return 1
  bootstrap_verify_file /etc/fstab || return 1
  common::validate_esp_pair "$BOOTSTRAP_TARGET" || return 1
  if [[ $BOOTSTRAP_TARGET == / ]]; then
    state=$(passwd -S root) || return 1
  else
    state=$(arch-chroot "$BOOTSTRAP_TARGET" passwd -S root) || return 1
  fi
  [[ $(awk '{print $2}' <<<"$state") == P ]] ||
    {
      bootstrap_die 'root emergency authentication is locked or has no password'
      return 1
    }
  helper=/usr/lib/arch-workstation-bootstrap/uki-sync
  hook=/usr/share/libalpm/hooks/zzz-bootstrap-uki-sync.hook
  if [[ -f $BOOTSTRAP_TARGET$helper && -e $BOOTSTRAP_TARGET/etc/pacman.d/hooks/zzz-bootstrap-uki-sync.hook ]]; then
    bootstrap_die 'an /etc hook overrides the packaged UKI hook; review the migration explicitly'
    return 1
  fi
  if [[ ! -f $BOOTSTRAP_TARGET$helper && -f $BOOTSTRAP_TARGET/usr/local/lib/bootstrap-arch/uki-sync ]]; then
    # Read-only verification of prior installations stays supported. Do not
    # delete or migrate their existing hook implicitly.
    helper=/usr/local/lib/bootstrap-arch/uki-sync
    hook=/etc/pacman.d/hooks/zzz-bootstrap-uki-sync.hook
    bootstrap_log 'legacy unowned UKI helper: plan an explicit package migration'
  fi
  bootstrap_verify_file "$helper" || return 1
  bootstrap_verify_file "$hook" || return 1
  [[ -x "$BOOTSTRAP_TARGET$helper" ]] ||
    {
      bootstrap_die 'UKI sync helper is not executable'
      return 1
    }
  grep -Fqx 'When = PostTransaction' "$BOOTSTRAP_TARGET$hook" ||
    {
      bootstrap_die 'UKI sync hook is not post-transaction'
      return 1
    }
  grep -Eq "^Exec = $helper( --sign)?$" "$BOOTSTRAP_TARGET$hook" ||
    {
      bootstrap_die 'UKI sync hook does not use the approved helper'
      return 1
    }
  bootstrap_verify_file /efi/EFI/Linux/arch-linux.efi || return 1
  bootstrap_verify_file /efi/EFI/Linux/arch-linux-lts.efi || return 1
  bootstrap_verify_file /efi2/EFI/Linux/arch-linux.efi || return 1
  bootstrap_verify_file /efi2/EFI/Linux/arch-linux-lts.efi || return 1
  bootstrap_verify_file /efi/EFI/Linux/arch-recovery.efi || return 1
  bootstrap_verify_file /efi2/EFI/Linux/arch-recovery.efi || return 1
  bootstrap_verify_file /efi/EFI/BOOT/BOOTX64.EFI || return 1
  bootstrap_verify_file /efi2/EFI/BOOT/BOOTX64.EFI || return 1
  grep -Fqx 'HOOKS=(base systemd autodetect microcode modconf kms keyboard sd-vconsole block mdadm_udev sd-encrypt filesystems fsck)' "$BOOTSTRAP_TARGET/etc/mkinitcpio.conf" ||
    {
      bootstrap_die 'mkinitcpio hook order differs from the supported storage path'
      return 1
    }
  grep -Fq 'x-initrd.attach' "$BOOTSTRAP_TARGET/etc/crypttab" || {
    bootstrap_die 'crypttab is missing x-initrd.attach'
    return 1
  }
  grep -Fq 'fido2-device=auto' "$BOOTSTRAP_TARGET/etc/crypttab" || {
    bootstrap_die 'crypttab is missing FIDO2 autodetection'
    return 1
  }
  cmp -s "$BOOTSTRAP_TARGET/etc/crypttab" "$BOOTSTRAP_TARGET/etc/crypttab.initramfs" ||
    {
      bootstrap_die 'runtime and initramfs crypttab policies differ'
      return 1
    }
  grep -Fq 'discard' "$BOOTSTRAP_TARGET/etc/crypttab.initramfs" || {
    bootstrap_die 'initramfs crypttab is missing the approved discard policy'
    return 1
  }
  cmp -s "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-linux.efi" "$BOOTSTRAP_TARGET/efi2/EFI/Linux/arch-linux.efi" ||
    {
      bootstrap_die 'stable UKI copies differ between ESPs'
      return 1
    }
  cmp -s "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-linux-lts.efi" "$BOOTSTRAP_TARGET/efi2/EFI/Linux/arch-linux-lts.efi" ||
    {
      bootstrap_die 'LTS UKI copies differ between ESPs'
      return 1
    }
  cmp -s "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-recovery.efi" "$BOOTSTRAP_TARGET/efi2/EFI/Linux/arch-recovery.efi" ||
    {
      bootstrap_die 'recovery UKI copies differ between ESPs'
      return 1
    }
  cmp -s "$BOOTSTRAP_TARGET/efi/EFI/BOOT/BOOTX64.EFI" "$BOOTSTRAP_TARGET/efi2/EFI/BOOT/BOOTX64.EFI" ||
    {
      bootstrap_die 'fallback UKI copies differ between ESPs'
      return 1
    }
  cmp -s "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-recovery.efi" "$BOOTSTRAP_TARGET/efi/EFI/BOOT/BOOTX64.EFI" ||
    {
      bootstrap_die 'fallback UKI is not the signed recovery UKI'
      return 1
    }
  for uki in \
    /efi/EFI/Linux/arch-linux.efi /efi2/EFI/Linux/arch-linux.efi \
    /efi/EFI/Linux/arch-linux-lts.efi /efi2/EFI/Linux/arch-linux-lts.efi \
    /efi/EFI/Linux/arch-recovery.efi /efi2/EFI/Linux/arch-recovery.efi \
    /efi/EFI/BOOT/BOOTX64.EFI /efi2/EFI/BOOT/BOOTX64.EFI; do
    bootstrap_verify_uki_signature "$BOOTSTRAP_TARGET$uki" "$cert" || return 1
  done
  bootstrap_verify_boot_order || return 1
  bootstrap_log 'post-install filesystem, dual-ESP identity, and db-certificate UKI signature checks passed'
}
