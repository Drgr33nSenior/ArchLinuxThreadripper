#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"
(($# == 1)) || common::die 'usage: render.sh <downloaded-chart.tgz>'
common::verify_sha256 "$1" "$(common::lock_get "$root/infrastructure/gpu-operator/chart.lock" CHART_SHA256)"
k3s_version=$(common::lock_get "$root/versions.lock" K3S_VERSION)
[[ $k3s_version =~ ^v1\.35\.[0-9]+\+k3s[0-9]+$ ]] || common::die 'review operator compatibility before changing the Kubernetes minor'
kubernetes_version=${k3s_version#v}
kubernetes_version=${kubernetes_version%%+*}
helm template amd-gpu-operator "$1" --namespace kube-amd-gpu --kube-version "$kubernetes_version" \
  --include-crds --no-hooks --values "$root/infrastructure/gpu-operator/values.yaml"
