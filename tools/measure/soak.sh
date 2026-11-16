#!/usr/bin/env bash
# Soak the live elastic KV cycle: every INTERVAL seconds, one picture through the live service-tools
# endpoint (which shrinks SGLang's tail first), then wait for service-tools' own idle grow. Records
# SGLang's tail state, each GPU's VRAM and (with CHAT_PROBE) a chat probe every round, then kernel GPU
# faults and pod restarts. Run on the workstation.
#
#   soak.sh [ROUNDS] [INTERVAL_SECONDS]      (defaults 5 and 720)
#
# Log: ~/ai-stack/soak-<UTC stamp>.log. Optional site value CHAT_PROBE: a chat probe script
# (URL COUNT -> JSON with decode_tok_per_s).
set -uo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
tools::load_site
ns=ai-home-lab
rounds=${1:-5}
interval=${2:-720}
out=$HOME/ai-stack/soak-$(date -u +%Y%m%dT%H%M%SZ).log
sg=$(kubectl -n $ns get svc sglang -o jsonpath='{.spec.clusterIP}')
since=$(date '+%Y-%m-%d %H:%M:%S')
log() { printf '%s %s\n' "$(date -u +%T)" "$*" | tee -a "$out"; }
tail_state() { curl -s -m 5 "http://$sg:30000/kv_tail" | jq -c '{state, tail_tokens, tail_slots_in_use, fault}'; }
vram() { # every amdgpu device, keyed by PCI address
  local f
  for f in /sys/bus/pci/devices/*/mem_info_vram_used; do
    [[ -r $f ]] || continue
    awk -v p="$(basename "$(dirname "$f")")" '{printf "%s=%.2f ", p, $1/2^30}' "$f"
  done
}
chat() {
  if [[ -n ${CHAT_PROBE:-} ]]; then python3 "$CHAT_PROBE" "http://$sg:30000" 1 | jq -c .decode_tok_per_s; else echo skipped; fi
}
for r in $(seq 1 "$rounds"); do
  log "round $r before: tail $(tail_state) vram $(vram)"
  log "round $r picture: $(kubectl -n $ns exec -i deploy/service-tools -c tools -- python3 - <"$here/soak_picture.py" 2>&1 | tail -1)"
  log "round $r after: tail $(tail_state) vram $(vram) chat $(chat)"
  [[ $r == "$rounds" ]] || sleep "$interval"
done
log "faults since $since: $(sudo -n journalctl -k --since "$since" --no-pager | grep -ciE 'amdgpu.*(fault|timeout|reset)|page fault')"
log "restarts: $(kubectl -n $ns get pods --no-headers | awk '$4!="0" && $1!~/migrate|postgres/{print $1"="$4}' | tr '\n' ' ')"
log SOAK=done
