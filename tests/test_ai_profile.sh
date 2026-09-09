#!/usr/bin/env bash
# Offline policy fixtures; no host tuning, chroot or firmware commands run.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/bootstrap/common.sh"
source "$root/lib/bootstrap/config.sh"
source "$root/lib/bootstrap/install.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

bootstrap_load_config "$root/infrastructure/host/install.conf.example"
[[ $HOST_PROFILE == headless && $TUNED_PROFILE == accelerator-performance ]]
bootstrap_select_host_profile
[[ $BOOTSTRAP_TUNED_PROFILE == accelerator-performance && ${#BOOTSTRAP_PROFILE_PACKAGES[@]} == 0 ]]

# The optional setting must not leak in from the caller's environment.
sed '/^TUNED_PROFILE=/d' "$root/config/install.conf.example" >"$work/legacy.conf"
TUNED_PROFILE=untrusted
bootstrap_load_config "$work/legacy.conf"
[[ $TUNED_PROFILE == auto ]]
bootstrap_select_host_profile
[[ $BOOTSTRAP_TUNED_PROFILE == balanced ]]
sed 's/^TUNED_PROFILE=.*/TUNED_PROFILE=untrusted/' "$root/config/install.conf.example" >"$work/invalid.conf"
if bootstrap_load_config "$work/invalid.conf" >/dev/null 2>&1; then exit 1; fi

bootstrap_load_config "$root/infrastructure/host/install.conf.example"
BOOTSTRAP_DRY_RUN=0
BOOTSTRAP_TARGET="$work/target"
mkdir -p "$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/accelerator-performance"
printf '# synthetic installed profile\n' >"$BOOTSTRAP_TARGET/usr/lib/tuned/profiles/accelerator-performance/tuned.conf"
bootstrap_configure_offline_policy
[[ $(<"$BOOTSTRAP_TARGET/etc/tuned/active_profile") == accelerator-performance ]]
[[ $(<"$BOOTSTRAP_TARGET/etc/tuned/profile_mode") == manual ]]
bootstrap_configure_offline_policy
HOST_PROFILE=desktop
bootstrap_select_host_profile
[[ ${BOOTSTRAP_PROFILE_PACKAGES[*]} == *gnome* && $BOOTSTRAP_TUNED_PROFILE == accelerator-performance ]]

BOOTSTRAP_TARGET="$work/missing-profile"
if bootstrap_configure_offline_policy >/dev/null 2>&1; then exit 1; fi
[[ ! -e $BOOTSTRAP_TARGET ]]
BOOTSTRAP_DRY_RUN=1
bootstrap_configure_offline_policy >/dev/null 2>&1
[[ ! -e $BOOTSTRAP_TARGET ]]

for template in kernel.cmdline kernel-recovery.cmdline; do
  grep -q 'iommu=pt' "$root/templates/arch/$template"
  if grep -Eq 'amd_iommu=on|mitigations=off|pcie_acs_override|nosmt' "$root/templates/arch/$template"; then exit 1; fi
done

# CLI discovery and arity checks must not invoke target hardware/build actions.
cli_help=$(bash "$root/bin/workstationctl" --help)
[[ $cli_help == *'build cmake-ninja-args'* && $cli_help == *'rocm build-llama'* ]]
if bash "$root/bin/workstationctl" build cmake-ninja-args normal extra >/dev/null 2>&1; then exit 1; else [[ $? == 2 ]]; fi
if bash "$root/bin/workstationctl" rocm build-llama >/dev/null 2>&1; then exit 1; else [[ $? == 2 ]]; fi
if bash "$root/bin/workstationctl" kernel build >/dev/null 2>&1; then exit 1; else [[ $? == 2 ]]; fi
echo 'Explicit AI TuneD selection, legacy defaults and kernel command-line checks passed'
