#!/usr/bin/env bash
# Builder-only adapter for arch-install-scripts 31's nested PID namespace.
# Keep /usr/bin/pacstrap package-owned and unchanged. Refuse upstream drift.
set -euo pipefail

pacstrap_program() {
  local upstream=$1 adapter=$2
  local marker='pid_unshare="unshare --fork --pid"'
  [[ $adapter =~ ^[A-Za-z0-9_./-]+$ ]] || {
    printf 'ERROR: unsafe pacstrap adapter path\n' >&2
    return 1
  }
  [[ $(grep -Fxc "$marker" "$upstream") == 1 ]] || {
    printf 'ERROR: upstream pacstrap PID launcher changed; review the builder adapter\n' >&2
    return 1
  }
  sed "s|^$marker$|pid_unshare=\"unshare --fork --pid /bin/bash $adapter --reap-child\"|" "$upstream"
}

main() {
  if [[ ${1:-} == --reap-child ]]; then
    shift
    (($#)) || {
      printf 'ERROR: a child command is required\n' >&2
      return 1
    }
    # pacstrap otherwise makes pacman PID 1. GPGME double-forks, leaving orphan
    # gpg processes for PID 1 to reap. Pacman does not do that, so a large package
    # set exhausts the PID limit. Bash's SIGCHLD handler reaps these children.
    # Do not exec here: Bash must remain PID 1 in this namespace.
    "$@" <&0 &
    local child=$!
    trap 'kill -TERM "$child" 2>/dev/null || :' TERM
    trap 'kill -INT "$child" 2>/dev/null || :' INT
    wait "$child"
    return "$?"
  fi
  local adapter program
  adapter=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")
  program=$(pacstrap_program /usr/bin/pacstrap "$adapter") || return 1
  # Preserve the upstream argument vector and diagnostic program name. Only the
  # nested PID launcher is changed; mounts, signatures and package logic remain.
  exec /bin/bash -c "$program" /usr/bin/pacstrap "$@"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
