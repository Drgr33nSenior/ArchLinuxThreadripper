#!/usr/bin/env bash
# Sourced production orchestration with host adapters only; requires a test PTY.
set -euo pipefail
project=$1 fixture=$2 mode=$3
source "$project/bin/arch-workstation-codex"
ci_platform() { [[ ! -f $fixture/wrong-platform ]]; }
ci_ram_root() { printf '%s/ram\n' "$fixture"; }
findmnt() { [[ ! -f $fixture/wrong-fs ]] && printf 'tmpfs\n'; }
bootstrap_no_swap_for_codex() { [[ ! -f $fixture/swap ]]; }
bootstrap_network_codex() { [[ ! -f $fixture/offline ]]; }
export PATH="$fixture/bin:$PATH"
ci_main "$mode"
