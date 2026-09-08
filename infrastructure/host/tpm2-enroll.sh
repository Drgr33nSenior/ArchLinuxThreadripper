#!/usr/bin/env bash
# Header mutation is opt-in. PINs and recovery passphrases stay in cryptenroll.
set -euo pipefail
export LC_ALL=C
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$repo_root/lib/common.sh"

usage() {
  printf '%s\n' 'Usage: tpm2-enroll.sh <LUKS-UUID> <early-boot-PCR11-SHA256> [--execute]'
  printf '%s\n' 'Default: print the enrollment plan. --execute requires a terminal and recovery confirmation.'
}

main() {
  [[ ${1:-} != --help ]] || { usage; return; }
  (($# == 2 || $# == 3)) || { usage >&2; return 2; }
  local uuid=$1 pcr11=$2 mode=${3:---dry-run} device resolved response
  [[ $uuid =~ ^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$ ]] || common::die 'invalid LUKS UUID'
  [[ $pcr11 =~ ^[a-fA-F0-9]{64}$ && $pcr11 != 0000000000000000000000000000000000000000000000000000000000000000 ]] || common::die 'provide the predicted early-unlock PCR 11 SHA256 digest, not the post-login value'
  [[ $mode == --dry-run || $mode == --execute ]] || common::die 'unsupported execution mode'
  device="/dev/disk/by-uuid/$uuid"
  # PCRs 0+7+11, with only PCR 11 supplied as the predicted early-boot value.
  local args=(systemd-cryptenroll --tpm2-device=auto --tpm2-with-pin=yes
    "--tpm2-pcrs=0:sha256+7:sha256+11:sha256=$pcr11" "$device")
  if [[ $mode == --dry-run ]]; then common::print_command "${args[@]}"; return; fi
  common::require_root
  common::require_linux
  [[ -t 0 && -t 1 ]] || common::die 'enrollment requires a local terminal'
  [[ -b $device ]] || common::die 'LUKS UUID does not resolve to a block device'
  resolved=$(readlink -f -- "$device")
  [[ $(cryptsetup luksUUID "$resolved") == "$uuid" ]] || common::die 'LUKS identity mismatch'
  cryptsetup luksDump --dump-json-metadata "$resolved" | jq -e '
    (.keyslots|length)>0 and all(.tokens[]?; .type != "systemd-tpm2")
  ' >/dev/null || common::die 'not LUKS2, no recovery keyslot, or a TPM token already exists; inspect manually without wiping slots'
  systemd-analyze has-tpm2 >/dev/null || common::die 'TPM2 support is incomplete'
  local efivars=/sys/firmware/efi/efivars guid=8be4df61-93ca-11d2-aa0d-00e098032b8c
  [[ $(od -An -j4 -N1 -tu1 "$efivars/SecureBoot-$guid" | tr -d ' ') == 1 \
    && $(od -An -j4 -N1 -tu1 "$efivars/SetupMode-$guid" | tr -d ' ') == 0 ]] \
    || common::die 'Secure Boot must be enabled and firmware must not be in Setup Mode'
  # Require the real SPI TPM, enabled Secure Boot and a known early-boot digest.
  # Hardware identity and a tested recovery path cannot be inferred from auto.
  printf 'Confirm SPI TPM selected, Secure Boot enabled, tested recovery passphrase, offline header backup, and reviewed early-PCR11 prediction.\nType: ENROLL %s %s\n> ' "$resolved" "$uuid" >&2
  IFS= read -r response
  [[ $response == "ENROLL $resolved $uuid" ]] || common::die 'confirmation did not match'
  [[ $(readlink -f -- "$device") == "$resolved" && $(cryptsetup luksUUID "$resolved") == "$uuid" ]] || common::die 'device identity changed'
  "${args[@]}" || common::die 'TPM enrollment failed; retain the existing recovery slots and inspect locally'
  common::info 'Enrollment finished. Keep all recovery slots; back up the new header and test cold boot manually.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
