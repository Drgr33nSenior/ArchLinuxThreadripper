#!/usr/bin/env bash
# Linux USB writer fixtures. Only OS adapters are mocked; the target "device"
# is a regular temporary file. No real disk is inspected, opened or written.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=infrastructure/iso/usb.sh
source "$root/infrastructure/iso/usb.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
iso="$work/iso dir/arch-workstation-test.iso"
mkdir -p "${iso%/*}"
head -c 8192 /dev/urandom >"$iso"
printf '%s  %s\n' "$(common::sha256_file "$iso")" "${iso##*/}" >"${iso%/*}/SHA256SUMS"
target="$work/target-medium"
: >"$target"
fixture_path=/dev/disk/by-id/usb-Fixture_Drive_000123-0:0
USB_OS=linux
phase=normal
usb_platform() { USB_OS=linux; }
common::require_root() { :; }
usb_size() { wc -c <"$1" | tr -d ' '; }
usb_lock_path() { printf '%s/lock\n' "$work"; }
usb_linux_is_link() { [[ $1 == "$fixture_path" ]]; }
usb_linux_is_block() { [[ $1 == /dev/sdb ]]; }
usb_linux_resolve() {
  case $1 in
    "$fixture_path") printf '/dev/sdb\n' ;;
    *) return 1 ;;
  esac
}
usb_linux_lsblk() {
  local mount=null ro=false tran='"usb"' size=1000204885504 sector=512
  case $phase in
    mounted) mount='"/media/fixture"' ;;
    read-only) ro=true ;;
    sata) tran='"sata"' ;;
    small) size=4096 ;;
    unaligned) sector=3000 ;;
  esac
  jq -cn --argjson mount "$mount" --argjson ro "$ro" --argjson tran "$tran" --argjson size "$size" --argjson sector "$sector" \
    '{blockdevices:[{name:"sdb",path:"/dev/sdb",type:"disk",tran:$tran,size:$size,ro:$ro,"log-sec":$sector,
      model:"ST1000LM035-1RK172",serial:"C120D4Y3",mountpoints:[null],
      children:[{name:"sdb1",path:"/dev/sdb1",type:"part",size:4096,ro:false,mountpoints:[$mount]}]}]}'
}
usb_linux_udev() {
  local serial=Fixture_Drive_000123-0:0
  [[ $phase != wrong-serial ]] || serial=Other_Drive_999-0:0
  [[ ! -f $work/replugged ]] || printf 'DEVPATH=/devices/replugged\n'
  [[ -f $work/replugged ]] || printf 'DEVPATH=/devices/pci0000:00/usb2/2-1/block/sdb\n'
  printf 'ID_BUS=usb\nID_SERIAL=%s\nID_MODEL=Mobile_Drive\n' "$serial"
}
usb_linux_holders() { [[ $phase != held ]] || printf '/sys/class/block/sdb/sdb1/holders/dm-3\n'; }
usb_linux_swaps() { [[ $phase != swap ]] || printf '/dev/sdb2\n'; }
usb_linux_ancestors() {
  case $1 in
    /dev/mapper/cryptroot) [[ $phase == root-disk ]] && printf 'cryptroot\nmd0\nsdb2\nsdb\n' || printf 'cryptroot\nmd0\nnvme0n1p2\nnvme0n1\n' ;;
    /dev/sdb1) printf 'sdb1\nsdb\n' ;;
  esac
}
usb_linux_root_source() { printf '/dev/mapper/cryptroot\n'; }
usb_linux_source_device() { [[ $phase == iso-on-target ]] && printf '/dev/sdb1\n' || printf '/dev/mapper/cryptroot\n'; }
usb_linux_list() { printf '%s\n' "$fixture_path"; }
usb_linux_open() { exec 9>"$target"; printf 'open\n' >>"$work/actions"; }
usb_linux_copy() { dd if="$1" bs=4096 >&9 2>/dev/null; }
usb_linux_flush() { printf 'flush %s\n' "$1" >>"$work/actions"; }
usb_linux_compare() {
  [[ $phase != corrupt ]] || printf 'x' | dd of="$target" bs=1 seek=100 conv=notrunc 2>/dev/null
  head -c "$1" "$3" >/dev/null
  head -c "$1" "$target" | cmp -- "$2" -
}
usb_confirm() {
  printf 'confirm %s\n' "$1" >>"$work/actions"
  [[ $1 == "ERASE $fixture_path 1000204885504 "* ]] || return 1
  [[ $phase != replug ]] || touch "$work/replugged"
  [[ $phase != cancel ]] || common::die 'fixture confirmation cancelled'
}
reset() { : >"$target"; rm -f "$work/actions" "$work/replugged"; rmdir "$work/lock" 2>/dev/null || true; }
expect_refusal() {
  local name=$1
  shift
  reset
  if (phase=$name; "$@") >"$work/out" 2>&1; then
    printf 'FAIL: %s was not refused\n' "$name" >&2
    exit 1
  fi
  [[ ! -s $target ]] || { printf 'FAIL: %s wrote the target\n' "$name" >&2; exit 1; }
}

# Bare kernel names, partitions and non-by-id paths are refused before inspection.
for bad in /dev/sdb /dev/disk/by-id/usb-Fixture_Drive_000123-0:0-part1 /dev/disk/by-id/ata-Disk /dev/disk/by-path/usb-x; do
  reset
  if usb_linux_write "$iso" "$bad" false >"$work/out" 2>&1; then printf 'FAIL: %s accepted\n' "$bad" >&2; exit 1; fi
done
# Unsafe media and placement are refused in preview and execute mode alike.
for unsafe in mounted read-only sata small unaligned held swap root-disk wrong-serial iso-on-target; do
  expect_refusal "$unsafe" usb_linux_write "$iso" "$fixture_path" true
done
# Preview reads identity only.
reset
usb_linux_write "$iso" "$fixture_path" false >"$work/out" 2>&1
grep -q 'Preview only' "$work/out"
grep -q '"disk_serial":"C120D4Y3"' "$work/out"
[[ ! -e $work/actions && ! -s $target ]]
# Execute writes, flushes and verifies the exact ISO bytes after typed confirmation.
reset
usb_linux_write "$iso" "$fixture_path" true >"$work/out" 2>&1 || { cat "$work/out" >&2; exit 1; }
cmp -- "$iso" "$target"
grep -q "confirm ERASE $fixture_path 1000204885504 " "$work/actions"
grep -q '^flush /dev/sdb$' "$work/actions"
grep -q 'USB bytes verified' "$work/out"
[[ ! -e $work/lock ]]
# Cancelled confirmation, replugged media and readback corruption never report success.
expect_refusal cancel usb_linux_write "$iso" "$fixture_path" true
reset
if (phase=replug; usb_linux_write "$iso" "$fixture_path" true) >"$work/out" 2>&1; then echo 'FAIL: replug accepted' >&2; exit 1; fi
[[ ! -s $target ]]
reset
if (phase=corrupt; usb_linux_write "$iso" "$fixture_path" true) >"$work/out" 2>&1; then echo 'FAIL: corrupt readback accepted' >&2; exit 1; fi
grep -q 'readback mismatch' "$work/out"
# An existing lock blocks a second writer.
reset
mkdir "$work/lock"
if usb_linux_write "$iso" "$fixture_path" true >"$work/out" 2>&1; then echo 'FAIL: lock ignored' >&2; exit 1; fi
rmdir "$work/lock"
# Listing names stable paths and is not a selection.
usb_list >"$work/list"
grep -q "^$fixture_path	/dev/sdb	ST1000LM035-1RK172	C120D4Y3	1000204885504	usb	writable$" "$work/list"
printf 'Linux USB writer safety fixtures passed; no real device operations ran\n'
