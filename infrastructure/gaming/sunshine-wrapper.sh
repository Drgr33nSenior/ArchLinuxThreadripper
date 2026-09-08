#!/usr/bin/env bash
set -euo pipefail

# Upstream start-sunshine.sh owns session readiness, first-run templates and
# credential setup. Preserve those control operations without logging arguments.
if [[ $# == 1 && ( $1 == --version || $1 == --help ) ]] || [[ $# == 4 && $2 == --creds ]]; then
  exec /usr/lib/workstation/sunshine.real "$@"
fi
[[ $# == 1 ]] || { printf 'Sunshine wrapper requires the existing config path\n' >&2; exit 2; }
exec /opt/workstation/bin/workstationctl sunshine launch /usr/lib/workstation/sunshine.real "$1"
