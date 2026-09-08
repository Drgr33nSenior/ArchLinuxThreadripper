#!/usr/bin/env bash
# Opt-in runtime regression: only run in a disposable, networkless Linux
# container with a writable /tmp. No key material, signing or package installs.
# Run as PID 1 for the negative control, then below the pacstrap --reap-child
# adapter with the same 64-PID limit. Compare ERR replies (gpgme-tool exits zero
# even when Assuan commands fail). The fixed run must have 100 OK replies.
set -euo pipefail
export GNUPGHOME
GNUPGHOME=$(mktemp -d /tmp/archiso-synthetic-gpgme.XXXXXX)
exec gpgme-tool < <(
  for ((i = 0; i < 100; i++)); do
    printf 'KEYLIST SYNTHETIC-ABSENT-KEY\n'
  done
  printf 'BYE\n'
)
