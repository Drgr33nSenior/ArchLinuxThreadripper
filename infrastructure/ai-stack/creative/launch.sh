#!/usr/bin/env bash
set -euo pipefail
umask 077
# .NET requires user-scoped mutex storage, including for numeric UIDs without
# a passwd entry. Keep each launch's runtime directory private.
XDG_RUNTIME_DIR=$(mktemp -d /tmp/workstation-creative-runtime.XXXXXXXX)
export XDG_RUNTIME_DIR
mkdir -p /SwarmUI/Data/comfy-input /SwarmUI/Data/comfy-user /SwarmUI/Output/comfy /tmp/comfy-temp
cd /SwarmUI
# Invoke the prebuilt application directly. Upstream launch-linux.sh can build.
exec /SwarmUI/src/bin/live_release/SwarmUI --host 0.0.0.0 --port 7801 --launch_mode none
