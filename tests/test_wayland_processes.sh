#!/usr/bin/env bash
set -euo pipefail
# Linux Bash/setsid integration only, using an already-present reviewed image.
# This does not test graphics or claim that the new image builds successfully.
if [[ -z ${HOME_LAB_WAYLAND_RUNTIME_IMAGE:-} ]]; then
  printf 'SKIP: set HOME_LAB_WAYLAND_RUNTIME_IMAGE for isolated Linux process-group tests\n'
  exit 0
fi
[[ $HOME_LAB_WAYLAND_RUNTIME_IMAGE =~ ^sha256:[a-f0-9]{64}$ ]] || exit 2
repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
docker image inspect "$HOME_LAB_WAYLAND_RUNTIME_IMAGE" --format '{{.Os}}/{{.Architecture}}' | grep -Fxq linux/amd64
# The only host mount is this non-secret source file, read-only. No workspace,
# persistent home, GPU, input devices, network, capabilities or host sockets.
docker run --rm --pull never --platform linux/amd64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 \
  --pids-limit 32 --memory 128m --cpus 1 \
  --mount "type=bind,source=$repo_root/infrastructure/gaming/wayland-session.sh,target=/mnt/session-functions.sh,readonly" \
  --entrypoint /bin/bash "$HOME_LAB_WAYLAND_RUNTIME_IMAGE" -c '
    set -euo pipefail
    source /mnt/session-functions.sh
    for iteration in 1 2 3; do
      gaming_pids=()
      gaming_start /bin/sleep 60
      child=${gaming_pids[0]}
      sleep 0.2
      kill -0 -- "-$child"
      gaming_cleanup
      if kill -0 -- "-$child" 2>/dev/null; then exit 1; fi
    done
    gaming_pids=()
    gaming_start /bin/bash -c "trap \"\" TERM; exec sleep 60"
    child=${gaming_pids[0]}
    sleep 0.2
    kill -0 -- "-$child"
    gaming_cleanup
    if kill -0 -- "-$child" 2>/dev/null; then exit 1; fi
    printf "Repeated Linux process-group cleanup and bounded TERM-resistant shutdown passed; no compositor or GPU exercised\n"
  '
