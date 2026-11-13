#!/usr/bin/env bash
# Render the Family AI icons from logo.svg into this folder (committed outputs).
# Runs in the pinned Arch base image with librsvg and ImageMagick from the signed
# Arch repositories; needs podman and network access for pacman.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
root=$(cd -- "$here/../../../.." && pwd -P)
image=$(awk -F= '$1 == "SUNSHINE_ARCH_BASE_IMAGE" {print $2}' "$root/versions.lock")
[[ $image =~ @sha256:[a-f0-9]{64}$ ]] || {
  echo "STOP: no pinned Arch base image" >&2
  exit 1
}
podman run --rm --pull=missing -v "$here:/theme:Z" -w /theme "$image" bash -euc '
  pacman -Sy --noconfirm --needed librsvg imagemagick >/dev/null
  r() { rsvg-convert -w "$1" -h "$1" logo.svg -o "$2"; }
  r 512 favicon.png
  r 96 favicon-96x96.png
  r 180 apple-touch-icon.png
  r 192 web-app-manifest-192x192.png
  r 512 web-app-manifest-512x512.png
  r 500 logo.png
  r 500 splash.png
  r 500 splash-dark.png
  r 32 /tmp/favicon-32.png
  magick /tmp/favicon-32.png -strip favicon.ico
  for f in *.png; do magick "$f" -strip "$f"; done
  cp logo.svg favicon.svg
'
