#!/usr/bin/env bash
# Roll out merged main on the workstation: the runtime package from a shipped tree (install-runtime.sh),
# a plan-only family render checked against live, then a family-mode re-apply and post-rollout checks.
#
#   rollout-main.sh SHA12
#
# SHA12 names the shipped tree $WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12 (default ~/src). Logs and
# copies of the render: ~/build/workstation-runtime-SHA12/.
#
# Stops (STOP: ...) before any change when a picture or video is in flight, and before the re-apply when
# the render would change a Deployment's container sizing, the SGLang image or the SGLang environment
# (rollout_check.py). Reviewed changes are allowed explicitly:
#   EXPECT_CHANGED="deployment=container:memory ..."   sizing changes (requests = limits = memory)
#   EXPECT_SGLANG_ENV="NAME ..."                       SGLang environment variables that may change
# SGLang restarts only when its rendered pod template changed; service-tools usually restarts.
# Optional site value CHAT_PROBE: a chat probe script (URL COUNT -> JSON) run against SGLang at the end.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
sha=${1:?usage: rollout-main.sh SHA12 (--help for details)}
tools::load_site
ns=ai-home-lab
conf=/etc/workstation/workstation.conf
src=$(tools::source_tree "$sha")
out=$HOME/build/workstation-runtime-$sha
log() { printf '%s %s\n' "$(date -u +%T)" "$*"; }
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}

log '== preflight'
[[ -f $src/versions.lock ]] || stop "no source tree at $src"
tools=$(kubectl -n $ns get svc service-tools -o jsonpath='{.spec.clusterIP}')
health=$(curl -s -m 5 "http://$tools:8000/health")
grep -q '"image_active": *0' <<<"$health" || stop 'a picture is in flight'
grep -q '"video_active": *false' <<<"$health" || stop 'a video is in flight'
live_sglang=$(kubectl -n $ns get deploy sglang -o jsonpath='{.spec.template.spec.containers[0].image}')

log "== runtime package from $sha"
bash "$here/install-runtime.sh" "$sha"

log '== plan-only render, then the resource and SGLang checks'
plan=$(sudo workstationctl --config "$conf" mode family 2>&1 | tee "$out/family-plan.log")
render=$(sed -n 's/^render: //p' <<<"$plan" | tail -1)
if [[ -z $render ]] || ! sudo test -f "$render/workloads.yaml"; then stop 'no family render'; fi
kubectl -n $ns get deploy -o json >"$out/live-deployments.json"
sudo cat "$render/workloads.yaml" | tee "$out/family-workloads.yaml" >/dev/null
python3 "$here/rollout_check.py" "$out/family-workloads.yaml" "$out/live-deployments.json" "$live_sglang" ||
  stop 'the render changes more than the reviewed allowlists (EXPECT_CHANGED, EXPECT_SGLANG_ENV); nothing applied'

log '== mode family (re-apply)'
sudo workstationctl --config "$conf" mode family --execute | tail -4
for d in sglang litellm open-webui service-tools comfyui-gpu1 comfyui-gpu0; do kubectl -n $ns rollout status deploy/$d --timeout=10m >/dev/null; done

log '== checks'
kubectl -n $ns get pods --no-headers | awk '$3!="Running" && $3!="Completed"{print "NOT RUNNING:", $1, $3}'
log "sglang image $(kubectl -n $ns get deploy sglang -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*@sha256:\(.\{12\}\).*/\1/')"
lit=$(kubectl -n $ns get svc litellm -o jsonpath='{.spec.clusterIP}')
log "gateway $(curl -s -m 5 "http://$lit:4000/_service/status")"
log "open-webui image $(kubectl -n $ns get deploy open-webui -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*@sha256:\(.\{12\}\).*/\1/')"
log "service-tools kv_tail $(curl -s -m 5 "http://$tools:8000/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kv_tail"])')"
sg=$(kubectl -n $ns get svc sglang -o jsonpath='{.spec.clusterIP}')
log "sglang kv_tail $(curl -s -m 5 "http://$sg:30000/kv_tail" | python3 -c 'import json,sys; s=json.load(sys.stdin); print({k: s.get(k) for k in ("state","tail_tokens","fault")})')"
if [[ -n ${CHAT_PROBE:-} ]]; then
  log "chat $(python3 "$CHAT_PROBE" "http://$sg:30000" 1)"
else
  log 'chat probe skipped (set CHAT_PROBE)'
fi
log ROLLOUT_MAIN=done
