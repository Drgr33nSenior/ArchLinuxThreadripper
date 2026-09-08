#!/usr/bin/env bash
# Simulate regeneration and signature state; never use real signing material.
set -Eeuo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/tests/bootstrap/storage-fixtures.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
storage_fixture_create "$work/root"
export BOOTSTRAP_UKI_SYNC_ROOT="$work/root"
mkdir -p "$work/root/etc/kernel" "$work/root/efi/EFI/Linux" "$work/root/efi2/EFI/Linux" \
  "$work/root/efi/EFI/BOOT" "$work/root/efi2/EFI/BOOT" "$work/root/var/lib/sbctl/keys/db"
printf 'synthetic non-certificate\n' > "$work/root/var/lib/sbctl/keys/db/db.pem"
printf 'systemd.unit=emergency.target\n' > "$work/root/etc/kernel/cmdline.recovery"
# shellcheck disable=SC2329
sbverify() { [[ $1 == --cert && -r $2 && -s $3 ]] && grep -Fxq SIGNED "$3"; }
sbctl() {
  [[ $# == 3 && $1 == sign && $2 == -s && $3 == "$BOOTSTRAP_UKI_SYNC_ROOT/efi/EFI/Linux/"* ]] || return 2
  [[ ${MOCK_SIGN_FAILURE:-false} != true ]] || return 1
  printf 'SIGNED\n' >> "$3"
}
sync() { :; }
export -f sbctl sbverify sync

seed_ukis() {
  local uki
  for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
    printf 'fresh-regenerated-primary-%s\n' "$uki" > "$work/root/efi/EFI/Linux/$uki"
    printf 'old-signed-backup-%s\nSIGNED\n' "$uki" > "$work/root/efi2/EFI/Linux/$uki"
  done
  printf 'old-signed-fallback\nSIGNED\n' > "$work/root/efi/EFI/BOOT/BOOTX64.EFI"
  printf 'old-signed-fallback\nSIGNED\n' > "$work/root/efi2/EFI/BOOT/BOOTX64.EFI"
}
seed_ukis
if bash "$repo_root/templates/arch/uki-sync" >/dev/null 2>&1; then exit 1; fi
grep -Fq old-signed-backup "$work/root/efi2/EFI/Linux/arch-linux.efi"
export MOCK_SIGN_FAILURE=true
if bash "$repo_root/templates/arch/uki-sync" --sign >/dev/null 2>&1; then exit 1; fi
grep -Fq old-signed-backup "$work/root/efi2/EFI/Linux/arch-linux.efi"
MOCK_SIGN_FAILURE=false
bash "$repo_root/templates/arch/uki-sync" --sign
for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
  cmp -s "$work/root/efi/EFI/Linux/$uki" "$work/root/efi2/EFI/Linux/$uki"
done
cmp -s "$work/root/efi/EFI/Linux/arch-recovery.efi" "$work/root/efi/EFI/BOOT/BOOTX64.EFI"
cmp -s "$work/root/efi/EFI/BOOT/BOOTX64.EFI" "$work/root/efi2/EFI/BOOT/BOOTX64.EFI"
bash "$repo_root/templates/arch/uki-sync"

# A regeneration-triggered update signs only after both ESP identities pass.
seed_ukis
export STORAGE_FIXTURE_MODE='alias'
if bash "$repo_root/templates/arch/uki-sync" --sign >/dev/null 2>&1; then exit 1; fi
if grep -Fq SIGNED "$work/root/efi/EFI/Linux/arch-linux.efi"; then exit 1; fi
export STORAGE_FIXTURE_MODE=valid

# Package installation before UKI configuration exists is a deliberate no-op,
# but a configured machine missing any primary UKI must fail, never silently skip.
mkdir -p "$work/unconfigured"
BOOTSTRAP_UKI_SYNC_ROOT="$work/unconfigured" bash "$repo_root/templates/arch/uki-sync" --sign >/dev/null 2>&1
mv "$work/root/efi/EFI/Linux/arch-recovery.efi" "$work/missing-recovery"
if bash "$repo_root/templates/arch/uki-sync" --sign >/dev/null 2>&1; then exit 1; fi
grep -Fq old-signed-backup "$work/root/efi2/EFI/Linux/arch-linux.efi"

# Regression contract for all current upstream mkinitcpio dependency triggers.
hook="$repo_root/templates/arch/zzz-bootstrap-uki-sync.hook"
for target in 'usr/lib/initcpio/*' 'usr/lib/firmware/*' 'usr/lib/modules/*/extramodules/' \
  'usr/src/*/dkms.conf' usr/lib/systemd/systemd usr/lib/systemd/systemd-udevd \
  usr/bin/cryptsetup usr/bin/lvm usr/bin/mdadm usr/bin/pdata_tools usr/lib/libcryptsetup.so \
  usr/lib/libp11-kit.so usr/lib/libpcsclite.so usr/lib/modprobe.d/ 'usr/lib/modules/*/vmlinuz' \
  mkinitcpio mkinitcpio-git; do
  grep -Fqx "Target = $target" "$hook"
done
grep -Fqx 'Operation = Remove' "$hook"
grep -Fqx 'Exec = /usr/lib/arch-workstation-bootstrap/uki-sync --sign' "$hook"
if grep -Fqx 'Target = arch-workstation-boot' "$hook"; then
  echo 'The data-only runtime package must not trigger a bare pacman --root signing hook' >&2; exit 1
fi
echo 'UKI rebuild/sign/verify/stage and initial-install lifecycle tests passed'
