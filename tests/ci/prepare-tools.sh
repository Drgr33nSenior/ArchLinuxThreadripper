#!/usr/bin/env bash
# CI-only private tool preparation. No global installation or target contact.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
output="${1:?new private tool directory}"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 && ! -e $output ]] || exit 2
umask 077
mkdir -p "$output/bin" "$output/downloads"
while IFS=$'\t' read -r name url digest; do
  archive="$output/downloads/$name"
  curl --fail --silent --show-error --location --retry 2 --max-time 180 "$url" -o "$archive"
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status
  case "$name" in
    shfmt | kubectl) install -m 755 "$archive" "$output/bin/$name" ;;
    ninja) unzip -q "$archive" -d "$output/bin" ;;
    alloy)
      unzip -q "$archive" -d "$output/alloy"
      ln -s "$output/alloy/alloy-linux-amd64" "$output/bin/grafana-alloy"
      ;;
    *)
      mkdir "$output/$name"
      tar -xf "$archive" --strip-components=1 -C "$output/$name"
      case "$name" in
        shellcheck | ccache | helm | alertmanager) ln -s "$output/$name/$name" "$output/bin/$name" ;;
        prometheus) ln -s "$output/$name/promtool" "$output/bin/promtool" ;;
        cmake | bats) ln -s "$output/$name/bin/$name" "$output/bin/$name" ;;
        *) exit 2 ;;
      esac
      ;;
  esac
done < <(jq -r '.[] | [.name,.url,.sha256] | @tsv' "$root/tests/ci/tools-linux.json")
chart_url=$(awk -F= '$1=="CHART_URL" {print $2}' "$root/infrastructure/gpu-operator/chart.lock")
chart_sha=$(awk -F= '$1=="CHART_SHA256" {print $2}' "$root/infrastructure/gpu-operator/chart.lock")
curl --fail --silent --show-error --location --max-time 120 "$chart_url" -o "$output/gpu-chart.tgz"
printf '%s  %s\n' "$chart_sha" "$output/gpu-chart.tgz" | sha256sum --check --status
printf 'Prepared private, checksum-verified source-check tools.\n'
