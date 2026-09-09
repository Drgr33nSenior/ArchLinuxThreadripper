#!/usr/bin/env bash
# Real select/verify functions, synthetic firmware and ESPs only.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/workstation/runtime.sh"
source "$root/lib/bootstrap/common.sh"
source "$root/lib/bootstrap/install.sh"
source "$root/lib/bootstrap/verify.sh"
source "$root/tests/bootstrap/storage-fixtures.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
BOOTSTRAP_TARGET=$work
COMMON_ESP_A_PARTUUID=11111111-1111-1111-1111-111111111111
COMMON_ESP_B_PARTUUID=22222222-2222-2222-2222-222222222222
# The imported fixture is deliberately replaced below by a writable mock.
# shellcheck disable=SC2218
efibootmgr -v >"$work/inventory"
printf 'Boot0008* Arch Linux (git)\tHD(1,GPT,%s,0x800,0x400000)/File(\\EFI\\Linux\\arch-linux-git.efi)\n' "$COMMON_ESP_A_PARTUUID" >>"$work/inventory"
printf '0001,0002,0003,0004,0005,0006,0007,0008\n' >"$work/order"
efibootmgr() {
  if [[ ${1:-} == --bootorder ]]; then
    printf '%s\n' "$2" >"$work/order"
    return
  fi
  printf 'BootOrder: %s\n' "$(<"$work/order")"
  sed '/^BootOrder:/d' "$work/inventory"
}
common::validate_esp_pair() { :; }
sbverify() { [[ ${BAD_SIGNATURE:-0} == 0 ]]; }
mkdir -p "$work/efi/EFI/Linux" "$work/efi2/EFI/Linux"
printf 'signed fixture\n' >"$work/efi/EFI/Linux/arch-linux-git.efi"
cp "$work/efi/EFI/Linux/arch-linux-git.efi" "$work/efi2/EFI/Linux/arch-linux-git.efi"
bootstrap_verify_boot_order initial
for label in 'Arch Linux (git)' 'Arch Linux (LTS)' 'Arch Linux (stable)'; do
  ws_select_boot_label "$label"
  bootstrap_verify_boot_order installed
  if [[ $label != 'Arch Linux (stable)' ]]; then
    if bootstrap_verify_boot_order initial 2>/dev/null; then exit 1; fi
  fi
done
ws_select_boot_label 'Arch Linux (git)'
if BAD_SIGNATURE=1 bootstrap_verify_boot_order 2>/dev/null; then exit 1; fi
printf '0002,0001,0003,0004,0005\n' >"$work/order"
if bootstrap_verify_boot_order 2>/dev/null; then exit 1; fi
printf '0002,0001,0003,0004,0005,0006\n' >"$work/order"
sed 's/arch-recovery.efi/wrong.efi/g' "$work/inventory" >"$work/wrong"
mv "$work/wrong" "$work/inventory"
if bootstrap_verify_boot_order 2>/dev/null; then exit 1; fi
printf 'Installed/initial BootOrder policy fixtures passed; no firmware changed\n'
