#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/bootstrap/common.sh"
source "$root/lib/bootstrap/config.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

bootstrap_load_config "$root/infrastructure/host/install.conf.example"
[[ $RAID_DEVICE == /dev/md0 && $CRYPT_NAME == crypt_root && $LUKS_MEMORY_KIB == 4194304 && $BOOT_UNLOCK == tpm2-pin ]]
bootstrap_load_config "$root/config/install.conf.example"
[[ $RAID_DEVICE == /dev/md/archroot && $BOOT_UNLOCK == fido2 ]]
sed 's|RAID_DEVICE=/dev/md0|RAID_DEVICE=/dev/nvme0n1|' "$root/infrastructure/host/install.conf.example" > "$work/invalid.conf"
if bootstrap_load_config "$work/invalid.conf" >/dev/null 2>&1; then exit 1; fi

uuid=11111111-2222-3333-4444-555555555555
pcr=1111111111111111111111111111111111111111111111111111111111111111
plan=$(bash "$root/infrastructure/host/tpm2-enroll.sh" "$uuid" "$pcr")
[[ $plan == *'--tpm2-with-pin=yes'* && $plan == *'0:sha256+7:sha256+11:sha256='* && $plan != *'wipe-slot'* ]]
if bash "$root/infrastructure/host/tpm2-enroll.sh" "$uuid" invalid >/dev/null 2>&1; then exit 1; fi
if bash "$root/infrastructure/host/tpm2-enroll.sh" "$uuid" "$pcr" --execute < /dev/null >/dev/null 2>&1; then exit 1; fi

bash "$root/infrastructure/host/check-network.sh" 192.168.50.10 192.168.50.0/24 10.42.0.0/16 10.43.0.0/16 10.43.0.10 >/dev/null
if bash "$root/infrastructure/host/check-network.sh" 192.168.50.10 192.168.50.0/24 192.168.0.0/16 10.43.0.0/16 10.43.0.10 >/dev/null 2>&1; then exit 1; fi
if bash "$root/infrastructure/host/check-network.sh" 203.0.113.10 203.0.113.0/24 10.42.0.0/16 10.43.0.0/16 10.43.0.10 >/dev/null 2>&1; then exit 1; fi

if chart_error=$(bash "$root/infrastructure/gpu-operator/render.sh" "$root/README.md" 2>&1); then exit 1; fi
[[ $chart_error == *'SHA-256 mismatch'* ]]
if [[ -n ${HOME_LAB_GPU_CHART:-} ]]; then
  bash "$root/infrastructure/gpu-operator/render.sh" "$HOME_LAB_GPU_CHART" > "$work/operator.yaml"
else
  echo 'SKIP: set HOME_LAB_GPU_CHART to the pinned local chart archive for operator render checks'
fi

if command -v kubectl >/dev/null && command -v ruby >/dev/null; then
  for overlay in default single-gpu dual-gpu rdna4-compat parent kids family; do
    kubectl kustomize "$root/apps/overlays/$overlay" > "$work/$overlay.yaml"
  done
  kubectl kustomize "$root/apps/overlays/default" > "$work/default-repeated.yaml"
  cmp "$work/default.yaml" "$work/default-repeated.yaml"
  cmp "$work/default.yaml" "$work/dual-gpu.yaml"
  ruby "$root/tests/home-lab/check-manifests.rb" "$work"
else
  echo 'SKIP: home-lab manifest semantics require kubectl and Ruby'
fi
if [[ -n ${HOME_LAB_PYTHON:-} ]]; then
  "$HOME_LAB_PYTHON" "$root/tests/home-lab/render_templates.py"
else
  echo 'SKIP: set HOME_LAB_PYTHON to a configured Python with Jinja2/PyYAML for template tests'
fi
echo 'Home-lab storage, TPM plan, network and overlay tests passed'
