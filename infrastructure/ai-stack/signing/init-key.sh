#!/usr/bin/env bash
# One-time owner step on the build host: create the AI-stack image signing key.
# The private key is encrypted with a passphrase you choose; the passphrase is
# also sealed for this user with `systemd-creds --user` so unattended builds can
# sign. Keep a copy of the passphrase in your password manager: the sealed copy
# is bound to this host and user. Prints the public key to commit as
# signing/cosign.pub. Never prints or stores the passphrase anywhere else.
# Usage: init-key.sh
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
lock=$here/../stack.lock
cosign=$(awk -F= '$1 == "COSIGN_IMAGE" {print $2}' "$lock")
[[ $cosign =~ @sha256:[a-f0-9]{64}$ ]] || stop 'COSIGN_IMAGE must be pinned by digest in stack.lock'
keys=${AI_STACK_SIGNING_DIR:-$HOME/.config/ai-stack/signing}
[[ ! -e $keys/cosign.key ]] || stop "$keys/cosign.key already exists; refusing to replace a signing key"
[[ -t 0 ]] || stop 'run this in an interactive terminal'
install -d -m 0700 "$keys"
read -r -s -p 'New signing key passphrase: ' COSIGN_PASSWORD && echo
read -r -s -p 'Repeat passphrase: ' again && echo
[[ -n $COSIGN_PASSWORD && $COSIGN_PASSWORD == "$again" ]] || stop 'passphrases are empty or differ'
unset again
export COSIGN_PASSWORD
# Root in the rootless container maps to this user, so the key files are ours.
podman run --rm --pull=missing --network none --user 0:0 -e COSIGN_PASSWORD -v "$keys:/keys" -w /keys \
  "$cosign" generate-key-pair --output-key-prefix cosign >/dev/null
chmod 0600 "$keys/cosign.key"
printf '%s' "$COSIGN_PASSWORD" | systemd-creds --user encrypt --name=cosign_password - "$keys/cosign_password.cred"
unset COSIGN_PASSWORD
chmod 0600 "$keys/cosign_password.cred"
printf 'Signing key: %s (encrypted); sealed passphrase: %s\n' "$keys/cosign.key" "$keys/cosign_password.cred"
printf 'Commit this public key as infrastructure/ai-stack/signing/cosign.pub:\n\n'
cat "$keys/cosign.pub"
