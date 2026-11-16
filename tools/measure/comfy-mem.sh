#!/usr/bin/env bash
# Measure both ComfyUI pods' memory through real pictures on the live endpoint: a cold pair (models
# unloaded), then a warm pair, two at once so one lands on each pod. Read-only apart from the pictures.
# Run on the workstation; stops when a picture or video is in flight.
#
#   comfy-mem.sh
#
# Results: ~/ai-stack/comfy-mem-<UTC stamp>/ (comfy_mem.py timeline and per-phase peaks).
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
ns=ai-home-lab
out=$HOME/ai-stack/comfy-mem-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$out"
: >"$out/marks"
uids=$(kubectl -n $ns get pods -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.uid}{"\n"}{end}' | awk '/^comfyui-gpu[01]-/{print $2}')
tools=$(kubectl -n $ns get svc service-tools -o jsonpath='{.spec.clusterIP}')
health=$(curl -s -m 5 "http://$tools:8000/health")
grep -q '"image_active": *0' <<<"$health" || {
  echo 'STOP: a picture is in flight' >&2
  exit 1
}
grep -q '"video_active": *false' <<<"$health" || {
  echo 'STOP: a video is in flight' >&2
  exit 1
}
# shellcheck disable=SC2086 # one argument per pod UID
python3 "$here/comfy_mem.py" "$out" $uids >"$out/sampler.log" 2>&1 &
sampler=$!
mark() { echo "$(date +%s.%N) $1" >>"$out/marks"; }
pair() {
  local a b
  kubectl -n $ns exec -i deploy/service-tools -c tools -- python3 - <"$here/soak_picture.py" >"$out/$1-a.json" 2>&1 &
  a=$!
  sleep 1
  kubectl -n $ns exec -i deploy/service-tools -c tools -- python3 - <"$here/soak_picture.py" >"$out/$1-b.json" 2>&1 &
  b=$!
  wait "$a" "$b" || true
  echo "$1: $(tail -1 "$out/$1-a.json") | $(tail -1 "$out/$1-b.json")"
}
mark idle
sleep 5
mark cold
pair cold
mark warm
pair warm
mark after
sleep 5
touch "$out/STOP"
wait "$sampler" || true
cat "$out/sampler.log"
echo "results: $out"
