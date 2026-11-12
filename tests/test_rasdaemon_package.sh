#!/usr/bin/env bash
# Offline packaging fixture: no download, build or service activation.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
startdir="$root/infrastructure/packages/rasdaemon"
srcdir="$work/source"
pkgdir="$work/package"
# shellcheck source=infrastructure/packages/rasdaemon/PKGBUILD
source "$startdir/PKGBUILD"
[[ $pkgname == rasdaemon-workstation && $pkgver == 0.8.5 && $_commit =~ ^[a-f0-9]{40}$ ]]
[[ ${source[0]} == "rasdaemon-0.8.5.tar.gz::https://github.com/mchehab/rasdaemon/archive/refs/tags/v0.8.5.tar.gz" ]]
[[ ${#sha256sums[@]} == 1 && ${sha256sums[0]} =~ ^[a-f0-9]{64}$ ]]
[[ " ${depends[*]} " == *' perl-dbd-sqlite '* && " ${provides[*]} " == *' rasdaemon '* ]]
# Recording only: automatic page offlining and CPU isolation are not compiled in.
build_body=$(declare -f build)
for flag in --enable-sqlite3 --enable-mce --enable-aer --disable-memory-ce-pfa \
  --disable-memory-row-ce-pfa --disable-cpu-fault-isolation --disable-abrt-report; do
  [[ $build_body == *"$flag"* ]] || {
    printf 'missing configure flag %s\n' "$flag" >&2
    exit 1
  }
done
if [[ $build_body == *--enable-all* || $build_body == *--enable-memory-ce-pfa* ]]; then exit 1; fi
# Stage a synthetic install tree and run package() against it.
tree="$srcdir/rasdaemon-0.8.5"
# Directories exist first, as in test_k3s_package.sh, so BSD install(1) also works.
mkdir -p "$tree/misc" "$pkgdir/etc/conf.d" "$pkgdir/usr/include" "$pkgdir/usr/bin" "$pkgdir/usr/lib/systemd/system"
printf '[Service]\nExecStart=/usr/bin/rasdaemon -f -r\n' >"$tree/misc/rasdaemon.service"
printf '[Service]\n' >"$tree/misc/ras-mc-ctl.service"
make() {
  [[ $1 == "DESTDIR=$pkgdir" && $2 == install ]]
  : >"$pkgdir/etc/conf.d/rasdaemon"
  chmod 0655 "$pkgdir/etc/conf.d/rasdaemon"
  : >"$pkgdir/usr/include/config.h"
  : >"$pkgdir/usr/bin/rasdaemon"
}
(cd "$srcdir" && package)
[[ $(stat -c %a "$pkgdir/etc/conf.d/rasdaemon" 2>/dev/null || stat -f %Lp "$pkgdir/etc/conf.d/rasdaemon") == 644 ]]
[[ -f $pkgdir/usr/lib/systemd/system/rasdaemon.service && -d $pkgdir/var/lib/rasdaemon ]]
[[ ! -e $pkgdir/usr/include && ! -e $pkgdir/etc/systemd/system ]]
echo 'rasdaemon package pins, recording-only options and offline staging passed'
