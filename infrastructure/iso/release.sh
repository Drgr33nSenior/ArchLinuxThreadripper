#!/usr/bin/env bash
# Coordinate existing stages. No signing, cleanup, USB writes or new build logic.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"

usage() {
  printf '%s\n' \
    'Usage: release.sh [--context NAME] [--execute] [--allow-iso-mounts] ACTION ...' \
    '  packages [BRIDGE_COMMIT BRIDGE_VERSION]' \
    '                                 Build installer; optionally fetch/build/bundle Bridge.' \
    '  bridge-build BRIDGE_COMMIT BRIDGE_VERSION NEW_OUTPUT_DIRECTORY' \
    '                                 Build/test an unsigned Bridge package in amd64 Arch.' \
    '  bridge RUN_DIRECTORY REVIEWED_BRIDGE_DIRECTORY' \
    '                                 Add target payload/dependencies before owner signing.' \
    '  iso RUN_DIRECTORY PUBLIC_KEY.asc FINGERPRINT' \
    '                                 Assemble an ISO from this run after manual signing.' \
    'Default: dry-run. Each execution uses fresh job/output names and retains all state.' \
    'Review the checkout and docs/ISO.md before --execute. Signing is never automatic.'
}

step() {
  local label=$1 status=0
  shift
  common::info "$label"
  common::print_command "$@"
  if [[ $execute == true ]]; then
    "$@" || status=$?
    if ((status != 0)); then
      common::warn "Stopped at: $label. Later stages did not run; existing state is retained."
      return "$status"
    fi
  fi
}

main() {
  local execute=false context=desktop-linux allow_mounts=false
  while (($#)); do
    case $1 in
      --execute)
        execute=true
        shift
        ;;
      --context)
        (($# >= 2)) || common::die '--context needs a name'
        context=$2
        shift 2
        ;;
      --allow-iso-mounts)
        allow_mounts=true
        shift
        ;;
      -h | --help)
        usage
        return
        ;;
      --*) common::die 'unknown option; arbitrary Docker arguments are not accepted' ;;
      *) break ;;
    esac
  done
  (($#)) || {
    usage
    return 1
  }
  [[ $context =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || common::die 'invalid Docker context name'
  local action=$1 job run
  shift
  # Numeric UTC time plus process ID fits the existing Docker job-name contract.
  # No mutable "latest" pointer and no reuse of a previous source bundle.
  job="run-$(date -u +%Y%m%d-%H%M%S)-$$"
  local wrapper="$root/infrastructure/iso/docker.sh" docker_options=(--context "$context")
  case $action in
    packages)
      (($# == 0 || $# == 2)) || common::die 'packages accepts no arguments or an exact Bridge commit and version'
      if (($#)); then
        [[ $1 =~ ^[a-f0-9]{40}$ && $2 =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || common::die 'select an exact Bridge commit and vMAJOR.MINOR.PATCH version'
      fi
      [[ $allow_mounts == false ]] || common::die '--allow-iso-mounts is only valid for ISO assembly'
      [[ ! -L $root/build && ! -L $root/build/iso ]] || common::die 'build/ and build/iso/ must not be symlinks'
      run="$root/build/iso/$job"
      common::info "New run: $run"
      common::info 'Review the canonical PKGBUILD and allowlisted checkout before execution.'
      common::info 'If the package job fails, inspect its log with:'
      common::print_command docker --context "$context" logs --tail 60 "arch-workstation-iso-packages-$job"
      step '1/4: Build or reuse the pinned tools image' bash "$wrapper" "${docker_options[@]}" --execute image || return
      step '2/4: Check unprivileged amd64 userspace' bash "$wrapper" "${docker_options[@]}" --execute check || return
      step '3/4: Create a fresh source snapshot' mkdir -p "$root/build/iso" || return
      step 'Reserve the new run directory' mkdir "$run" || return
      step 'Prepare source from this checkout' bash "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$run/source" || return
      step '4/4: Build unsigned packages without network or root' bash "$wrapper" "${docker_options[@]}" --execute packages \
        "$job" "$run/source" "$run/packages" || return
      if (($#)); then
        step 'Build the selected Bridge commit in amd64 Arch' bash "$wrapper" "${docker_options[@]}" --execute bridge-build \
          "$job" "$1" "$2" "$run/bridge-artifacts" || return
        step 'Seal Bridge and snapshot dependencies with this installer run' bash "$wrapper" "${docker_options[@]}" --execute bridge \
          "$job" "$run/packages" "$run/bridge-artifacts" "$run/bundled" || return
      fi
      if [[ $execute == true ]]; then
        common::info "Packages ready: $run/packages"
        common::info 'Next: copy this assignment, then follow Bridge selection/signing in docs/ISO.md. Nothing was signed or published.'
        printf 'ISO_RUN=%q\n' "$run"
      else
        common::info 'Preview only: no files, Docker queries or jobs were created. --execute will choose its own fresh run.'
      fi
      ;;
    bridge-build)
      (($# == 3)) || common::die 'bridge-build requires exact commit, version and new output directory'
      [[ $allow_mounts == false ]] || common::die '--allow-iso-mounts is only valid for ISO assembly'
      # Validate paths through the wrapper before creating an image or a job.
      bash "$wrapper" "${docker_options[@]}" bridge-build "$job" "$1" "$2" "$3" || return
      step '1/3: Build or reuse the pinned tools image' bash "$wrapper" "${docker_options[@]}" --execute image || return
      step '2/3: Check unprivileged amd64 userspace' bash "$wrapper" "${docker_options[@]}" --execute check || return
      step '3/3: Fetch, build and test the reviewed Bridge commit' bash "$wrapper" "${docker_options[@]}" --execute bridge-build "$job" "$1" "$2" "$3" || return
      if [[ $execute == true ]]; then
        printf 'BRIDGE_ARTIFACTS=%q\n' "$(cd -- "$3" && pwd -P)"
      fi
      ;;
    bridge)
      (($# == 2)) || common::die 'bridge requires one completed installer run and one reviewed Bridge artifact directory'
      [[ $allow_mounts == false ]] || common::die '--allow-iso-mounts is only valid for ISO assembly'
      run=$(cd -- "$1" && pwd -P)
      cmp -s "$run/source/source.lock" "$run/packages/source.lock" || common::die 'installer run identities differ'
      [[ ! -e $run/bundled && ! -L $run/bundled ]] || common::die 'bundle already exists; retain it and use a new reviewed run'
      step 'Seal Bridge candidate, resolve snapshot dependencies, rebuild unsigned repository' bash "$wrapper" "${docker_options[@]}" --execute bridge "$job" "$run/packages" "$2" "$run/bundled" || return
      ;;
    iso)
      (($# == 3)) || common::die 'iso requires the run directory, public key and full fingerprint'
      if [[ $execute == true && $allow_mounts != true ]]; then
        common::die 'ISO execution needs explicit --allow-iso-mounts; read the privilege boundary in docs/ISO.md'
      fi
      [[ -d $1 && ! -L $1 ]] || common::die 'supply the run directory printed by the packages command'
      run=$(cd -- "$1" && pwd -P) || return
      [[ -f $run/source/source.lock && ! -L $run/source/source.lock &&
        -f $run/packages/source.lock && ! -L $run/packages/source.lock ]] || common::die 'run lacks source/package manifests; complete the packages stage first'
      cmp -s "$run/source/source.lock" "$run/packages/source.lock" || common::die 'source and package manifests differ; do not combine runs'
      if [[ $execute == true ]]; then docker_options+=(--execute); fi
      if [[ $allow_mounts == true ]]; then docker_options+=(--allow-iso-mounts); fi
      common::info "ISO output for this attempt: $run/$job"
      common::info 'If ISO assembly fails, inspect its log with:'
      common::print_command docker --context "$context" logs --tail 60 "arch-workstation-iso-iso-$job"
      # The existing wrapper owns signature-input, context, mount and job checks.
      local selected="$run/packages"
      if [[ -d $run/bundled ]]; then
        cmp -s "$run/source/source.lock" "$run/bundled/source.lock" || common::die 'Bridge bundle belongs to another installer run'
        [[ -f $run/bundled/bridge-bundle.json ]] || common::die 'incomplete Bridge bundle; do not fall back to packages'
        selected="$run/bundled"
      fi
      bash "$wrapper" "${docker_options[@]}" iso "$job" "$selected" "$2" "$3" "$run/$job" || return
      ;;
    *) common::die 'unknown action; use packages, bridge-build, bridge or iso' ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
