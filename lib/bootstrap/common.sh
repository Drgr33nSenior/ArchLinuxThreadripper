#!/usr/bin/env bash
# Shared primitives for the live-ISO bootstrap. This file intentionally has no
# destructive side effects; callers select a destructive action explicitly.

bootstrap_log() { printf '%s\n' "bootstrap-arch: $*" >&2; }
bootstrap_die() { bootstrap_log "ERROR: $*"; return 1; }
bootstrap_run() {
  if ((BOOTSTRAP_DRY_RUN)); then
    printf '+ ' >&2
    printf '%q ' "$@" >&2
    printf '\n' >&2
    return 0
  fi
  "$@"
}

bootstrap_require_root() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || { bootstrap_die 'run this action as root from the Arch ISO'; return 1; }
}

bootstrap_require_tty() {
  [[ -t 0 && -t 1 ]] || { bootstrap_die 'a controlling terminal is required for passphrase and serial confirmation'; return 1; }
}

bootstrap_require_arch_iso() {
  command -v pacstrap >/dev/null || { bootstrap_die 'pacstrap is missing; boot the official Arch installation ISO'; return 1; }
  command -v arch-chroot >/dev/null || { bootstrap_die 'arch-chroot is missing; boot the official Arch installation ISO'; return 1; }
  [[ -r /etc/arch-release ]] || { bootstrap_die 'this is not an Arch environment'; return 1; }
  [[ -d /run/archiso ]] || { bootstrap_die 'the destructive installer must run from an Arch live ISO'; return 1; }
}

bootstrap_require_uefi() {
  local sysfs_root=${BOOTSTRAP_SYSFS_ROOT:-/sys}
  [[ -d "$sysfs_root/firmware/efi/efivars" ]] || { bootstrap_die 'UEFI boot is required; CSM/legacy boot is not supported'; return 1; }
}

bootstrap_require_test_vm() {
  local virtualizer
  virtualizer=$(systemd-detect-virt --vm) || { bootstrap_die 'VM test profile requires QEMU'; return 1; }
  [[ $virtualizer == qemu || $virtualizer == kvm ]] \
    || { bootstrap_die 'VM test profile requires QEMU/KVM'; return 1; }
  [[ -r /sys/class/dmi/id/sys_vendor && $(</sys/class/dmi/id/sys_vendor) == QEMU ]] \
    || { bootstrap_die 'VM test profile requires the QEMU DMI identity'; return 1; }
}

bootstrap_validate_by_id_path() {
  local path=$1
  [[ $path == /dev/disk/by-id/* ]] || { bootstrap_die "disk must be an absolute /dev/disk/by-id path: $path"; return 1; }
  [[ ${path##*/} == nvme-* ]] || { bootstrap_die "disk must use a persistent NVMe by-id path: $path"; return 1; }
  [[ $path != *'..'* && $path != *$'\n'* ]] || { bootstrap_die "unsafe disk path: $path"; return 1; }
  [[ -L $path ]] || { bootstrap_die "disk by-id symlink does not exist: $path"; return 1; }
}

bootstrap_disk_realpath() { readlink -f -- "$1"; }

bootstrap_disk_serial() {
  local disk=$1 serial
  serial=$(udevadm info --query=property --name="$disk" 2>/dev/null | awk -F= '$1 == "ID_SERIAL_SHORT" { print substr($0, index($0,"=") + 1); exit }')
  if [[ -z $serial ]]; then
    serial=$(udevadm info --query=property --name="$disk" 2>/dev/null | awk -F= '$1 == "ID_SERIAL" { print substr($0, index($0,"=") + 1); exit }')
  fi
  [[ -n $serial ]] || { bootstrap_die "cannot determine serial for $disk"; return 1; }
  printf '%s\n' "$serial"
}

bootstrap_part_path() {
  local disk=$1 number=$2 inventory
  inventory=$(lsblk -nrpo NAME,PARTN "$disk") || { bootstrap_die "partition inspection failed: $disk"; return 1; }
  awk -v wanted="$number" '$2 == wanted { n++; path=$1 } END {if(n != 1) exit 1; print path}' <<< "$inventory"
}

bootstrap_target_path() { printf '%s%s\n' "$BOOTSTRAP_TARGET" "$1"; }

bootstrap_assert_safe_target() {
  local inventory
  [[ $BOOTSTRAP_TARGET == /mnt ]] || { bootstrap_die 'installation target is intentionally fixed at /mnt'; return 1; }
  inventory=$(findmnt -rn -o TARGET) || { bootstrap_die 'mount inventory failed; cannot prove /mnt is unused'; return 1; }
  [[ -n $inventory ]] || { bootstrap_die 'mount inventory is unexpectedly empty'; return 1; }
  if awk -v target="$BOOTSTRAP_TARGET" '$0 == target || index($0, target "/") == 1 { found=1 } END { exit !found }' <<< "$inventory"; then
    bootstrap_die '/mnt or a descendant is already mounted; refuse to overlay an existing target'
    return 1
  fi
}

bootstrap_render_template() {
  local template=$1 destination=$2
  sed \
    -e "s|@CRYPT_NAME@|$CRYPT_NAME|g" \
    -e "s|@LUKS_UUID@|$LUKS_UUID|g" \
    -e "s|@ROOT_UUID@|$ROOT_UUID|g" \
    -e "s|@MD_UUID@|$MD_UUID|g" \
    "$template" >"$destination"
}
