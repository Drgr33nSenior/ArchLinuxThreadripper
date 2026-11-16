#!/usr/bin/env bash
# Build and install the workstation runtime packages (arch-workstation-boot, arch-workstation-bridge-runtime)
# from a shipped source tree, without rendering or applying anything to the cluster. Run on the workstation.
#
#   install-runtime.sh SHA12
#
# SHA12 names the shipped tree $WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12 (default ~/src). Build
# output and logs: ~/build/workstation-runtime-SHA12 (reused when it exists). Stops unless the installed
# sandbox.py then equals the tree's.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
sha=${1:?usage: install-runtime.sh SHA12 (--help for details)}
tools::load_site
src=$(tools::source_tree "$sha")
out=$HOME/build/workstation-runtime-$sha
log() { printf '%s %s\n' "$(date +%T)" "$*"; }
[[ -f $src/versions.lock ]] || {
  echo "STOP: no source tree at $src" >&2
  exit 1
}
log "runtime package from $sha (before: $(pacman -Q arch-workstation-bridge-runtime | cut -d' ' -f2))"
if [[ -e $out/source ]]; then log "reusing $out"; else
  mkdir -p -- "$out"
  bash "$src/infrastructure/packages/bootstrap/prepare-source.sh" "$out/source"
  (cd "$out/source" && epoch=$(awk -F= '$1 == "SOURCE_DATE_EPOCH" {print $2}' source.lock) &&
    SOURCE_DATE_EPOCH=$epoch makepkg --cleanbuild >"$out/makepkg.log" 2>&1) || {
    tail -20 "$out/makepkg.log"
    exit 1
  }
fi
(cd "$out/source" && sudo pacman -U --noconfirm arch-workstation-boot-*.pkg.tar.zst arch-workstation-bridge-runtime-*.pkg.tar.zst >/dev/null)
pacman -Q arch-workstation-boot arch-workstation-bridge-runtime
installed=/usr/lib/bridge/workstation-runtime/lib/workstation/sandbox.py
if cmp -s "$installed" "$src/lib/workstation/sandbox.py"; then log "installed runtime matches $sha"; else
  echo "STOP: installed sandbox.py still differs from $src" >&2
  exit 1
fi
if account=$(getent passwd sandbox); then log "sandbox account: $account"; else log "sandbox account: not reserved by this package version"; fi
log INSTALL_RUNTIME=done
