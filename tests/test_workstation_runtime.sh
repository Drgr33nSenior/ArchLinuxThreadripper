#!/usr/bin/env bash
set -o errexit -o nounset -o pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
exec "$repo_root/tests/workstation/static.sh"
