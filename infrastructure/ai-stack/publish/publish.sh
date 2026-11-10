#!/usr/bin/env bash
# Publish one verified AI-stack image to a public registry, run by its owner.
#
# Usage: publish.sh registry.lan.thespry.family/local/NAME@sha256:DIGEST ghcr.io/OWNER/NAME
#
# 1. Verifies the lab image's signature and SBOM / vulnerability attestations
#    against the committed public key (signing/verify-image.sh).
# 2. Scans every layer for secrets (Trivy, TRIVY_IMAGE in stack.lock) and stops on
#    any finding; the report stays in the work directory.
# 3. Copies the image with its signature and attestations unchanged (cosign copy),
#    so the published digest is the lab digest and verifies with the same key.
# 4. Verifies the published copy.
#
# Needs a podman login to both registries (a GitHub token with write:packages for
# ghcr.io). The first push creates a private package; make it public on GitHub.
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
src=${1:-}
dest=${2:-}
[[ $src =~ ^registry\.lan\.thespry\.family/local/[a-z0-9-]+@sha256:([a-f0-9]{64})$ ]] ||
  stop 'usage: publish.sh registry.lan.thespry.family/local/NAME@sha256:DIGEST ghcr.io/OWNER/NAME'
digest=sha256:${BASH_REMATCH[1]}
[[ $dest =~ ^ghcr\.io/[a-z0-9-]+/[a-z0-9._-]+$ ]] || stop 'DEST must be ghcr.io/OWNER/NAME (lower case, no tag)'
authfile=${REGISTRY_AUTH_FILE:-${XDG_RUNTIME_DIR:-/run/user/$UID}/containers/auth.json}
[[ -s $authfile ]] || stop "no registry login at $authfile; run podman login first"
grep -q '"ghcr.io"' "$authfile" || stop 'no ghcr.io login; run: podman login ghcr.io -u OWNER'
digest_image() {
  local value
  value=$(awk -F= -v k="$1" '$1 == k {print $2}' "$here/stack.lock")
  [[ $value =~ @sha256:[a-f0-9]{64}$ ]] || stop "$1 must be pinned by digest in stack.lock"
  printf '%s\n' "$value"
}
cosign_image=$(digest_image COSIGN_IMAGE)
trivy_image=$(digest_image TRIVY_IMAGE)
work=$(mktemp -d "${TMPDIR:-/tmp}/ai-stack-publish.XXXXXX")
trap 'rm -f -- "$work/image.tar"' EXIT

bash "$here/signing/verify-image.sh" "$src"

podman pull --quiet "$src" >/dev/null
podman save --format docker-archive -o "$work/image.tar" "$src"
mkdir -p "$work/trivy-cache"
podman run --rm --pull=missing --network host --user 0:0 -v "$work:/work" "$trivy_image" image \
  --cache-dir /work/trivy-cache --input /work/image.tar --scanners secret --quiet \
  --format json --output /work/secrets.json
python3 - "$work/secrets.json" <<'PY' || stop "secret scan found something; see $work/secrets.json"
import json, sys
report = json.load(open(sys.argv[1]))
found = [(r.get("Target"), s.get("RuleID"), s.get("StartLine")) for r in report.get("Results", []) for s in r.get("Secrets") or []]
for target, rule, line in found:
    print(f"SECRET {rule} in {target} line {line}")  # rule and location only, never the value
sys.exit(1 if found else 0)
PY
printf 'SECRET_SCAN=clean %s\n' "$src"

podman run --rm --pull=missing --network host --user 0:0 -e DOCKER_CONFIG=/auth \
  -v "$authfile:/auth/config.json:ro" "$cosign_image" copy "$src" "$dest:latest" >/dev/null
bash "$here/signing/verify-image.sh" "$dest@$digest"
printf 'PUBLISHED=%s@%s\n' "$dest" "$digest"
