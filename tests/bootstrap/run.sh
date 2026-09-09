#!/usr/bin/env bash
# Offline tests for configuration and no-side-effect primitives.
set -Eeuo pipefail
IFS=$'\n\t'

TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly TEST_ROOT
source "$TEST_ROOT/lib/common.sh"
# shellcheck source=../../lib/bootstrap/common.sh
# shellcheck disable=SC1091
source "$TEST_ROOT/lib/bootstrap/common.sh"
# shellcheck source=../../lib/bootstrap/config.sh
# shellcheck disable=SC1091
source "$TEST_ROOT/lib/bootstrap/config.sh"
# shellcheck source=../../lib/bootstrap/preflight.sh
# shellcheck disable=SC1091
source "$TEST_ROOT/lib/bootstrap/preflight.sh"
# shellcheck source=../../lib/bootstrap/install.sh
# shellcheck disable=SC1091
source "$TEST_ROOT/lib/bootstrap/install.sh"
# shellcheck source=../../lib/bootstrap/verify.sh
# shellcheck disable=SC1091
source "$TEST_ROOT/lib/bootstrap/verify.sh"

failures=0
assert_ok() {
  if ! "$@"; then
    printf 'FAIL expected success: %q\n' "$*" >&2
    failures=$((failures + 1))
  fi
}
assert_fail() {
  if "$@"; then
    printf 'FAIL expected failure: %q\n' "$*" >&2
    failures=$((failures + 1))
  fi
}

temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT

valid_config="$temp_dir/valid.conf"
printf '%s\n' \
  'HOSTNAME=threadripper-ai' \
  'USERNAME=operator' \
  'LOCALE=en_GB.UTF-8' \
  'TIMEZONE=Europe/London' \
  'KEYMAP=uk' \
  'PRIMARY_DISK=/dev/disk/by-id/nvme-Example_A' \
  'SECONDARY_DISK=/dev/disk/by-id/nvme-Example_B' \
  'PRIMARY_DISK_SERIAL=Example_A' \
  'SECONDARY_DISK_SERIAL=Example_B' \
  'RAID_NAME=rootraid' \
  'LUKS_NAME=cryptroot' \
  'ESP_SIZE_MIB=2048' \
  'RAID_CHUNK_KIB=512' \
  'LUKS_ITER_TIME_MS=2000' \
  'LUKS_MEMORY_KIB=1048576' \
  'LUKS_PARALLEL=4' \
  'ALLOW_DISCARDS=true' \
  'PRIMARY_ESP_MOUNT=/efi' \
  'SECONDARY_ESP_MOUNT=/efi2' \
  'ENABLE_SSH=false' \
  'ENABLE_BLUETOOTH=false' \
  'ENABLE_PRINTING=false' >"$valid_config"
assert_ok bootstrap_load_config "$valid_config"
[[ $HOSTNAME == threadripper-ai ]] || {
  printf 'FAIL hostname not loaded\n' >&2
  failures=$((failures + 1))
}

unknown_config="$temp_dir/unknown.conf"
cp "$valid_config" "$unknown_config"
printf 'PASSPHRASE=not-allowed\n' >>"$unknown_config"
assert_fail bootstrap_load_config "$unknown_config"

unsafe_config="$temp_dir/unsafe.conf"
# shellcheck disable=SC2016
sed 's/^HOSTNAME=.*/HOSTNAME=$(touch-payload)/' "$valid_config" >"$unsafe_config"
assert_fail bootstrap_load_config "$unsafe_config"

BOOTSTRAP_DRY_RUN=1
export BOOTSTRAP_DRY_RUN
dry_run_output=$(bootstrap_run definitely-not-an-installed-command 2>&1)
[[ $dry_run_output == *definitely-not-an-installed-command* ]] || {
  printf 'FAIL dry-run did not print command\n' >&2
  failures=$((failures + 1))
}

# shellcheck disable=SC2016
assert_fail bootstrap_config_value_safe '$(untrusted)'
assert_fail bootstrap_config_value_safe 'two words'
assert_ok bootstrap_config_value_safe '/dev/disk/by-id/nvme-Example_A'

# The preflight test replaces host probes with deterministic mocks. It proves
# that configured serials are a prerequisite, rather than merely prompt text.
# shellcheck disable=SC2329
bootstrap_require_root() { :; }
bootstrap_require_arch_iso() { :; }
# shellcheck disable=SC2329
bootstrap_require_uefi() { :; }
# shellcheck disable=SC2329
bootstrap_require_install_commands() { :; }
bootstrap_select_gpu_packages() { :; }
bootstrap_no_active_swap() { :; }
# shellcheck disable=SC2034
bootstrap_check_disk() {
  if [[ $1 == /dev/disk/by-id/nvme-Example_A ]]; then
    BOOTSTRAP_DISK_REAL=/dev/mock-a
  else
    BOOTSTRAP_DISK_REAL=/dev/mock-b
  fi
  BOOTSTRAP_DISK_SIZE=1000000000
}
bootstrap_disk_serial() {
  case $1 in
    /dev/mock-a) printf '%s\n' Example_A ;;
    /dev/mock-b) printf '%s\n' Example_B ;;
    *) return 1 ;;
  esac
}
assert_ok bootstrap_load_config "$valid_config"
assert_ok bootstrap_preflight
PRIMARY_DISK_SERIAL=Wrong_A
assert_fail bootstrap_preflight
# shellcheck disable=SC2034
PRIMARY_DISK_SERIAL=Example_A

# Reject a target containing a descendant mount, not just /mnt itself.
BOOTSTRAP_TARGET=/mnt
# shellcheck disable=SC2329
findmnt() { printf '%s\n' /mnt/efi; }
assert_fail bootstrap_assert_safe_target
# shellcheck disable=SC2329
findmnt() { return 1; }
assert_fail bootstrap_assert_safe_target
# shellcheck disable=SC2329
findmnt() { printf '/\n/run/archiso\n'; }
assert_ok bootstrap_assert_safe_target

# A dry-run remains safe in a non-interactive context: it still performs its
# preflight gate but cannot reach the destructive phase or request a passphrase.
dry_run_preflight_calls=0
dry_run_plan_calls=0
dry_run_tty_calls=0
dry_run_destructive_calls=0
# shellcheck disable=SC2034,SC2329
bootstrap_preflight() {
  dry_run_preflight_calls=$((dry_run_preflight_calls + 1))
  BOOTSTRAP_DISK_A_REAL=/dev/mock-a
  BOOTSTRAP_DISK_B_REAL=/dev/mock-b
}
bootstrap_assert_safe_target() { :; }
bootstrap_install_plan() { dry_run_plan_calls=$((dry_run_plan_calls + 1)); }
bootstrap_require_tty() {
  dry_run_tty_calls=$((dry_run_tty_calls + 1))
  return 1
}
bootstrap_create_partitions() {
  dry_run_destructive_calls=$((dry_run_destructive_calls + 1))
  return 1
}
BOOTSTRAP_DRY_RUN=1
assert_ok bootstrap_install
[[ $dry_run_preflight_calls == 1 && $dry_run_plan_calls == 1 && $dry_run_tty_calls == 0 && $dry_run_destructive_calls == 0 ]] ||
  {
    printf 'FAIL dry-run did not remain non-interactive and side-effect free\n' >&2
    failures=$((failures + 1))
  }

# A real install must fail before preflight or disk operations when no terminal
# is present to collect the passphrase and exact destructive confirmation.
interactive_preflight_calls=0
bootstrap_preflight() { interactive_preflight_calls=$((interactive_preflight_calls + 1)); }
BOOTSTRAP_DRY_RUN=0
assert_fail bootstrap_install
[[ $interactive_preflight_calls == 0 && $dry_run_tty_calls == 1 && $dry_run_destructive_calls == 0 ]] ||
  {
    printf 'FAIL non-interactive install advanced past the TTY guard\n' >&2
    failures=$((failures + 1))
  }
BOOTSTRAP_DRY_RUN=1

# Signature verification is certificate-based and covers all direct and
# fallback UKIs. The sbverify mock rejects a signature on demand.
verify_root="$temp_dir/verify-root"
mkdir -p "$verify_root/etc" "$verify_root/usr/share/libalpm/hooks" "$verify_root/efi/EFI/Linux" "$verify_root/efi2/EFI/Linux" \
  "$verify_root/efi/EFI/BOOT" "$verify_root/efi2/EFI/BOOT" "$verify_root/var/lib/sbctl/keys/db" \
  "$verify_root/usr/lib/arch-workstation-bootstrap"
printf '%s\n' 'HOOKS=(base systemd autodetect microcode modconf kms keyboard sd-vconsole block mdadm_udev sd-encrypt filesystems fsck)' >"$verify_root/etc/mkinitcpio.conf"
printf '%s\n' 'cryptroot UUID=test none fido2-device=auto,discard,x-initrd.attach' >"$verify_root/etc/crypttab"
cp "$verify_root/etc/crypttab" "$verify_root/etc/crypttab.initramfs"
printf '%s\n' 'ARRAY /dev/md/rootraid metadata=1.2' >"$verify_root/etc/mdadm.conf"
printf '%s\n' '/dev/mapper/cryptroot / xfs defaults 0 0' >"$verify_root/etc/fstab"
printf '%s\n' certificate >"$verify_root/var/lib/sbctl/keys/db/db.pem"
cp "$TEST_ROOT/templates/arch/uki-sync" "$verify_root/usr/lib/arch-workstation-bootstrap/uki-sync"
chmod 0755 "$verify_root/usr/lib/arch-workstation-bootstrap/uki-sync"
cp "$TEST_ROOT/templates/arch/zzz-bootstrap-uki-sync.hook" "$verify_root/usr/share/libalpm/hooks/zzz-bootstrap-uki-sync.hook"
for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
  printf '%s\n' "$uki" >"$verify_root/efi/EFI/Linux/$uki"
  cp "$verify_root/efi/EFI/Linux/$uki" "$verify_root/efi2/EFI/Linux/$uki"
done
cp "$verify_root/efi/EFI/Linux/arch-recovery.efi" "$verify_root/efi/EFI/BOOT/BOOTX64.EFI"
cp "$verify_root/efi/EFI/Linux/arch-recovery.efi" "$verify_root/efi2/EFI/BOOT/BOOTX64.EFI"
# shellcheck disable=SC2329
bootstrap_require_root() { :; }
bootstrap_require_uefi() { :; }
# shellcheck disable=SC2329
sbverify() { [[ $1 == --cert && -r $2 && -s $3 ]]; }
source "$TEST_ROOT/tests/bootstrap/storage-fixtures.sh"
storage_fixture_create "$verify_root"
# shellcheck disable=SC2329
arch-chroot() {
  [[ $2 == passwd && $3 == -S && $4 == root ]] || return 1
  printf 'root P 2026-09-05 0 99999 7 -1\n'
}
# shellcheck disable=SC2034
BOOTSTRAP_TARGET=$verify_root
assert_ok bootstrap_verify
# Existing installations remain verifiable without an implicit migration, but
# a legacy /etc hook must never shadow the new package-owned hook silently.
mkdir -p "$verify_root/usr/local/lib/bootstrap-arch" "$verify_root/etc/pacman.d/hooks"
cp "$verify_root/usr/lib/arch-workstation-bootstrap/uki-sync" "$verify_root/usr/local/lib/bootstrap-arch/uki-sync"
sed 's|/usr/lib/arch-workstation-bootstrap/uki-sync|/usr/local/lib/bootstrap-arch/uki-sync|' \
  "$verify_root/usr/share/libalpm/hooks/zzz-bootstrap-uki-sync.hook" \
  >"$verify_root/etc/pacman.d/hooks/zzz-bootstrap-uki-sync.hook"
assert_fail bootstrap_verify
mv "$verify_root/usr/lib/arch-workstation-bootstrap/uki-sync" "$temp_dir/packaged-helper"
assert_ok bootstrap_verify
mv "$temp_dir/packaged-helper" "$verify_root/usr/lib/arch-workstation-bootstrap/uki-sync"
mv "$verify_root/etc/pacman.d/hooks/zzz-bootstrap-uki-sync.hook" "$temp_dir/legacy-hook"
# shellcheck disable=SC2329
sbverify() { return 1; }
assert_fail bootstrap_verify
sbverify() { [[ $1 == --cert && -r $2 && -s $3 ]]; }
# shellcheck disable=SC2329
efibootmgr() {
  cat <<'EOF'
BootOrder: 0001,0002,0003,0004,0005,0006
Boot0001* Arch Linux (stable)
Boot0002* Arch Linux (stable)
Boot0003* Arch Linux (recovery)
Boot0004* Arch Linux (stable backup)
Boot0005* Arch Linux (LTS backup)
Boot0006* Arch Linux (recovery backup)
EOF
}
assert_fail bootstrap_verify

# Firmware entry creation must refuse a rerun before adding even one duplicate
# label. A clean firmware namespace remains acceptable.
assert_fail bootstrap_assert_boot_labels_absent
# shellcheck disable=SC2329
efibootmgr() {
  cat <<'EOF'
BootOrder: 0007
Boot0007* Other OS
EOF
}
assert_ok bootstrap_assert_boot_labels_absent

# The post-transaction helper stages all updates before replacing the backup
# ESP and rejects an unsigned primary without advancing any secondary UKI.
helper_root="$temp_dir/helper-root"
helper_bin="$temp_dir/helper-bin"
storage_fixture_create "$helper_root"
mkdir -p "$helper_root/efi/EFI/Linux" "$helper_root/efi2/EFI/Linux" "$helper_root/efi/EFI/BOOT" \
  "$helper_root/efi2/EFI/BOOT" "$helper_root/var/lib/sbctl/keys/db" "$helper_bin"
printf '%s\n' certificate >"$helper_root/var/lib/sbctl/keys/db/db.pem"
for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
  printf 'signed-primary-%s\n' "$uki" >"$helper_root/efi/EFI/Linux/$uki"
  printf 'stale-secondary-%s\n' "$uki" >"$helper_root/efi2/EFI/Linux/$uki"
done
printf '%s\n' stale-fallback >"$helper_root/efi/EFI/BOOT/BOOTX64.EFI"
printf '%s\n' stale-fallback >"$helper_root/efi2/EFI/BOOT/BOOTX64.EFI"
# shellcheck disable=SC2016
printf '%s\n' '#!/usr/bin/env bash' '[[ $1 == --cert && -r $2 && -s $3 ]] || exit 1' 'grep -qx UNSIGNED "$3" && exit 1' 'exit 0' >"$helper_bin/sbverify"
chmod 0755 "$helper_bin/sbverify"
assert_ok env PATH="$helper_bin:/usr/bin:/bin" BOOTSTRAP_UKI_SYNC_ROOT="$helper_root" bash "$TEST_ROOT/templates/arch/uki-sync"
for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
  cmp -s "$helper_root/efi/EFI/Linux/$uki" "$helper_root/efi2/EFI/Linux/$uki" ||
    {
      printf 'FAIL sync helper did not update %s\n' "$uki" >&2
      failures=$((failures + 1))
    }
done
cmp -s "$helper_root/efi/EFI/Linux/arch-recovery.efi" "$helper_root/efi/EFI/BOOT/BOOTX64.EFI" ||
  {
    printf 'FAIL sync helper did not update primary fallback\n' >&2
    failures=$((failures + 1))
  }
printf '%s\n' preserved-secondary >"$helper_root/efi2/EFI/Linux/arch-linux-lts.efi"
printf '%s\n' UNSIGNED >"$helper_root/efi/EFI/Linux/arch-linux-lts.efi"
assert_fail env PATH="$helper_bin:/usr/bin:/bin" BOOTSTRAP_UKI_SYNC_ROOT="$helper_root" bash "$TEST_ROOT/templates/arch/uki-sync"
[[ $(<"$helper_root/efi2/EFI/Linux/arch-linux-lts.efi") == preserved-secondary ]] ||
  {
    printf 'FAIL sync helper copied an unsigned primary UKI\n' >&2
    failures=$((failures + 1))
  }

if ((failures)); then
  printf '%d bootstrap test(s) failed\n' "$failures" >&2
  exit 1
fi
printf 'bootstrap offline tests passed\n'
