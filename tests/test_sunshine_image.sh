#!/usr/bin/env bash
set -euo pipefail

# Opt-in, bounded candidate smoke test. No upstream entrypoint, GPU, host bind
# mounts, network, privileges or cluster. Ordinary make check never pulls images.
if [[ -z ${HOME_LAB_GAME_IMAGE:-} ]]; then
  printf 'SKIP: set HOME_LAB_GAME_IMAGE to an already-built local image ID for streaming smoke tests\n'
  exit 0
fi
[[ $HOME_LAB_GAME_IMAGE =~ ^sha256:[a-f0-9]{64}$ ]] || {
  printf 'Expected a local sha256 image ID\n' >&2
  exit 2
}
docker image inspect "$HOME_LAB_GAME_IMAGE" --format '{{.Os}}/{{.Architecture}}' | grep -Fxq linux/amd64
docker image inspect "$HOME_LAB_GAME_IMAGE" --format '{{.Config.User}}' | grep -Fxq 1000:1000
docker image inspect "$HOME_LAB_GAME_IMAGE" --format '{{json .Config.Entrypoint}}' |
  grep -Fxq '["/usr/bin/dumb-init","--","/usr/lib/workstation/wayland-session"]'
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
    [[ $(dpkg-query -W -f="\${Version}" kwin-wayland) == "$(ws_read_lock KWIN_DEBIAN_VERSION)" ]]
    [[ -z $(getcap /usr/bin/kwin_wayland) ]]
    grep -Fxq "Exec=/usr/lib/workstation/sunshine.real" /usr/share/applications/dev.lizardbyte.app.Sunshine.kwin.desktop
    [[ -x /usr/lib/workstation/wayland-session ]]
    for program in kwin_wayland Xwayland pipewire pipewire-pulse wireplumber dbus-run-session gdbus wayland-info setsid flock; do
      command -v "$program" >/dev/null
    done
    QT_QPA_PLATFORM=offscreen kwin_wayland --help >/tmp/kwin-help.txt
    for option in --virtual --xwayland --width --height --socket --exit-with-session; do
      grep -Fq -- "$option" /tmp/kwin-help.txt
    done
    Xwayland -help >/tmp/xwayland-help.txt 2>&1
    grep -Fq -- -enable-ei-portal /tmp/xwayland-help.txt
    /opt/workstation/bin/workstationctl sunshine profile /tmp/profile >/dev/null
    # Native argument parser only: --version exits before any server or encoder.
    for encoder in vaapi vulkan; do
      SUNSHINE_ENCODER=$encoder SUNSHINE_CAPTURE=kwin
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
    # Entry point refuses absent GPU before it can create a compositor. This is
    # a disposable home in the container tmpfs, never a host mount or real PVC.
    mkdir /tmp/session-home
    if env HOME=/tmp/session-home /usr/lib/workstation/wayland-session >/tmp/session.txt 2>&1; then
      printf "Device-free container unexpectedly started the gaming session\n" >&2
      exit 1
    fi
    grep -Fq "expected exactly one accessible allocated render device; found 0" /tmp/session.txt
    printf "Pinned Sunshine/Mesa/KWin, native profile parser, session contract and absent-GPU refusal passed; no stream exercised\n"
  '
