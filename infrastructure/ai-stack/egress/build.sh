#!/usr/bin/env bash
# Runs inside the pinned Go image (DRA_GO_IMAGE) with no network. Builds the workspace egress
# proxy: Smokescreen at EGRESS_PROXY_COMMIT with this stage's main.go added as
# cmd/sandbox-egress, from Smokescreen's vendored Go modules only, as one static binary.
set -euo pipefail
: "${EGRESS_PROXY_REPOSITORY:?}" "${EGRESS_PROXY_COMMIT:?}" "${DRA_GO_IMAGE:?}" "${JOBS:?}"
src=/work/src
out=/work/out
# Offline and repeatable: no toolchain or module downloads, no VCS stamping, no build paths.
export GOTOOLCHAIN=local GOPROXY=off GOFLAGS="-mod=vendor -trimpath -buildvcs=false -p=$JOBS"
export GOMAXPROCS=$JOBS CGO_ENABLED=0 HOME=/tmp GOCACHE=/tmp/go-build
have=$(go env GOVERSION)
rm -rf -- "${out:?}/bin"
mkdir -p "$out/bin"
cd "$src"
go vet ./cmd/sandbox-egress
go build -ldflags='-s -w' -o "$out/bin/sandbox-egress" ./cmd/sandbox-egress
cp -- "$src/LICENSE.txt" "$out/LICENSE.txt"
go version -m "$out/bin/sandbox-egress" >"$out/buildinfo.txt"
sha() { sha256sum "$1" | cut -d' ' -f1; }
cat >"$out/RECEIPT.json" <<JSON
{
  "schema": 1,
  "stage": "egress",
  "egress_proxy_repository": "$EGRESS_PROXY_REPOSITORY",
  "egress_proxy_commit": "$EGRESS_PROXY_COMMIT",
  "go_image": "$DRA_GO_IMAGE",
  "go_version": "$have",
  "build": "go build ./cmd/sandbox-egress, GOFLAGS=$GOFLAGS, CGO_ENABLED=0, GOTOOLCHAIN=local, no network",
  "go_sum_sha256": "$(sha "$src/go.sum")",
  "vendor_modules_sha256": "$(sha "$src/vendor/modules.txt")",
  "main_go_sha256": "$(sha "$src/cmd/sandbox-egress/main.go")",
  "outputs": [{"path": "bin/sandbox-egress", "sha256": "$(sha "$out/bin/sandbox-egress")"}]
}
JSON
echo "EGRESS_STAGE=built; go=$have; commit=${EGRESS_PROXY_COMMIT:0:12}"
