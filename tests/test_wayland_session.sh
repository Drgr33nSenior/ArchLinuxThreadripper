#!/usr/bin/env bash
# shellcheck disable=SC2329 # Function overrides are invoked through sourced hooks and reject subshells.
set -euo pipefail
repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# Functions only. Never starts a compositor, changes HOME or touches a GPU.
# shellcheck source=infrastructure/gaming/wayland-session.sh
source "$repo_root/infrastructure/gaming/wayland-session.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
reject() {
  if ("$@") >"$work/rejected.txt" 2>&1; then
    printf 'unexpected success in Wayland fixture\n' >&2
    exit 1
  fi
}

timeout() { printf '%s\n' "$renderer_fixture"; }
renderer_fixture='Compositing Type: OpenGL; OpenGL renderer string: AMD Radeon fixture (radeonsi)'
gaming_renderer
renderer_fixture='Compositing Type: OpenGL; OpenGL renderer string: llvmpipe (LLVM)'
reject gaming_renderer
renderer_fixture='Compositing Type: QPainter'
reject gaming_renderer
renderer_fixture='Compositing Type: OpenGL; OpenGL renderer string: Unknown'
reject gaming_renderer
renderer_fixture='CPU: AMD\nCompositing Type: OpenGL\nOpenGL renderer string: Unknown\nGPU vendor: AMD'
reject gaming_renderer
timeout() { return 124; }
reject gaming_renderer

# Requested refresh must match an observed mode; client FPS is not evidence.
XDG_RUNTIME_DIR="$work"
GAMING_REFRESH_HZ=60
GAMING_WIDTH=1920
GAMING_HEIGHT=1080
timeout() { printf '\t\twidth: 1920 px, height: 1080 px, refresh: 60.000 Hz,\n'; }
gaming_display
jq -e '.requested_hz == 60 and .actual_millihz == 60000 and .distinct_captured_frames == "NOT RUN"' "$work/workstation-display.json" >/dev/null
GAMING_REFRESH_HZ=120 reject gaming_display
GAMING_WIDTH=2560 reject gaming_display
timeout() { printf 'width: 1920 px, height: 1080 px, refresh: 30.000 Hz,\n'; }
reject gaming_display
timeout() { printf 'width: 1920 px, height: 1080 px, refresh: 60.000 Hz,\n%.0s' 1 2; }
reject gaming_display
timeout() { return 124; }
reject gaming_display

# Existing paired config and app definitions survive initialization verbatim.
XDG_CONFIG_HOME="$work/config"
mkdir -p "$XDG_CONFIG_HOME/sunshine"
printf 'paired-config-fixture\n' >"$XDG_CONFIG_HOME/sunshine/sunshine.conf"
printf 'existing-apps-fixture\n' >"$XDG_CONFIG_HOME/sunshine/apps.json"
cp "$XDG_CONFIG_HOME/sunshine/sunshine.conf" "$work/original.conf"
cp "$XDG_CONFIG_HOME/sunshine/apps.json" "$work/original-apps.json"
unset SUNSHINE_USER SUNSHINE_PASS
gaming_init_sunshine
gaming_init_sunshine
cmp "$XDG_CONFIG_HOME/sunshine/sunshine.conf" "$work/original.conf"
cmp "$XDG_CONFIG_HOME/sunshine/apps.json" "$work/original-apps.json"
SUNSHINE_USER=fixture-user reject gaming_init_sunshine
grep -Fq 'both Sunshine credential fields are required' "$work/rejected.txt"
mv "$XDG_CONFIG_HOME/sunshine/sunshine.conf" "$XDG_CONFIG_HOME/sunshine/original.conf"
ln -s "$XDG_CONFIG_HOME/sunshine/original.conf" "$XDG_CONFIG_HOME/sunshine/sunshine.conf"
reject gaming_init_sunshine
mkdir "$work/symlink-config"
ln -s "$XDG_CONFIG_HOME/sunshine" "$work/symlink-config/sunshine"
XDG_CONFIG_HOME="$work/symlink-config" reject gaming_init_sunshine

# Missing inherited session environment fails before any graphics/audio tools.
unset DBUS_SESSION_BUS_ADDRESS WAYLAND_DISPLAY DISPLAY
reject gaming_apps
grep -Fq 'KWin must launch the session hook' "$work/rejected.txt"

# Mock process-group cleanup: target only the PIDs started by this hook.
# Subshell confines trap changes; no actual process receives a signal.
(
  gaming_pids=(12345 12346)
  kill() {
    printf '%s\n' "$*" >>"$work/signals.txt"
    [[ $1 != -0 ]]
  }
  # shellcheck disable=SC2329 # Invoked by imported gaming_cleanup, not a real wait.
  wait() { printf '%s\n' "$*" >>"$work/waits.txt"; }
  gaming_cleanup
)
grep -Fxq -- '-TERM -- -12345' "$work/signals.txt"
grep -Fxq -- '-TERM -- -12346' "$work/signals.txt"
grep -Fxq -- '-KILL -- -12345' "$work/signals.txt"
[[ $(wc -l <"$work/waits.txt" | tr -d ' ') == 2 ]]
printf 'Wayland renderer, paired-state, missing-session and process-group fixtures passed (no compositor/GPU exercised)\n'
