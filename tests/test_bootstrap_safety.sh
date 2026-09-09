#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/bootstrap/common.sh"
source "$repo_root/lib/bootstrap/preflight.sh"
source "$repo_root/lib/bootstrap/install.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

# The real disk-check API accepts one argument. Reject this regular fixture
# before any block-device utility is reached, without a nounset exception.
bootstrap_validate_by_id_path() { :; }
bootstrap_disk_realpath() { printf '%s\n' "$work/ordinary-file"; }
printf 'synthetic non-device\n' >"$work/ordinary-file"
if (bootstrap_check_disk /dev/disk/by-id/nvme-SYNTHETIC) 2>"$work/refusal"; then exit 1; fi
grep -Fq 'does not resolve to a block device' "$work/refusal"

# Only synthetic identities are resolved. No host device is queried.
bootstrap_require_tty() { :; }
bootstrap_disk_realpath() {
  case $1 in
    /dev/disk/by-id/nvme-SYNTHETIC_A) printf '%s\n' /dev/synthetic-a ;;
    /dev/disk/by-id/nvme-SYNTHETIC_B) printf '%s\n' /dev/synthetic-b ;;
    *) return 1 ;;
  esac
}
bootstrap_disk_serial() {
  case $1 in
    /dev/synthetic-a) printf '%s\n' SYNTHETIC_A ;;
    /dev/synthetic-b) printf '%s\n' SYNTHETIC_B ;;
    *) return 1 ;;
  esac
}
PRIMARY_DISK=/dev/disk/by-id/nvme-SYNTHETIC_A
SECONDARY_DISK=/dev/disk/by-id/nvme-SYNTHETIC_B
PRIMARY_DISK_SERIAL=SYNTHETIC_A
SECONDARY_DISK_SERIAL=SYNTHETIC_B
BOOTSTRAP_DISK_A_REAL=/dev/synthetic-a
BOOTSTRAP_DISK_B_REAL=/dev/synthetic-b
BOOTSTRAP_DRY_RUN=0
confirmation='ERASE /dev/synthetic-a (SYNTHETIC_A) AND /dev/synthetic-b (SYNTHETIC_B)'
bootstrap_confirm_destruction <<<"$confirmation" 2>/dev/null
if bootstrap_confirm_destruction <<<'SYNTHETIC_A SYNTHETIC_B' 2>/dev/null; then exit 1; fi
if bootstrap_confirm_destruction </dev/null 2>/dev/null; then exit 1; fi
bootstrap_disk_realpath() { printf '%s\n' /dev/synthetic-changed; }
if bootstrap_confirm_destruction <<<"$confirmation" 2>/dev/null; then exit 1; fi
bootstrap_disk_realpath() {
  [[ $1 == "$PRIMARY_DISK" ]] && printf '%s\n' /dev/synthetic-a || printf '%s\n' /dev/synthetic-b
}
bootstrap_disk_serial() { printf '%s\n' SYNTHETIC_CHANGED; }
if bootstrap_confirm_destruction <<<"$confirmation" 2>/dev/null; then exit 1; fi

# Verify rejection propagates even when the installer is called from an `if`,
# where Bash otherwise suppresses errexit inside functions.
bootstrap_require_root() { :; }
bootstrap_preflight() { :; }
bootstrap_assert_safe_target() { :; }
bootstrap_assert_boot_labels_absent() { :; }
bootstrap_verify_boot_package() { :; }
efibootmgr() { :; }
bootstrap_confirm_destruction() { return 1; }
bootstrap_create_partitions() {
  printf 'reached\n' >"$work/destructive-reached"
  exit 1
}
if (bootstrap_install) >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/destructive-reached ]]
echo 'Resolved-device confirmation and preflight regression tests passed'
