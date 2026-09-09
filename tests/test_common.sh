#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"

tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT

printf 'FOO=bar\nCOUNT=24\n' >"$tmp_dir/valid.conf"
common::load_config "$tmp_dir/valid.conf" FOO COUNT
[ "$FOO" = bar ]
# COUNT is assigned by the allowlisted dynamic KEY=VALUE loader above.
# shellcheck disable=SC2153
[ "$COUNT" = 24 ]

printf 'UNKNOWN=value\n' >"$tmp_dir/unknown.conf"
if (common::load_config "$tmp_dir/unknown.conf" FOO) >/dev/null 2>&1; then
  printf 'unknown key was accepted\n' >&2
  exit 1
fi

printf 'FOO=one\nFOO=two\n' >"$tmp_dir/duplicate.conf"
if (common::load_config "$tmp_dir/duplicate.conf" FOO) >/dev/null 2>&1; then
  printf 'duplicate key was accepted\n' >&2
  exit 1
fi

[ "$(common::lock_get "$repo_root/versions.lock" K3S_VERSION)" = 'v1.35.7+k3s1' ]

printf 'common helpers passed\n'
