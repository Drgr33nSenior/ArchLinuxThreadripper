#!/usr/bin/env bash
# Offline packaging fixture: never download or execute the Linux server.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
startdir="$root/infrastructure/packages/k3s"
srcdir="$work/source"
pkgdir="$work/package"
mkdir -p "$srcdir" "$pkgdir/usr/bin" "$pkgdir/usr/lib/systemd/system"
# shellcheck source=infrastructure/packages/k3s/PKGBUILD
source "$startdir/PKGBUILD"
[[ $pkgname == k3s-workstation-bin && $pkgver == 1.35.7.k3s1 ]]
[[ ${source[0]} == "k3s-${_release}::https://github.com/k3s-io/k3s/releases/download/${_release}/k3s" ]]
[[ ${sha256sums[1]} == "$(shasum -a 256 "$startdir/k3s.service" | awk '{print $1}')" ]]
printf 'synthetic non-executable server contents\n' > "$srcdir/k3s-${_release}"
cp "$startdir/k3s.service" "$srcdir/k3s.service"
package
cmp "$srcdir/k3s-${_release}" "$pkgdir/usr/bin/k3s"
cmp "$srcdir/k3s.service" "$pkgdir/usr/lib/systemd/system/k3s.service"
[[ -x $pkgdir/usr/bin/k3s ]]
[[ ! -e $pkgdir/etc/systemd/system/multi-user.target.wants ]]
grep -Fxq 'ExecStart=/usr/bin/k3s server' "$pkgdir/usr/lib/systemd/system/k3s.service"
if rg -q 'uninstall|killall' "$pkgdir/usr/lib/systemd/system/k3s.service"; then exit 1; fi
echo 'K3s package identity, pinned service and offline staging passed'
