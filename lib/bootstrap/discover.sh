#!/usr/bin/env bash
# Read-only drafting of install.conf disk identities. It never writes a disk,
# never overwrites a file and never answers an installer prompt. The owner
# reviews the draft; preflight and dry-run remain the authoritative checks.

readonly -a BOOTSTRAP_DISCOVER_KEYS=(PRIMARY_DISK SECONDARY_DISK PRIMARY_DISK_SERIAL SECONDARY_DISK_SERIAL)

bootstrap_discover_links() {
  local link
  for link in /dev/disk/by-id/nvme-*; do
    [[ -L $link && $link != *-part[0-9]* ]] || continue
    printf '%s\n' "$link"
  done
}

# Prefer the model/serial link over EUI/NGUID links and namespace duplicates.
bootstrap_discover_link_rank() {
  local name=${1##*/}
  case $name in
    nvme-eui.* | nvme-nvme.* | nvme-uuid.*) printf '1\n' ;;
    *) printf '0\n' ;;
  esac
}

# Prints eligible disks as "size<TAB>device<TAB>by-id<TAB>serial" and excluded
# disks as "-<TAB>device<TAB>by-id<TAB>reason". Each device appears once.
bootstrap_discover_candidates() {
  local link real rank result size removable serial reason
  while IFS=$'\t' read -r real rank _ link; do
    [[ -n $real ]] || continue
    if ! result=$(bootstrap_check_disk "$link" 2>&1 >/dev/null && printf 'size=%s\n' "$BOOTSTRAP_DISK_SIZE"); then
      # bootstrap_assert_disk_unused also rejects active swap: lsblk reports it as [SWAP].
      reason=${result##*ERROR: }
      printf -- '-\t%s\t%s\t%s\n' "$real" "$link" "${reason:-disk inspection failed}"
      continue
    fi
    size=${result##*size=}
    removable=$(lsblk -dnro RM "$real" 2>/dev/null) || removable=unknown
    if [[ $removable != 0 ]]; then
      printf -- '-\t%s\t%s\t%s\n' "$real" "$link" "removable or unknown removable state: $removable"
      continue
    fi
    if ! serial=$(bootstrap_disk_serial "$real" 2>/dev/null) || ! bootstrap_config_value_safe "$serial" ||
      ! bootstrap_config_value_safe "$link"; then
      printf -- '-\t%s\t%s\t%s\n' "$real" "$link" 'serial or by-id path is unavailable or not a safe config value'
      continue
    fi
    printf '%s\t%s\t%s\t%s\n' "$size" "$real" "$link" "$serial"
  done < <(
    while IFS= read -r link; do
      real=$(bootstrap_disk_realpath "$link") || continue
      rank=$(bootstrap_discover_link_rank "$link")
      printf '%s\t%s\t%s\t%s\n' "$real" "$rank" "${#link}" "$link"
    done < <(bootstrap_discover_links) | sort -t $'\t' -k1,1 -k2,2n -k3,3n -k4,4 | awk -F '\t' '!seen[$1]++'
  )
}

bootstrap_discover_draft() {
  local template=$1 candidates=$2 eligible count sizes raw key fill=0 note=''
  local primary='' secondary='' primary_serial='' secondary_serial=''
  eligible=$(awk -F '\t' '$1 != "-"' <<<"$candidates" | sort -t $'\t' -k2,2V)
  count=$(awk 'NF { n++ } END { print n + 0 }' <<<"$eligible")
  sizes=$(awk -F '\t' 'NF { print $1 }' <<<"$eligible" | sort -u | awk 'NF { n++ } END { print n + 0 }')
  if [[ $count == 2 && $sizes == 1 ]]; then
    fill=1
    IFS=$'\t' read -r _ _ primary primary_serial < <(sed -n 1p <<<"$eligible")
    IFS=$'\t' read -r _ _ secondary secondary_serial < <(sed -n 2p <<<"$eligible")
  else
    note="found $count eligible NVMe disk(s) with $sizes distinct size(s); exactly two equal-size disks are required"
  fi
  printf '# Draft from bootstrap-arch discover (read-only); generated %s.\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '# Review every value. Preflight and dry-run remain mandatory; nothing was erased.\n'
  printf '# Eligible: whole, non-removable, unmounted NVMe disks without active swap or storage stacks.\n'
  awk -F '\t' 'NF && $1 != "-" { printf "#   %s -> %s serial=%s size=%s bytes\n", $3, $2, $4, $1 }' <<<"$eligible"
  awk -F '\t' '$1 == "-" { printf "#   excluded %s -> %s: %s\n", $3, $2, $4 }' <<<"$candidates"
  [[ -n $candidates ]] || printf '#   no /dev/disk/by-id/nvme-* whole disks were found\n'
  while IFS= read -r raw || [[ -n $raw ]]; do
    if [[ -z $raw || $raw == \#* ]]; then
      printf '%s\n' "$raw"
      continue
    fi
    key=${raw%%=*}
    bootstrap_config_allowed "$key" || {
      bootstrap_die "template contains a key outside the installer schema: $key"
      return 1
    }
    case $key in
      PRIMARY_DISK)
        ((fill)) || printf '# discover left the disk keys blank: %s.\n# Copy reviewed values from the candidates above; preflight refuses blank keys.\n' "$note"
        printf 'PRIMARY_DISK=%s\n' "$primary"
        ;;
      SECONDARY_DISK) printf 'SECONDARY_DISK=%s\n' "$secondary" ;;
      PRIMARY_DISK_SERIAL) printf 'PRIMARY_DISK_SERIAL=%s\n' "$primary_serial" ;;
      SECONDARY_DISK_SERIAL) printf 'SECONDARY_DISK_SERIAL=%s\n' "$secondary_serial" ;;
      *) printf '%s\n' "$raw" ;;
    esac
  done <"$template"
}

bootstrap_discover() {
  local output=${1:-} template="$BOOTSTRAP_ROOT/config/install.conf.example" candidates draft key
  bootstrap_require_root || return 1
  [[ -r $template && -f $template ]] || {
    bootstrap_die "installer configuration template is unavailable: $template"
    return 1
  }
  for key in "${BOOTSTRAP_DISCOVER_KEYS[@]}"; do
    [[ $(grep -c "^$key=" "$template") == 1 ]] || {
      bootstrap_die "template must define $key exactly once"
      return 1
    }
  done
  if [[ -n $output && (-e $output || -L $output) ]]; then
    bootstrap_die "refusing to overwrite an existing path: $output"
    return 1
  fi
  candidates=$(bootstrap_discover_candidates) || return 1
  draft=$(bootstrap_discover_draft "$template" "$candidates") || return 1
  if [[ $draft == *$'\nPRIMARY_DISK=\n'* ]]; then
    bootstrap_log 'disk keys left blank; review the listed candidates'
  fi
  if [[ -z $output ]]; then
    printf '%s\n' "$draft"
    return 0
  fi
  (
    umask 077
    set -o noclobber
    printf '%s\n' "$draft" >"$output"
  ) || {
    bootstrap_die "could not create the new draft file: $output"
    return 1
  }
  bootstrap_log "wrote draft configuration to $output; review it before preflight"
}
