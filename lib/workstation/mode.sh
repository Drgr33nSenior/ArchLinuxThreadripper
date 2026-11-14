#!/usr/bin/env bash
# One-command service modes: `workstationctl mode <family|coder|gaming|maintenance> [--execute]`.
#
#   family       session `shared`: render the shared arm (chat, pictures, video), apply it
#                inside the session switch, start SGLang then both picture generators
#   coder        session `ai`: render the large arm (chat only, full context), same path
#   gaming       session `gaming`: every AI workload stopped, the game Deployment started
#   maintenance  session `maintenance`: every managed GPU workload stopped
#
# Without --execute it renders and checks, prints the plan and changes nothing live.
# With it, every gate of `session switch` applies (lock, boot evidence, qualification,
# capacity, GPU release); the render is applied only after the GPUs are free.

ws_mode_session_name() {
  case "$1" in
    family) printf 'shared\n' ;;
    coder) printf 'ai\n' ;;
    gaming | maintenance) printf '%s\n' "$1" ;;
    *) ws_die 'mode must be family, coder, gaming or maintenance' ;;
  esac
}

# This boot's hardware evidence, as written by workstation-hardware-evidence.service.
ws_mode_hardware() {
  local boot
  boot="$(tr -d '-' </proc/sys/kernel/random/boot_id)"
  printf '%s/boot-%s/hardware.json\n' "${MODE_HARDWARE_ROOT:-/var/lib/workstation-evidence}" "$boot"
}

ws_mode_gateway_status() {
  local address
  address="$(ws_session_kubectl -n "$SESSION_NAMESPACE" get service litellm -o json | jq -er .spec.clusterIP)" || return 1
  curl --silent --max-time 5 "http://$address:4000/_service/status"
}

# Every step exits explicitly on failure: callers (tests, the Bridge) may run this
# where bash ignores `set -e`, such as inside an `if` condition.
ws_mode() (
  set -euo pipefail
  local name="$1" execute="${2:-}" session_mode render_mode='' hardware dir root stamp render managed summary contract
  session_mode="$(ws_mode_session_name "$name")" || exit 1
  [[ -z "$execute" || "$execute" == --execute ]] || ws_die 'the only option is --execute'
  [[ "$name" != family ]] || render_mode=shared
  [[ "$name" != coder ]] || render_mode=large
  ws_require_arch
  ws_require_root
  ws_session_config_validate || exit 1
  ws_session_installed_policy || exit 1
  dir="${SESSION_STATE_DIRECTORY:-${MODE_STATE_DIRECTORY:-/var/lib/workstation-session}}"
  hardware="$(ws_mode_hardware)" || exit 1
  [[ -f "$hardware" ]] ||
    ws_die "no hardware evidence for this boot ($hardware). Enable workstation-hardware-evidence.service, or run: workstationctl hardware collect $(dirname -- "$hardware")"

  if [[ -z "$render_mode" ]]; then
    ws_note "mode $name: session $session_mode; no render"
    [[ "$execute" == --execute ]] || {
      ws_note 'plan only; rerun with --execute to switch'
      exit 0
    }
    ws_session_switch "$session_mode" "$hardware" "$dir" --execute || exit 1
    exit 0
  fi

  [[ -n ${MODE_SERVICE_SITE:-} && -f "$MODE_SERVICE_SITE" ]] ||
    ws_die 'set MODE_SERVICE_SITE to the site.json used for service renders'
  root="${MODE_RENDER_DIRECTORY:-/var/lib/workstation-mode}"
  [[ "$root" == /* && "$root" != / ]] || ws_die 'MODE_RENDER_DIRECTORY must be an absolute path'
  (
    umask 077
    mkdir -p -- "$root"
  ) || ws_die "cannot create $root"
  [[ ! -L "$root" && "$(stat -c '%u:%a' "$root")" == 0:700 ]] || ws_die "$root must be a root-owned mode 0700 directory"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$name" || exit 1
  render="$root/$stamp"
  [[ ! -e "$render" ]] || ws_die "$render already exists"
  managed="$(jq -rn --arg ai "$SESSION_AI_DEPLOYMENT" --arg game "$SESSION_GAME_DEPLOYMENT" --arg image "${SESSION_IMAGE_DEPLOYMENT:-}" \
    --arg image2 "${SESSION_IMAGE2_DEPLOYMENT:-}" '[$ai, $game, $image, $image2] | map(select(. != "")) | join(",")')" || exit 1

  # The live chat image is kept: a mode switch changes the arm, never the image.
  local image
  image="$(ws_session_deployment "$SESSION_AI_DEPLOYMENT" | jq -er '[.spec.template.spec.containers[] | select(.name == "sglang") | .image] | if length == 1 then .[0] else error("no sglang container") end')" ||
    ws_die 'cannot read the live SGLang image'
  [[ "$image" =~ @sha256:[a-f0-9]{64}$ ]] || ws_die 'the live SGLang image is not pinned by digest'
  ws_session_kubectl -n "$SESSION_NAMESPACE" get persistentvolumeclaims -o json >"$render.claims.json" ||
    ws_die 'cannot read the existing claims'
  ws_session_kubectl -n "$SESSION_NAMESPACE" get deployments -o json >"$render.live.json" ||
    ws_die 'cannot read the live Deployments'
  # The coding workspace pool fits coder mode only (docs/ASSISTANT.md, capacity by mode).
  # Family mode renders without it, so the switch still works and WebUI offers no workspace.
  local -a sandbox_args=()
  if [[ -n ${MODE_SANDBOX_CONFIG:-} ]]; then
    if [[ "$render_mode" == large ]]; then
      sandbox_args=(--sandbox-config "$MODE_SANDBOX_CONFIG")
    else
      ws_note 'coding workspaces are off in this mode: the pool does not fit beside the picture generators'
    fi
  fi
  "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/service.py" render "$MODE_SERVICE_SITE" "$render" \
    --mode "$render_mode" --experience "${MODE_SERVICE_EXPERIENCE:-assistant-memory}" \
    --existing-claims "$render.claims.json" \
    --serving-profile "${MODE_SERVING_PROFILE:-$(ws_repo_root)/infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json}" \
    --runtime-image "$image" "${sandbox_args[@]}" >/dev/null || ws_die 'service render failed'
  contract="$(jq -ce '{status, contract, arm: .selection.arm, context: .budgets.context_length, pool: .budgets.max_total_tokens}' "$render/serving-contract.json")" ||
    ws_die 'the render has no serving contract'
  [[ "$(jq -r .status <<<"$contract")" == active ]] ||
    ws_die "the rendered contract is not active (qualified profile and promoted catalogue): $contract"
  summary="$("$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/service_mode.py" prepare \
    "$render/workloads.yaml" "$render.live.json" "$render/apply.json" --managed "$managed")" ||
    ws_die 'cannot prepare the render for the switch'
  jq -n --arg mode "$name" --arg session "$session_mode" --argjson contract "$contract" --argjson summary "$summary" \
    '{mode: $mode, session: $session, contract: $contract, replicas: $summary.deployments, not_live: $summary.not_live}' |
    tee "$render/mode-plan.json" || ws_die 'cannot write the mode plan'
  ws_note "render: $render"
  if [[ "$execute" != --execute ]]; then
    ws_note 'plan only; nothing changed. Rerun with --execute to switch.'
    exit 0
  fi

  ws_session_switch "$session_mode" "$hardware" "$dir" --execute "$render/apply.json" || exit 1
  ws_note "gateway: $(ws_mode_gateway_status || printf 'not answering')"
  ws_note "coding clients: the context limit is now $(jq -r .context <<<"$contract") tokens; refresh Qwen Code from $render/client.conf"
)
