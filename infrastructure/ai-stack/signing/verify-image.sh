#!/usr/bin/env bash
# Verify an AI-stack image before deploying it: its signature and its SBOM and
# vulnerability attestations must verify against the committed public key.
# No transparency log is used (signing-config.json lists none).
# Usage: verify-image.sh registry.lan.thespry.family/local/NAME@sha256:DIGEST
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ref=${1:-}
[[ $ref =~ ^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9/._-]+@sha256:[a-f0-9]{64}$ ]] || stop 'usage: verify-image.sh REGISTRY/NAME@sha256:DIGEST'
[[ -s $here/cosign.pub ]] || stop 'signing/cosign.pub is missing; run signing/init-key.sh and commit the key'
cosign=$(awk -F= '$1 == "COSIGN_IMAGE" {print $2}' "$here/../stack.lock")
[[ $cosign =~ @sha256:[a-f0-9]{64}$ ]] || stop 'COSIGN_IMAGE must be pinned by digest in stack.lock'
run() { podman run --rm --pull=missing --network host --user 0:0 -v "$here:/signing:ro" "$cosign" "$@"; }
run verify --key /signing/cosign.pub --insecure-ignore-tlog=true "$ref" >/dev/null
for type in cyclonedx vuln; do
  run verify-attestation --key /signing/cosign.pub --insecure-ignore-tlog=true --type "$type" "$ref" >/dev/null
done
printf 'VERIFIED=%s signature,cyclonedx,vuln\n' "$ref"
