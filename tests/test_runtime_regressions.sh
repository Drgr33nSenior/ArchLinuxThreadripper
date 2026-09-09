#!/usr/bin/env bash
# Only synthetic responses: no firmware, package transaction or host-service call.
# shellcheck disable=SC2329
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/bin/workstationctl" --help >/dev/null
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

ws_boot_benchmark() { printf '%s\n' "$*"; }
[[ $(main boot-benchmark "$work/boot") == "$work/boot" ]]
if main boot-benchmark "$work/boot" unexpected >/dev/null 2>&1; then exit 1; fi

ws_require_arch() { :; }
ws_require_root() { :; }
pacman() { printf '%s\n' "$*" >>"$work/pacman"; }
ws_dev_setup server "$root/templates/workstation/packages.pacman"
grep -Fq -- '-Syu --needed --' "$work/pacman"
if grep -Eq '(^| )(gnome|gdm|steam|tuned-ppd|lib32-[^ ]+|virt-manager)( |$)' "$work/pacman"; then exit 1; fi
before=$(wc -l <"$work/pacman")
if (ws_dev_setup server "$root/templates/workstation/packages-desktop.pacman") >/dev/null 2>&1; then exit 1; fi
[[ $(wc -l <"$work/pacman") == "$before" ]]
ws_dev_setup workstation "$root/templates/workstation/packages-desktop.pacman"
grep -q 'gnome gdm' "$work/pacman"
ws_dev_setup virtualization "$root/templates/workstation/packages.pacman"
tail -n 1 "$work/pacman" | grep -Eq '(^| )qemu-base( |$)'
tail -n 1 "$work/pacman" | grep -Eq '(^| )libxml2( |$)'
tail -n 1 "$work/pacman" | grep -Eq '(^| )jq( |$)'
tuned-adm() { printf '%s\n' "$*" >"$work/tuned"; }
ws_profile server >/dev/null
[[ $(<"$work/tuned") == 'profile balanced' ]]
ws_profile desktop >/dev/null
[[ $(<"$work/tuned") == 'profile desktop' ]]

# Exercise archive validation without downloading/extracting a vendor archive.
tar() {
  [[ ${tar_failure:-false} == false ]] || return 42
  printf '%s\n' "$archive_entries"
}
archive_entries=$'jetbrains-toolbox-3.7.2/bin/\njetbrains-toolbox-3.7.2/bin/jetbrains-toolbox'
[[ $(ws_toolbox_archive_root unused 3.7.2) == jetbrains-toolbox-3.7.2 ]]
for archive_entries in /absolute/file 'jetbrains-toolbox-3.7.2/../outside' 'jetbrains-toolbox-9/bin/tool' '' \
  $'jetbrains-toolbox-3.7.2/bin/tool\nanother/file'; do
  if (ws_toolbox_archive_root unused 3.7.2) >/dev/null 2>&1; then exit 1; fi
done
tar_failure=true
if (ws_toolbox_archive_root unused 3.7.2) >/dev/null 2>&1; then exit 1; fi
unset -f tar

efibootmgr() {
  [[ ${efi_failure:-false} == false ]] || return 42
  printf 'BootOrder: 0001,0002\nBoot0001* Arch Linux (LTS)\tHD(1,GPT,11111111-2222-3333-4444-555555555555,0x800,0x400000)/File(\\EFI\\Linux\\arch-linux-lts.efi)\n'
}
[[ $(ws_bootnum_for_label 'Arch Linux (LTS)') == 0001 ]]
efi_failure=true
if ws_bootnum_for_label 'Arch Linux (LTS)' >/dev/null 2>&1; then exit 1; else [[ $? == 2 ]]; fi
efi_failure=false

(
  common::validate_esp_pair() {
    COMMON_ESP_A_DEVICE=/dev/synthetic1
    COMMON_ESP_A_DISK=/dev/synthetic
  }
  lsblk() {
    [[ ${partition_failure:-false} == false ]] || return 42
    printf '%s\n' "$partition_number"
  }
  efibootmgr() { printf '%s\n' "$*" >>"$work/firmware-writes"; }
  partition_number=1
  ws_create_git_boot_entry /efi 'Arch Linux (git)'
  (($(wc -l <"$work/firmware-writes") == 1))
  for partition_number in '' 0 $'1\n2'; do
    if (ws_create_git_boot_entry /efi 'Arch Linux (git)') >/dev/null 2>&1; then exit 1; fi
  done
  partition_failure=true
  if (ws_create_git_boot_entry /efi 'Arch Linux (git)') >/dev/null 2>&1; then exit 1; fi
  (($(wc -l <"$work/firmware-writes") == 1))
)

(
  # A rejected ESP/firmware inventory must occur before the first filesystem write.
  common::validate_esp_pair() { return 1; }
  sbverify() { :; }
  install() { printf 'unexpected write\n' >>"$work/writes"; }
  printf 'synthetic UKI\n' >"$work/arch-linux-git.efi"
  printf 'synthetic public certificate\n' >"$work/db.pem"
  if (ws_kernel_promote linux-git "$work/arch-linux-git.efi" /efi /efi2 "$work/db.pem") >/dev/null 2>&1; then exit 1; fi
  [[ ! -e $work/writes ]]
  common::validate_esp_pair() { COMMON_ESP_A_PARTUUID=11111111-2222-3333-4444-555555555555; }
  ws_bootnum_for_label() { return 2; }
  if (ws_kernel_promote linux-git "$work/arch-linux-git.efi" /efi /efi2 "$work/db.pem") >/dev/null 2>&1; then exit 1; fi
  [[ ! -e $work/writes ]]
)

(
  # Package refusal must not reload units or start any service.
  pacman() { return 1; }
  systemctl() { printf 'unexpected service call\n' >>"$work/services"; }
  if (ws_backup_install) >/dev/null 2>&1; then exit 1; fi
  [[ ! -e $work/services ]]
)
printf 'Runtime CLI, headless packages, archive, firmware and packaged-backup regressions passed\n'
