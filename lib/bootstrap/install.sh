#!/usr/bin/env bash

readonly -a BOOTSTRAP_BASE_PACKAGES=(
  base base-devel linux linux-lts linux-headers linux-lts-headers linux-firmware
  amd-ucode mdadm cryptsetup xfsprogs mkinitcpio systemd-ukify sbctl sbsigntools efibootmgr dosfstools gptfdisk
  sudo networkmanager tuned irqbalance zsh zsh-completions bash-completion
  zsh-autosuggestions zsh-syntax-highlighting fzf pkgfile git git-lfs git-delta openssh gnupg
  libsecret libfido2 vulkan-tools mesa-utils clinfo libva-utils hwloc numactl perf
  mesa ocl-icd ccache cmake ninja jq tpm2-tss tpm2-tools
)
readonly -a BOOTSTRAP_DESKTOP_PACKAGES=(gnome gdm tuned-ppd)
readonly -a BOOTSTRAP_MULTILIB_PACKAGES=(
  steam lib32-mesa gamescope gamemode lib32-gamemode mangohud lib32-mangohud
)

bootstrap_select_host_profile() {
  case ${HOST_PROFILE:-headless} in
    headless) BOOTSTRAP_PROFILE_PACKAGES=(); BOOTSTRAP_TUNED_PROFILE=balanced ;;
    desktop) BOOTSTRAP_PROFILE_PACKAGES=("${BOOTSTRAP_DESKTOP_PACKAGES[@]}"); BOOTSTRAP_TUNED_PROFILE=desktop ;;
    *) bootstrap_die 'unknown host profile'; return 1 ;;
  esac
  # Runtime policy is independent of whether the host has a desktop. Old
  # configurations retain their defaults; AI is an explicit profile selection.
  case ${TUNED_PROFILE:-auto} in
    auto) ;;
    balanced|desktop|throughput-performance|accelerator-performance|virtual-host)
      BOOTSTRAP_TUNED_PROFILE=$TUNED_PROFILE ;;
    *) bootstrap_die 'unsupported TuneD profile'; return 1 ;;
  esac
}

bootstrap_select_gpu_packages() {
  local inventory amd intel
  if [[ ${VM_TEST_MODE:-false} == true ]]; then
    bootstrap_require_test_vm || return 1
    BOOTSTRAP_GPU_PACKAGES=()
    BOOTSTRAP_GPU_MULTILIB=()
    BOOTSTRAP_GPU_DESCRIPTION='QEMU storage/boot test ONLY; physical GPU qualification is pending'
    return 0
  fi
  inventory=$(lspci -Dn) || { bootstrap_die 'PCI enumeration failed'; return 1; }
  amd=$(awk '$2 ~ /^03[0-9a-f][0-9a-f]:$/ && $3 ~ /^1002:/ {n++} END {print n+0}' <<< "$inventory")
  intel=$(awk '$2 ~ /^03[0-9a-f][0-9a-f]:$/ && $3 ~ /^8086:/ {n++} END {print n+0}' <<< "$inventory")
  if ((amd > 0 && intel == 0)); then
    BOOTSTRAP_GPU_PACKAGES=(linux-firmware-amdgpu vulkan-radeon)
    BOOTSTRAP_GPU_MULTILIB=(lib32-vulkan-radeon)
    BOOTSTRAP_GPU_DESCRIPTION="AMD amdgpu/RADV ($amd detected GPU devices); ROCm is a later package stage"
  elif ((intel > 0 && amd == 0)); then
    BOOTSTRAP_GPU_PACKAGES=(linux-firmware-intel vulkan-intel intel-compute-runtime intel-graphics-compiler intel-gmmlib level-zero-loader intel-media-driver intel-gpu-tools)
    BOOTSTRAP_GPU_MULTILIB=(lib32-vulkan-intel)
    BOOTSTRAP_GPU_DESCRIPTION="legacy Intel Xe ($intel detected GPU devices)"
  else
    bootstrap_die 'unsupported or mixed GPU vendor inventory; review package selection before installation'
    return 1
  fi
}

bootstrap_install_plan() {
  bootstrap_log 'dry-run install plan:'
  bootstrap_log "  erase: $BOOTSTRAP_DISK_A_REAL and $BOOTSTRAP_DISK_B_REAL"
  bootstrap_log '  layout: 2 GiB independent ESP on each disk + equal mdadm RAID0 members'
  bootstrap_log "  stack: mdadm metadata 1.2 / ${RAID_CHUNK_KIB} KiB → LUKS2 Argon2id → XFS su=${RAID_CHUNK_KIB}k,sw=2"
  bootstrap_log '  boot: stable and LTS UKIs on both ESPs; keys are created but never enrolled'
  bootstrap_log "  host: ${HOST_PROFILE:-headless}; TuneD ${BOOTSTRAP_TUNED_PROFILE:-balanced}, ${BOOTSTRAP_GPU_DESCRIPTION:-pending GPU discovery}, Zsh and baseline tools"
  [[ ${HOST_PROFILE:-headless} != desktop ]] || bootstrap_log '  desktop opt-in: GNOME/GDM, tuned-ppd and host Steam/multilib'
  bootstrap_log '  recovery: interactive root maintenance password; remote root login forbidden'
}

bootstrap_create_partitions() {
  local disk esp_end="+${ESP_SIZE_MIB}MiB"
  for disk in "$BOOTSTRAP_DISK_A_REAL" "$BOOTSTRAP_DISK_B_REAL"; do
    bootstrap_run wipefs --all --force "$disk" || return 1
    bootstrap_run sgdisk --zap-all "$disk" || return 1
    bootstrap_run sgdisk --clear --new="1:1MiB:$esp_end" --typecode=1:EF00 --change-name=1:ESP \
      --new=2:0:0 --typecode=2:FD00 --change-name=2:root-raid "$disk" || return 1
  done
  bootstrap_run partprobe "$BOOTSTRAP_DISK_A_REAL" || return 1
  bootstrap_run partprobe "$BOOTSTRAP_DISK_B_REAL" || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    udevadm settle || return 1
    BOOTSTRAP_ESP_A=$(bootstrap_part_path "$BOOTSTRAP_DISK_A_REAL" 1) || return 1
    BOOTSTRAP_ESP_B=$(bootstrap_part_path "$BOOTSTRAP_DISK_B_REAL" 1) || return 1
    BOOTSTRAP_MEMBER_A=$(bootstrap_part_path "$BOOTSTRAP_DISK_A_REAL" 2) || return 1
    BOOTSTRAP_MEMBER_B=$(bootstrap_part_path "$BOOTSTRAP_DISK_B_REAL" 2) || return 1
    [[ -b $BOOTSTRAP_ESP_A && -b $BOOTSTRAP_ESP_B && -b $BOOTSTRAP_MEMBER_A && -b $BOOTSTRAP_MEMBER_B ]] \
      || bootstrap_die 'partition discovery failed after partitioning'
  fi
}

bootstrap_create_storage_stack() {
  local md_device="$RAID_DEVICE" payload_offset chunk_bytes
  bootstrap_run mkfs.fat -F 32 -n EFI-A "$BOOTSTRAP_ESP_A" || return 1
  bootstrap_run mkfs.fat -F 32 -n EFI-B "$BOOTSTRAP_ESP_B" || return 1
  bootstrap_run mdadm --create "$md_device" --level=0 --raid-devices=2 --metadata=1.2 --chunk="$RAID_CHUNK_KIB" \
    --run "$BOOTSTRAP_MEMBER_A" "$BOOTSTRAP_MEMBER_B" || return 1
  bootstrap_run udevadm settle || return 1
  # cryptsetup reads the passphrase directly from the terminal. It is never
  # accepted in configuration, arguments, process environments, or logs.
  bootstrap_run cryptsetup luksFormat --type luks2 --pbkdf argon2id --iter-time "$LUKS_ITER_TIME_MS" \
    --pbkdf-memory "$LUKS_MEMORY_KIB" --pbkdf-parallel "$LUKS_PARALLEL" "$md_device" || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    # Record KDF costs only, never salts, keys or the complete header metadata.
    BOOTSTRAP_ARGON2_RECORD=$(cryptsetup luksDump --dump-json-metadata "$md_device" | jq -c \
      '{pbkdf:"argon2id",keyslots:[.keyslots|to_entries[]|{slot:.key,kdf:(.value.kdf|{type,time,memory,cpus})}]}') || return 1
    payload_offset=$(LC_ALL=C cryptsetup luksDump "$md_device" | awk '$1 == "offset:" && $3 == "[bytes]" { print $2; exit }') || return 1
    [[ $payload_offset =~ ^[0-9]+$ ]] || { bootstrap_die 'could not determine the LUKS payload offset'; return 1; }
    chunk_bytes=$((RAID_CHUNK_KIB * 1024))
    ((payload_offset % chunk_bytes == 0)) || { bootstrap_die "LUKS payload offset $payload_offset is not aligned to $chunk_bytes bytes"; return 1; }
  fi
  bootstrap_run cryptsetup open "$md_device" "$CRYPT_NAME" || return 1
  bootstrap_run mkfs.xfs -f -d "su=${RAID_CHUNK_KIB}k,sw=2" "/dev/mapper/$CRYPT_NAME" || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    LUKS_UUID=$(blkid -s UUID -o value "$md_device") || return 1
    ROOT_UUID=$(blkid -s UUID -o value "/dev/mapper/$CRYPT_NAME") || return 1
    MD_UUID=$(mdadm --detail --export "$md_device" | awk -F= '$1 == "MD_UUID" { print $2; exit }') || return 1
    [[ -n $LUKS_UUID && -n $ROOT_UUID && -n $MD_UUID ]] || bootstrap_die 'failed to read new storage UUIDs'
  fi
}

bootstrap_mount_target() {
  bootstrap_run mount -o noatime "/dev/mapper/$CRYPT_NAME" "$BOOTSTRAP_TARGET" || return 1
  bootstrap_run install -d -m 0755 "$BOOTSTRAP_TARGET$PRIMARY_ESP_MOUNT" "$BOOTSTRAP_TARGET$SECONDARY_ESP_MOUNT" || return 1
  bootstrap_run mount "$BOOTSTRAP_ESP_A" "$BOOTSTRAP_TARGET$PRIMARY_ESP_MOUNT" || return 1
  bootstrap_run mount "$BOOTSTRAP_ESP_B" "$BOOTSTRAP_TARGET$SECONDARY_ESP_MOUNT" || return 1
}

bootstrap_create_luks_header_backup() {
  local backup_dir="$BOOTSTRAP_TARGET/etc/cryptsetup"
  bootstrap_run install -d -m 0700 "$backup_dir" || return 1
  bootstrap_run cryptsetup luksHeaderBackup "$RAID_DEVICE" --header-backup-file "$backup_dir/luks-$LUKS_UUID.header" || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    printf '%s\n' "$BOOTSTRAP_ARGON2_RECORD" > "$backup_dir/argon2id-parameters.json" || return 1
    chmod 0600 "$backup_dir/luks-$LUKS_UUID.header" "$backup_dir/argon2id-parameters.json" || return 1
  fi
  bootstrap_log 'copy the LUKS header backup to offline media before relying on this installation'
}

bootstrap_write_target_config() {
  local template_dir="$BOOTSTRAP_ROOT/templates/arch" crypttab_template
  crypttab_template="$template_dir/crypttab"
  [[ $BOOT_UNLOCK != tpm2-pin ]] || crypttab_template="$template_dir/crypttab-tpm2"
  bootstrap_run install -d -m 0755 "$BOOTSTRAP_TARGET/etc/kernel" "$BOOTSTRAP_TARGET/efi/EFI/Linux" "$BOOTSTRAP_TARGET/efi2/EFI/Linux" \
    "$BOOTSTRAP_TARGET/efi/EFI/BOOT" "$BOOTSTRAP_TARGET/efi2/EFI/BOOT" || return 1
  bootstrap_render_template "$template_dir/mkinitcpio.conf" "$BOOTSTRAP_TARGET/etc/mkinitcpio.conf" || return 1
  bootstrap_render_template "$crypttab_template" "$BOOTSTRAP_TARGET/etc/crypttab" || return 1
  # mkinitcpio's sd-encrypt hook consumes crypttab.initramfs and embeds it as
  # /etc/crypttab. Keep the runtime copy as well for audit and later rebuilds.
  bootstrap_render_template "$crypttab_template" "$BOOTSTRAP_TARGET/etc/crypttab.initramfs" || return 1
  bootstrap_render_template "$template_dir/kernel.cmdline" "$BOOTSTRAP_TARGET/etc/kernel/cmdline" || return 1
  bootstrap_render_template "$template_dir/kernel-recovery.cmdline" "$BOOTSTRAP_TARGET/etc/kernel/cmdline.recovery" || return 1
  bootstrap_render_template "$template_dir/linux.preset" "$BOOTSTRAP_TARGET/etc/mkinitcpio.d/linux.preset" || return 1
  bootstrap_render_template "$template_dir/linux-lts.preset" "$BOOTSTRAP_TARGET/etc/mkinitcpio.d/linux-lts.preset" || return 1
  bootstrap_run install -Dm 0644 "$template_dir/no-hibernation.conf" "$BOOTSTRAP_TARGET/etc/systemd/sleep.conf.d/10-no-hibernation.conf" || return 1
  if (( BOOTSTRAP_DRY_RUN )); then
    bootstrap_log "+ mdadm --detail --scan > $BOOTSTRAP_TARGET/etc/mdadm.conf"
    bootstrap_log "+ genfstab -U $BOOTSTRAP_TARGET > $BOOTSTRAP_TARGET/etc/fstab"
  else
    mdadm --detail --scan >"$BOOTSTRAP_TARGET/etc/mdadm.conf" || return 1
    genfstab -U "$BOOTSTRAP_TARGET" >"$BOOTSTRAP_TARGET/etc/fstab" || return 1
    if [[ $HOST_PROFILE == desktop ]]; then
      sed -i '/^#\[multilib\]$/,/^#Include/ s/^#//' "$BOOTSTRAP_TARGET/etc/pacman.conf" || return 1
    fi
    printf '%s\n' "$HOSTNAME" >"$BOOTSTRAP_TARGET/etc/hostname" || return 1
    sed -i "s/^#${LOCALE} UTF-8$/${LOCALE} UTF-8/" "$BOOTSTRAP_TARGET/etc/locale.gen" || return 1
    printf 'LANG=%s\n' "$LOCALE" >"$BOOTSTRAP_TARGET/etc/locale.conf" || return 1
    printf 'KEYMAP=%s\n' "$KEYMAP" >"$BOOTSTRAP_TARGET/etc/vconsole.conf" || return 1
    if [[ -f /etc/arch-workstation-iso/release.lock ]]; then
      # Preserve the live ISO's coherent snapshot for pacstrap and the later
      # chroot transactions; never fall back to rolling mirrors mid-install.
      install -m0644 /etc/pacman.d/mirrorlist "$BOOTSTRAP_TARGET/etc/pacman.d/mirrorlist" || return 1
      install -Dm0644 /etc/arch-workstation-iso/release.lock "$BOOTSTRAP_TARGET/etc/arch-workstation/release.lock" || return 1
    fi
    if [[ ${VM_TEST_MODE:-false} == true ]]; then
      printf 'QEMU STORAGE/BOOT TEST ONLY; physical GPU and TPM validation pending\n' > "$BOOTSTRAP_TARGET/etc/arch-workstation-vm-test" || return 1
    fi
  fi
}

bootstrap_chroot() { bootstrap_run arch-chroot "$BOOTSTRAP_TARGET" "$@"; }

bootstrap_configure_offline_policy() {
  bootstrap_select_host_profile || return 1
  if ((BOOTSTRAP_DRY_RUN)); then
    bootstrap_log "+ persist TuneD profile $BOOTSTRAP_TUNED_PROFILE in manual mode (activated only after boot)"
    bootstrap_log '+ persist sshd PermitRootLogin no (sshd remains opt-in)'
    return 0
  fi
  [[ -f "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/$BOOTSTRAP_TUNED_PROFILE/tuned.conf" ]] \
    || { bootstrap_die "installed TuneD profile is missing: $BOOTSTRAP_TUNED_PROFILE"; return 1; }
  install -d -m0755 "$BOOTSTRAP_TARGET/etc/tuned" "$BOOTSTRAP_TARGET/etc/ssh/sshd_config.d" \
    "$BOOTSTRAP_TARGET/etc/arch-workstation" || return 1
  printf '%s\n' "$BOOTSTRAP_TUNED_PROFILE" > "$BOOTSTRAP_TARGET/etc/tuned/active_profile" || return 1
  printf 'manual\n' > "$BOOTSTRAP_TARGET/etc/tuned/profile_mode" || return 1
  printf 'PermitRootLogin no\n' > "$BOOTSTRAP_TARGET/etc/ssh/sshd_config.d/00-arch-workstation-root.conf" || return 1
  printf 'HOST_PROFILE=%s\n' "$HOST_PROFILE" > "$BOOTSTRAP_TARGET/etc/arch-workstation/host-profile.conf" || return 1
  chmod 0644 "$BOOTSTRAP_TARGET/etc/tuned/active_profile" "$BOOTSTRAP_TARGET/etc/tuned/profile_mode" \
    "$BOOTSTRAP_TARGET/etc/ssh/sshd_config.d/00-arch-workstation-root.conf" \
    "$BOOTSTRAP_TARGET/etc/arch-workstation/host-profile.conf" || return 1
}

bootstrap_set_passwords() {
  if ((BOOTSTRAP_DRY_RUN)); then
    bootstrap_log '+ interactively set the local user and emergency root passwords'
    return 0
  fi
  bootstrap_require_tty || return 1
  bootstrap_log "set the password for $INSTALL_USER (never stored by bootstrap-arch)"
  arch-chroot "$BOOTSTRAP_TARGET" passwd "$INSTALL_USER" || return 1
  bootstrap_log 'set a strong root maintenance password for authenticated emergency recovery; root SSH login stays forbidden'
  arch-chroot "$BOOTSTRAP_TARGET" passwd root || return 1
  # passwd -S reports state only, never a credential or password hash.
  local state
  state=$(arch-chroot "$BOOTSTRAP_TARGET" passwd -S root) || return 1
  [[ $(awk '{print $2}' <<< "$state") == P ]] \
    || { bootstrap_die 'root recovery authentication is locked or has no password'; return 1; }
}

bootstrap_configure_system() {
  bootstrap_chroot locale-gen || return 1
  bootstrap_chroot ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime || return 1
  bootstrap_chroot hwclock --systohc || return 1
  if [[ $HOST_PROFILE == desktop ]]; then
    # Avoid empty-array expansion under Bash 3.2 in VM test mode.
    bootstrap_chroot pacman -Syu --needed --noconfirm "${BOOTSTRAP_MULTILIB_PACKAGES[@]}" ${BOOTSTRAP_GPU_MULTILIB[@]+"${BOOTSTRAP_GPU_MULTILIB[@]}"} || return 1
  fi
  bootstrap_chroot useradd -m -G wheel -s /usr/bin/zsh "$INSTALL_USER" || return 1
  bootstrap_chroot sed -i 's/^# \(%wheel ALL=(ALL:ALL) ALL\)$/\1/' /etc/sudoers || return 1
  bootstrap_configure_offline_policy || return 1
  bootstrap_chroot systemctl enable NetworkManager.service tuned.service irqbalance.service fstrim.timer systemd-timesyncd.service || return 1
  if [[ $HOST_PROFILE == desktop ]]; then
    bootstrap_chroot systemctl enable gdm.service tuned-ppd.service || return 1
    bootstrap_chroot systemctl set-default graphical.target || return 1
  else
    bootstrap_chroot systemctl set-default multi-user.target || return 1
  fi
  bootstrap_chroot systemctl disable NetworkManager-wait-online.service || return 1
  if [[ $ENABLE_SSH == true ]]; then
    bootstrap_chroot systemctl enable sshd.service || return 1
  fi
  if [[ $ENABLE_BLUETOOTH == true ]]; then
    bootstrap_chroot pacman -S --needed --noconfirm bluez || return 1
    bootstrap_chroot systemctl enable bluetooth.service || return 1
  fi
  if [[ $ENABLE_PRINTING == true ]]; then
    bootstrap_chroot pacman -S --needed --noconfirm cups || return 1
    bootstrap_chroot systemctl enable cups.service || return 1
  fi
  bootstrap_set_passwords || return 1
  # This creates local signing material only. Firmware Setup Mode and key
  # enrollment remain a manual, audited post-install task.
  bootstrap_chroot sbctl create-keys || return 1
  bootstrap_chroot mkinitcpio -P || return 1
  bootstrap_copy_and_sign_ukis
}

bootstrap_copy_and_sign_ukis() {
  local uki source target
  if (( ! BOOTSTRAP_DRY_RUN )); then common::validate_esp_pair "$BOOTSTRAP_TARGET" || return 1; fi
  for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
    source="$BOOTSTRAP_TARGET/efi/EFI/Linux/$uki"
    target="$BOOTSTRAP_TARGET/efi2/EFI/Linux/$uki"
    bootstrap_run test -f "$source" || return 1
    bootstrap_chroot sbctl sign -s "/efi/EFI/Linux/$uki" || return 1
    bootstrap_run cp --preserve=mode,timestamps "$source" "$target" || return 1
  done
  bootstrap_run cp --preserve=mode,timestamps "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-recovery.efi" "$BOOTSTRAP_TARGET/efi/EFI/BOOT/BOOTX64.EFI" || return 1
  bootstrap_run cp --preserve=mode,timestamps "$BOOTSTRAP_TARGET/efi/EFI/Linux/arch-recovery.efi" "$BOOTSTRAP_TARGET/efi2/EFI/BOOT/BOOTX64.EFI"
}

bootstrap_bootnum_for_label() {
  local label=$1 partuuid loader
  case $label in
    *' backup)') partuuid=${COMMON_ESP_B_PARTUUID:?validate ESP identities first} ;;
    *) partuuid=${COMMON_ESP_A_PARTUUID:?validate ESP identities first} ;;
  esac
  case $label in
    'Arch Linux (stable)'|'Arch Linux (stable backup)') loader='\EFI\Linux\arch-linux.efi' ;;
    'Arch Linux (LTS)'|'Arch Linux (LTS backup)') loader='\EFI\Linux\arch-linux-lts.efi' ;;
    'Arch Linux (recovery)'|'Arch Linux (recovery backup)') loader='\EFI\Linux\arch-recovery.efi' ;;
    *) bootstrap_die 'unknown project UEFI label'; return 1 ;;
  esac
  common::bootnum_for_label "$label" "$partuuid" "$loader"
}

bootstrap_set_boot_order() {
  local labels=(
    'Arch Linux (stable)' 'Arch Linux (LTS)' 'Arch Linux (recovery)'
    'Arch Linux (stable backup)' 'Arch Linux (LTS backup)' 'Arch Linux (recovery backup)'
  ) label number order='' existing entry inventory
  ((BOOTSTRAP_DRY_RUN)) && { bootstrap_log '+ efibootmgr --bootorder <stable,LTS,recovery,backup entries>'; return 0; }
  common::validate_esp_pair "$BOOTSTRAP_TARGET" || return 1
  for label in "${labels[@]}"; do
    number=$(bootstrap_bootnum_for_label "$label") || return 1
    order+="${order:+,}$number"
  done
  inventory=$(efibootmgr -v) || { bootstrap_die 'firmware BootOrder inspection failed'; return 1; }
  existing=$(awk -F': ' '$1 == "BootOrder" {n++; value=toupper($2)} END {if(n != 1) exit 1; print value}' <<< "$inventory") || return 1
  [[ $existing =~ ^[A-F0-9]{4}(,[A-F0-9]{4})*$ ]] || { bootstrap_die 'firmware did not report a valid BootOrder'; return 1; }
  IFS=',' read -r -a BOOTSTRAP_EXISTING_BOOT_ORDER <<<"$existing"
  for entry in "${BOOTSTRAP_EXISTING_BOOT_ORDER[@]}"; do
    [[ ",$order," == *",$entry,"* ]] || order+=",$entry"
  done
  efibootmgr --bootorder "$order"
}

bootstrap_assert_boot_labels_absent() {
  local labels=(
    'Arch Linux (stable)' 'Arch Linux (LTS)' 'Arch Linux (recovery)'
    'Arch Linux (stable backup)' 'Arch Linux (LTS backup)' 'Arch Linux (recovery backup)'
  ) label inventory entries
  inventory=$(efibootmgr -v) || { bootstrap_die 'firmware entry inspection failed'; return 1; }
  common::validate_boot_inventory <<< "$inventory" || { bootstrap_die 'firmware entry inventory is malformed or empty'; return 1; }
  for label in "${labels[@]}"; do
    entries=$(common::boot_entries_for_label "$label" <<< "$inventory") || return 1
    if [[ -n $entries ]]; then
      bootstrap_die "UEFI label already exists; remove or rename it explicitly before installation: $label"
      return 1
    fi
  done
}

bootstrap_create_firmware_entries() {
  # NVRAM writes are deliberately post-UKI and never change Secure Boot mode.
  # Reject the complete set before the first create so a rerun cannot silently
  # accumulate duplicate direct-UEFI entries.
  bootstrap_assert_boot_labels_absent || return 1
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_B_REAL" --part 1 --label 'Arch Linux (LTS backup)' --loader '\EFI\Linux\arch-linux-lts.efi' || return 1
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_B_REAL" --part 1 --label 'Arch Linux (stable backup)' --loader '\EFI\Linux\arch-linux.efi' || return 1
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_B_REAL" --part 1 --label 'Arch Linux (recovery backup)' --loader '\EFI\Linux\arch-recovery.efi' || return 1
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_A_REAL" --part 1 --label 'Arch Linux (recovery)' --loader '\EFI\Linux\arch-recovery.efi' || return 1
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_A_REAL" --part 1 --label 'Arch Linux (LTS)' --loader '\EFI\Linux\arch-linux-lts.efi' || return 1
  # efibootmgr prepends newly created entries. Create stable last so it is the
  # preferred direct-UEFI path while preserving all recovery entries.
  bootstrap_run efibootmgr --create --disk "$BOOTSTRAP_DISK_A_REAL" --part 1 --label 'Arch Linux (stable)' --loader '\EFI\Linux\arch-linux.efi' || return 1
  bootstrap_set_boot_order
}

bootstrap_install() {
  BOOTSTRAP_TARGET=/mnt
  bootstrap_require_root || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    bootstrap_require_tty || return 1
  fi
  bootstrap_preflight || return 1
  bootstrap_assert_safe_target || return 1
  if (( ! BOOTSTRAP_DRY_RUN )); then
    command -v efibootmgr >/dev/null 2>&1 || { bootstrap_die 'efibootmgr is required before destructive installation'; return 1; }
    bootstrap_assert_boot_labels_absent || return 1
    bootstrap_verify_boot_package || return 1
  fi
  if ((BOOTSTRAP_DRY_RUN)); then
    bootstrap_install_plan
    return 0
  fi
  bootstrap_confirm_destruction || return 1
  bootstrap_create_partitions || return 1
  bootstrap_create_storage_stack || return 1
  bootstrap_mount_target || return 1
  bootstrap_create_luks_header_backup || return 1
  bootstrap_run pacstrap -K "$BOOTSTRAP_TARGET" "${BOOTSTRAP_BASE_PACKAGES[@]}" \
    ${BOOTSTRAP_PROFILE_PACKAGES[@]+"${BOOTSTRAP_PROFILE_PACKAGES[@]}"} ${BOOTSTRAP_GPU_PACKAGES[@]+"${BOOTSTRAP_GPU_PACKAGES[@]}"} || return 1
  bootstrap_write_target_config || return 1
  bootstrap_configure_system || return 1
  bootstrap_install_boot_package || return 1
  bootstrap_create_firmware_entries || return 1
  bootstrap_log 'install complete. Do not enable Secure Boot until you have exported firmware keys and manually enrolled the new owner keys.'
}
