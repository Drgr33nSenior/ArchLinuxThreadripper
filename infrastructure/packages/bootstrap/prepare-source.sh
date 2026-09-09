#!/usr/bin/env bash
# Produce local source artifacts only. Never build or sign packages as root.
set -euo pipefail
export LC_ALL=C TZ=UTC
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)
source "$root/lib/common.sh"
(($# == 1)) || common::die 'usage: prepare-source.sh <new-output-directory>'
[[ ! -e $1 && ! -L $1 ]] || common::die 'output already exists; choose a new directory'
command -v bsdtar >/dev/null || common::die 'libarchive bsdtar is required'
epoch=$(common::lock_get "$root/infrastructure/iso/versions.lock" SOURCE_DATE_EPOCH)
version=$(common::lock_get "$root/infrastructure/iso/versions.lock" BOOTSTRAP_PACKAGE_VERSION)
[[ $epoch =~ ^[0-9]{10}$ && $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || common::die 'invalid source epoch or package version'
timestamp=$(date -u -d "@$epoch" +%Y%m%d%H%M.%S 2>/dev/null) || timestamp=$(date -u -r "$epoch" +%Y%m%d%H%M.%S)
mkdir -- "$1"
output=$(cd -- "$1" && pwd -P)
mkdir "$output/project"
# Only exact, reviewed paths. Reject symlinks, including symlinked ancestors.
while IFS= read -r path; do
  [[ $path =~ ^[A-Za-z0-9_./@-]+$ && $path != /* && $path != *..* ]] || common::die 'unsafe source allowlist entry'
  component=$path
  while [[ $component != . ]]; do
    [[ ! -L $root/$component ]] || common::die "symlink in source allowlist: $path"
    component=$(dirname -- "$component")
  done
  [[ -f $root/$path ]] || common::die "missing source: $path"
  mkdir -p -- "$output/project/$(dirname -- "$path")"
  install -m644 "$root/$path" "$output/project/$path"
done <"$root/infrastructure/packages/bootstrap/source.files"
cp "$root/infrastructure/packages/bootstrap/source.files" "$output/project/source.files"
revision=$(git -C "$root" rev-parse --verify HEAD 2>/dev/null) || revision=unknown
printf 'REPOSITORY_REVISION=%s\nSOURCE_IDENTITY=SOURCE-MANIFEST.sha256\n' "$revision" >"$output/project/BUILD-IDENTITY"
(
  cd "$output/project"
  while IFS= read -r path; do printf '%s  %s\n' "$(common::sha256_file "$path")" "$path"; done <source.files
  printf '%s  BUILD-IDENTITY\n' "$(common::sha256_file BUILD-IDENTITY)"
) >"$output/project/SOURCE-MANIFEST.sha256"
{
  sed 's|^|project/|' "$output/project/source.files"
  printf '%s\n' project/source.files project/SOURCE-MANIFEST.sha256 project/BUILD-IDENTITY
} | sort >"$output/archive.files"
while IFS= read -r path; do touch -t "$timestamp" "$output/$path"; done <"$output/archive.files"
bsdtar --format=ustar --uid 0 --gid 0 --uname root --gname root -cf - -C "$output" -T "$output/archive.files" | gzip -n >"$output/bootstrap-source.tar.gz"
digest=$(common::sha256_file "$output/bootstrap-source.tar.gz")
printf 'SOURCE_SHA256=%s\nPACKAGE_VERSION=%s\nSOURCE_DATE_EPOCH=%s\n' "$digest" "$version" "$epoch" >"$output/source.lock"
# The source archive contains the canonical template. Only its literal header
# pins differ in the generated recipe; makepkg needs no out-of-band source.lock.
sed -e "s/^_source_digest='@SOURCE_SHA256@'$/_source_digest='$digest'/" \
  -e "s/^_source_version='@PACKAGE_VERSION@'$/_source_version='$version'/" \
  -e "s/^_source_epoch='@SOURCE_DATE_EPOCH@'$/_source_epoch='$epoch'/" \
  "$output/project/infrastructure/packages/bootstrap/PKGBUILD" >"$output/PKGBUILD"
common::info "Prepared source $digest; makepkg/signing/ISO creation have NOT run."
