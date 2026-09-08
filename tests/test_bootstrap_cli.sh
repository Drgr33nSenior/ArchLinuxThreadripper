#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/bin/bootstrap-arch"

# Exercise the actual parser without target-host or storage side effects.
bootstrap_load_config() { :; }
bootstrap_install() { printf '%s\n' "$BOOTSTRAP_DRY_RUN"; }
[[ $(main --config unused install) == 1 ]]
[[ $(main --config unused --dry-run install) == 1 ]]
[[ $(main --config unused --execute install) == 0 ]]
if main --config unused --dry-run --execute install >/dev/null 2>&1; then exit 1; fi
if main --config unused --execute verify >/dev/null 2>&1; then exit 1; fi

# Called by the sourced package-selection function.
# shellcheck disable=SC2329
lspci() { printf '0000:01:00.0 0300: 1002:ffff\n0000:02:00.0 0300: 1002:ffff\n'; }
bootstrap_select_gpu_packages
[[ ${BOOTSTRAP_GPU_PACKAGES[*]} == *vulkan-radeon* ]]
[[ ${BOOTSTRAP_GPU_PACKAGES[*]} != *intel* ]]
lspci() { printf '0000:01:00.0 0300: 8086:ffff\n'; }
bootstrap_select_gpu_packages
[[ ${BOOTSTRAP_GPU_PACKAGES[*]} == *vulkan-intel* ]]
echo 'Bootstrap dry-run and detected GPU selection tests passed'
