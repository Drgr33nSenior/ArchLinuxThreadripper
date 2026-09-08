#!/usr/bin/env bash
# Coordinate existing stages. No signing, cleanup, USB writes or new build logic.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"

usage() {
  printf '%s\n' \
    'Usage: release.sh [--context NAME] [--execute] [--allow-iso-mounts] ACTION ...' \
    '  packages                       Build tools, snapshot this checkout, build packages.' \
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
      --execute) execute=true; shift ;;
      --context) (($# >= 2)) || common::die '--context needs a name'; context=$2; shift 2 ;;
      --allow-iso-mounts) allow_mounts=true; shift ;;
      -h|--help) usage; return ;;
      --*) common::die 'unknown option; arbitrary Docker arguments are not accepted' ;;
      *) break ;;
    esac
  done
  (($#)) || { usage; return 1; }
  [[ $context =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || common::die 'invalid Docker context name'
  local action=$1 job run
  shift
  # Numeric UTC time plus process ID fits the existing Docker job-name contract.
  # No mutable "latest" pointer and no reuse of a previous source bundle.
  job="run-$(date -u +%Y%m%d-%H%M%S)-$$"
  local wrapper="$root/infrastructure/iso/docker.sh" docker_options=(--context "$context")
  case $action in
    packages)
      (($# == 0)) || common::die 'packages takes no paths; it always snapshots the current checkout'
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
      if [[ $execute == true ]]; then
        common::info "Packages ready: $run/packages"
        common::info 'Next: copy this assignment, then follow stage 2 in docs/ISO.md. Nothing was signed or published.'
        printf 'ISO_RUN=%q\n' "$run"
      else
        common::info 'Preview only: no files, Docker queries or jobs were created. --execute will choose its own fresh run.'
      fi
      ;;
    iso)
      (($# == 3)) || common::die 'iso requires the run directory, public key and full fingerprint'
      if [[ $execute == true && $allow_mounts != true ]]; then
        common::die 'ISO execution needs explicit --allow-iso-mounts; read the privilege boundary in docs/ISO.md'
      fi
      [[ -d $1 && ! -L $1 ]] || common::die 'supply the run directory printed by the packages command'
      run=$(cd -- "$1" && pwd -P) || return
      [[ -f $run/source/source.lock && ! -L $run/source/source.lock \
        && -f $run/packages/source.lock && ! -L $run/packages/source.lock ]] || common::die 'run lacks source/package manifests; complete the packages stage first'
      cmp -s "$run/source/source.lock" "$run/packages/source.lock" || common::die 'source and package manifests differ; do not combine runs'
      if [[ $execute == true ]]; then docker_options+=(--execute); fi
      if [[ $allow_mounts == true ]]; then docker_options+=(--allow-iso-mounts); fi
      common::info "ISO output for this attempt: $run/$job"
      common::info 'If ISO assembly fails, inspect its log with:'
      common::print_command docker --context "$context" logs --tail 60 "arch-workstation-iso-iso-$job"
      # The existing wrapper owns signature-input, context, mount and job checks.
      bash "$wrapper" "${docker_options[@]}" iso "$job" "$run/packages" "$2" "$3" "$run/$job" || return
      ;;
    *) common::die 'unknown action; use packages or iso' ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
