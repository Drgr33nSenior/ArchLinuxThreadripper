#!/usr/bin/env bash
# Exercise install.conf drafting with synthetic disks only. No host block
# device, udev database or mount table is queried.
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/bin/bootstrap-arch"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# Synthetic inventory: device|size|removable|serial|check-failure.
inventory=$'/dev/nvme0n1|2000398934016|0|S7A0000001|\n/dev/nvme1n1|2000398934016|0|S7A0000002|\n/dev/nvme2n1|2000398934016|1|USB0000003|\n/dev/nvme3n1|1000204886016|0|S7B0000004|disk or a child partition is mounted: /dev/nvme3n1'
links=$'/dev/disk/by-id/nvme-eui.0001\n/dev/disk/by-id/nvme-Model_S7A0000001\n/dev/disk/by-id/nvme-Model_S7A0000001_1\n/dev/disk/by-id/nvme-eui.0002\n/dev/disk/by-id/nvme-Model_S7A0000002\n/dev/disk/by-id/nvme-USB_USB0000003\n/dev/disk/by-id/nvme-Model_S7B0000004'

field() { awk -F '|' -v device="$1" -v column="$2" '$1 == device { print $column }' <<<"$inventory"; }
# The functions below replace host probes called by the sourced discover code.
# shellcheck disable=SC2329
bootstrap_require_root() { :; }
# shellcheck disable=SC2329
bootstrap_discover_links() { printf '%s\n' "$links"; }
# shellcheck disable=SC2329
bootstrap_disk_realpath() {
  case $1 in
    *eui.0001 | *S7A0000001*) printf '/dev/nvme0n1\n' ;;
    *eui.0002 | *S7A0000002*) printf '/dev/nvme1n1\n' ;;
    *USB0000003*) printf '/dev/nvme2n1\n' ;;
    *S7B0000004*) printf '/dev/nvme3n1\n' ;;
    *) return 1 ;;
  esac
}
# shellcheck disable=SC2329
bootstrap_check_disk() {
  local real failure
  real=$(bootstrap_disk_realpath "$1")
  failure=$(field "$real" 5)
  [[ -z $failure ]] || {
    bootstrap_die "$failure"
    return 1
  }
  # Mirrors the production check's outputs.
  # shellcheck disable=SC2034
  BOOTSTRAP_DISK_REAL=$real
  BOOTSTRAP_DISK_SIZE=$(field "$real" 2)
}
# shellcheck disable=SC2329
lsblk() {
  [[ $1 == -dnro && $2 == RM ]] || return 2
  field "$3" 3
}
# The real serial parser reads these synthetic udev properties.
# shellcheck disable=SC2329
udevadm() {
  [[ $1 == info && $2 == --query=property ]] || return 2
  printf 'ID_SERIAL_SHORT=%s\n' "$(field "${3#--name=}" 4)"
}

# Exactly two equal-size eligible disks: keys are filled with model/serial links.
bootstrap_discover >"$work/draft.conf" 2>"$work/stderr"
grep -Fqx 'PRIMARY_DISK=/dev/disk/by-id/nvme-Model_S7A0000001' "$work/draft.conf"
grep -Fqx 'SECONDARY_DISK=/dev/disk/by-id/nvme-Model_S7A0000002' "$work/draft.conf"
grep -Fqx 'PRIMARY_DISK_SERIAL=S7A0000001' "$work/draft.conf"
grep -Fqx 'SECONDARY_DISK_SERIAL=S7A0000002' "$work/draft.conf"
grep -Fq 'excluded /dev/disk/by-id/nvme-USB_USB0000003 -> /dev/nvme2n1: removable' "$work/draft.conf"
grep -Fq 'excluded /dev/disk/by-id/nvme-Model_S7B0000004 -> /dev/nvme3n1: disk or a child partition is mounted' "$work/draft.conf"
if grep -Eq 'nvme-eui|_1$' "$work/draft.conf"; then exit 1; fi
[[ ! -s $work/stderr ]]
# Active keys come only from the template and the draft passes the real parser.
diff <(grep -v '^#' "$repo_root/config/install.conf.example" | grep -v '_DISK' | grep -v '^$') \
  <(grep -v '^#' "$work/draft.conf" | grep -v '_DISK' | grep -v '^$')
(
  bootstrap_load_config "$work/draft.conf"
  [[ $PRIMARY_DISK == /dev/disk/by-id/nvme-Model_S7A0000001 && $SECONDARY_DISK_SERIAL == S7A0000002 ]]
)

# Three eligible disks: list candidates, leave the keys blank, preflight refuses.
inventory=${inventory/|1|USB0000003|/|0|USB0000003|}
bootstrap_discover >"$work/blank.conf" 2>"$work/stderr"
for key in PRIMARY_DISK SECONDARY_DISK PRIMARY_DISK_SERIAL SECONDARY_DISK_SERIAL; do
  grep -Fqx "$key=" "$work/blank.conf"
done
grep -Fq 'found 3 eligible NVMe disk(s)' "$work/blank.conf"
grep -Fq '/dev/disk/by-id/nvme-USB_USB0000003 -> /dev/nvme2n1 serial=USB0000003' "$work/blank.conf"
grep -Fq 'disk keys left blank' "$work/stderr"
if (bootstrap_load_config "$work/blank.conf") 2>/dev/null; then exit 1; fi

# Two eligible disks of different sizes are not a RAID0 pair.
inventory=${inventory/|0|USB0000003|/|1|USB0000003|}
inventory=${inventory/1000204886016|0|S7B0000004|disk or a child partition is mounted: \/dev\/nvme3n1/1000204886016|0|S7B0000004|}
inventory=${inventory/\/dev\/nvme1n1|2000398934016|0|S7A0000002|/\/dev\/nvme1n1|2000398934016|0|S7A0000002|failed}
bootstrap_discover >"$work/sizes.conf" 2>/dev/null
grep -Fqx 'PRIMARY_DISK=' "$work/sizes.conf"
grep -Fq 'found 2 eligible NVMe disk(s) with 2 distinct size(s)' "$work/sizes.conf"

# No eligible disks at all.
links=''
bootstrap_discover >"$work/none.conf" 2>/dev/null
grep -Fq 'no /dev/disk/by-id/nvme-* whole disks were found' "$work/none.conf"
grep -Fqx 'PRIMARY_DISK_SERIAL=' "$work/none.conf"

# --output creates a new private file and never overwrites.
bootstrap_discover "$work/new.conf" 2>/dev/null
[[ $(stat -c %a "$work/new.conf" 2>/dev/null || stat -f %Lp "$work/new.conf") == 600 ]]
printf 'owner data\n' >"$work/existing.conf"
if bootstrap_discover "$work/existing.conf" 2>"$work/stderr"; then exit 1; fi
grep -Fq 'refusing to overwrite' "$work/stderr"
[[ $(<"$work/existing.conf") == 'owner data' ]]
ln -s "$work/missing-target" "$work/link.conf"
if bootstrap_discover "$work/link.conf" 2>/dev/null; then exit 1; fi
[[ ! -e $work/missing-target ]]

# The CLI keeps discover separate from configured, mode-bearing actions.
# shellcheck disable=SC2329
bootstrap_load_config() { printf 'config loaded\n' >"$work/loaded"; }
for arguments in '--config x discover' '--execute discover' '--dry-run discover' '--events discover' \
  '--output y verify' '--config x --output y preflight'; do
  IFS=' ' read -r -a argv <<<"$arguments"
  status=0
  main "${argv[@]}" >/dev/null 2>&1 || status=$?
  [[ $status == 2 ]] || {
    printf 'unexpected status %s for: %s\n' "$status" "$arguments" >&2
    exit 1
  }
done
[[ ! -e $work/loaded ]]
main --output "$work/cli.conf" discover 2>/dev/null
grep -Fq 'PRIMARY_DISK=' "$work/cli.conf"
echo 'Bootstrap discover drafting tests passed (synthetic disks only)'
