#!/usr/bin/env bash
# No real firmware, mounts, signing, block-device probes or chroots are used.
set -Eeuo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/common.sh"
source "$repo_root/lib/bootstrap/common.sh"
source "$repo_root/lib/bootstrap/config.sh"
source "$repo_root/lib/bootstrap/install.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

(
  source "$repo_root/tests/bootstrap/storage-fixtures.sh"
  storage_fixture_create "$work/storage"
  common::validate_esp_pair "$STORAGE_FIXTURE_ROOT"
  [[ $COMMON_ESP_A_DEVICE == /dev/testnvme0n1p1 && $COMMON_ESP_B_DEVICE == /dev/testnvme1n1p1 ]]
  [[ $(bootstrap_bootnum_for_label 'Arch Linux (stable)') == 0001 ]]
  [[ $(bootstrap_bootnum_for_label 'Arch Linux (recovery backup)') == 0006 ]]
  loader='\EFI\Linux\arch-linux.efi'
  for STORAGE_FIXTURE_MODE in mount-failed block-failed alias ancestor subdirectory wrong-fstype ambiguous same-disk same-guid wrong-parttype wrong-uuid; do
    if common::validate_esp_pair "$STORAGE_FIXTURE_ROOT" >/dev/null 2>&1; then
      echo "unsafe ESP inventory accepted: $STORAGE_FIXTURE_MODE" >&2; exit 1
    fi
  done
  STORAGE_FIXTURE_MODE=valid
  printf 'UUID=AAAA-0001 /efi vfat defaults 0 2\n' >> "$work/storage/etc/fstab"
  if common::validate_esp_pair "$STORAGE_FIXTURE_ROOT" >/dev/null 2>&1; then exit 1; fi
  storage_fixture_create "$work/storage"
  printf 'PARTUUID=11111111-1111-1111-1111-111111111111 /efi vfat defaults 0 2\nPARTUUID=22222222-2222-2222-2222-222222222222 /efi2 vfat defaults 0 2\n' > "$work/storage/etc/fstab"
  common::validate_esp_pair "$STORAGE_FIXTURE_ROOT"

  # Snapshot realistic output before replacing only the read-only EFI command.
  efi_fixture_output=$(efibootmgr -v)
  # shellcheck disable=SC2329
  efibootmgr() { printf '%s\n' "$efi_fixture_output"; }
  efi_fixture_output=$(printf '%s\n' "$efi_fixture_output" | sed 's/Boot0001\* /Boot0001  /')
  [[ $(common::bootnum_for_label 'Arch Linux (stable)' "$COMMON_ESP_A_PARTUUID" "$loader") == 0001 ]]
  status=0
  common::bootnum_for_label absent >/dev/null 2>&1 || status=$?
  [[ $status == 1 ]]
  for arguments in wrong-loader wrong-partition; do
    status=0
    if [[ $arguments == wrong-loader ]]; then
      common::bootnum_for_label 'Arch Linux (stable)' "$COMMON_ESP_A_PARTUUID" '\EFI\Other\wrong.efi' >/dev/null 2>&1 || status=$?
    else
      common::bootnum_for_label 'Arch Linux (stable)' "$COMMON_ESP_B_PARTUUID" "$loader" >/dev/null 2>&1 || status=$?
    fi
    [[ $status == 2 ]]
  done
  efi_fixture_output+=$'\nBoot0008* Arch Linux (stable)\tHD(1,GPT,11111111-1111-1111-1111-111111111111,0x800,0x400000)/File(\\EFI\\Linux\\arch-linux.efi)'
  status=0; common::bootnum_for_label 'Arch Linux (stable)' >/dev/null 2>&1 || status=$?
  [[ $status == 2 ]]
  if bootstrap_assert_boot_labels_absent >/dev/null 2>&1; then exit 1; fi
  efi_fixture_output=malformed
  status=0; common::bootnum_for_label absent >/dev/null 2>&1 || status=$?
  [[ $status == 2 ]]
  if bootstrap_assert_boot_labels_absent >/dev/null 2>&1; then exit 1; fi
  # Failed enumeration must never authorize firmware creation or disk erasure.
  efibootmgr() { return 2; }
  status=0; common::bootnum_for_label absent >/dev/null 2>&1 || status=$?
  [[ $status == 2 ]]
  if bootstrap_assert_boot_labels_absent >/dev/null 2>&1; then exit 1; fi
)

(
  source "$repo_root/lib/bootstrap/preflight.sh"
  BOOTSTRAP_TARGET=/mnt
  # shellcheck disable=SC2329
  findmnt() { return 2; }
  if bootstrap_assert_safe_target >/dev/null 2>&1; then exit 1; fi
  findmnt() { :; }
  if bootstrap_assert_safe_target >/dev/null 2>&1; then exit 1; fi
  findmnt() { printf '/\n/run/archiso\n'; }
  bootstrap_assert_safe_target
  # PARTN inspection can be checked without constructing any block devices.
  # shellcheck disable=SC2329
  lsblk() { printf '/dev/synthetic1 1\n'; return 2; }
  if bootstrap_part_path /dev/synthetic 1 >/dev/null 2>&1; then exit 1; fi
  if bootstrap_assert_disk_unused /dev/synthetic >/dev/null 2>&1; then exit 1; fi
  lsblk() { [[ $2 != TYPE ]] || return 2; }
  if bootstrap_assert_disk_unused /dev/synthetic >/dev/null 2>&1; then exit 1; fi
  lsblk() { [[ $2 != TYPE ]] || printf 'disk\npart\n'; }
  bootstrap_assert_disk_unused /dev/synthetic
  lsblk() { [[ $2 != TYPE ]] || printf 'disk\npart\nraid0\n'; }
  if bootstrap_assert_disk_unused /dev/synthetic >/dev/null 2>&1; then exit 1; fi
  lsblk() { [[ $2 != MOUNTPOINT ]] || printf '/mounted\n'; }
  if bootstrap_assert_disk_unused /dev/synthetic >/dev/null 2>&1; then exit 1; fi
)

(
  bootstrap_load_config "$repo_root/config/install.conf.example"
  [[ $HOST_PROFILE == headless && $ENABLE_SSH == false ]]
  sed '/^HOST_PROFILE=/d' "$repo_root/config/install.conf.example" > "$work/legacy.conf"
  bootstrap_load_config "$work/legacy.conf"
  [[ $HOST_PROFILE == headless ]]
  sed 's/^HOST_PROFILE=.*/HOST_PROFILE=unknown/' "$repo_root/config/install.conf.example" > "$work/invalid.conf"
  if bootstrap_load_config "$work/invalid.conf" >/dev/null 2>&1; then exit 1; fi
  bootstrap_load_config "$repo_root/config/install.conf.example"
  BOOTSTRAP_TARGET="$work/profiles"
  BOOTSTRAP_DRY_RUN=0
  BOOTSTRAP_GPU_MULTILIB=(lib32-vulkan-radeon)
  mkdir -p "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/balanced" "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/desktop"
  printf '# mock installed TuneD profile\n' > "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/balanced/tuned.conf"
  printf '# mock installed TuneD profile\n' > "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/desktop/tuned.conf"
  # Every potentially mutating target command is mocked to record arguments.
  # shellcheck disable=SC2329
  bootstrap_chroot() { printf '%s\n' "$*" >> "$work/profile-commands"; }
  bootstrap_set_passwords() { :; }
  bootstrap_copy_and_sign_ukis() { :; }
  for HOST_PROFILE in headless desktop; do
    : > "$work/profile-commands"
    bootstrap_select_host_profile
    bootstrap_configure_system
    [[ $(< "$BOOTSTRAP_TARGET/etc/tuned/profile_mode") == manual ]]
    [[ $(< "$BOOTSTRAP_TARGET/etc/ssh/sshd_config.d/00-arch-workstation-root.conf") == 'PermitRootLogin no' ]]
    if [[ $HOST_PROFILE == headless ]]; then
      [[ ${#BOOTSTRAP_PROFILE_PACKAGES[@]} == 0 ]]
      [[ $(< "$BOOTSTRAP_TARGET/etc/tuned/active_profile") == balanced ]]
      grep -Fqx 'systemctl set-default multi-user.target' "$work/profile-commands"
      if grep -E '(gdm|tuned-ppd|steam|lib32-|graphical.target)' "$work/profile-commands"; then exit 1; fi
    else
      [[ ${BOOTSTRAP_PROFILE_PACKAGES[*]} == *gnome* ]]
      [[ $(< "$BOOTSTRAP_TARGET/etc/tuned/active_profile") == desktop ]]
      grep -Fqx 'systemctl set-default graphical.target' "$work/profile-commands"
      grep -Fq steam "$work/profile-commands"
    fi
    if grep -E '(tuned-adm|enable sshd.service|--now)' "$work/profile-commands"; then exit 1; fi
  done
  # Repeat policy persistence succeeds without daemon activation or state drift.
  bootstrap_configure_offline_policy
  BOOTSTRAP_DRY_RUN=1
  BOOTSTRAP_TARGET="$work/no-dryrun-policy-files"
  bootstrap_configure_offline_policy >/dev/null 2>&1
  [[ ! -e $BOOTSTRAP_TARGET ]]
)

(
  BOOTSTRAP_DRY_RUN=0; BOOTSTRAP_TARGET="$work/recovery"; INSTALL_USER=operator
  bootstrap_require_tty() { :; }
  # Never call passwd or read shadow. The mock exposes only synthetic state.
  # shellcheck disable=SC2329
  arch-chroot() {
    printf '%s\n' "$*" >> "$work/password-commands"
    if [[ $3 == -S ]]; then printf 'root %s 2026-09-05 0 99999 7 -1\n' "${mock_password_state:-P}"; fi
  }
  bootstrap_set_passwords >/dev/null 2>&1
  grep -Fqx "$work/recovery passwd root" "$work/password-commands"
  grep -Fqx "$work/recovery passwd operator" "$work/password-commands"
  for mock_password_state in L NP; do
    if bootstrap_set_passwords >/dev/null 2>&1; then exit 1; fi
  done
  arch-chroot() { return 1; }
  if bootstrap_set_passwords >/dev/null 2>&1; then exit 1; fi
)

(
  bootstrap_load_config "$repo_root/config/install.conf.example"
  BOOTSTRAP_DRY_RUN=0; BOOTSTRAP_TARGET="$work/never-mounted"
  BOOTSTRAP_DISK_A_REAL=/dev/synthetic-a; BOOTSTRAP_DISK_B_REAL=/dev/synthetic-b
  BOOTSTRAP_ESP_A=/dev/synthetic-a1; BOOTSTRAP_ESP_B=/dev/synthetic-b1
  # Failure of the first command must stop each real function even when the
  # caller uses an if statement (which otherwise suppresses Bash errexit).
  # shellcheck disable=SC2329
  bootstrap_run() { printf '%s\n' "$*" >> "$work/refused-commands"; return 1; }
  for action in bootstrap_create_partitions bootstrap_create_storage_stack bootstrap_mount_target bootstrap_create_luks_header_backup; do
    : > "$work/refused-commands"
    if "$action" >/dev/null 2>&1; then exit 1; fi
    [[ $(wc -l < "$work/refused-commands" | tr -d ' ') == 1 ]]
  done
)

echo 'ESP, UEFI, fail-closed inspection, headless profile and authenticated recovery contract tests passed'
