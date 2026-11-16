#!/usr/bin/env bash
# A/B the ComfyUI memory limits. A = the current limits, B = B_MEMORY on both (default 9Gi; Guaranteed:
# requests = limits). Each arm starts from fresh pods, then: a cold pair and a warm pair of pictures
# through the live endpoint (one per pod), then one clip that the owner starts from a chat; both pods'
# cgroup memory is sampled every 0.5 s. The original limits are restored at the end and on any failure.
# Run on the workstation; stops when a picture or video is in flight.
#
#   comfy-ab.sh          SKIP_CLIP=1 skips the clips; ARMS="B" runs only arm B; B_MEMORY=10Gi sets arm B
#
# A failed picture stops the run; if both generators are idle, the stale in-flight marker it left (which
# blocks every family picture) is removed, as docs/ASSISTANT.md's picture recovery describes.
# Results: ~/ai-stack/comfy-ab-<UTC stamp>/.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
b_memory=${B_MEMORY:-9Gi}
[[ $b_memory =~ ^[1-9][0-9]*Gi$ ]] || {
  echo "STOP: B_MEMORY must look like 9Gi" >&2
  exit 1
}
ns=ai-home-lab
root=$HOME/ai-stack/comfy-ab-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$root"
log() { printf '%s %s\n' "$(date +%T)" "$*" | tee -a "$root/run.log"; }
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
limit() { kubectl -n $ns get deploy "$1" -o jsonpath='{.spec.template.spec.containers[?(@.name=="comfyui")].resources.limits.memory}'; }
tools=$(kubectl -n $ns get svc service-tools -o jsonpath='{.spec.clusterIP}')
health() { curl -s -m 5 "http://$tools:8000/health"; }
idle() { health | grep -q '"image_active": *0' && health | grep -q '"video_active": *false'; }

orig0=$(limit comfyui-gpu0)
orig1=$(limit comfyui-gpu1)
[[ -n $orig0 && -n $orig1 ]] || stop "could not read the starting limits"
log "starting limits gpu0=$orig0 gpu1=$orig1 (restored at the end)"
idle || stop 'a picture or video is in flight'

setmem() { # gpu0 gpu1: patch both, wait for the new pods (Recreate: the old pod stops first)
  local d m
  for d in comfyui-gpu0 comfyui-gpu1; do
    if [[ $d == comfyui-gpu0 ]]; then m=$1; else m=$2; fi
    kubectl -n $ns patch deploy "$d" --type=strategic -p \
      "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"comfyui\",\"resources\":{\"requests\":{\"memory\":\"$m\"},\"limits\":{\"memory\":\"$m\"}}}]}}}}" >/dev/null
  done
  for d in comfyui-gpu0 comfyui-gpu1; do kubectl -n $ns rollout status deploy/$d --timeout=10m >/dev/null; done
  ready
}
ready() { # pod Ready is not enough: wait until each ComfyUI answers through its Service, then settle
  local d ip
  for d in comfyui-gpu0 comfyui-gpu1; do
    ip=$(kubectl -n $ns get svc "$d" -o jsonpath='{.spec.clusterIP}')
    for _ in $(seq 1 120); do
      curl -sf -m 3 -o /dev/null "http://$ip:7821/system_stats" && break
      sleep 1
    done
    curl -sf -m 3 -o /dev/null "http://$ip:7821/system_stats" || stop "$d not answering"
  done
  sleep 15
}
recover() { # clear in-flight markers a failed test picture left, only when nothing is running
  local d q
  for d in comfyui-gpu1 comfyui-gpu0; do
    q=$(kubectl -n $ns exec deploy/$d -c comfyui -- /SwarmUI/dlbackend/ComfyUI/venv/bin/python -c \
      "import json,urllib.request; q=json.load(urllib.request.urlopen('http://127.0.0.1:7821/queue',timeout=5)); print(len(q['queue_running'])+len(q['queue_pending']))")
    [[ $q == 0 ]] || {
      log "$d still has $q queued; leaving markers for the operator"
      return
    }
  done
  idle || {
    log 'service-tools reports work in flight; leaving markers for the operator'
    return
  }
  kubectl -n $ns exec deploy/service-tools -c tools -- sh -c 'rm -fv /state/image-inflight*.json' | tee -a "$root/run.log"
  log "recovery_required now: $(health | python3 -c 'import json,sys; print(json.load(sys.stdin)["image_recovery_required"])')"
}
restored=0
restore() {
  [[ $restored == 1 ]] && return
  restored=1
  if [[ $(limit comfyui-gpu0) != "$orig0" || $(limit comfyui-gpu1) != "$orig1" ]]; then
    log "restoring gpu0=$orig0 gpu1=$orig1"
    setmem "$orig0" "$orig1"
  fi
  log "limits now gpu0=$(limit comfyui-gpu0) gpu1=$(limit comfyui-gpu1)"
}
trap restore EXIT

pair() { # out name
  local a b
  kubectl -n $ns exec -i deploy/service-tools -c tools -- python3 - <"$here/soak_picture.py" >"$1/$2-a.json" 2>&1 &
  a=$!
  sleep 1
  kubectl -n $ns exec -i deploy/service-tools -c tools -- python3 - <"$here/soak_picture.py" >"$1/$2-b.json" 2>&1 &
  b=$!
  wait "$a" "$b" || true
  log "$2 pictures: $(python3 -c 'import json,sys; print(*[json.loads(open(f).read().splitlines()[-1]).get("seconds") for f in sys.argv[1:]])' "$1/$2-a.json" "$1/$2-b.json") s"
  if ! python3 -c 'import json,sys; sys.exit(0 if all(json.loads(open(f).read().splitlines()[-1]).get("ok") for f in sys.argv[1:]) else 1)' "$1/$2-a.json" "$1/$2-b.json"; then
    log "$2: a picture failed: $(tail -qn1 "$1/$2-a.json" "$1/$2-b.json" | grep -o '"error": "[^}]*' | head -2 | tr '\n' ' ')"
    recover
    stop "picture failed in $2; results so far in $root"
  fi
}

clip() { # out
  [[ ${SKIP_CLIP:-0} == 1 ]] && return
  log '>>> Start ONE 5-second clip now from Open WebUI, same prompt both times, e.g. "a paper boat drifting on a pond". Waiting up to 10 min.'
  for _ in $(seq 1 600); do
    health | grep -q '"video_active": *true' && break
    sleep 1
  done
  health | grep -q '"video_active": *true' || {
    log 'no clip started; skipping'
    return
  }
  local t0=$SECONDS
  while health | grep -q '"video_active": *true'; do sleep 1; done
  log "clip finished after ~$((SECONDS - t0)) s; ComfyUI: $(kubectl -n $ns logs deploy/comfyui-gpu0 -c comfyui --since=15m 2>/dev/null | grep -o 'Prompt executed in [0-9.]* seconds' | tail -1)"
}

arm() { # name
  local out=$root/$1 uids sampler
  mkdir -p "$out"
  : >"$out/marks"
  uids=$(kubectl -n $ns get pods -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.uid}{"\n"}{end}' | awk '/^comfyui-gpu[01]-/{print $2}')
  # shellcheck disable=SC2086 # one argument per pod UID
  python3 "$here/comfy_mem.py" "$out" $uids >"$out/sampler.log" 2>&1 &
  sampler=$!
  echo "$(date +%s.%N) cold" >>"$out/marks"
  pair "$out" cold
  echo "$(date +%s.%N) warm" >>"$out/marks"
  pair "$out" warm
  echo "$(date +%s.%N) clip" >>"$out/marks"
  clip "$out"
  touch "$out/STOP"
  wait "$sampler" || true
  log "$1 memory peaks (GiB): $(python3 "$here/comfy_summary.py" "$out/summary.json")"
}

if [[ " ${ARMS:-A B} " == *" A "* ]]; then
  log "== arm A: gpu0 $orig0, gpu1 $orig1 (fresh pods)"
  kubectl -n $ns rollout restart deploy/comfyui-gpu0 deploy/comfyui-gpu1 >/dev/null
  for d in comfyui-gpu0 comfyui-gpu1; do kubectl -n $ns rollout status deploy/$d --timeout=10m >/dev/null; done
  ready
  arm A
fi
if [[ " ${ARMS:-A B} " == *" B "* ]]; then
  log "== arm B: $b_memory on both (fresh pods)"
  setmem "$b_memory" "$b_memory"
  arm B
fi
log "results: $root"
