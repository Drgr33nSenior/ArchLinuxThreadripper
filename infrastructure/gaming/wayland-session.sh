#!/usr/bin/env bash
# One container-owned user session. Never invoke the inherited Xorg supervisor.
set -euo pipefail

gaming_fail() {
  printf 'Wayland session: %s\n' "$*" >&2
  exit 1
}

gaming_gpu() {
  local device bdf
  device=$(/opt/workstation/bin/workstationctl sunshine devices) || return 1
  bdf=$(jq -er .bdf <<<"$device")
  # Mesa preference, not ownership. KWin --virtual opens the sole accessible
  # render node; KWIN_DRM_DEVICES only applies to the physical DRM backend.
  export DRI_PRIME="pci-${bdf//[:.]/_}" LIBVA_DRIVER_NAME=radeonsi
}

gaming_home() {
  [[ $EUID != 0 ]] || gaming_fail 'run as the non-root session user'
  [[ -d $HOME && -O $HOME && -w $HOME ]] || gaming_fail 'persistent home must be owned and writable by the session user; no automatic chown'
  cd -- "$HOME"
  export XDG_CONFIG_HOME="$HOME/.config" XDG_CACHE_HOME="$HOME/.cache"
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
}

gaming_init_sunshine() {
  local config_dir="$XDG_CONFIG_HOME/sunshine"
  [[ ! -L $config_dir ]] || gaming_fail 'Sunshine directory must not be a symlink'
  mkdir -p "$config_dir"
  [[ -O $config_dir && -w $config_dir ]] || gaming_fail 'Sunshine directory is not owned and writable'
  chmod 0700 "$config_dir"
  # Do not reset paired clients, overwrite apps, or import Xrandr prep commands.
  if [[ ! -e $config_dir/sunshine.conf && ! -L $config_dir/sunshine.conf ]]; then
    printf '# Workstation Wayland; process profile supplies capture and encoder.\n' >"$config_dir/sunshine.conf"
  fi
  if [[ ! -e $config_dir/apps.json && ! -L $config_dir/apps.json ]]; then
    cp /usr/share/workstation/wayland-apps.json "$config_dir/apps.json"
  fi
  [[ -f $config_dir/sunshine.conf && ! -L $config_dir/sunshine.conf ]] || gaming_fail 'Sunshine config must be a regular file'
  # Preserve the existing Secret-backed credential update contract. The native
  # CLI takes arguments; never echo them or its output into container logs.
  if [[ -n ${SUNSHINE_USER:-} || -n ${SUNSHINE_PASS:-} ]]; then
    [[ -n ${SUNSHINE_USER:-} && -n ${SUNSHINE_PASS:-} ]] || gaming_fail 'both Sunshine credential fields are required'
    /usr/bin/sunshine "$config_dir/sunshine.conf" --creds "$SUNSHINE_USER" "$SUNSHINE_PASS" >/dev/null 2>&1 ||
      gaming_fail 'Sunshine credential initialization failed (output withheld)'
  fi
}

gaming_renderer() {
  local info renderer
  info=$(timeout --kill-after=2s 15s gdbus call --session --dest org.kde.KWin \
    --object-path /KWin --method org.kde.KWin.supportInformation) || return 1
  [[ $info == *'Compositing Type: OpenGL'* && $info == *'OpenGL renderer string:'* ]] ||
    gaming_fail 'KWin did not report OpenGL compositing'
  # gdbus prints a GVariant string with escaped newlines. Inspect the renderer
  # field only, not an AMD CPU name elsewhere in the support report.
  renderer=${info#*OpenGL renderer string:}
  renderer=${renderer%%\\n*}
  renderer=${renderer%%$'\n'*}
  case $(printf '%s' "$renderer" | tr '[:upper:]' '[:lower:]') in
    *llvmpipe* | *softpipe* | *swrast* | *software\ rasterizer*) gaming_fail 'software compositor is not a gaming candidate' ;;
  esac
  [[ $renderer == *AMD* || $renderer == *radeonsi* || $renderer == *RADV* || $renderer == *ATI* ]] ||
    gaming_fail 'KWin renderer is not identifiable as AMD; inspect local supportInformation'
}

gaming_display() {
  local info modes expected
  # KWin 6.3.6's virtual backend creates one 60000 mHz mode. There is no
  # --refresh-rate launch switch. Never mistake a client FPS request for a mode.
  [[ ${GAMING_REFRESH_HZ:-60} == 60 ]] || gaming_fail 'pinned KWin virtual backend supports 60 Hz only; qualify a newer custom-mode backend before requesting high refresh'
  info=$(timeout --kill-after=2s 10s wayland-info) || gaming_fail 'cannot observe the actual Wayland output mode'
  # wayland-info prints Hz to three decimal places, not the protocol's mHz.
  modes=$(sed -nE 's/^[[:space:]]*width: ([0-9]+) px, height: ([0-9]+) px, refresh: ([0-9]+\.[0-9]{3}) Hz,?$/\1 \2 \3/p' <<<"$info")
  expected="${GAMING_WIDTH:-1920} ${GAMING_HEIGHT:-1080} 60.000"
  [[ $modes == "$expected" ]] || gaming_fail 'expected exactly one observed virtual mode matching configured dimensions at 60.000 Hz'
  jq -n --argjson requested "${GAMING_REFRESH_HZ:-60}" \
    --argjson width "${GAMING_WIDTH:-1920}" --argjson height "${GAMING_HEIGHT:-1080}" \
    '{requested_hz:$requested,actual_millihz:60000,width:$width,height:$height,source:"wayland-info",distinct_captured_frames:"NOT RUN"}' \
    >"$XDG_RUNTIME_DIR/workstation-display.json"
}

gaming_start() {
  # Each managed process owns a session/group, so shutdown also reaches its
  # children. No arbitrary command strings or shell evaluation from config.
  setsid "$@" &
  gaming_pids+=("$!")
}

gaming_cleanup() {
  local pid attempt alive
  trap - EXIT TERM INT
  for pid in "${gaming_pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  for ((attempt = 0; attempt < 50; attempt++)); do
    alive=false
    for pid in "${gaming_pids[@]}"; do
      if kill -0 -- "-$pid" 2>/dev/null; then alive=true; fi
    done
    [[ $alive == true ]] || break
    sleep 0.1
  done
  for pid in "${gaming_pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  for pid in "${gaming_pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

gaming_audio_ready() {
  local attempt pid
  for ((attempt = 0; attempt < 100; attempt++)); do
    for pid in "${gaming_pids[@]}"; do kill -0 "$pid" 2>/dev/null || return 1; done
    if timeout --kill-after=1s 1s pactl info >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  return 1
}

gaming_apps() {
  local status=0
  local -a gaming_pids=() steam_args=()
  [[ -n ${DBUS_SESSION_BUS_ADDRESS:-} && -n ${WAYLAND_DISPLAY:-} && -n ${DISPLAY:-} ]] ||
    gaming_fail 'KWin must launch the session hook with its D-Bus, Wayland and Xwayland environment'
  [[ -S $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY ]] || gaming_fail 'KWin Wayland socket is unavailable'
  gaming_renderer
  gaming_display
  # D-Bus activated portals need this session's actual compositor environment.
  dbus-update-activation-environment WAYLAND_DISPLAY DISPLAY XAUTHORITY XDG_RUNTIME_DIR XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
  trap gaming_cleanup EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  gaming_start pipewire
  gaming_start wireplumber
  gaming_start pipewire-pulse
  gaming_audio_ready || gaming_fail 'PipeWire audio startup failed or timed out'
  # A container-local sink, not the workstation's physical or host Pulse socket.
  timeout --kill-after=2s 10s pactl load-module module-null-sink sink_name=workstation sink_properties=device.description=Workstation >/dev/null
  timeout --kill-after=2s 10s pactl set-default-sink workstation
  if [[ ${ENABLE_STEAM:-true} == true ]]; then
    read -r -a steam_args <<<"${STEAM_ARGS:--silent}"
    gaming_start /usr/games/steam "${steam_args[@]}"
  fi
  if [[ ${ENABLE_SUNSHINE:-true} == true ]]; then
    gaming_start /usr/bin/sunshine "$XDG_CONFIG_HOME/sunshine/sunshine.conf"
  fi
  # Any essential process exit ends this session. KWin --exit-with-session then
  # exits too; Kubernetes handles restart/backoff, not a second supervisor.
  wait -n "${gaming_pids[@]}" || status=$?
  ((status != 0)) || status=1
  gaming_cleanup
  return "$status"
}

gaming_main() {
  umask 077
  gaming_home
  if [[ ${1:-} == --apps && $# == 1 ]]; then
    gaming_apps
    return
  fi
  (($# == 0)) || gaming_fail 'unexpected session arguments'
  # Load/validate the same profile as the CLI; no host configuration discovery.
  # shellcheck source=lib/common.sh
  source /opt/workstation/lib/common.sh
  # shellcheck source=lib/workstation/runtime.sh
  source /opt/workstation/lib/workstation/runtime.sh
  ws_sunshine_config_validate
  [[ $GAMING_REFRESH_HZ == 60 ]] || gaming_fail 'pinned KWin virtual backend supports 60 Hz only; no unsupported refresh flag will be passed'
  case $SUNSHINE_CAPTURE in kwin | portal) ;; *) gaming_fail 'Wayland image requires kwin or portal capture; select the x11 image explicitly for rollback' ;; esac
  for enabled in "${ENABLE_STEAM:-true}" "${ENABLE_SUNSHINE:-true}"; do
    [[ $enabled == true || $enabled == false ]] || gaming_fail 'ENABLE_STEAM and ENABLE_SUNSHINE must be true or false'
  done
  [[ ${ENABLE_STEAM:-true} == true || ${ENABLE_SUNSHINE:-true} == true ]] || gaming_fail 'enable at least one streaming session path'
  # The lock persists on the existing home, not a host-wide/global path.
  exec 9>"$XDG_CACHE_HOME/workstation-wayland.lock"
  flock -n 9 || gaming_fail 'another Wayland session holds this persistent home'
  gaming_gpu
  if [[ ${ENABLE_SUNSHINE:-true} == true ]]; then gaming_init_sunshine; fi
  unset SUNSHINE_USER SUNSHINE_PASS WEBUI_USER WEBUI_PASS
  # Never inherit upstream DISPLAY=:55, a host bus or a host Pulse server.
  unset DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS PULSE_SERVER KWIN_DRM_DEVICES
  XDG_RUNTIME_DIR=$(mktemp -d /tmp/workstation-wayland.XXXXXXXX)
  export XDG_RUNTIME_DIR XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=KDE
  export QT_QPA_PLATFORM=wayland KWIN_COMPOSE=O2
  # KWin assigns Xwayland DISPLAY/XAUTHORITY and passes them to --exit-with-session.
  # Do not guess :0, scan another session's X socket or force permission bypasses.
  exec dbus-run-session -- kwin_wayland --virtual --xwayland --socket=wayland-0 \
    --width="$GAMING_WIDTH" --height="$GAMING_HEIGHT" \
    --exit-with-session '/usr/lib/workstation/wayland-session --apps'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then gaming_main "$@"; fi
