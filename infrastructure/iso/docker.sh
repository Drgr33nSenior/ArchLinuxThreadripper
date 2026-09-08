#!/usr/bin/env bash
# Docker Desktop is an explicitly local dev builder. Never use the active
# context implicitly, publish an image, mount host devices or relax seccomp.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"

usage() {
  printf '%s\n' \
    'Usage: docker.sh [--context NAME] [--execute] [--allow-iso-mounts] ACTION ...' \
    '  image' \
    '  check' \
    '  packages JOB SOURCE_BUNDLE_DIRECTORY NEW_OUTPUT_DIRECTORY' \
    '  iso JOB SIGNED_PACKAGE_DIRECTORY PUBLIC_KEY.asc FINGERPRINT NEW_OUTPUT_DIRECTORY' \
    'Default: dry-run, local desktop-linux context; all job state is retained.'
}

regular_file() {
  [[ -f $1 && ! -L $1 ]] || common::die "regular, non-symlink input required: $1"
}

directory_path() {
  [[ -d $1 && ! -L $1 ]] || common::die "directory input required: $1"
  local resolved
  resolved=$(cd -- "$1" && pwd -P) || common::die 'cannot resolve input directory'
  [[ $resolved != *','* && $resolved != *$'\n'* && $resolved != *$'\r'* ]] || common::die 'Docker mount paths cannot contain commas or newlines'
  printf '%s\n' "$resolved"
}

file_path() {
  local parent
  regular_file "$1"
  parent=$(directory_path "$(dirname -- "$1")") || return 1
  printf '%s/%s\n' "$parent" "$(basename -- "$1")"
}

new_output_path() {
  [[ ! -e $1 && ! -L $1 ]] || common::die 'output exists; choose a new directory'
  local leaf=${1##*/} parent
  [[ $leaf =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && $leaf != *..* ]] || common::die 'use a simple new output directory name'
  parent=$(directory_path "$(dirname -- "$1")") || return 1
  printf '%s/%s\n' "$parent" "$leaf"
}

main() {
  local context=desktop-linux execute=false allow_mounts=false
  while (($#)); do
    case $1 in
      --context) (($# >= 2)) || common::die '--context needs a name'; context=$2; shift 2 ;;
      --execute) execute=true; shift ;;
      --allow-iso-mounts) allow_mounts=true; shift ;;
      -h|--help) usage; return ;;
      --*) common::die 'unknown option; arbitrary Docker arguments are not accepted' ;;
      *) break ;;
    esac
  done
  (($#)) || { usage; return 1; }
  [[ $context =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || common::die 'invalid Docker context name'
  local action=$1 job='' output='' source='' public_key='' fingerprint='' endpoint digest image image_id='' file state
  shift
  local docker=(docker --context "$context") run=() mounts=()
  # The Dockerfile is the authoritative base-image pin. Include every build
  # input in the local tag; a lock/recipe change cannot silently reuse old tools.
  digest=$(for file in infrastructure/iso/docker/Dockerfile infrastructure/iso/docker/setup.sh \
    infrastructure/iso/docker/entrypoint.sh infrastructure/iso/docker/pacstrap.sh \
    infrastructure/iso/.dockerignore infrastructure/iso/versions.lock; do
      common::sha256_file "$root/$file"
    done | common::sha256_file -)
  image="arch-workstation-iso-builder:${digest:0:20}"
  case $action in
    image|check) (($# == 0)) || common::die 'unexpected arguments' ;;
    packages)
      (($# == 3)) || common::die 'packages requires JOB, source directory and new output directory'
      job=$1; source=$(directory_path "$2"); output=$(new_output_path "$3")
      for file in bootstrap-source.tar.gz source.lock PKGBUILD; do
        regular_file "$source/$file"
        mounts+=(--mount "type=bind,source=$source/$file,target=/input/$file,readonly")
      done
      ;;
    iso)
      (($# == 5)) || common::die 'iso requires JOB, signed package directory, public key, fingerprint and new output directory'
      job=$1; source=$(directory_path "$2"); public_key=$(file_path "$3"); fingerprint=$4; output=$(new_output_path "$5")
      [[ $fingerprint =~ ^[A-F0-9]{40}$ ]] || common::die 'a full uppercase primary fingerprint is required'
      grep -qx -- '-----BEGIN PGP PUBLIC KEY BLOCK-----' "$public_key" || common::die 'only an armored public key may be supplied'
      if grep -q 'PRIVATE KEY' "$public_key"; then common::die 'private signing material is forbidden'; fi
      [[ $public_key != *','* && $public_key != *$'\n'* && $public_key != *$'\r'* ]] || common::die 'invalid public key path'
      local bootstrap=() boot=()
      shopt -s nullglob
      bootstrap=("$source"/arch-workstation-bootstrap-*.pkg.tar.zst)
      boot=("$source"/arch-workstation-boot-*.pkg.tar.zst)
      ((${#bootstrap[@]} == 1 && ${#boot[@]} == 1)) || common::die 'exactly one package of each split-package name is required'
      for file in "${bootstrap[0]}" "${boot[0]}" "$source/arch-workstation.db.tar.gz"; do
        regular_file "$file"; regular_file "$file.sig"
        [[ ${file##*/} =~ ^[A-Za-z0-9_.+-]+$ ]] || common::die 'invalid package filename'
        mounts+=(--mount "type=bind,source=$file,target=/release/${file##*/},readonly"
          --mount "type=bind,source=$file.sig,target=/release/${file##*/}.sig,readonly")
      done
      mounts+=(--mount "type=bind,source=$public_key,target=/release/signing-key.asc,readonly")
      for file in lib/common.sh infrastructure/iso/prepare.sh infrastructure/iso/build.sh \
        infrastructure/iso/versions.lock templates/arch/no-hibernation.conf; do
        regular_file "$root/$file"
        mounts+=(--mount "type=bind,source=$root/$file,target=/project/$file,readonly")
      done
      source=$(directory_path "$root/infrastructure/iso/profile")
      mounts+=(--mount "type=bind,source=$source,target=/project/infrastructure/iso/profile,readonly")
      ;;
    *) common::die 'unknown action' ;;
  esac
  [[ $action == iso || $allow_mounts == false ]] || common::die '--allow-iso-mounts is only valid for ISO assembly'
  if [[ $action == packages || $action == iso ]]; then
    [[ $job =~ ^[a-z][a-z0-9-]{0,31}$ ]] || common::die 'JOB must be a short lowercase identifier'
  fi
  local container="arch-workstation-iso-$action-$job" volume="arch-workstation-iso-$action-$job"
  if [[ $execute == true && $action == iso && $allow_mounts != true ]]; then
    common::die 'ISO execution needs explicit --allow-iso-mounts; read the container privilege boundary in ISO.md'
  fi
  if [[ $execute == true ]]; then
    endpoint=$("${docker[@]}" context inspect "$context" --format '{{.Endpoints.docker.Host}}')
    [[ $endpoint == unix:///* ]] || common::die 'only a local Unix-socket Docker context is permitted'
    [[ $("${docker[@]}" info --format '{{.OperatingSystem}}') == 'Docker Desktop' ]] \
      || common::die 'this wrapper only targets a local Docker Desktop dev context'
    if [[ $action != image ]]; then
      [[ $("${docker[@]}" image inspect "$image" --format '{{.Os}}/{{.Architecture}}') == linux/amd64 ]] || common::die 'build the amd64 image first'
      [[ $("${docker[@]}" image inspect "$image" --format '{{index .Config.Labels "io.arch-workstation.recipe"}}') == "$digest" ]] || common::die 'builder recipe label mismatch'
      image_id=$("${docker[@]}" image inspect "$image" --format '{{.Id}}')
      [[ $image_id =~ ^sha256:[a-f0-9]{64}$ ]] || common::die 'invalid local builder image ID'
    fi
  fi
  if [[ $action == image ]]; then
    run=("${docker[@]}" build --platform linux/amd64 --label "io.arch-workstation.recipe=$digest"
      --tag "$image" --file "$root/infrastructure/iso/docker/Dockerfile" "$root/infrastructure/iso")
  else
    run=("${docker[@]}" run --platform linux/amd64 --pull never --memory 6g --memory-swap 6g --cpus 4 --pids-limit 512
      --security-opt no-new-privileges --env "BUILDER_IMAGE_ID=${image_id:-$image}")
    if [[ $action == check ]]; then
      run+=(--rm --read-only --network none --cap-drop ALL --user 1000:1000)
    else
      run+=(--name "$container" --label io.arch-workstation.scope=dev --label io.arch-workstation.purpose=iso-builder
        --mount "type=volume,source=$volume,target=/work")
      if [[ $action == packages ]]; then
        run+=(--network none --cap-drop ALL --user 1000:1000)
      else
        common::warn 'ISO assembly uses container root and CAP_SYS_ADMIN for chroot mounts. It does not use --privileged, host devices, host namespaces or an unconfined security profile.'
        run+=(--user 0:0 --cap-add SYS_ADMIN --tmpfs '/run/arch-workstation:rw,noexec,nosuid,nodev,mode=0700')
      fi
    fi
    # macOS ships Bash 3.2: expanding an empty array with nounset fails there.
    if [[ $action != check ]]; then run+=("${mounts[@]}"); fi
    run+=("${image_id:-$image}" "$action")
    [[ $action != iso ]] || run+=("$fingerprint")
  fi
  common::print_command "${run[@]}"
  if [[ -n $job ]]; then common::print_command "${docker[@]}" cp "$container:/work/output/." "$output"; fi
  [[ $execute == true ]] || return 0
  if [[ -n $job ]]; then
    # A failed state query is not evidence that a job is absent.
    state=$("${docker[@]}" container ls --all --filter "name=$container" --format '{{.Names}}') \
      || common::die 'cannot inspect job containers'
    if [[ $'\n'$state$'\n' == *$'\n'"$container"$'\n'* ]]; then
      common::die 'job state already exists; inspect it and choose a new job, never clean it automatically'
    fi
    state=$("${docker[@]}" volume ls --filter "name=$volume" --format '{{.Name}}') \
      || common::die 'cannot inspect job volumes'
    if [[ $'\n'$state$'\n' == *$'\n'"$volume"$'\n'* ]]; then
      common::die 'job volume already exists; inspect it and choose a new job'
    fi
    "${docker[@]}" volume create --label io.arch-workstation.scope=dev --label io.arch-workstation.purpose=iso-builder "$volume" >/dev/null
  fi
  "${run[@]}"
  if [[ -n $job ]]; then
    mkdir -- "$output"
    "${docker[@]}" cp "$container:/work/output/." "$output"
    common::info "Exported $output; container $container and volume $volume are retained. Nothing was published or signed."
  fi
}
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
