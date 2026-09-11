#!/usr/bin/env bash
# Real synthetic archives; signatures and pacman are fixtures, never host installs.
# shellcheck disable=SC2329
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/bootstrap/common.sh"
source "$root/lib/bootstrap/config.sh"
source "$root/lib/bootstrap/bridge.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/bundle/dependencies" "$work/db" "$work/target/etc/bridge"
dir="$work/bundle"
source_hash=$(printf source | common::sha256_file -)
bridge_source=$(printf bridge-source | common::sha256_file -)
recipe_hash=$(printf bridge-recipe | common::sha256_file -)
printf 'INSTALLER_SOURCE_SHA256=%s\nSOURCE_SHA256=%s\nPKGBUILD_SHA256=%s\nMEMORY_CONTRACT=selected-installer-memory-v1\nPERFORMANCE_CONTRACT=selected-installer-performance-v1\n' "$source_hash" "$bridge_source" "$recipe_hash" >"$work/builder.lock"
bridge_memory_contract_identity "$work/builder.lock" "$source_hash" "$bridge_source" "$recipe_hash"
sed '/^PERFORMANCE_CONTRACT=/d' "$work/builder.lock" >"$work/missing-performance.lock"
if bridge_memory_contract_identity "$work/missing-performance.lock" "$source_hash" "$bridge_source" "$recipe_hash"; then exit 1; fi
sed 's/PERFORMANCE_CONTRACT=selected-installer-performance-v1/PERFORMANCE_CONTRACT=wrong/' "$work/builder.lock" >"$work/wrong-performance.lock"
if bridge_memory_contract_identity "$work/wrong-performance.lock" "$source_hash" "$bridge_source" "$recipe_hash"; then exit 1; fi
if bridge_memory_contract_identity "$work/builder.lock" "$(printf different | common::sha256_file -)" "$bridge_source" "$recipe_hash"; then exit 1; fi
if bridge_memory_contract_identity "$work/builder.lock" "$source_hash" "$(printf another-source | common::sha256_file -)" "$recipe_hash"; then exit 1; fi
if bridge_memory_contract_identity "$work/builder.lock" "$source_hash" "$bridge_source" "$(printf another-recipe | common::sha256_file -)"; then exit 1; fi
printf 'INSTALLER_SOURCE_SHA256=%s\nSOURCE_SHA256=%s\nPKGBUILD_SHA256=%s\nMEMORY_CONTRACT=wrong\n' "$source_hash" "$bridge_source" "$recipe_hash" >"$work/wrong-builder.lock"
if bridge_memory_contract_identity "$work/wrong-builder.lock" "$source_hash" "$bridge_source" "$recipe_hash"; then exit 1; fi
printf 'INSTALLER_SOURCE_SHA256=%s\nINSTALLER_SOURCE_SHA256=%s\nSOURCE_SHA256=%s\nPKGBUILD_SHA256=%s\nMEMORY_CONTRACT=selected-installer-memory-v1\n' "$source_hash" "$source_hash" "$bridge_source" "$recipe_hash" >"$work/duplicate-builder.lock"
if bridge_memory_contract_identity "$work/duplicate-builder.lock" "$source_hash" "$bridge_source" "$recipe_hash"; then exit 1; fi
for name in arch-workstation-bootstrap arch-workstation-boot arch-workstation-backup arch-workstation-bridge-runtime spry-ai-workstation-bridge bash; do
  pkgdir="$work/$name"
  mkdir "$pkgdir"
  arch=any
  [[ $name != spry-ai-workstation-bridge && $name != bash ]] || arch=x86_64
  printf 'pkgname = %s\npkgver = 1.0.0-1\narch = %s\n' "$name" "$arch" >"$pkgdir/.PKGINFO"
  path=''
  case $name in
    arch-workstation-bootstrap) path=usr/lib/arch-workstation-bootstrap/INSTALLER-SOURCE.sha256 ;;
    arch-workstation-bridge-runtime) path=usr/lib/bridge/workstation-runtime/INSTALLER-SOURCE.sha256 ;;
    spry-ai-workstation-bridge) path=usr/share/doc/spry-ai-workstation-bridge/source.sha256 ;;
  esac
  if [[ -n $path ]]; then
    mkdir -p "$pkgdir/$(dirname "$path")"
    printf '%s\n' "$source_hash" >"$pkgdir/$path"
  fi
  filename="$name-1.0.0-1-$arch.pkg.tar.zst"
  destination=$dir
  [[ $name != bash ]] || destination="$dir/dependencies"
  bsdtar -cf "$destination/$filename" -C "$pkgdir" .PKGINFO ${path:+"$path"}
  printf 'SYNTHETIC SIGNATURE\n' >"$destination/$filename.sig"
  digest=$(common::sha256_file "$destination/$filename")
  jq -n --arg file "$filename" --arg name "$name" --arg arch "$arch" --arg sha256 "$digest" \
    '{file:$file,name:$name,version:"1.0.0-1",arch:$arch,sha256:$sha256}' >"$work/$name.json"
  if [[ $name != bash ]]; then
    mkdir "$work/db/$name"
    printf '%%FILENAME%%\n%s\n\n%%NAME%%\n%s\n\n%%VERSION%%\n1.0.0-1\n\n%%ARCH%%\n%s\n\n%%SHA256SUM%%\n%s\n' "$filename" "$name" "$arch" "$digest" >"$work/db/$name/desc"
  fi
done
bsdtar -czf "$dir/arch-workstation.db.tar.gz" -C "$work/db" .
printf 'SYNTHETIC SIGNATURE\n' >"$dir/arch-workstation.db.tar.gz.sig"
jq -s . "$work"/arch-workstation-*.json "$work/spry-ai-workstation-bridge.json" >"$work/packages.json"
jq -n --arg hash "$source_hash" --arg db "$(common::sha256_file "$dir/arch-workstation.db.tar.gz")" \
  --slurpfile packages "$work/packages.json" --slurpfile deps "$work/bash.json" \
  '{schema:1,installer_source:$hash,bridge_source:$hash,snapshot:"2026/09/04",database_sha256:$db,packages:$packages[0],dependencies:$deps}' >"$dir/bridge-bundle.json"
printf 'SYNTHETIC SIGNATURE\n' >"$dir/bridge-bundle.json.sig"
# Failed official-keyring preparation must preserve failure and clean its own
# scratch directory without an unbound-variable trap masking the primary error.
(
  mkdir "$work/signature-scratch"
  export TMPDIR="$work/signature-scratch"
  gpg() { return 7; }
  if bridge_official_signature "$dir/dependencies/bash-1.0.0-1-x86_64.pkg.tar.zst" 2>"$work/signature-error"; then exit 1; fi
  [[ -z $(ls -A "$TMPDIR") && ! -s $work/signature-error ]]
)
gpg() { [[ ${bad_signature:-false} == false ]] && printf '[GNUPG:] VALIDSIG AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n[GNUPG:] TRUST_FULLY\n'; }
bridge_official_signature() { [[ ${bad_official:-false} == false ]]; }
bridge_verify_bundle "$dir" fixture AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
if (
  bad_signature=true
  bridge_verify_bundle "$dir" fixture
); then exit 1; fi
if (
  bad_official=true
  bridge_verify_bundle "$dir" fixture
); then exit 1; fi
cp "$dir/bridge-bundle.json" "$work/original.json"
# Arch dependency filenames may contain a version epoch (for example zlib).
mv "$dir/dependencies/bash-1.0.0-1-x86_64.pkg.tar.zst" "$dir/dependencies/bash-1:1.0.0-1-x86_64.pkg.tar.zst"
jq '.dependencies[0].file="bash-1:1.0.0-1-x86_64.pkg.tar.zst"' "$work/original.json" >"$dir/bridge-bundle.json"
bridge_verify_bundle "$dir"
mv "$dir/dependencies/bash-1:1.0.0-1-x86_64.pkg.tar.zst" "$dir/dependencies/bash-1.0.0-1-x86_64.pkg.tar.zst"
cp "$work/original.json" "$dir/bridge-bundle.json"
for change in '.packages[0].version="wrong"' '.packages[0].name="wrong"' '.packages[0].arch="aarch64"' '.dependencies=[]' '.installer_source="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' '.database_sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'; do
  jq "$change" "$work/original.json" >"$dir/bridge-bundle.json"
  if bridge_verify_bundle "$dir" fixture; then exit 1; fi
done
cp "$work/original.json" "$dir/bridge-bundle.json"
cp "$dir/arch-workstation.db.tar.gz" "$work/original.db"
printf '%%FILENAME%%\nwrong.pkg.tar.zst\n' >>"$work/db/arch-workstation-boot/desc"
bsdtar -czf "$dir/arch-workstation.db.tar.gz" -C "$work/db" .
jq --arg hash "$(common::sha256_file "$dir/arch-workstation.db.tar.gz")" '.database_sha256=$hash' "$work/original.json" >"$dir/bridge-bundle.json"
if bridge_verify_bundle "$dir" fixture; then exit 1; fi
cp "$work/original.db" "$dir/arch-workstation.db.tar.gz"
cp "$work/original.json" "$dir/bridge-bundle.json"
BOOTSTRAP_ROOT="$work/source"
mkdir -p "$BOOTSTRAP_ROOT/infrastructure/iso"
cp "$root/infrastructure/iso/versions.lock" "$BOOTSTRAP_ROOT/infrastructure/iso/versions.lock"
printf '%s\n' "$source_hash" >"$BOOTSTRAP_ROOT/INSTALLER-SOURCE.sha256"
BOOTSTRAP_BRIDGE_BUNDLE=$dir
BOOTSTRAP_TARGET="$work/target"
BOOTSTRAP_DRY_RUN=0
sed 's/^INSTALL_BRIDGE=true$/INSTALL_BRIDGE=false/' "$root/config/install.conf.example" >"$work/disabled.conf"
bootstrap_load_config "$work/disabled.conf"
[[ $INSTALL_BRIDGE == false ]]
sed 's/^INSTALL_BRIDGE=true$/INSTALL_BRIDGE=maybe/' "$root/config/install.conf.example" >"$work/invalid.conf"
if (bootstrap_load_config "$work/invalid.conf" >/dev/null 2>&1); then exit 1; fi
bootstrap_load_config "$root/config/install.conf.example"
[[ $INSTALL_BRIDGE == true ]]
pacman() {
  if [[ " $* " == *' -Up '* ]]; then
    [[ ${missing_dependency:-false} == false ]]
    return
  fi
  [[ $1 == --root && $2 == "$BOOTSTRAP_TARGET" && " $* " == *' -U '* ]] || return 1
  printf '%s\n' "$*" >>"$work/target-transactions"
}
bootstrap_run() { "$@"; }
install() {
  [[ $1 == -Dm644 ]] || return 1
  mkdir -p "$(dirname "$3")"
  command install -m644 "$2" "$3"
}
bootstrap_bridge_preflight >/dev/null
if (
  missing_dependency=true
  bootstrap_bridge_preflight >/dev/null 2>&1
); then exit 1; fi
printf 'OWNER_STATE_SENTINEL\n' >"$BOOTSTRAP_TARGET/etc/bridge/server.json"
printf '[options]\nSigLevel = Required DatabaseOptional\n' >"$BOOTSTRAP_TARGET/etc/pacman.conf"
bootstrap_install_bridge >/dev/null
bootstrap_install_bridge >/dev/null
[[ $(wc -l <"$work/target-transactions" | tr -d ' ') == 2 ]]
[[ $(<"$BOOTSTRAP_TARGET/etc/bridge/server.json") == OWNER_STATE_SENTINEL ]]
printf '\n' >>"$dir/bridge-bundle.json"
if bootstrap_install_bridge >/dev/null 2>&1; then exit 1; fi
INSTALL_BRIDGE=false
bootstrap_bridge_preflight >/dev/null
bootstrap_install_bridge
[[ $(wc -l <"$work/target-transactions" | tr -d ' ') == 2 ]]
printf 'Bridge archive, signature, dependency, preflight and target-root fixtures passed; no real package installed\n'
