#!/usr/bin/env bash

readonly -a BOOTSTRAP_CONFIG_KEYS=(
  HOSTNAME USERNAME LOCALE KEYMAP TIMEZONE PRIMARY_DISK SECONDARY_DISK
  PRIMARY_DISK_SERIAL SECONDARY_DISK_SERIAL RAID_NAME LUKS_NAME ESP_SIZE_MIB
  RAID_CHUNK_KIB LUKS_ITER_TIME_MS LUKS_MEMORY_KIB LUKS_PARALLEL ALLOW_DISCARDS
  PRIMARY_ESP_MOUNT SECONDARY_ESP_MOUNT ENABLE_SSH ENABLE_BLUETOOTH ENABLE_PRINTING
  RAID_DEVICE BOOT_UNLOCK VM_TEST_MODE HOST_PROFILE TUNED_PROFILE
)

bootstrap_config_allowed() {
  local key=$1 candidate
  for candidate in "${BOOTSTRAP_CONFIG_KEYS[@]}"; do [[ $candidate == "$key" ]] && return 0; done
  return 1
}

bootstrap_config_value_safe() {
  local value=$1
  [[ $value =~ ^[A-Za-z0-9._/@:+-]+$ ]] || return 1
  # shellcheck disable=SC2016
  [[ $value != *'..'* && $value != *'$('* && $value != *'`'* ]] || return 1
}

bootstrap_load_config() {
  local file=$1 raw key value required seen_keys='' current_value
  [[ -r $file && -f $file ]] || { bootstrap_die "configuration is not a readable regular file: $file"; return 1; }
  for key in "${BOOTSTRAP_CONFIG_KEYS[@]}"; do unset "$key" 2>/dev/null || true; done
  while IFS= read -r raw || [[ -n $raw ]]; do
    [[ -z $raw || $raw == \#* ]] && continue
    [[ $raw == *=* ]] || { bootstrap_die "invalid config line: $raw"; return 1; }
    key=${raw%%=*}; value=${raw#*=}
    bootstrap_config_allowed "$key" || { bootstrap_die "unknown or forbidden config key: $key"; return 1; }
    [[ " $seen_keys " != *" $key "* ]] || { bootstrap_die "duplicate config key: $key"; return 1; }
    bootstrap_config_value_safe "$value" || { bootstrap_die "unsafe value for $key"; return 1; }
    printf -v "$key" '%s' "$value"
    seen_keys="$seen_keys $key"
  done <"$file"
  for required in "${BOOTSTRAP_CONFIG_KEYS[@]}"; do
    [[ $required != RAID_DEVICE && $required != BOOT_UNLOCK && $required != VM_TEST_MODE && $required != HOST_PROFILE && $required != TUNED_PROFILE ]] || continue
    [[ -n ${!required:-} ]] || { bootstrap_die "missing required config key: $required"; return 1; }
  done
  : "${RAID_DEVICE:=/dev/md/$RAID_NAME}" "${BOOT_UNLOCK:=fido2}"
  : "${VM_TEST_MODE:=false}"
  : "${HOST_PROFILE:=headless}"
  : "${TUNED_PROFILE:=auto}"
  case "$TUNED_PROFILE" in
    auto|balanced|desktop|throughput-performance|accelerator-performance|virtual-host) ;;
    *) bootstrap_die 'TUNED_PROFILE must be auto or a supported packaged TuneD profile'; return 1 ;;
  esac
  [[ $HOST_PROFILE == headless || $HOST_PROFILE == desktop ]] || { bootstrap_die 'HOST_PROFILE must be headless or desktop'; return 1; }
  [[ $VM_TEST_MODE == false || $VM_TEST_MODE == true ]] || { bootstrap_die 'VM_TEST_MODE must be true or false'; return 1; }
  [[ $RAID_DEVICE == "/dev/md/$RAID_NAME" || $RAID_DEVICE == /dev/md0 ]] \
    || { bootstrap_die 'RAID_DEVICE must be /dev/md0 or the configured named md device'; return 1; }
  [[ $BOOT_UNLOCK == fido2 || $BOOT_UNLOCK == tpm2-pin ]] \
    || { bootstrap_die 'BOOT_UNLOCK must be fido2 or tpm2-pin'; return 1; }
  [[ $HOSTNAME =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] || { bootstrap_die 'HOSTNAME is invalid'; return 1; }
  [[ $USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { bootstrap_die 'USERNAME is invalid'; return 1; }
  [[ $RAID_NAME =~ ^[A-Za-z0-9_.-]+$ && $LUKS_NAME =~ ^[A-Za-z0-9_.-]+$ ]] || { bootstrap_die 'RAID_NAME and LUKS_NAME are invalid'; return 1; }
  [[ $RAID_NAME != "$LUKS_NAME" ]] || { bootstrap_die 'RAID_NAME and LUKS_NAME must differ'; return 1; }
  [[ $ESP_SIZE_MIB == 2048 ]] || { bootstrap_die 'ESP_SIZE_MIB must remain 2048 for the approved layout'; return 1; }
  [[ $RAID_CHUNK_KIB == 512 ]] || { bootstrap_die 'RAID_CHUNK_KIB must remain 512 for the approved XFS geometry'; return 1; }
  [[ $LUKS_ITER_TIME_MS =~ ^[1-9][0-9]{0,4}$ && $LUKS_MEMORY_KIB =~ ^[1-9][0-9]{0,7}$ && $LUKS_PARALLEL =~ ^[1-4]$ ]] \
    || { bootstrap_die 'invalid Argon2id calibration limits'; return 1; }
  ((LUKS_ITER_TIME_MS >= 1000 && LUKS_ITER_TIME_MS <= 10000 && LUKS_MEMORY_KIB >= 262144 && LUKS_MEMORY_KIB <= 4194304)) \
    || { bootstrap_die 'Argon2id policy requires 1–10 seconds, 256 MiB–4 GiB and 1–4 lanes'; return 1; }
  [[ $ALLOW_DISCARDS == true ]] || { bootstrap_die 'ALLOW_DISCARDS must be true for the approved discard design'; return 1; }
  [[ $PRIMARY_ESP_MOUNT == /efi && $SECONDARY_ESP_MOUNT == /efi2 ]] \
    || { bootstrap_die 'the supported ESP mount points are /efi and /efi2'; return 1; }
  for key in ENABLE_SSH ENABLE_BLUETOOTH ENABLE_PRINTING; do
    current_value=${!key}
    [[ $current_value == true || $current_value == false ]] || { bootstrap_die "$key must be true or false"; return 1; }
  done
  # Compatibility aliases are private to the installer; config/install.conf is
  # the only public schema.
  BOOTSTRAP_DISK_A=$PRIMARY_DISK
  BOOTSTRAP_DISK_B=$SECONDARY_DISK
  INSTALL_USER=$USERNAME
  CRYPT_NAME=$LUKS_NAME
  # Values above are consumed by the separate preflight/install modules.
  # shellcheck disable=SC2034
  : "$BOOTSTRAP_DISK_A" "$BOOTSTRAP_DISK_B" "$INSTALL_USER" "$CRYPT_NAME"
}
