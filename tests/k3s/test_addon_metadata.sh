#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
cp -R "$root/kubernetes" "$work/kubernetes"
cp "$root/versions.lock" "$work/versions.lock"
kubectl() { [[ $# == 2 && $1 == kustomize ]] || return 90; printf 'synthetic offline render\n'; }
export -f kubectl
bash "$work/kubernetes/install-pinned-addons.sh" render >/dev/null
# Advancing a release without its immutable audit record must fail before any
# operation could reach a cluster (even when apply was selected).
sed -i.bak 's/v1\.21\.1/v1.21.2/g' "$work/versions.lock"
if bash "$work/kubernetes/install-pinned-addons.sh" apply >"$work/failure" 2>&1; then exit 1; fi
grep -q 'metadata/name differs from versions.lock' "$work/failure"
sed -i.bak -e 's/v1\.21\.1/v1.21.2/g' -e 's/v1-21-1/v1-21-2/g' "$work/kubernetes/cert-manager/install-metadata.yaml"
bash "$work/kubernetes/install-pinned-addons.sh" render >/dev/null
# Changing manifest bytes within one version also requires a new name.
sed -i.bak 's/5f6a499b8c18/111111111111/g' "$work/versions.lock"
if bash "$work/kubernetes/install-pinned-addons.sh" render >/dev/null 2>&1; then exit 1; fi
sed -i.bak 's/5f6a499b8c18/111111111111/g' "$work/kubernetes/cert-manager/install-metadata.yaml"
bash "$work/kubernetes/install-pinned-addons.sh" render >/dev/null
echo 'Immutable add-on audit-record upgrade and checksum drift tests passed'
