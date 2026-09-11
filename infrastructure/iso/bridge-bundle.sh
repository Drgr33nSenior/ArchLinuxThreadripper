#!/usr/bin/env bash
# Run in the unprivileged Arch builder. No keys, installation or service startup.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/bootstrap/bridge.sh"
(($# == 3)) || common::die 'usage: bridge-bundle.sh RUN_PACKAGES REVIEWED_BRIDGE_ARTIFACTS NEW_OUTPUT'
packages=$1 candidate=$2 output=$3
[[ ! -e $output && ! -L $output ]] || common::die 'output exists; select a fresh bundle run'
mkdir "$output"
mkdir "$output/dependencies" "$output/evidence" "$output/work"
shopt -s nullglob
selected=("$candidate"/spry-ai-workstation-bridge-*.pkg.tar.zst)
((${#selected[@]} == 1)) || common::die 'select exactly one Bridge package'
pkg=${selected[0]}
[[ $(bridge_field "$pkg" pkgname) == spry-ai-workstation-bridge && $(bridge_field "$pkg" arch) == x86_64 ]] || common::die 'wrong Bridge package name/architecture'
bridge_source=$(bsdtar -xOf "$pkg" usr/share/doc/spry-ai-workstation-bridge/source.sha256)
[[ $bridge_source =~ ^[a-f0-9]{64}$ ]] || common::die 'Bridge package lacks source-archive identity; rebuild with the reviewed recipe'
[[ -f $candidate/builder.lock && ! -L $candidate/builder.lock ]] || common::die 'Bridge build evidence lacks the selected installer memory-contract identity'
installer_source=$(common::lock_get "$packages/source.lock" SOURCE_SHA256)
sources=("$candidate"/spry-bridge-*-src.tar.gz)
((${#sources[@]} == 1)) || common::die 'retain the exact Bridge source archive'
[[ $(common::sha256_file "${sources[0]}") == "$bridge_source" ]] || common::die 'Bridge source archive differs from package provenance'
[[ -f $candidate/PKGBUILD && ! -L $candidate/PKGBUILD ]] || common::die 'reviewed Bridge PKGBUILD is required'
recipe_hash=$(bsdtar -xOf "$pkg" .BUILDINFO | awk -F ' = ' '$1=="pkgbuild_sha256sum" {n++; v=$2} END {if(n!=1) exit 1; print v}')
[[ $(common::sha256_file "$candidate/PKGBUILD") == "$recipe_hash" ]] || common::die 'Bridge .BUILDINFO does not identify this recipe'
grep -Fq "sha256sums=('$bridge_source')" "$candidate/PKGBUILD" || common::die 'recipe/source digest mismatch'
bridge_memory_contract_identity "$candidate/builder.lock" "$installer_source" "$bridge_source" "$recipe_hash" ||
  common::die 'Bridge build evidence does not identify this installer, source archive and recipe; do not bundle it'
snapshot=$(common::lock_get "$root/infrastructure/iso/versions.lock" ARCH_SNAPSHOT)
cp "$packages"/*.pkg.tar.zst "$output/"
cp "$pkg" "$output/"
cp "$packages/source.lock" "$output/"
cp "$candidate/PKGBUILD" "$output/evidence/Bridge.PKGBUILD"
cp "$candidate/builder.lock" "$output/evidence/Bridge.builder.lock"
cp "${sources[0]}" "$output/evidence/"
all=("$output"/*.pkg.tar.zst)
((${#all[@]} == 5)) || common::die 'expected four matching installer packages plus Bridge'
runtime=("$output"/arch-workstation-bridge-runtime-*.pkg.tar.zst)
((${#runtime[@]} == 1)) || common::die 'runtime payload missing'
mkdir "$output/work/tree"
for archive in "$pkg" "${runtime[0]}"; do
  entries=$(bsdtar -tf "$archive")
  if grep -Eq '(^/|(^|/)\.\.(/|$)|^etc/|^var/|^\.INSTALL$)' <<<"$entries"; then common::die 'unexpected active/unsafe package payload'; fi
  bsdtar -xf "$archive" -C "$output/work/tree"
done
tree="$output/work/tree/usr/lib/bridge"
[[ $(<"$tree/workstation-runtime/INSTALLER-SOURCE.sha256") == "$installer_source" &&
$(<"$tree/workstation-reference/INSTALLER-SOURCE.sha256") == "$installer_source" ]] || common::die 'runtime/reference source differs from installer run'
for kind in runtime reference; do (cd "$tree/workstation-$kind" && sha256sum -c --strict SOURCE-MANIFEST.sha256); done
bash "$tree/workstation-runtime/bin/workstationctl" agent configure "$output/work/native"
"$tree/bridge-hostd" --check-reference "$tree/workstation-reference" --check-native "$output/work/native" >"$output/evidence/catalog.json"
"$tree/bridge-hostd" --manifest "$tree/workstation-runtime" >"$output/evidence/runtime.json"
"$tree/bridge-hostd" --manifest "$tree/workstation-reference" >"$output/evidence/reference.json"
# Resolve against the installer's snapshot with an EMPTY local database. No Go,
# optional GPU SDK, models, host DB mutation or live-root dependency installation.
mkdir -p "$output/work/db/sync" "$output/work/cache"
printf '[options]\nArchitecture = x86_64\nSigLevel = Required DatabaseOptional\n' >"$output/work/pacman.conf"
for repo in core extra; do
  url="https://archive.archlinux.org/repos/$snapshot/$repo/os/x86_64"
  curl -q --fail --silent --show-error --proto '=https' --max-time 180 "$url/$repo.db" -o "$output/work/db/sync/$repo.db"
  printf '\n[%s]\nServer = %s\n' "$repo" "$url" >>"$output/work/pacman.conf"
done
dependencies=()
while IFS= read -r dep; do dependencies+=("$dep"); done < <(for archive in "$pkg" "${runtime[0]}"; do bsdtar -xOf "$archive" .PKGINFO | sed -n 's/^depend = //p'; done | sort -u)
pacman --config "$output/work/pacman.conf" --dbpath "$output/work/db" --cachedir "$output/work/cache" --logfile "$output/work/pacman.log" \
  -Sp --print-format '%l' -- "${dependencies[@]}" >"$output/evidence/dependency-urls.txt"
while IFS= read -r url; do
  [[ $url == "https://archive.archlinux.org/repos/$snapshot/"* && $url == *.pkg.tar.zst ]] || common::die 'unexpected dependency URL'
  name=${url##*/}
  for suffix in '' .sig; do curl -q --fail --silent --show-error --proto '=https' --max-time 180 "$url$suffix" -o "$output/dependencies/$name$suffix"; done
  bridge_official_signature "$output/dependencies/$name" || common::die 'official dependency signature failed'
done <"$output/evidence/dependency-urls.txt"
repo-add "$output/arch-workstation.db.tar.gz" "${all[@]}"
record() {
  local file=$1
  jq -n --arg file "${file##*/}" --arg name "$(bridge_field "$file" pkgname)" --arg version "$(bridge_field "$file" pkgver)" \
    --arg arch "$(bridge_field "$file" arch)" --arg sha256 "$(common::sha256_file "$file")" \
    '{file:$file,name:$name,version:$version,arch:$arch,sha256:$sha256}'
}
for archive in "${all[@]}"; do record "$archive"; done | jq -s . >"$output/work/packages.json"
for archive in "$output"/dependencies/*.pkg.tar.zst; do record "$archive"; done | jq -s . >"$output/work/dependencies.json"
jq -n --arg installer_source "$installer_source" --arg bridge_source "$bridge_source" --arg snapshot "$snapshot" \
  --arg database_sha256 "$(common::sha256_file "$output/arch-workstation.db.tar.gz")" \
  --slurpfile packages "$output/work/packages.json" --slurpfile dependencies "$output/work/dependencies.json" \
  '{schema:1,installer_source:$installer_source,bridge_source:$bridge_source,snapshot:$snapshot,database_sha256:$database_sha256,packages:$packages[0],dependencies:$dependencies[0]}' >"$output/bridge-bundle.json"
bridge_verify_bundle "$output" || common::die 'bundle identity/repository verification failed'
printf 'PENDING owner signatures: signed offline transaction validation runs in installer preflight. Snapshot dependency resolution completed; no package installation performed.\n' >"$output/evidence/offline-transaction.txt"
(cd "$output" && sha256sum ./*.pkg.tar.zst arch-workstation.db.tar.gz bridge-bundle.json dependencies/*.pkg.tar.zst evidence/Bridge.PKGBUILD evidence/Bridge.builder.lock >SHA256SUMS)
printf 'Unsigned candidate bundle ready; owner review/signing required. Services and hardware NOT qualified.\n'
