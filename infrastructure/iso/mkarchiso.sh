#!/usr/bin/env bash
# Build-only adapter. Keep the package-owned mkarchiso unchanged.
set -euo pipefail

mkarchiso_program() {
  local upstream=$1
  # shellcheck disable=SC2016 # Match literal upstream shell source.
  local marker='            pacman -Q --sysroot "${pacstrap_dir}" >"${isofs_dir}/${install_dir}/pkglist.${arch}.txt"'
  [[ $(grep -Fxc "$marker" "$upstream") == 1 ]] || {
    printf 'ERROR: upstream mkarchiso package-list query changed; review the adapter\n' >&2
    return 1
  }
  # --root selects the installed package database without entering the live
  # sysroot. The build configuration retains its external, verified GPGDir.
  # Never initialize/copy a private keyring into airootfs just for this query.
  awk -v marker="$marker" '
    $0 == marker {
      print "            local _pkglist_errors"
      print "            _pkglist_errors=$(pacman -Q --root \"${pacstrap_dir}\" --config \"${pacman_conf}\" 2>&1 >\"${isofs_dir}/${install_dir}/pkglist.${arch}.txt\") || {"
      print "                printf '\''%s\\nERROR: ISO package inventory query failed\\n'\'' \"${_pkglist_errors}\" >&2; exit 1;"
      print "            }"
      print "            [[ -z ${_pkglist_errors} ]] || {"
      print "                printf '\''%s\\nERROR: ISO package inventory emitted diagnostics; inspect the keyring and databases\\n'\'' \"${_pkglist_errors}\" >&2; exit 1;"
      print "            }"
      print "            [[ -s \"${isofs_dir}/${install_dir}/pkglist.${arch}.txt\" ]] || {"
      print "                printf '\''ERROR: ISO package inventory is empty\\n'\'' >&2; exit 1;"
      print "            }"
      next
    }
    { print }
  ' "$upstream"
}

main() {
  local program
  program=$(mkarchiso_program /usr/bin/mkarchiso) || return 1
  exec /bin/bash -c "$program" /usr/bin/mkarchiso "$@"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
