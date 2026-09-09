#!/usr/bin/env bash

bootstrap_require_install_commands() {
  local command_name
  for command_name in \
    arch-chroot blkid blockdev bsdtar cryptsetup efibootmgr findmnt genfstab gpg install \
    lsblk lspci jq mdadm mkfs.fat mkfs.xfs mount pacman pacstrap partprobe readlink sgdisk \
    udevadm wipefs; do
    command -v "$command_name" >/dev/null 2>&1 ||
      {
        bootstrap_die "required live-ISO command is unavailable: $command_name"
        return 1
      }
  done
}

bootstrap_assert_disk_unused() {
  local real=$1 mounts types
  mounts=$(lsblk -nrpo MOUNTPOINT "$real") || {
    bootstrap_die "disk mount inspection failed: $real"
    return 1
  }
  if awk 'NF { found=1 } END { exit !found }' <<<"$mounts"; then
    bootstrap_die "disk or a child partition is mounted: $real"
    return 1
  fi
  types=$(lsblk -nrpo TYPE "$real") || {
    bootstrap_die "disk holder inspection failed: $real"
    return 1
  }
  [[ -n $types ]] || {
    bootstrap_die "empty disk holder inspection: $real"
    return 1
  }
  if awk '$1 ~ /^(crypt|lvm|raid[0-9]*)$/ { found=1 } END { exit !found }' <<<"$types"; then
    bootstrap_die "disk participates in an active storage stack: $real"
    return 1
  fi
}

bootstrap_check_disk() {
  local by_id=$1 real size kind
  bootstrap_validate_by_id_path "$by_id" || return 1
  real=$(bootstrap_disk_realpath "$by_id") || return 1
  [[ -b $real ]] || {
    bootstrap_die "by-id path does not resolve to a block device: $by_id"
    return 1
  }
  kind=$(lsblk -dnro TYPE "$real") || {
    bootstrap_die "disk inspection failed: $real"
    return 1
  }
  [[ $kind == disk ]] || {
    bootstrap_die "not a whole disk: $by_id -> $real"
    return 1
  }
  size=$(blockdev --getsize64 "$real") || return 1
  [[ $size =~ ^[1-9][0-9]*$ ]] || {
    bootstrap_die "invalid disk size: $real"
    return 1
  }
  ((size >= 68719476736)) || {
    bootstrap_die "disk is smaller than the 64 GiB safety minimum: $real"
    return 1
  }
  bootstrap_assert_disk_unused "$real" || return 1
  BOOTSTRAP_DISK_REAL=$real
  BOOTSTRAP_DISK_SIZE=$size
}

bootstrap_preflight() {
  bootstrap_require_root || return 1
  bootstrap_require_arch_iso || return 1
  bootstrap_require_uefi || return 1
  bootstrap_require_install_commands || return 1
  bootstrap_select_host_profile || return 1
  bootstrap_select_gpu_packages || return 1
  bootstrap_no_active_swap || return 1
  [[ ! -e $RAID_DEVICE && ! -e /dev/mapper/$CRYPT_NAME ]] ||
    {
      bootstrap_die 'the requested md or dm-crypt mapping already exists; refuse installation'
      return 1
    }
  [[ -e /usr/share/zoneinfo/$TIMEZONE ]] || {
    bootstrap_die "unknown timezone: $TIMEZONE"
    return 1
  }
  bootstrap_check_disk "$BOOTSTRAP_DISK_A" || return 1
  local disk_a=$BOOTSTRAP_DISK_REAL size_a=$BOOTSTRAP_DISK_SIZE
  bootstrap_check_disk "$BOOTSTRAP_DISK_B" || return 1
  local disk_b=$BOOTSTRAP_DISK_REAL size_b=$BOOTSTRAP_DISK_SIZE
  [[ $disk_a != "$disk_b" ]] || {
    bootstrap_die 'the two by-id paths resolve to the same disk'
    return 1
  }
  [[ $size_a == "$size_b" ]] || {
    bootstrap_die "RAID0 members differ in size: $size_a != $size_b"
    return 1
  }
  BOOTSTRAP_DISK_A_REAL=$disk_a
  BOOTSTRAP_DISK_B_REAL=$disk_b
  # shellcheck disable=SC2034
  : "$BOOTSTRAP_DISK_A_REAL" "$BOOTSTRAP_DISK_B_REAL"
  BOOTSTRAP_DISK_A_SERIAL=$(bootstrap_disk_serial "$disk_a") || return 1
  BOOTSTRAP_DISK_B_SERIAL=$(bootstrap_disk_serial "$disk_b") || return 1
  [[ $BOOTSTRAP_DISK_A_SERIAL == "$PRIMARY_DISK_SERIAL" ]] ||
    {
      bootstrap_die "PRIMARY_DISK_SERIAL does not match $disk_a"
      return 1
    }
  [[ $BOOTSTRAP_DISK_B_SERIAL == "$SECONDARY_DISK_SERIAL" ]] ||
    {
      bootstrap_die "SECONDARY_DISK_SERIAL does not match $disk_b"
      return 1
    }
  if [[ ${VM_TEST_MODE:-false} == true ]]; then
    bootstrap_require_test_vm || return 1
    [[ $BOOTSTRAP_DISK_A_SERIAL == ARCHLAB_TEST_A && $BOOTSTRAP_DISK_B_SERIAL == ARCHLAB_TEST_B &&
      $(lsblk -dnro MODEL "$disk_a") == 'QEMU NVMe Ctrl' &&
      $(lsblk -dnro MODEL "$disk_b") == 'QEMU NVMe Ctrl' ]] ||
      {
        bootstrap_die 'VM tests require the two dedicated QEMU NVMe fixtures, never passed-through disks'
        return 1
      }
  fi
  bootstrap_network_mirrors || return 1
  bootstrap_log "preflight passed: $disk_a ($BOOTSTRAP_DISK_A_SERIAL), $disk_b ($BOOTSTRAP_DISK_B_SERIAL)"
}

bootstrap_no_active_swap() {
  [[ -r /proc/swaps ]] || {
    bootstrap_die 'cannot inspect live-ISO swap state'
    return 1
  }
  [[ $(awk 'NR > 1 {n++} END {print n+0}' /proc/swaps) == 0 ]] ||
    {
      bootstrap_die 'active swap detected; resolve it manually before installation'
      return 1
    }
}

bootstrap_confirm_destruction() {
  local expected response
  ((BOOTSTRAP_DRY_RUN)) && return 0
  bootstrap_require_tty || return 1
  expected="ERASE ${BOOTSTRAP_DISK_A_REAL} (${PRIMARY_DISK_SERIAL}) AND ${BOOTSTRAP_DISK_B_REAL} (${SECONDARY_DISK_SERIAL})"
  printf 'This permanently erases both approved disks. Type exactly: %s\n> ' "$expected" >&2
  IFS= read -r response || {
    bootstrap_die 'confirmation input ended; no disks were changed'
    return 1
  }
  [[ $response == "$expected" ]] || {
    bootstrap_die 'resolved-path and serial confirmation did not match; no disks were changed'
    return 1
  }
  [[ $(bootstrap_disk_realpath "$PRIMARY_DISK") == "$BOOTSTRAP_DISK_A_REAL" && $(bootstrap_disk_realpath "$SECONDARY_DISK") == "$BOOTSTRAP_DISK_B_REAL" ]] ||
    {
      bootstrap_die 'device paths changed during confirmation'
      return 1
    }
  [[ $(bootstrap_disk_serial "$BOOTSTRAP_DISK_A_REAL") == "$PRIMARY_DISK_SERIAL" && $(bootstrap_disk_serial "$BOOTSTRAP_DISK_B_REAL") == "$SECONDARY_DISK_SERIAL" ]] ||
    {
      bootstrap_die 'device identity changed during confirmation'
      return 1
    }
}
