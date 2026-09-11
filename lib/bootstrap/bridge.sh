#!/usr/bin/env bash
# Shared release/installer checks. Read archives; never extract or install here.
bridge_field() {
  bsdtar -xOf "$1" .PKGINFO | awk -F ' = ' -v key="$2" '$1==key {n++; v=$2} END {if(n!=1) exit 1; print v}'
}

bridge_signature() {
  local file=$1 keyring=$2 fingerprint=${3:-} status
  [[ -f $file.sig && ! -L $file.sig ]] || {
    common::warn 'Bridge artifact signature missing'
    return 1
  }
  status=$(gpg --batch --homedir "$keyring" --status-fd 1 --verify -- "$file.sig" "$file" 2>/dev/null) || return 1
  awk -v f="$fingerprint" '$2=="VALIDSIG" && (f=="" || $3==f || $NF==f) {v=1}
    $2=="TRUST_FULLY" || $2=="TRUST_ULTIMATE" {t=1} END {exit !(v && t)}' <<<"$status"
}

bridge_official_signature() (
  local status fingerprint signer
  [[ -f $1.sig && ! -L $1.sig ]] || return 1
  scratch=$(mktemp -d) || return 1
  trap 'rm -rf -- "$scratch"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  # Arch ships an armored keyring; gpgv expects binary OpenPGP packets.
  gpg --batch --homedir "$scratch" --dearmor --output "$scratch/archlinux.gpg" /usr/share/pacman/keyrings/archlinux.gpg 2>/dev/null || return 1
  status=$(gpgv --homedir "$scratch" --status-fd 1 --keyring "$scratch/archlinux.gpg" "$1.sig" "$1" 2>/dev/null) || return 1
  fingerprint=$(awk '$2=="VALIDSIG" {print $NF}' <<<"$status")
  signer=$(awk '$2=="VALIDSIG" {print $3}' <<<"$status")
  [[ $fingerprint =~ ^[A-F0-9]{40}$ && $signer =~ ^[A-F0-9]{40}$ ]] || return 1
  awk -v p="$fingerprint" -v s="$signer" '$0==p || $0==s {revoked=1} END {exit revoked}' /usr/share/pacman/keyrings/archlinux-revoked
)

# The Bridge build gate is bound to the immutable installer source selected for
# this release run. It is release evidence only; installed runtime policy keeps
# its separate manifest and qualification checks.
bridge_memory_contract_identity() {
  (($# == 4)) || return 1
  local lock=$1 installer=$2 bridge_source=$3 recipe=$4 source kind performance built_source built_recipe
  [[ -f $lock && ! -L $lock && $installer =~ ^[a-f0-9]{64}$ && $bridge_source =~ ^[a-f0-9]{64}$ && $recipe =~ ^[a-f0-9]{64}$ ]] || return 1
  source=$(awk -F= '$1=="INSTALLER_SOURCE_SHA256" {n++; v=$2} END {if(n!=1 || v !~ /^[a-f0-9]{64}$/) exit 1; print v}' "$lock") || return 1
  built_source=$(awk -F= '$1=="SOURCE_SHA256" {n++; v=$2} END {if(n!=1 || v !~ /^[a-f0-9]{64}$/) exit 1; print v}' "$lock") || return 1
  built_recipe=$(awk -F= '$1=="PKGBUILD_SHA256" {n++; v=$2} END {if(n!=1 || v !~ /^[a-f0-9]{64}$/) exit 1; print v}' "$lock") || return 1
  kind=$(awk -F= '$1=="MEMORY_CONTRACT" {n++; v=$2} END {if(n!=1) exit 1; print v}' "$lock") || return 1
  performance=$(awk -F= '$1=="PERFORMANCE_CONTRACT" {n++; v=$2} END {if(n!=1) exit 1; print v}' "$lock") || return 1
  [[ $kind == selected-installer-memory-v1 && $performance == selected-installer-performance-v1 && $source == "$installer" && $built_source == "$bridge_source" && $built_recipe == "$recipe" ]]
}

bridge_verify_bundle() (
  local dir=$1 keyring=${2:-} fingerprint=${3:-} record path name version arch digest listing desc file count=0
  [[ -f $dir/bridge-bundle.json && ! -L $dir/bridge-bundle.json ]] || return 1
  [[ -z $keyring ]] || bridge_signature "$dir/bridge-bundle.json" "$keyring" "$fingerprint" || return 1
  jq -e '
    def hash: type=="string" and test("^[a-f0-9]{64}$");
    def entry: (.file|test("^[A-Za-z0-9+_.:-]+[.]pkg[.]tar[.]zst$")) and
      (.sha256|hash) and (.name|test("^[a-z0-9+_.-]+$")) and
      (.version|type=="string" and length>0) and (.arch=="any" or .arch=="x86_64");
    .schema==1 and (.installer_source|hash) and (.bridge_source|hash) and
    (.snapshot|test("^[0-9]{4}/[0-9]{2}/[0-9]{2}$")) and (.database_sha256|hash) and
    (.packages|length==5) and (.packages|all(entry)) and
    (([.packages[].name]|sort) == (["arch-workstation-backup","arch-workstation-boot","arch-workstation-bootstrap","arch-workstation-bridge-runtime","spry-ai-workstation-bridge"]|sort)) and
    (.packages|all(if .name=="spry-ai-workstation-bridge" then .arch=="x86_64" else .arch=="any" end)) and
    ([.packages[]|select(.name!="spry-ai-workstation-bridge")|.version]|unique|length==1) and
    (.dependencies|length>0) and (.dependencies|all(entry)) and
    ([.packages[].file,.dependencies[].file]|length== (unique|length)) and
    ([.dependencies[].name]|length==(unique|length)) and
    ([.dependencies[].name]|index("go")==null)
  ' "$dir/bridge-bundle.json" >/dev/null || return 1
  [[ $(common::sha256_file "$dir/arch-workstation.db.tar.gz") == "$(jq -r .database_sha256 "$dir/bridge-bundle.json")" ]] || return 1
  [[ -z $keyring ]] || bridge_signature "$dir/arch-workstation.db.tar.gz" "$keyring" "$fingerprint" || return 1
  shopt -s nullglob
  local bundle_packages=("$dir"/*.pkg.tar.zst) deps=("$dir"/dependencies/*.pkg.tar.zst)
  ((${#bundle_packages[@]} == 5 && ${#deps[@]} == $(jq '.dependencies|length' "$dir/bridge-bundle.json"))) || return 1
  while IFS= read -r record; do
    path=$(jq -r .path <<<"$record")
    name=$(jq -r .name <<<"$record") version=$(jq -r .version <<<"$record")
    arch=$(jq -r .arch <<<"$record") digest=$(jq -r .sha256 <<<"$record")
    file="$dir/$path"
    [[ -f $file && ! -L $file && $(common::sha256_file "$file") == "$digest" ]] || return 1
    [[ $(bridge_field "$file" pkgname) == "$name" && $(bridge_field "$file" pkgver) == "$version" && $(bridge_field "$file" arch) == "$arch" ]] || return 1
    if [[ -n $keyring ]]; then
      if [[ $path == dependencies/* ]]; then
        bridge_official_signature "$file" || return 1
      else bridge_signature "$file" "$keyring" "$fingerprint" || return 1; fi
    fi
  done < <(
    jq -c '.packages[] | . + {path:.file}' "$dir/bridge-bundle.json"
    jq -c '.dependencies[] | . + {path:("dependencies/"+.file)}' "$dir/bridge-bundle.json"
  )
  # Database must describe exactly these local packages, not an older repo-add run.
  for name in arch-workstation-bootstrap arch-workstation-bridge-runtime spry-ai-workstation-bridge; do
    file="$dir/$(jq -r --arg n "$name" '.packages[]|select(.name==$n)|.file' "$dir/bridge-bundle.json")"
    case $name in
      arch-workstation-bootstrap)
        path=usr/lib/arch-workstation-bootstrap/INSTALLER-SOURCE.sha256
        digest=$(jq -r .installer_source "$dir/bridge-bundle.json")
        ;;
      arch-workstation-bridge-runtime)
        path=usr/lib/bridge/workstation-runtime/INSTALLER-SOURCE.sha256
        digest=$(jq -r .installer_source "$dir/bridge-bundle.json")
        ;;
      *)
        path=usr/share/doc/spry-ai-workstation-bridge/source.sha256
        digest=$(jq -r .bridge_source "$dir/bridge-bundle.json")
        ;;
    esac
    [[ $(bsdtar -xOf "$file" "$path") == "$digest" ]] || return 1
  done
  listing=$(bsdtar -tf "$dir/arch-workstation.db.tar.gz") || return 1
  local seen=' '
  while IFS= read -r path; do
    [[ $path == */desc && $path != /* && $path != *..* ]] || continue
    desc=$(bsdtar -xOf "$dir/arch-workstation.db.tar.gz" "$path") || return 1
    file=$(awk '/^%FILENAME%$/ {getline; print}' <<<"$desc")
    [[ $seen != *" $file "* ]] || return 1
    seen+="$file "
    name=$(awk '/^%NAME%$/ {getline; print}' <<<"$desc")
    version=$(awk '/^%VERSION%$/ {getline; print}' <<<"$desc")
    arch=$(awk '/^%ARCH%$/ {getline; print}' <<<"$desc")
    digest=$(awk '/^%SHA256SUM%$/ {getline; print}' <<<"$desc")
    jq -e --arg f "$file" --arg n "$name" --arg v "$version" --arg h "$digest" --arg a "$arch" \
      '[.packages[]|select(.file==$f and .name==$n and .version==$v and .sha256==$h and .arch==$a)]|length==1' "$dir/bridge-bundle.json" >/dev/null || return 1
    count=$((count + 1))
  done <<<"$listing"
  ((count == 5)) || return 1
)

bridge_offline_transaction() (
  local dir=$1
  scratch=$(mktemp -d) || return 1
  trap 'rm -rf -- "$scratch"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  mkdir "$scratch/db" "$scratch/root"
  printf '[options]\nArchitecture = x86_64\nSigLevel = Required DatabaseOptional\nLocalFileSigLevel = Required\n' >"$scratch/pacman.conf"
  local files=() path
  while IFS= read -r path; do files+=("$dir/$path"); done < <(
    jq -r '.packages[]|select(.name=="spry-ai-workstation-bridge" or .name=="arch-workstation-bridge-runtime")|.file' "$dir/bridge-bundle.json"
    jq -r '.dependencies[].file|"dependencies/"+.' "$dir/bridge-bundle.json"
  )
  # Empty private database proves dependency closure; print never installs.
  pacman --config "$scratch/pacman.conf" --root "$scratch/root" --dbpath "$scratch/db" \
    --logfile "$scratch/pacman.log" --arch x86_64 -Up --print-format '%n %v' -- "${files[@]}"
)

bootstrap_bridge_preflight() {
  [[ ${INSTALL_BRIDGE:-true} == true ]] || {
    bootstrap_log 'Bridge: installation explicitly disabled'
    return 0
  }
  local dir=${BOOTSTRAP_BRIDGE_BUNDLE:-/var/cache/arch-workstation/repo}
  bridge_verify_bundle "$dir" /etc/pacman.d/gnupg || {
    bootstrap_die 'Bridge bundle missing, stale, untrusted or altered'
    return 1
  }
  [[ $(jq -r .snapshot "$dir/bridge-bundle.json") == "$(common::lock_get "$BOOTSTRAP_ROOT/infrastructure/iso/versions.lock" ARCH_SNAPSHOT)" &&
  -f $BOOTSTRAP_ROOT/INSTALLER-SOURCE.sha256 &&
  $(jq -r .installer_source "$dir/bridge-bundle.json") == "$(<"$BOOTSTRAP_ROOT/INSTALLER-SOURCE.sha256")" ]] || {
    bootstrap_die 'Bridge/installer source or snapshot mismatch'
    return 1
  }
  bridge_offline_transaction "$dir" || {
    bootstrap_die 'Bridge offline dependency closure is incomplete'
    return 1
  }
  BOOTSTRAP_BRIDGE_SHA256=$(common::sha256_file "$dir/bridge-bundle.json")
  bootstrap_log "Bridge payload verified: installer $(jq -r .installer_source "$dir/bridge-bundle.json"), Bridge source $(jq -r .bridge_source "$dir/bridge-bundle.json"); services remain inactive"
}

bootstrap_install_bridge() {
  [[ ${INSTALL_BRIDGE:-true} == true ]] || return 0
  local dir=${BOOTSTRAP_BRIDGE_BUNDLE:-/var/cache/arch-workstation/repo} expected=${BOOTSTRAP_BRIDGE_SHA256:?Bridge preflight required} path
  [[ $(common::sha256_file "$dir/bridge-bundle.json") == "$expected" ]] || {
    bootstrap_die 'Bridge bundle changed after preflight'
    return 1
  }
  bootstrap_bridge_preflight || return 1
  [[ -f $BOOTSTRAP_TARGET/etc/pacman.conf ]] || {
    bootstrap_die 'target pacman configuration missing'
    return 1
  }
  if grep -Fq '/var/cache/arch-workstation' "$BOOTSTRAP_TARGET/etc/pacman.conf"; then
    bootstrap_die 'target pacman configuration still references live-ISO storage'
    return 1
  fi
  local files=()
  while IFS= read -r path; do files+=("$dir/$path"); done < <(
    jq -r '.packages[]|select(.name=="spry-ai-workstation-bridge" or .name=="arch-workstation-bridge-runtime")|.file' "$dir/bridge-bundle.json"
    jq -r '.dependencies[].file|"dependencies/"+.' "$dir/bridge-bundle.json"
  )
  # Only local archives; target database and normal package hooks. No live install.
  bootstrap_run pacman --root "$BOOTSTRAP_TARGET" --gpgdir /etc/pacman.d/gnupg --needed --noconfirm -U -- "${files[@]}" || return 1
  bootstrap_run install -Dm644 "$dir/bridge-bundle.json" "$BOOTSTRAP_TARGET/usr/share/doc/arch-workstation-bridge-runtime/bridge-bundle.json"
}
