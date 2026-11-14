#!/usr/bin/env bash
# Runs inside the pinned Go image (DRA_GO_IMAGE) with no network. Builds the AMD GPU DRA
# driver at DRA_DRIVER_COMMIT with upstream's own `make cmds`, from the vendored Go modules
# only, then collects the prebuilt AMD SMI libraries upstream vendors (the plugin links
# them through cgo) and writes RECEIPT.json. The source tree is mounted read-only.
set -euo pipefail
: "${DRA_DRIVER_REPOSITORY:?}" "${DRA_DRIVER_COMMIT:?}" "${DRA_GO_IMAGE:?}" "${JOBS:?}"
src=/work/src
out=/work/out
# Offline and repeatable: no toolchain or module downloads, no VCS stamping, no build paths.
export GOTOOLCHAIN=local GOPROXY=off GOFLAGS="-mod=vendor -trimpath -buildvcs=false -p=$JOBS"
export GOMAXPROCS=$JOBS CGO_ENABLED=1 HOME=/tmp GOCACHE=/tmp/go-build
# Upstream's build image is golang:$GOLANG_VERSION (env.mk); refresh.sh pins the same release.
want=$(sed -n 's/^GOLANG_VERSION ?= //p' "$src/env.mk")
have=$(go env GOVERSION)
[[ $have == "go$want" ]] || {
  printf 'STOP: upstream builds with Go %s; the pinned image has %s\n' "$want" "$have" >&2
  exit 1
}
rm -rf -- "${out:?}/bin" "${out:?}/lib"
mkdir -p "$out/bin" "$out/lib"
# The *_GIT_TAG presets skip the Makefile's `git describe` (the source is an archive).
make -C "$src" IMAGE_GIT_TAG= CHART_GIT_TAG= PREFIX="$out/bin" cmds
cp -a "$src/third_party/amd_smi/lib/." "$out/lib/"
cp -- "$src/LICENSE" "$out/LICENSE"
for binary in "$out"/bin/*; do go version -m "$binary"; done >"$out/buildinfo.txt"
(cd "$out" && find bin lib LICENSE buildinfo.txt -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) >"$out/outputs.sha256"
sha() { sha256sum "$1" | cut -d' ' -f1; }
outputs=$(awk '{printf "%s{\"path\": \"%s\", \"sha256\": \"%s\"}", (NR > 1 ? ", " : ""), $2, $1}' "$out/outputs.sha256")
cat >"$out/RECEIPT.json" <<JSON
{
  "schema": 1,
  "stage": "dra",
  "dra_driver_repository": "$DRA_DRIVER_REPOSITORY",
  "dra_driver_commit": "$DRA_DRIVER_COMMIT",
  "go_image": "$DRA_GO_IMAGE",
  "go_version": "$have",
  "build": "make cmds, GOFLAGS=$GOFLAGS, GOTOOLCHAIN=local, no network",
  "go_sum_sha256": "$(sha "$src/go.sum")",
  "vendor_modules_sha256": "$(sha "$src/vendor/modules.txt")",
  "amd_smi_version": "$(head -n 1 "$src/third_party/amd_smi/lib/.version")",
  "outputs": [$outputs]
}
JSON
echo "DRA_STAGE=built; go=$have; commit=${DRA_DRIVER_COMMIT:0:12}"
