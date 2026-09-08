#!/usr/bin/env bash
# Sourced by the pinned upstream entrypoint. Do not run apt, modprobe, download
# drivers or change power settings at session start. Mesa is baked into the image;
# the Arch host owns amdgpu. Missing devices fail later in the non-root launcher.
for gaming_mesa_package in mesa-libgallium:amd64 mesa-vulkan-drivers:amd64; do
  gaming_mesa_status=$(dpkg-query -W -f='${Status}' "$gaming_mesa_package") || exit 1
  if [[ $gaming_mesa_status != 'install ok installed' ]]; then
    printf 'Gaming image is missing its build-time Mesa packages\n' >&2
    exit 1
  fi
done
unset gaming_mesa_package gaming_mesa_status
export LIBVA_DRIVER_NAME=radeonsi
# This selects Mesa's rendering preference, not ownership. Device-cgroup access
# must already expose one allocated GPU; the non-root launch checks it again.
gaming_gpu_bdf=$(/opt/workstation/bin/workstationctl sunshine devices | jq -er .bdf) || exit 1
export DRI_PRIME="pci-${gaming_gpu_bdf//[:.]/_}"
unset gaming_gpu_bdf
