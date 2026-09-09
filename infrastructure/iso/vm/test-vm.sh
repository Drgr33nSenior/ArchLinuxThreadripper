#!/usr/bin/env bash
# Explicit, unprivileged QEMU harness; no libvirt, passthrough or shared folders.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
source "$root/lib/common.sh"

vm_path() {
  [[ $1 =~ ^/[A-Za-z0-9_./-]+$ && $1 != *..* && $1 != /dev/* && $1 != /proc/* && $1 != /sys/* ]] ||
    common::die 'use an absolute file path without spaces, commas, traversal or device paths'
  [[ ! -L $1 ]] || common::die 'symlink input refused'
}

vm_image_check() {
  local metadata
  [[ -f $1 && ! -L $1 && -O $1 ]] || common::die 'disk must be an owned regular fixture file'
  # Force raw: guest-controlled bytes can never name a host backing/data file.
  metadata=$(qemu-img info -f raw --output=json "$1") || common::die 'cannot inspect fixture image'
  jq -e '.format == "raw" and .["virtual-size"] == 68719476736' <<<"$metadata" >/dev/null ||
    common::die 'fixture must be a standalone 64 GiB raw file'
}

main() {
  (($# >= 3)) || common::die 'usage: test-vm.sh create DIR OVMF_VARS [--execute] | boot/recover DIR OVMF_CODE ISO-or-- [--network] [--execute]'
  local action=$1 directory=$2 firmware=$3 medium='' execute=false network=false option
  shift 3
  vm_path "$directory"
  vm_path "$firmware"
  [[ -f $firmware ]] || common::die 'firmware must be a regular file, never a host firmware device'
  if [[ $action != create ]]; then
    (($# >= 1)) || common::die 'supply the ISO path or - for installed-disk boot'
    medium=$1
    shift
    if [[ $medium != - ]]; then
      vm_path "$medium"
      [[ -f $medium ]] || common::die 'ISO must be a regular file'
    fi
  fi
  for option in "$@"; do
    case $option in
      --execute) execute=true ;;
      --network) network=true ;;
      *) common::die 'unknown VM option; arbitrary QEMU arguments are not accepted' ;;
    esac
  done
  [[ $action == create || $action == boot || $action == recover ]] || common::die 'unknown VM action'
  if [[ $action == create ]]; then
    [[ ! -e $directory && $network == false ]] || common::die 'create requires a new directory and no network option'
    common::print_command qemu-img create -f raw "$directory/nvme-a.raw" 64G
    common::print_command qemu-img create -f raw "$directory/nvme-b.raw" 64G
    [[ $execute == true ]] || return 0
    [[ $EUID != 0 ]] || common::die 'never create or run this VM as root'
    mkdir -m700 -- "$directory"
    cp "$firmware" "$directory/OVMF_VARS.fd"
    qemu-img create -f raw "$directory/nvme-a.raw" 64G
    qemu-img create -f raw "$directory/nvme-b.raw" 64G
    printf 'ARCHLAB_VM_V1\n' >"$directory/fixture.marker"
    return
  fi
  [[ -d $directory && -O $directory && -f $directory/fixture.marker &&
    $(<"$directory/fixture.marker") == ARCHLAB_VM_V1 ]] || common::die 'not an owned disposable fixture directory'
  [[ -f $directory/OVMF_VARS.fd && ! -L $directory/OVMF_VARS.fd ]] || common::die 'missing regular private VM variable store'
  local disk_mode='' nic=none
  if [[ $action == recover ]]; then
    [[ $medium != - && $network == false ]] || common::die 'recovery requires the ISO and has no networking'
    disk_mode=',snapshot=on'
  fi
  [[ $network == false ]] || nic=user,model=virtio-net-pci
  local args=(qemu-system-x86_64 -name arch-workstation-test -machine q35 -accel tcg -cpu max -smp 4 -m 8192
    -no-reboot -nic "$nic"
    -drive "if=pflash,format=raw,readonly=on,file=$firmware"
    -drive "if=pflash,format=raw,file=$directory/OVMF_VARS.fd"
    -drive "if=none,id=nvmea,format=raw,file=$directory/nvme-a.raw$disk_mode"
    -device 'nvme,drive=nvmea,serial=ARCHLAB_TEST_A'
    -drive "if=none,id=nvmeb,format=raw,file=$directory/nvme-b.raw$disk_mode"
    -device 'nvme,drive=nvmeb,serial=ARCHLAB_TEST_B')
  if [[ $medium != - ]]; then args+=(-drive "file=$medium,media=cdrom,format=raw,readonly=on" -boot once=d); fi
  common::print_command "${args[@]}"
  [[ $execute == true ]] || return 0
  [[ $EUID != 0 ]] || common::die 'never run this VM as root'
  vm_image_check "$directory/nvme-a.raw"
  vm_image_check "$directory/nvme-b.raw"
  # A directory lock is scoped to this fixture; never kill an existing QEMU.
  mkdir "$directory/running.lock" || common::die 'VM lock exists; inspect the owning process before manual recovery'
  VM_LOCK_DIRECTORY="$directory/running.lock"
  trap 'rmdir -- "$VM_LOCK_DIRECTORY"' EXIT
  "${args[@]}"
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
