#!/usr/bin/env bash
# A local repository snapshot is an immutable set, never an in-place overwrite.

ws_package_signature() {
  local file=$1 fingerprint=$2 valid
  valid=$(gpg --batch --status-fd 1 --verify "$file.sig" "$file" 2>/dev/null) \
    || ws_die "invalid package/signature: $file"
  awk -v key="$fingerprint" '$2 == "VALIDSIG" && ($3 == key || $NF == key) {ok=1} END {exit !ok}' <<< "$valid" \
    || ws_die "signature is not from the selected signing key: $file"
}

ws_package_snapshot() (
  ws_require_arch
  ws_require_user
  local profile=$1 output=$2 key=$3 package name metadata pkgname version sha staged archives=()
  shift 3
  [[ $profile == stable || $profile == experimental ]] || ws_die 'package profile must be stable or experimental'
  [[ $key =~ ^[A-Fa-f0-9]{40}$ || $key =~ ^[A-Fa-f0-9]{64}$ ]] || ws_die 'provide a full public signing fingerprint'
  key=$(printf '%s' "$key" | tr '[:lower:]' '[:upper:]')
  (($# > 0)) && [[ ! -e $output ]] || ws_die 'provide packages and a new snapshot directory'
  command -v repo-add >/dev/null || ws_die 'Arch repo-add is required'
  command -v bsdtar >/dev/null || ws_die 'bsdtar is required'
  # Validate the whole supplied set before signing/copying any output.
  for package in "$@"; do
    [[ -f $package && -f $package.sig && ${package##*/} =~ ^[a-zA-Z0-9@._+:-]+\.pkg\.tar\.(zst|xz)$ ]] \
      || ws_die 'every input must be a signed Arch package archive'
    ws_package_signature "$package" "$key"
  done
  mkdir -p -- "$(dirname -- "$output")"
  staged=$(mktemp -d "$(dirname -- "$output")/.rocm-snapshot.XXXXXX")
  # Failed snapshots stay at the reported path for inspection; never prune an
  # existing package cache/repository as a failure-recovery shortcut.
  ws_note "staging local repository at $staged"
  for package in "$@"; do
    name=${package##*/}
    [[ ! -e $staged/$name ]] || ws_die 'duplicate package filename in snapshot'
    cp -- "$package" "$package.sig" "$staged/" || ws_die 'cannot copy package into snapshot'
    ws_package_signature "$staged/$name" "$key"
    metadata=$(bsdtar -xOf "$staged/$name" .PKGINFO) || ws_die 'cannot read package metadata'
    pkgname=$(awk -F' = ' '$1 == "pkgname" {print $2}' <<< "$metadata")
    version=$(awk -F' = ' '$1 == "pkgver" {print $2}' <<< "$metadata")
    [[ -n $pkgname && -n $version ]] || ws_die 'package metadata is incomplete'
    archives+=("$name")
    sha=$(common::sha256_file "$staged/$name") || ws_die 'cannot hash staged package'
    jq -n --arg file "$name" --arg name "$pkgname" --arg version "$version" --arg sha "$sha" \
      '{file:$file,name:$name,version:$version,sha256:$sha,optimization:"unverified-see-build-manifest"}' >> "$staged/packages.jsonl"
  done
  jq -s --arg profile "$profile" --arg key "$key" \
    'if (map(.name)|unique|length) != length then error("multiple versions of a package") else
      {schema:1,profile:$profile,signing_fingerprint:$key,hardware_accepted:false,packages:.} end' \
    "$staged/packages.jsonl" > "$staged/manifest.json" || ws_die 'cannot construct a complete snapshot manifest'
  (
    cd "$staged" || exit 1
    repo-add --sign --key "$key" workstation-rocm.db.tar.gz "${archives[@]}" || ws_die 'local repository creation failed'
    gpg --batch --local-user "$key" --detach-sign --output manifest.json.sig manifest.json || ws_die 'snapshot manifest signing failed'
  ) || ws_die "snapshot was not published; inspect staging directory: $staged"
  mv -- "$staged" "$output" || ws_die 'cannot finalize snapshot directory'
  ws_note "signed local package set: $output; validate it on the target before designating it last-known-good"
)

ws_package_restore_plan() {
  local snapshot=$1 key=$2 file expected
  [[ $key =~ ^[A-Fa-f0-9]{40}$ || $key =~ ^[A-Fa-f0-9]{64}$ ]] || ws_die 'provide a full public signing fingerprint'
  key=$(printf '%s' "$key" | tr '[:lower:]' '[:upper:]')
  [[ -f $snapshot/manifest.json ]] || ws_die 'snapshot manifest is missing'
  ws_package_signature "$snapshot/manifest.json" "$key"
  jq -e --arg key "$key" '
    .schema == 1 and .signing_fingerprint == $key and (.packages|type) == "array" and
    (.packages|length)>0 and (.packages|map(.file)|unique|length) == (.packages|length) and
    all(.packages[]; (.file|type) == "string" and (.sha256|test("^[a-f0-9]{64}$")))
  ' "$snapshot/manifest.json" >/dev/null || ws_die 'invalid snapshot manifest'
  local packages=()
  while IFS=$'\t' read -r file expected; do
    [[ $file =~ ^[a-zA-Z0-9@._+:-]+\.pkg\.tar\.(zst|xz)$ ]] || ws_die 'unsafe snapshot package filename'
    common::verify_sha256 "$snapshot/$file" "$expected"
    ws_package_signature "$snapshot/$file" "$key"
    packages+=("$snapshot/$file")
  done < <(jq -r '.packages[] | [.file,.sha256] | @tsv' "$snapshot/manifest.json")
  ((${#packages[@]} > 0)) || ws_die 'snapshot contains no packages'
  ws_note 'Stop GPU workloads and review the complete dependency-compatible set before running:'
  common::print_command sudo pacman -U -- "${packages[@]}"
}
