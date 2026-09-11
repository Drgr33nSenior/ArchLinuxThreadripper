#!/usr/bin/env bash
# Real orchestration/archive checks; synthetic Git, compiler, download and makepkg.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
source "$root/lib/common.sh"
if ! command -v bsdtar >/dev/null || ! command -v sha256sum >/dev/null; then
  printf 'SKIP: Bridge build fixtures require bsdtar and sha256sum\n'
  exit 0
fi
# macOS's BSD sha256sum lacks GNU long options; shasum preserves strict checks.
if [[ $(uname -s) == Darwin ]]; then
  sha256sum() { command shasum -a 256 "$@"; }
  export -f sha256sum
fi
mkdir -p "$work/input" "$work/builder" "$work/tree/spry-ai-workstation-bridge-0.0.0" "$work/compiler/go/bin"
printf '1.27.1\n' >"$work/tree/spry-ai-workstation-bridge-0.0.0/.go-version"
bsdtar -czf "$work/input/spry-bridge-0.0.0-src.tar.gz" -C "$work/tree" spry-ai-workstation-bridge-0.0.0
printf '# reviewed synthetic recipe\n' >"$work/input/PKGBUILD"
(cd "$work/input" && sha256sum spry-bridge-0.0.0-src.tar.gz PKGBUILD >SHA256SUMS)
bsdtar -czf "$work/go.tar.gz" -C "$work/compiler" go
printf 'BRIDGE_GO_VERSION=1.27.1\nBRIDGE_GO_LINUX_AMD64_SHA256=%s\nSOURCE_DATE_EPOCH=1788480000\n' \
  "$(common::sha256_file "$work/go.tar.gz")" >"$work/builder/versions.lock"
printf 'go 2:1.27.0-1\n' >"$work/builder/packages.txt"
bash "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$work/installer" >/dev/null
for file in tests/hardware/test_serving_memory.py tests/hardware/test_model_kernels.py tests/hardware/sglang-profile-evidence.py tests/fixtures/telemetry/alerts_test.yaml tests/fixtures/telemetry/otlp_sanitization.json; do
  grep -Fxq "$file" "$work/installer/project/source.files"
done
BUILDER_IMAGE_ID="sha256:$(printf '%064d' 0)"
export BRIDGE_BUILD_FIXTURE="$work" BUILDER_IMAGE_ID
revision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
# shellcheck disable=SC2329
git() {
  case "$*" in
    init*) mkdir -p "$2/packaging/arch" ;;
    *' fetch '*)
      [[ $* == *'https://github.com/Drgr33nSenior/Spry.ai-workstation-bridge.git aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ]] || return 1
      [[ ${BRIDGE_TEST_FAILURE:-} != fetch ]] || return 23
      ;;
    *'rev-parse'*) printf '%s\n' "${BRIDGE_TEST_REVISION:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" ;;
    *' checkout '*)
      printf '1.27.1\n' >"$2/.go-version"
      printf 'fixture /source.sha256\n' >"$2/packaging/arch/PKGBUILD.in"
      if [[ ${BRIDGE_TEST_FAILURE:-} == legacy ]]; then printf 'old recipe\n' >"$2/packaging/arch/PKGBUILD.in"; fi
      ;;
    *'status --porcelain'*) [[ ${BRIDGE_TEST_FAILURE:-} != status ]] || return 27 ;;
    *) return 1 ;;
  esac
}
# shellcheck disable=SC2329 # Exported fixtures are invoked in a child Bash.
uname() { printf '%s\n' "${BRIDGE_TEST_ARCH:-x86_64}"; }
# shellcheck disable=SC2329
id() { printf '%s\n' "${BRIDGE_TEST_UID:-1000}"; }
# shellcheck disable=SC2329
curl() {
  [[ $* == *https://go.dev/dl/go1.27.1.linux-amd64.tar.gz* ]] || return 1
  [[ ${BRIDGE_TEST_FAILURE:-} != download ]] || return 13
  cp "$BRIDGE_BUILD_FIXTURE/go.tar.gz" go.tar.gz
}
# shellcheck disable=SC2329
go() {
  [[ $GOTOOLCHAIN == local && $GOOS == linux && $GOARCH == amd64 && $GOMAXPROCS == 4 && $GOFLAGS == -p=4 ]] || return 1
  if [[ $* == 'test -mod=readonly -list ^TestCandidateInstallerMemoryContract$ ./internal/memory' ]]; then
    [[ ${BRIDGE_TEST_FAILURE:-} != missing_contract ]] || return 0
    printf '%s\n' TestCandidateInstallerMemoryContract
    return 0
  fi
  if [[ $* == 'test -mod=readonly -count=1 -v -run ^TestCandidateInstallerMemoryContract$ ./internal/memory' ]]; then
    [[ $BRIDGE_INSTALLER_MEMORY_CANDIDATE == */installer/project && -f $BRIDGE_INSTALLER_MEMORY_CANDIDATE/SOURCE-MANIFEST.sha256 ]] || return 1
    [[ ${BRIDGE_TEST_FAILURE:-} != contract ]] || return 29
    return 0
  fi
  if [[ $* == 'run ./cmd/bridge-arch-package --version v0.0.0' ]]; then
    mkdir -p dist/arch
    cp "$BRIDGE_BUILD_FIXTURE/input/"* dist/arch/
    return
  fi
  if [[ $* == 'env -json '* ]]; then
    printf '{"GOVERSION":"go1.27.1","GOARCH":"amd64"}\n'
    return
  fi
  printf '%s\n' "${BRIDGE_TEST_GO:-go1.27.1}"
}
# shellcheck disable=SC2329
makepkg() {
  printf '%s\n' "$*" >>"$BRIDGE_BUILD_FIXTURE/makepkg.log"
  case $* in
    --verifysource) [[ ${BRIDGE_TEST_FAILURE:-} != verify ]] || return 17 ;;
    --cleanbuild)
      [[ ${BRIDGE_TEST_FAILURE:-} != build ]] || return 19
      mkdir -p pkg/usr/share/doc/spry-ai-workstation-bridge
      printf 'pkgname = spry-ai-workstation-bridge\npkgver = 0.0.0-1\narch = %s\n' "${BRIDGE_TEST_PACKAGE_ARCH:-x86_64}" >pkg/.PKGINFO
      printf 'pkgbuild_sha256sum = %s\n' "$(sha256sum PKGBUILD | awk '{print $1}')" >pkg/.BUILDINFO
      sha256sum spry-bridge-0.0.0-src.tar.gz | awk '{print $1}' >pkg/usr/share/doc/spry-ai-workstation-bridge/source.sha256
      if [[ ${BRIDGE_TEST_FAILURE:-} == provenance ]]; then printf 'wrong\n' >pkg/usr/share/doc/spry-ai-workstation-bridge/source.sha256; fi
      bsdtar -czf spry-ai-workstation-bridge-0.0.0-1-x86_64.pkg.tar.zst -C pkg .PKGINFO .BUILDINFO usr
      if [[ ${BRIDGE_TEST_FAILURE:-} == tamper ]]; then printf 'changed\n' >>PKGBUILD; fi
      ;;
    *) return 1 ;;
  esac
}
export -f uname id git curl go makepkg
script="$root/infrastructure/iso/docker/bridge-build.sh"
mkdir "$work/success"
bash "$script" "$revision" v0.0.0 "$work/success" "$work/builder" "$work/installer" >"$work/success.log" 2>&1 || {
  cat "$work/success.log"
  exit 1
}
[[ $(tr '\n' ' ' <"$work/makepkg.log") == '--verifysource --cleanbuild ' ]]
(cd "$work/success/output" && sha256sum --check --strict SHA256SUMS >/dev/null)
cmp "$work/input/PKGBUILD" "$work/success/output/PKGBUILD"
grep -Fxq 'GO_VERSION=1.27.1' "$work/success/output/builder.lock"
grep -Fxq "SOURCE_COMMIT=$revision" "$work/success/output/builder.lock"
grep -Fxq "INSTALLER_SOURCE_SHA256=$(common::sha256_file "$work/installer/bootstrap-source.tar.gz")" "$work/success/output/builder.lock"
grep -Fxq 'MEMORY_CONTRACT=selected-installer-memory-v1' "$work/success/output/builder.lock"
if bash "$script" "$revision" v0.0.0 "$work/success" "$work/builder" "$work/installer" >"$work/repeat.log" 2>&1; then exit 1; fi
for failure in fetch legacy download status verify build provenance tamper contract missing_contract; do
  mkdir "$work/$failure"
  status=0
  BRIDGE_TEST_FAILURE=$failure bash "$script" "$revision" v0.0.0 "$work/$failure" "$work/builder" "$work/installer" >"$work/$failure.log" 2>&1 || status=$?
  [[ $status != 0 && ! -e $work/$failure/output && -d $work/$failure/checkout ]]
  case $failure in download) [[ $status == 13 ]] ;; verify) [[ $status == 17 ]] ;; build) [[ $status == 19 ]] ;; esac
done
for failure in arch uid go package_arch revision; do
  mkdir "$work/$failure"
  case $failure in
    arch) export BRIDGE_TEST_ARCH=arm64 ;;
    uid) export BRIDGE_TEST_UID=0 ;;
    go) export BRIDGE_TEST_GO=go1.27.0 ;;
    package_arch) export BRIDGE_TEST_PACKAGE_ARCH=aarch64 ;;
    revision) export BRIDGE_TEST_REVISION=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ;;
  esac
  if bash "$script" "$revision" v0.0.0 "$work/$failure" "$work/builder" "$work/installer" >"$work/$failure.log" 2>&1; then exit 1; fi
  [[ ! -e $work/$failure/output ]]
  unset BRIDGE_TEST_ARCH BRIDGE_TEST_UID BRIDGE_TEST_GO BRIDGE_TEST_PACKAGE_ARCH BRIDGE_TEST_REVISION
done
mkdir "$work/bad-manifest"
printf 'unlisted\n' >>"$work/input/SHA256SUMS"
if bash "$script" "$revision" v0.0.0 "$work/bad-manifest" "$work/builder" "$work/installer" >"$work/manifest.log" 2>&1; then exit 1; fi
[[ ! -e $work/bad-manifest/output ]]
cp "$work/installer/source.lock" "$work/source.lock.original"
printf 'SOURCE_SHA256=%064d\n' 0 >"$work/installer/source.lock"
mkdir "$work/bad-installer-source"
if bash "$script" "$revision" v0.0.0 "$work/bad-installer-source" "$work/builder" "$work/installer" >"$work/source-lock.log" 2>&1; then exit 1; fi
[[ ! -e $work/bad-installer-source/output ]]
mv "$work/source.lock.original" "$work/installer/source.lock"
mkdir "$work/extra-installer" "$work/extra-tree"
bsdtar -xzf "$work/installer/bootstrap-source.tar.gz" -C "$work/extra-tree"
printf 'unexpected source entry\n' >"$work/extra-tree/project/unexpected"
(cd "$work/extra-tree" && find project -type f -print | sort >"$work/extra-files")
bsdtar -czf "$work/extra-installer/bootstrap-source.tar.gz" -C "$work/extra-tree" -T "$work/extra-files"
printf 'SOURCE_SHA256=%s\n' "$(common::sha256_file "$work/extra-installer/bootstrap-source.tar.gz")" >"$work/extra-installer/source.lock"
mkdir "$work/extra-installer-source"
if bash "$script" "$revision" v0.0.0 "$work/extra-installer-source" "$work/builder" "$work/extra-installer" >"$work/extra-installer.log" 2>&1; then exit 1; fi
[[ ! -e $work/extra-installer-source/output ]]
printf 'Bridge build orchestration, input/toolchain/package identity and failure fixtures passed; no real compilation performed\n'
