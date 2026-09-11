#!/usr/bin/env bash
# Only invoked in the isolated amd64 builder. Inputs are reviewed executable code.
set -euo pipefail

bridge_build() {
  (($# == 5)) || {
    printf 'usage: bridge-build.sh COMMIT VERSION WORK BUILDER INSTALLER_SOURCE_BUNDLE\n' >&2
    return 1
  }
  local revision=$1 version=$2 work=$3 builder=$4 installer_bundle=$5 input archive go_version go_hash package source_hash recipe_hash field dirty installer_hash installer_root
  local repository=https://github.com/Drgr33nSenior/Spry.ai-workstation-bridge.git
  [[ $revision =~ ^[a-f0-9]{40}$ && $version =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || return 1
  [[ $(uname -m) == x86_64 && $(id -u) != 0 ]] || {
    printf 'Unprivileged amd64 builder required\n' >&2
    return 1
  }
  # The Go distribution includes Unicode test filenames; Arch provides C.UTF-8.
  export LC_ALL=C.UTF-8
  [[ -d $installer_bundle && ! -L $installer_bundle && -f $installer_bundle/bootstrap-source.tar.gz && ! -L $installer_bundle/bootstrap-source.tar.gz &&
    -f $installer_bundle/source.lock && ! -L $installer_bundle/source.lock ]] || {
    printf 'Selected installer source bundle is missing or unsafe; do not substitute a checkout.\n' >&2
    return 1
  }
  [[ ! -e $work/checkout && ! -L $work/checkout && ! -e $work/installer && ! -L $work/installer && ! -e $work/source && ! -L $work/source && ! -e $work/output && ! -L $work/output ]] || return 1
  local archives=() packages=()
  shopt -s nullglob
  export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/false
  git init "$work/checkout"
  git -C "$work/checkout" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 fetch --depth=1 "$repository" "$revision"
  [[ $(git -C "$work/checkout" rev-parse 'FETCH_HEAD^{commit}') == "$revision" ]] || return 1
  git -C "$work/checkout" -c core.hooksPath=/dev/null checkout --detach "$revision"
  [[ $(git -C "$work/checkout" rev-parse HEAD) == "$revision" ]] || return 1
  installer_hash=$(awk -F= '$1=="SOURCE_SHA256" {n++; v=$2} END {if(n!=1 || v !~ /^[a-f0-9]{64}$/) exit 1; print v}' "$installer_bundle/source.lock") || {
    printf 'Selected installer source lock lacks one valid source identity.\n' >&2
    return 1
  }
  [[ $(sha256sum "$installer_bundle/bootstrap-source.tar.gz" | awk '{print $1}') == "$installer_hash" ]] || {
    printf 'Selected installer source archive differs from its lock; retain both for review.\n' >&2
    return 1
  }
  # The archive is source data, never a path authority. Its exact-tree manifest
  # must verify before Bridge uses it as the selected-pair contract input.
  if bsdtar -tzf "$installer_bundle/bootstrap-source.tar.gz" | awk '
    /^project\/[A-Za-z0-9_.@\/-]+$/ && $0 !~ /(^|\/)\.\.($|\/)/ { next }
    { exit 1 }
  '; then :; else
    printf 'Selected installer source archive has an unsafe entry.\n' >&2
    return 1
  fi
  mkdir "$work/installer"
  bsdtar -xzf "$installer_bundle/bootstrap-source.tar.gz" -C "$work/installer"
  installer_root="$work/installer/project"
  [[ -d $installer_root && ! -L $installer_root && -f $installer_root/SOURCE-MANIFEST.sha256 && ! -L $installer_root/SOURCE-MANIFEST.sha256 ]] || return 1
  if find "$installer_root" -mindepth 1 ! -type d ! -type f -print -quit | grep -q .; then
    printf 'Selected installer source tree contains a non-regular entry.\n' >&2
    return 1
  fi
  (
    cd "$installer_root"
    awk '{if ($1 !~ /^[a-f0-9]{64}$/ || $2 !~ /^[A-Za-z0-9_.@\/-]+$/ || $2 ~ /(^|\/)\.\.($|\/)/) exit 1; print $2}' SOURCE-MANIFEST.sha256 | sort >"$work/installer-expected-files"
    printf '%s\n' SOURCE-MANIFEST.sha256 source.files >>"$work/installer-expected-files"
    sort -u "$work/installer-expected-files" -o "$work/installer-expected-files"
    find . -type f -print | sed 's|^\./||' | sort >"$work/installer-actual-files"
    cmp "$work/installer-expected-files" "$work/installer-actual-files"
  ) || {
    printf 'Selected installer source tree differs from its declared source closure.\n' >&2
    return 1
  }
  (cd "$installer_root" && sha256sum -c --strict SOURCE-MANIFEST.sha256) || {
    printf 'Selected installer source tree does not match its manifest.\n' >&2
    return 1
  }
  # Older published recipes cannot satisfy the existing signed-bundle contract.
  grep -Fq '/source.sha256' "$work/checkout/packaging/arch/PKGBUILD.in" || {
    printf 'Selected Bridge commit lacks ISO package source identity; publish/review the Bridge integration changes and select their exact commit. No fallback to local edits.\n' >&2
    return 1
  }
  mkdir "$work/source"
  cd "$work/source"
  go_version=$(awk -F= '$1=="BRIDGE_GO_VERSION" {n++; v=$2} END {if(n!=1) exit 1; print v}' "$builder/versions.lock") || return 1
  go_hash=$(awk -F= '$1=="BRIDGE_GO_LINUX_AMD64_SHA256" {n++; v=$2} END {if(n!=1) exit 1; print v}' "$builder/versions.lock") || return 1
  [[ $go_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $go_hash =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ $(<"$work/checkout/.go-version") == "$go_version" ]] || {
    printf 'Bridge requires another Go version; review the build-only compiler lock before retrying\n' >&2
    return 1
  }
  # No auto toolchain resolver. The official archive is installed only in this job.
  curl -q --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --max-time 300 \
    "https://go.dev/dl/go$go_version.linux-amd64.tar.gz" -o go.tar.gz
  printf '%s  go.tar.gz\n' "$go_hash" | sha256sum --check --strict
  mkdir toolchain
  bsdtar -xf go.tar.gz -C toolchain
  export PATH="$PWD/toolchain/go/bin:$PATH" GOTOOLCHAIN=local GOENV=off
  export GOOS=linux GOARCH=amd64 CGO_ENABLED=0 GOMAXPROCS=4 GOFLAGS=-p=4
  export GOPATH="$work/gopath" GOCACHE="$work/gocache" GOSUMDB=sum.golang.org GOPROXY=https://proxy.golang.org
  [[ $(go env GOVERSION) == "go$go_version" ]] || return 1
  # This gate uses only the immutable source bundle selected for this installer
  # run. It is evidence for this release pair, not authority for an installed
  # Bridge runtime or host policy.
  (cd "$work/checkout" && go test -mod=readonly -list '^TestCandidateInstallerMemoryContract$' ./internal/memory | grep -Fxq TestCandidateInstallerMemoryContract) || {
    printf 'Selected Bridge commit lacks the required selected-installer memory-contract test.\n' >&2
    return 1
  }
  (cd "$work/checkout" && BRIDGE_INSTALLER_MEMORY_CANDIDATE="$installer_root" \
    go test -mod=readonly -count=1 -v -run '^TestCandidateInstallerMemoryContract$' ./internal/memory) || {
    printf 'Bridge memory contract rejected the selected installer source; no package was emitted.\n' >&2
    return 1
  }
  (cd "$work/checkout" && go test -mod=readonly -list '^TestCandidateInstallerPerformanceContract$' ./internal/performance | grep -Fxq TestCandidateInstallerPerformanceContract) || {
    printf 'Selected Bridge commit lacks the required selected-installer performance-contract test.\n' >&2
    return 1
  }
  (cd "$work/checkout" && BRIDGE_INSTALLER_PERFORMANCE_CANDIDATE="$installer_root" \
    go test -mod=readonly -count=1 -v -run '^TestCandidateInstallerPerformanceContract$' ./internal/performance) || {
    printf 'Bridge performance contract rejected the selected installer source; no package was emitted.\n' >&2
    return 1
  }
  # Generate only from the verified checkout, using that commit's own generator.
  (cd "$work/checkout" && go run ./cmd/bridge-arch-package --version "$version")
  dirty=$(git -C "$work/checkout" status --porcelain --untracked-files=no) || return 1
  [[ -z $dirty ]] || return 1
  input="$work/checkout/dist/arch"
  archives=("$input"/spry-bridge-*-src.tar.gz)
  ((${#archives[@]} == 1)) || return 1
  archive="spry-bridge-${version#v}-src.tar.gz"
  [[ ${archives[0]##*/} == "$archive" ]] || return 1
  for field in "$archive" PKGBUILD SHA256SUMS; do [[ -f $input/$field && ! -L $input/$field ]] || return 1; done
  cp -- "$input/$archive" "$input/PKGBUILD" "$input/SHA256SUMS" "$work/source/"
  # Compare the exact two-file manifest, never follow paths supplied in a checksum file.
  sha256sum "$archive" PKGBUILD >expected-sums
  cmp SHA256SUMS expected-sums || return 1
  source_hash=$(sha256sum "$archive" | awk '{print $1}')
  recipe_hash=$(sha256sum PKGBUILD | awk '{print $1}')
  # Explicitly retain recipe check(); neither dependencies nor tests are bypassed.
  export SOURCE_DATE_EPOCH
  SOURCE_DATE_EPOCH=$(awk -F= '$1=="SOURCE_DATE_EPOCH" {print $2}' "$builder/versions.lock")
  [[ $SOURCE_DATE_EPOCH =~ ^[0-9]{10}$ ]] || return 1
  makepkg --verifysource
  makepkg --cleanbuild
  packages=(./spry-ai-workstation-bridge-*.pkg.tar.zst)
  ((${#packages[@]} == 1)) || return 1
  package=${packages[0]}
  bsdtar -xOf "$package" .PKGINFO >package-info
  bsdtar -xOf "$package" .BUILDINFO >build-info
  bsdtar -xOf "$package" usr/share/doc/spry-ai-workstation-bridge/source.sha256 >package-source
  [[ $(sed -n 's/^pkgname = //p' package-info) == spry-ai-workstation-bridge &&
  $(sed -n 's/^arch = //p' package-info) == x86_64 &&
  $(sed -n 's/^pkgver = //p' package-info) == "${version#v}-1" ]] || return 1
  [[ $(sed -n 's/^pkgbuild_sha256sum = //p' build-info) == "$recipe_hash" &&
  $(<package-source) == "$source_hash" ]] || return 1
  sha256sum "$archive" PKGBUILD >final-sums
  cmp expected-sums final-sums || return 1
  (cd "$input" && sha256sum "$archive" PKGBUILD) >input-sums
  cmp expected-sums input-sums || return 1
  dirty=$(git -C "$work/checkout" status --porcelain --untracked-files=no) || return 1
  [[ $(git -C "$work/checkout" rev-parse HEAD) == "$revision" && -z $dirty ]] || return 1
  # Publish only to this new local output after all checks; failures retain job state.
  mkdir "$work/output"
  cp -- "$package" "$archive" PKGBUILD "$work/output/"
  cp "$builder/versions.lock" "$builder/packages.txt" "$work/output/"
  cp build-info "$work/output/BUILDINFO"
  go env -json GOVERSION GOOS GOARCH CGO_ENABLED GOFLAGS >"$work/output/go-environment.json"
  printf 'BUILDER_IMAGE_ID=%s\nGO_VERSION=%s\nGO_ARCHIVE_SHA256=%s\nSOURCE_SHA256=%s\nPKGBUILD_SHA256=%s\nINSTALLER_SOURCE_SHA256=%s\nMEMORY_CONTRACT=selected-installer-memory-v1\nPERFORMANCE_CONTRACT=selected-installer-performance-v1\nGOMAXPROCS=4\nGOFLAGS=-p=4\nSOURCE_REPOSITORY=%s\nSOURCE_COMMIT=%s\n' \
    "${BUILDER_IMAGE_ID:?}" "$go_version" "$go_hash" "$source_hash" "$recipe_hash" "$installer_hash" "$repository" "$revision" >"$work/output/builder.lock"
  (cd "$work/output" && sha256sum ./*.pkg.tar.zst "$archive" PKGBUILD versions.lock packages.txt BUILDINFO go-environment.json builder.lock >SHA256SUMS)
  printf 'Unsigned Bridge package built and recipe tests passed; signing, bundling and workstation qualification are separate.\n'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then bridge_build "$@"; fi
