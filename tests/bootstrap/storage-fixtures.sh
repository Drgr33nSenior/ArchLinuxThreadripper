#!/usr/bin/env bash
# Synthetic mount/block inventory only. Every unknown probe is rejected; these
# functions never fall through to the workstation's devices or mount table.
storage_fixture_create() {
  STORAGE_FIXTURE_ROOT=$1
  STORAGE_FIXTURE_MODE=valid
  export STORAGE_FIXTURE_ROOT STORAGE_FIXTURE_MODE
  mkdir -p "$1/etc" "$1/efi" "$1/efi2"
  printf 'UUID=AAAA-0001 /efi vfat defaults 0 2\nUUID=BBBB-0002 /efi2 vfat defaults 0 2\n' >"$1/etc/fstab"
}

# shellcheck disable=SC2329
findmnt() {
  [[ ${STORAGE_FIXTURE_MODE:-valid} != mount-failed ]] || return 2
  [[ $# == 6 && $1 == --noheadings && $2 == --raw && $3 == --mountpoint &&
    $5 == --output && $6 == SOURCE,FSTYPE,TARGET,FSROOT ]] || return 2
  local source target=$4 fsroot=/ fstype=vfat
  case $4 in
    "$STORAGE_FIXTURE_ROOT/efi") source=/dev/testnvme0n1p1 ;;
    "$STORAGE_FIXTURE_ROOT/efi2") source=/dev/testnvme1n1p1 ;;
    *) return 2 ;;
  esac
  case ${STORAGE_FIXTURE_MODE:-valid} in
    alias) source=/dev/testnvme0n1p1 ;;
    ancestor) target=$STORAGE_FIXTURE_ROOT ;;
    subdirectory) fsroot=/subdirectory ;;
    wrong-fstype) fstype=xfs ;;
    ambiguous) printf 'unexpected duplicate\n' ;;
  esac
  printf '%s %s %s %s\n' "$source" "$fstype" "$target" "$fsroot"
}

# shellcheck disable=SC2329
readlink() {
  [[ $# == 3 && $1 == -f && $2 == -- ]] || return 2
  case $3 in
    /dev/testnvme[01]n1 | /dev/testnvme[01]n1p1) printf '%s\n' "$3" ;;
    *) return 2 ;;
  esac
}

# shellcheck disable=SC2329
lsblk() {
  [[ ${STORAGE_FIXTURE_MODE:-valid} != block-failed ]] || return 2
  [[ $# == 6 && $1 == --nodeps && $2 == --noheadings && $3 == --raw && $4 == --output ]] || return 2
  local number guid parent type=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
  case $6 in
    /dev/testnvme0n1p1)
      number=0
      guid=11111111-1111-1111-1111-111111111111
      ;;
    /dev/testnvme1n1p1)
      number=1
      guid=22222222-2222-2222-2222-222222222222
      ;;
    /dev/testnvme[01]n1)
      [[ $5 == TYPE,PTTYPE ]] || return 2
      printf 'disk gpt\n'
      return 0
      ;;
    *) return 2 ;;
  esac
  parent="testnvme${number}n1"
  case ${STORAGE_FIXTURE_MODE:-valid} in
    same-disk) parent=testnvme0n1 ;;
    same-guid) guid=11111111-1111-1111-1111-111111111111 ;;
    wrong-parttype) type=0fc63daf-8483-4772-8e79-3d69d8477de4 ;;
  esac
  case $5 in
    TYPE,PARTTYPE,PARTUUID,PKNAME) printf 'part %s %s %s\n' "$type" "$guid" "$parent" ;;
    UUID)
      case ${STORAGE_FIXTURE_MODE:-valid}:$number in
        wrong-uuid:*) printf 'CAFE-9999\n' ;;
        *:0) printf 'AAAA-0001\n' ;;
        *:1) printf 'BBBB-0002\n' ;;
      esac
      ;;
    *) return 2 ;;
  esac
}

# shellcheck disable=SC2329
efibootmgr() {
  [[ $# == 1 && $1 == -v ]] || return 2
  local number label loader guid=11111111-1111-1111-1111-111111111111
  printf 'BootCurrent: 0001\nBootOrder: 0001,0002,0003,0004,0005,0006,0007\n'
  for number in 0001 0002 0003 0004 0005 0006; do
    case $number in
      0001 | 0004)
        label='stable'
        loader=arch-linux.efi
        ;;
      0002 | 0005)
        label='LTS'
        loader=arch-linux-lts.efi
        ;;
      0003 | 0006)
        label='recovery'
        loader=arch-recovery.efi
        ;;
    esac
    if [[ $number -gt 3 ]]; then
      label="$label backup"
      guid=22222222-2222-2222-2222-222222222222
    fi
    printf 'Boot%s* Arch Linux (%s)\tHD(1,GPT,%s,0x800,0x400000)/File(\\EFI\\Linux\\%s)\n' "$number" "$label" "$guid" "$loader"
  done
  printf 'Boot0007* Other OS\tHD(1,GPT,33333333-3333-3333-3333-333333333333,0x800,0x400000)/File(\\EFI\\Other\\BOOTX64.EFI)\n'
}

export -f findmnt readlink lsblk
