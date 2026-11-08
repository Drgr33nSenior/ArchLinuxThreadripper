#!/usr/bin/env bash
# Offline checks for the NFS CSI driver inputs; never contacts a cluster.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
dir=$root/infrastructure/nfs-csi
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

grep -Eq '^CHART_VERSION=v[0-9]+\.[0-9]+\.[0-9]+$' "$dir/chart.lock" || fail 'chart version is not exact'
grep -Eq '^CHART_SHA256=[a-f0-9]{64}$' "$dir/chart.lock" || fail 'chart digest is missing'
grep -Eq '^CHART_URL=https://' "$dir/chart.lock" || fail 'chart URL must use HTTPS'

# Every image override carries a digest; snapshots stay disabled.
tags=$(grep -E '^\s+tag:' "$dir/values.yaml")
[[ $(grep -c . <<<"$tags") == 5 ]] || fail 'expected five pinned images'
! grep -v -E 'tag: v[0-9.]+@sha256:[a-f0-9]{64}$' <<<"$tags" | grep -q . || fail 'an image is not pinned by digest'
grep -Fq 'enableSnapshotter: false' "$dir/values.yaml" || fail 'snapshotter sidecar must be disabled'

sc=$dir/storageclass.yaml
grep -Fxq 'provisioner: nfs.csi.k8s.io' "$sc" || fail 'unexpected provisioner'
grep -Fxq 'reclaimPolicy: Retain' "$sc" || fail 'NAS volumes must be retained'
grep -Fxq '  onDelete: retain' "$sc" || fail 'NAS directories must be kept on delete'
grep -Fxq '  share: /srv/microserver/k8s' "$sc" || fail 'unexpected export path'
! grep -q 'is-default-class' "$sc" || fail 'nas-nfs must not be the default class'

if [[ -n ${HOME_LAB_NFS_CHART:-} ]]; then
  rendered=$(bash "$dir/render.sh" "$HOME_LAB_NFS_CHART")
  images=$(grep -E '^\s+image:' <<<"$rendered" | sort -u)
  [[ $(grep -c . <<<"$images") == 5 ]] || fail 'render should use exactly the five pinned images'
  ! grep -v '@sha256:' <<<"$images" | grep -q . || fail 'rendered image without digest'
  ! grep -q 'snapshot-controller' <<<"$rendered" || fail 'snapshot controller rendered'
  grep -Fq -- '--extra-create-metadata=true' <<<"$rendered" || fail 'subDir templating needs extra-create-metadata'
else
  echo 'SKIP: set HOME_LAB_NFS_CHART to the pinned chart archive for render checks'
fi
printf 'NFS CSI pins, StorageClass retention and render checks passed\n'
