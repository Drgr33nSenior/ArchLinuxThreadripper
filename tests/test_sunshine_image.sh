#!/usr/bin/env bash
set -euo pipefail

# Opt-in, bounded candidate smoke test. No upstream entrypoint, GPU, host bind
# mounts, network, privileges or cluster. Ordinary make check never pulls images.
if [[ -z ${HOME_LAB_GAME_IMAGE:-} ]]; then
  printf 'SKIP: set HOME_LAB_GAME_IMAGE to an already-built local image ID for streaming smoke tests\n'
  exit 0
fi
[[ $HOME_LAB_GAME_IMAGE =~ ^sha256:[a-f0-9]{64}$ ]] || { printf 'Expected a local sha256 image ID\n' >&2; exit 2; }
docker image inspect "$HOME_LAB_GAME_IMAGE" --format '{{.Os}}/{{.Architecture}}' | grep -Fxq linux/amd64
docker run --rm --pull never --platform linux/amd64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 \
  --pids-limit 64 --memory 256m --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=16m \
  --env XDG_CONFIG_HOME=/tmp/config --entrypoint /bin/bash "$HOME_LAB_GAME_IMAGE" -c '
    set -euo pipefail
    source /opt/workstation/lib/common.sh
    source /opt/workstation/lib/workstation/runtime.sh
    expected=$(ws_read_lock SUNSHINE_VERSION)
    version=$(sunshine --version)
    [[ $version == *"version: $expected"* ]]
    [[ -z $(getcap /usr/lib/workstation/sunshine.real) ]]
    mesa=$(ws_read_lock SUNSHINE_MESA_DEBIAN_VERSION)
    for arch in amd64 i386; do
      for package in mesa-libgallium mesa-vulkan-drivers libgl1-mesa-dri libegl-mesa0 libglx-mesa0 libgbm1; do
        [[ $(dpkg-query -W -f="\${Version}" "$package:$arch") == "$mesa" ]]
      done
    done
    [[ -r /usr/lib/x86_64-linux-gnu/dri/radeonsi_drv_video.so ]]
    [[ -s /usr/share/workstation/gaming-packages.tsv ]]
    /opt/workstation/bin/workstationctl sunshine profile /tmp/profile >/dev/null
    # Native argument parser only: --version exits before any server or encoder.
    for encoder in vaapi vulkan; do
      SUNSHINE_ENCODER=$encoder SUNSHINE_CAPTURE=wlr
      mapfile -t args < <(ws_sunshine_args /dev/dri/renderD128)
      parsed=$(/usr/lib/workstation/sunshine.real /tmp/parser.conf "${args[@]}" --version)
      [[ $parsed == *"version: $expected"* && $parsed != *Unrecognized* && $parsed != *Fatal* ]]
    done
    if /opt/workstation/bin/workstationctl sunshine devices >/tmp/devices.txt 2>&1; then
      printf "Device-free container unexpectedly passed GPU qualification\n" >&2
      exit 1
    fi
    before=$(common::sha256_file /tmp/parser.conf)
    if /usr/bin/sunshine /tmp/parser.conf >/tmp/launch.txt 2>&1; then
      printf "Device-free container unexpectedly launched Sunshine\n" >&2
      exit 1
    fi
    grep -Fq "expected exactly one accessible allocated render device; found 0" /tmp/launch.txt
    [[ $(common::sha256_file /tmp/parser.conf) == "$before" ]]
    printf "Pinned Sunshine/Mesa, native profile parser, config generation and absent-GPU refusal passed; no stream exercised\n"
  '
