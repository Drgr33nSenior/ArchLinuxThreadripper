#!/usr/bin/env bash
# Check documented command construction without keys, firmware or block devices.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

guidance_block() {
  awk -v anchor="<a id=\"$1\"></a>" '
    $0 == anchor { found++; waiting=1; next }
    waiting && $0 == "```sh" { waiting=0; reading=1; next }
    reading && $0 == "```" { reading=0; closed++; next }
    reading { print }
    END { if (found != 1 || closed != 1 || reading) exit 1 }
  ' "$root/docs/INSTALLATION.md"
}

for block in verify-owner-ukis tpm2-enrol-preview tpm2-enrol-execute installed-wifi-connect tpm2-prediction-inspect; do
  guidance_block "$block" >"$work/$block.sh" || {
    printf 'Missing or incomplete guidance block: %s\n' "$block" >&2
    exit 1
  }
  bash -n "$work/$block.sh"
done

# Exercise the documented preview against the production helper, not a summary.
export ENROLL_SCRIPT="$root/infrastructure/host/tpm2-enroll.sh"
export LUKS_UUID=11111111-2222-3333-4444-555555555555
export EARLY_PCR11_SHA256=1111111111111111111111111111111111111111111111111111111111111111
plan=$(bash "$work/tpm2-enrol-preview.sh")
[[ $plan == " + systemd-cryptenroll --tpm2-device=auto --tpm2-with-pin=yes --tpm2-pcrs=0:sha256+7:sha256+11:sha256=$EARLY_PCR11_SHA256 /dev/disk/by-uuid/$LUKS_UUID" ]]
if EARLY_PCR11_SHA256=REPLACE_WITH_REVIEWED_DIGEST bash "$work/tpm2-enrol-preview.sh" >"$work/refused" 2>&1; then exit 1; fi
if EARLY_PCR11_SHA256=0000000000000000000000000000000000000000000000000000000000000000 bash "$work/tpm2-enrol-preview.sh" >"$work/refused" 2>&1; then exit 1; fi

# No sudo executable, root privilege or real signature utility is invoked.
export GUIDANCE_LOG="$work/commands" GUIDANCE_FAIL_SIGNATURE='' GUIDANCE_FAIL_COMPARE=''
# Called only in the child shell that executes the selected documentation block.
# shellcheck disable=SC2329
sudo() {
  printf '%s\n' "$*" >>"$GUIDANCE_LOG"
  case $1 in
    sbverify)
      [[ $# == 4 && $2 == --cert && $3 == /var/lib/sbctl/keys/db/db.pem ]] || return 91
      case $4 in
        /efi/EFI/Linux/arch-linux.efi | /efi/EFI/Linux/arch-linux-lts.efi | /efi/EFI/Linux/arch-recovery.efi | \
          /efi2/EFI/Linux/arch-linux.efi | /efi2/EFI/Linux/arch-linux-lts.efi | /efi2/EFI/Linux/arch-recovery.efi | \
          /efi/EFI/BOOT/BOOTX64.EFI | /efi2/EFI/BOOT/BOOTX64.EFI) ;;
        *) return 92 ;;
      esac
      [[ $4 != "$GUIDANCE_FAIL_SIGNATURE" ]] || return 93
      ;;
    cmp)
      [[ $# == 3 ]] || return 94
      [[ $2 != "$GUIDANCE_FAIL_COMPARE" ]] || return 95
      ;;
    bash)
      [[ $# == 5 && $2 == "$ENROLL_SCRIPT" && $3 == "$LUKS_UUID" && $4 == "$EARLY_PCR11_SHA256" && $5 == --execute ]] || return 96
      ;;
    nmcli)
      [[ $# == 8 && $2 == --ask && $3 == device && $4 == wifi && $5 == connect && $6 == 'YOUR SSID' && $7 == ifname && $8 == wlan0 ]] || return 98
      ;;
    journalctl)
      [[ $# == 7 && $2 == -b && $3 == -u && $4 == systemd-pcrphase-initrd.service && $5 == --no-pager && $6 == -n && $7 == 20 ]] || return 99
      ;;
    *) return 97 ;;
  esac
}
export -f sudo
bash "$work/verify-owner-ukis.sh"
for esp in /efi /efi2; do
  for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
    grep -Fqx "sbverify --cert /var/lib/sbctl/keys/db/db.pem $esp/EFI/Linux/$uki" "$GUIDANCE_LOG"
  done
  grep -Fqx "sbverify --cert /var/lib/sbctl/keys/db/db.pem $esp/EFI/BOOT/BOOTX64.EFI" "$GUIDANCE_LOG"
  grep -Fqx "cmp /efi/EFI/Linux/arch-recovery.efi $esp/EFI/BOOT/BOOTX64.EFI" "$GUIDANCE_LOG"
done
for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
  grep -Fqx "cmp /efi/EFI/Linux/$uki /efi2/EFI/Linux/$uki" "$GUIDANCE_LOG"
done
[[ $(wc -l <"$GUIDANCE_LOG" | tr -d ' ') == 13 ]]
if GUIDANCE_LOG="$work/signature-failure" GUIDANCE_FAIL_SIGNATURE=/efi2/EFI/Linux/arch-linux-lts.efi bash "$work/verify-owner-ukis.sh" >"$work/refused" 2>&1; then exit 1; fi
if grep -Fq '/efi2/EFI/Linux/arch-recovery.efi' "$work/signature-failure"; then exit 1; fi
if GUIDANCE_LOG="$work/compare-failure" GUIDANCE_FAIL_COMPARE=/efi/EFI/Linux/arch-linux.efi bash "$work/verify-owner-ukis.sh" >"$work/refused" 2>&1; then exit 1; fi
if grep -Fqx 'cmp /efi/EFI/Linux/arch-linux-lts.efi /efi2/EFI/Linux/arch-linux-lts.efi' "$work/compare-failure"; then exit 1; fi
bash "$work/tpm2-enrol-execute.sh"
grep -Fqx "bash $ENROLL_SCRIPT $LUKS_UUID $EARLY_PCR11_SHA256 --execute" "$GUIDANCE_LOG"
# Exercise the documented Wi-Fi command with no real service or network calls.
# Called in the child shell running the documentation snippet.
# shellcheck disable=SC2329
nmcli() {
  [[ $* == 'device status' || $* == 'device wifi list' ]] || return 99
  printf '%s\n' "nmcli $*" >>"$GUIDANCE_LOG"
}
# shellcheck disable=SC2329
timedatectl() { [[ $* == status ]]; }
export -f nmcli timedatectl
bash -e "$work/installed-wifi-connect.sh"
grep -Fqx 'nmcli device status' "$GUIDANCE_LOG"
grep -Fqx 'nmcli device wifi list' "$GUIDANCE_LOG"
grep -Fqx 'nmcli --ask device wifi connect YOUR SSID ifname wlan0' "$GUIDANCE_LOG"

# Inspect only the documented image and bounded phase journal. These commands
# are intercepted; no package database, real UKI or host journal is read.
export GUIDANCE_FAIL_INSPECT=''
# shellcheck disable=SC2329
pacman() {
  printf '%s\n' "pacman $*" >>"$GUIDANCE_LOG"
  [[ $# == 4 && $1 == -Q && $2 == systemd && $3 == systemd-ukify && $4 == mkinitcpio ]] || return 100
  [[ $GUIDANCE_FAIL_INSPECT != packages ]]
}
# shellcheck disable=SC2329
sha256sum() {
  printf '%s\n' "sha256sum $*" >>"$GUIDANCE_LOG"
  [[ $# == 1 && $1 == /efi/EFI/Linux/arch-linux.efi ]] || return 101
  [[ $GUIDANCE_FAIL_INSPECT != image ]]
}
export -f pacman sha256sum
bash "$work/tpm2-prediction-inspect.sh"
grep -Fqx 'pacman -Q systemd systemd-ukify mkinitcpio' "$GUIDANCE_LOG"
grep -Fqx 'sha256sum /efi/EFI/Linux/arch-linux.efi' "$GUIDANCE_LOG"
grep -Fqx 'journalctl -b -u systemd-pcrphase-initrd.service --no-pager -n 20' "$GUIDANCE_LOG"
for failure in packages image; do
  if GUIDANCE_FAIL_INSPECT=$failure GUIDANCE_LOG="$work/inspect-$failure" bash "$work/tpm2-prediction-inspect.sh" >"$work/refused" 2>&1; then exit 1; fi
  if grep -Fq 'journalctl' "$work/inspect-$failure"; then exit 1; fi
done
printf 'Installation signature-check, TPM inspection/enrolment and Wi-Fi command guidance tests passed (no live operations)\n'
