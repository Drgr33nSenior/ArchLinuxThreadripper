#!/usr/bin/env bash

# Shared, side-effect-free helpers for the workstation provisioning commands.
# Callers choose strict-mode policy before sourcing this file.

common::info() {
  printf 'INFO: %s\n' "$*" >&2
}

common::warn() {
  printf 'WARN: %s\n' "$*" >&2
}

common::die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

common::require_command() {
  command -v "$1" >/dev/null 2>&1 || common::die "Required command is unavailable: $1"
}

common::require_root() {
  [ "$(id -u)" -eq 0 ] || common::die 'This operation requires root privileges.'
}

common::require_linux() {
  [ "$(uname -s)" = Linux ] || common::die 'This operation is supported only on Linux.'
}

common::is_true() {
  case ${1:-} in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

common::is_allowed_name() {
  common_name=$1
  shift
  for common_allowed in "$@"; do
    [ "$common_name" = "$common_allowed" ] && return 0
  done
  return 1
}

# Load a strict KEY=VALUE file without evaluating it as shell code. Values are
# literal and must stay on one line. The remaining arguments are the allowlist.
common::load_config() {
  common_file=$1
  shift
  [ -r "$common_file" ] || common::die "Configuration is not readable: $common_file"

  common_seen=''
  common_line_number=0
  while IFS= read -r common_line || [ -n "$common_line" ]; do
    common_line_number=$((common_line_number + 1))
    common_line=${common_line%$'\r'}
    case $common_line in
      '' | '#'*) continue ;;
    esac

    case $common_line in
      *=*) ;;
      *) common::die "Invalid configuration line $common_line_number in $common_file" ;;
    esac

    common_key=${common_line%%=*}
    common_value=${common_line#*=}
    case $common_key in
      '' | *[!A-Z0-9_]*) common::die "Invalid key on line $common_line_number in $common_file" ;;
    esac
    common::is_allowed_name "$common_key" "$@" || common::die "Unknown key in $common_file: $common_key"
    case "\n$common_seen" in
      *"\n$common_key\n"*) common::die "Duplicate key in $common_file: $common_key" ;;
    esac
    case $common_value in
      *$'\n'* | *$'\r'*) common::die "Control character in $common_key" ;;
    esac

    printf -v "$common_key" '%s' "$common_value"
    common_seen="${common_seen}${common_key}\n"
  done <"$common_file"
}

common::require_values() {
  for common_required_name in "$@"; do
    eval "common_required_value=\${$common_required_name-}"
    [ -n "$common_required_value" ] || common::die "Required configuration value is empty: $common_required_name"
  done
}

common::lock_get() {
  common_lock_file=$1
  common_lock_key=$2
  [ -r "$common_lock_file" ] || common::die "Lock file is not readable: $common_lock_file"
  common_lock_value=$(awk -F= -v wanted="$common_lock_key" '
    $0 !~ /^#/ && $1 == wanted { count++; value=substr($0, index($0, "=") + 1) }
    END { if (count != 1 || value == "") exit 1; print value }
  ' "$common_lock_file") || common::die "Missing or duplicate lock value: $common_lock_key"
  printf '%s\n' "$common_lock_value"
}

common::sha256_file() {
  common_hash_file=$1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$common_hash_file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$common_hash_file" | awk '{print $1}'
  else
    common::die 'No SHA-256 implementation is available.'
  fi
}

common::verify_sha256() {
  common_hash_file=$1
  common_expected_hash=$2
  [ -f "$common_hash_file" ] || common::die "File is unavailable for checksum verification: $common_hash_file"
  common_actual_hash=$(common::sha256_file "$common_hash_file")
  [ "$common_actual_hash" = "$common_expected_hash" ] || common::die "SHA-256 mismatch for $common_hash_file"
}

common::print_command() {
  printf ' +'
  for common_arg in "$@"; do
    printf ' %q' "$common_arg"
  done
  printf '\n'
}

common::run() {
  common::print_command "$@" >&2
  if ! common::is_true "${DRY_RUN:-false}"; then
    "$@"
  fi
}

common::confirm_exact() {
  common_expected_phrase=$1
  common_prompt=${2:-'Type the confirmation phrase'}
  [ -t 0 ] || common::die 'Interactive confirmation requires a terminal.'
  printf '%s [%s]: ' "$common_prompt" "$common_expected_phrase" >&2
  IFS= read -r common_actual_phrase
  [ "$common_actual_phrase" = "$common_expected_phrase" ] || common::die 'Confirmation did not match; no changes were made.'
}

# Modern efibootmgr separates the label from the device path with a tab. Keep
# matching literal (labels may contain regexp syntax) and retain the path for
# callers to check the GPT partition and loader, not just a human-readable name.
common::boot_entries_for_label() {
  awk -v expected="$1" '
    /^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][* ]/ {
      number=toupper(substr($0, 5, 4)); text=substr($0, 9)
      if (!sub(/^[* ] /, "", text)) sub(/^ /, "", text)
      separator=index(text, "\t")
      label=separator ? substr(text, 1, separator-1) : text
      path=separator ? substr(text, separator+1) : ""
      if (label == expected) printf "%s\t%s\n", number, path
    }
  '
}

common::validate_boot_inventory() {
  awk '
    /^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][* ] / {seen=1; next}
    /^Boot[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f] / {seen=1; next}
    /^(BootCurrent|BootNext): [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]$/ {seen=1; next}
    /^BootOrder: [0-9A-Fa-f,]+$/ {seen=1; next}
    /^Timeout: [0-9]+ seconds$/ {seen=1; next}
    /^Boot/ {invalid=1}
    END {if(!seen || invalid) exit 2}
  '
}

# Return 1 only for a genuinely absent label. Return 2 for failed/malformed
# inspection, duplicates or a wrong target so callers cannot create on error.
common::bootnum_for_label() {
  local label=$1 partuuid=${2:-} loader=${3:-} inventory entries count path
  inventory=$(LC_ALL=C efibootmgr -v) || {
    common::warn 'UEFI inventory failed'
    return 2
  }
  common::validate_boot_inventory <<<"$inventory" || {
    common::warn 'Malformed UEFI inventory'
    return 2
  }
  entries=$(common::boot_entries_for_label "$label" <<<"$inventory") || return 2
  count=$(awk 'NF {n++} END {print n+0}' <<<"$entries")
  [[ $count != 0 ]] || {
    common::warn "UEFI entry missing: $label"
    return 1
  }
  [[ $count == 1 ]] || {
    common::warn "UEFI entry not unique: $label"
    return 2
  }
  if [[ -n $partuuid || -n $loader ]]; then
    [[ -n $partuuid && -n $loader ]] || {
      common::warn 'UEFI validation requires partition and loader together'
      return 2
    }
    path=${entries#*$'\t'}
    # Device paths use case-insensitive FAT names and GPT GUIDs. Do not accept
    # a label-only record, a different partition, or a suffix-matching loader.
    path=$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')
    partuuid=$(printf '%s' "$partuuid" | tr '[:upper:]' '[:lower:]')
    loader=$(printf '%s' "$loader" | tr '[:upper:]' '[:lower:]')
    [[ $path == *"hd("*",gpt,$partuuid,"* && $path == *"/file($loader)"* ]] ||
      {
        common::warn "UEFI partition or loader mismatch: $label"
        return 2
      }
  fi
  printf '%s\n' "${entries%%$'\t'*}"
}

# Validate exact mounts against the installation's authoritative fstab. This
# rejects a parent filesystem, bind subdirectory, duplicate ESP, non-GPT ESP,
# wrong fstab source, or two ESPs on one disk. Inspection failure is not absence.
# Outputs the resolved identities in COMMON_ESP_{A,B}_{DEVICE,PARTUUID,DISK}.
common::validate_esp_pair() {
  local root=${1:-/} mountpoint record source fstype target fsroot extra expected
  local device kind parttype partuuid parent uuid parent_type prior_part='' prior_disk='' side=A
  unset COMMON_ESP_A_DEVICE COMMON_ESP_A_PARTUUID COMMON_ESP_A_DISK \
    COMMON_ESP_B_DEVICE COMMON_ESP_B_PARTUUID COMMON_ESP_B_DISK
  [[ $root == / ]] && root='' || root=${root%/}
  [[ -r $root/etc/fstab ]] || {
    common::warn 'Cannot read fstab for ESP identity validation'
    return 1
  }
  for mountpoint in /efi /efi2; do
    record=$(findmnt --noheadings --raw --mountpoint "$root$mountpoint" --output SOURCE,FSTYPE,TARGET,FSROOT) ||
      {
        common::warn "Cannot inspect exact ESP mount: $root$mountpoint"
        return 1
      }
    [[ -n $record && $record != *$'\n'* ]] || {
      common::warn 'ESP mount is absent or ambiguous'
      return 1
    }
    IFS=$' \t' read -r source fstype target fsroot extra <<<"$record"
    [[ -z $extra && $source == /dev/* && $fstype == vfat && $target == "$root$mountpoint" && $fsroot == / ]] ||
      {
        common::warn "Not an exact whole-filesystem vfat mount: $root$mountpoint"
        return 1
      }
    expected=$(awk -v wanted="$mountpoint" '
      $0 !~ /^[[:space:]]*#/ && $2 == wanted {n++; source=$1; type=$3}
      END {if(n != 1 || type != "vfat") exit 1; print source}
    ' "$root/etc/fstab") || {
      common::warn "Missing, duplicate or non-vfat fstab ESP: $mountpoint"
      return 1
    }
    device=$(readlink -f -- "$source") || return 1
    [[ $device == /dev/* && $device != *$'\n'* ]] || return 1
    record=$(lsblk --nodeps --noheadings --raw --output TYPE,PARTTYPE,PARTUUID,PKNAME "$device") ||
      {
        common::warn "ESP block-device inspection failed: $device"
        return 1
      }
    [[ -n $record && $record != *$'\n'* ]] || return 1
    IFS=$' \t' read -r kind parttype partuuid parent extra <<<"$record"
    parttype=$(printf '%s' "$parttype" | tr '[:upper:]' '[:lower:]')
    partuuid=$(printf '%s' "$partuuid" | tr '[:upper:]' '[:lower:]')
    [[ -z $extra && $kind == part && $parttype == c12a7328-f81f-11d2-ba4b-00a0c93ec93b &&
      $partuuid =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ &&
      $parent =~ ^[a-zA-Z0-9_.!-]+$ ]] || {
      common::warn "Not a GPT ESP partition: $device"
      return 1
    }
    parent=$(readlink -f -- "/dev/$parent") || return 1
    parent_type=$(lsblk --nodeps --noheadings --raw --output TYPE,PTTYPE "$parent") || return 1
    [[ $parent_type == 'disk gpt' ]] || {
      common::warn "ESP parent is not a GPT disk: $parent"
      return 1
    }
    case $expected in
      UUID=*)
        uuid=$(lsblk --nodeps --noheadings --raw --output UUID "$device") || return 1
        [[ -n $uuid && $uuid != *$'\n'* && $expected == "UUID=$uuid" ]] || {
          common::warn "ESP UUID differs from fstab: $mountpoint"
          return 1
        }
        ;;
      PARTUUID=*)
        expected=$(printf '%s' "$expected" | tr '[:upper:]' '[:lower:]')
        [[ $expected == "partuuid=$partuuid" ]] || {
          common::warn "ESP PARTUUID differs from fstab: $mountpoint"
          return 1
        }
        ;;
      *)
        common::warn "ESP fstab source must use UUID or PARTUUID: $mountpoint"
        return 1
        ;;
    esac
    [[ $partuuid != "$prior_part" && $parent != "$prior_disk" ]] ||
      {
        common::warn 'Backup ESP must be a distinct partition on another disk'
        return 1
      }
    printf -v "COMMON_ESP_${side}_DEVICE" '%s' "$device"
    printf -v "COMMON_ESP_${side}_PARTUUID" '%s' "$partuuid"
    printf -v "COMMON_ESP_${side}_DISK" '%s' "$parent"
    prior_part=$partuuid
    prior_disk=$parent
    side=B
  done
}
