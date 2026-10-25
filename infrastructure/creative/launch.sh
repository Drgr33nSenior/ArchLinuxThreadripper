#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir -p /SwarmUI/Data/comfy-input /SwarmUI/Data/comfy-user /SwarmUI/Output/comfy /tmp/comfy-temp
cd /SwarmUI
# Invoke the prebuilt application directly. Upstream launch-linux.sh can build.
exec /SwarmUI/src/bin/live_release/SwarmUI --host 0.0.0.0 --port 7801 --launch_mode none
