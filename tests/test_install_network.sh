#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/bootstrap/common.sh"
source "$root/lib/bootstrap/network.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Every host/network operation is a fixture; no actual DNS, routing or HTTP.
case_name=ok
timedatectl() { [[ $case_name != time ]] && printf 'yes\n'; }
getent() { [[ $case_name != dns ]] && printf '192.0.2.1 STREAM fixture\n'; }
ip() { [[ $case_name != route ]]; }
pacman-conf() {
  if [[ $1 == --repo-list ]]; then
    [[ $case_name != empty ]] && printf 'core\nextra\n'
  elif [[ $case_name == unsafe ]]; then
    printf 'https://dummy-user:NOT_A_CREDENTIAL@fixture.invalid/repo\n'
  else
    printf 'https://fixture.invalid/%s\n' "$2"
  fi
}
bootstrap_network_curl() {
  [[ $case_name != mirror ]] || return 22
  [[ $case_name != tls ]] || return 60
  printf 'fixture' >"$4"
}
bsdtar() { [[ $case_name != archive ]] && printf 'fixture/desc\n'; }
for case_name in ok time dns route empty unsafe mirror tls archive; do
  status=0
  bootstrap_network_mirrors >"$work/$case_name.log" 2>&1 || status=$?
  if [[ $case_name == ok ]]; then [[ $status == 0 ]]; else [[ $status != 0 ]]; fi
done
if grep -q 'NOT_A_CREDENTIAL' "$work/unsafe.log"; then exit 1; fi
case_name=ok
# Isolate the unauthenticated curl adapter; verify authentication is not implied.
env() { printf '%s' "$http_code"; }
for http_code in 200 401; do bootstrap_network_codex api >"$work/probe" 2>&1; done
http_code=403
if bootstrap_network_codex api >"$work/probe" 2>&1; then exit 1; fi
http_code=401
if bootstrap_network_codex device >"$work/probe" 2>&1; then exit 1; fi

# Actual preflight-to-install call: replace storage probes, never format anything.
source "$root/lib/bootstrap/install.sh"
bootstrap_require_root() { :; }
bootstrap_require_tty() { :; }
bootstrap_preflight() { bootstrap_network_mirrors; }
bootstrap_assert_safe_target() { :; }
bootstrap_confirm_destruction() { printf 'unexpected confirmation\n' >"$work/confirmation"; }
bootstrap_create_partitions() { printf 'unexpected erasure\n' >"$work/erasure"; }
case_name=dns
BOOTSTRAP_DRY_RUN=0
if bootstrap_install >"$work/refusal" 2>&1; then exit 1; fi
[[ ! -e $work/confirmation && ! -e $work/erasure ]]

# Existing first-boot policy: record the actual configure commands, no target writes.
bootstrap_chroot() { printf '%s\n' "$*" >>"$work/commands"; }
bootstrap_configure_offline_policy() { :; }
bootstrap_set_passwords() { :; }
bootstrap_copy_and_sign_ukis() { :; }
LOCALE=en_GB.UTF-8 TIMEZONE=Europe/London INSTALL_USER=fixture HOST_PROFILE=headless
ENABLE_SSH=false ENABLE_BLUETOOTH=false ENABLE_PRINTING=false
bootstrap_configure_system
grep -Fq 'systemctl enable NetworkManager.service' "$work/commands"
if grep -Eq 'iwd|/home/|sshd.service' "$work/commands"; then exit 1; fi
ENABLE_SSH=true
bootstrap_configure_system
grep -Fqx 'systemctl enable sshd.service' "$work/commands"
printf 'Network refusal, unauthenticated endpoint and first-boot policy fixtures passed\n'
