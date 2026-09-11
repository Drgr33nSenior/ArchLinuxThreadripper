#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -z ${HOME_LAB_PYTHON:-} ]]; then
  printf 'SKIP: set HOME_LAB_PYTHON for serving runtime wrapper fixtures\n'
  exit 0
fi
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
# shellcheck source=lib/workstation/performance.sh
source "$root/lib/workstation/performance.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
ws_repo_root() { printf '%s\n' "$root"; }
ws_measure_python() { printf '%s\n' "$HOME_LAB_PYTHON"; }
SESSION_NODE=fixture-node
kernel_evidence_impl=$(declare -f ws_kernel_evidence)
ws_serving_pod() {
  jq -n --argjson ready "${FIXTURE_READY:-false}" --arg node "${FIXTURE_NODE:-fixture-node}" '{uid:"11111111-2222-3333-4444-555555555555",
    started_at:"2026-09-11T00:00:00Z",container_id:"containerd://fixture",restart_count:0,
    node:$node,image:("fixture@sha256:"+("a"*64)),image_id:"containerd://fixture",ready:$ready}'
}

# A not-Ready Pod is current model loading. The wrapper must not obtain stale
# ready-only evidence or start a representative warmup.
ws_kernel_evidence() { : >"$work/kernel-evidence-called"; return 99; }
FIXTURE_READY=false ws_serving_warm_status fixture - "$work/loading.json"
jq -e '.status == "model-loading" and .representative_warmup == "not-applicable" and .identity == null' "$work/loading.json" >/dev/null
[[ ! -e $work/kernel-evidence-called ]]

# A Ready bit alone is not model/runtime/device proof. A failed fresh probe
# remains an explicit unknown state without helper diagnostics.
FIXTURE_READY=true ws_serving_warm_status fixture - "$work/unknown.json"
jq -e '.status == "unknown" and .kubernetes_readiness == "healthy" and .identity == null and
  (.reason|test("fresh current"))' "$work/unknown.json" >/dev/null
[[ -e $work/kernel-evidence-called ]]
if FIXTURE_NODE=other ws_serving_warm_status fixture - "$work/outside.json" 2>/dev/null; then
  printf 'warm status accepted a Pod outside the reviewed node scope\n' >&2
  exit 1
fi

# Exercise the real nested ws_kernel_evidence wrapper. A failed inner compiler
# probe must propagate out of that wrapper even though warm-status calls it in a
# conditional. The public result is private, bounded and unknown; probe output
# never reaches it.
eval "$kernel_evidence_impl"
# shellcheck disable=SC2329 # Called indirectly through the restored ws_kernel_evidence wrapper.
ws_serving_evidence() {
  mkdir -m 700 -- "$2"
  ws_serving_pod "$1" >"$2/pod.json"
  printf '%s\n' '{"schema":1}' >"$2/runtime.json"
}
# shellcheck disable=SC2329 # Called indirectly through the restored ws_kernel_evidence wrapper.
ws_kernel_probe() { printf '%s\n' 'private compiler probe failure'; return 97; }
FIXTURE_READY=true ws_serving_warm_status fixture - "$work/probe-failed.json"
jq -e '.status == "unknown" and .identity == null and (.reason|test("fresh current"))' "$work/probe-failed.json" >/dev/null
if grep -F -- 'private compiler probe failure' "$work/probe-failed.json" >/dev/null; then
  printf 'warm status exposed private probe output\n' >&2
  exit 1
fi

# A command exit code is not sufficient evidence either. A malformed compiler
# payload from an otherwise successful nested probe must remain unknown instead
# of reaching the Python parser as a false fresh observation.
# shellcheck disable=SC2329 # Called indirectly through the restored ws_kernel_evidence wrapper.
ws_kernel_probe() { printf '%s\n' 'not-json'; }
FIXTURE_READY=true ws_serving_warm_status fixture - "$work/malformed.json"
jq -e '.status == "unknown" and .identity == null' "$work/malformed.json" >/dev/null

# Parseable schema markers are also insufficient. The real serving-runtime
# path calls model_kernels.evidence; its structural refusal must remain a
# private, bounded unknown result instead of leaking its traceback or failing
# this read-only status call.
# shellcheck disable=SC2329 # Called indirectly through the restored ws_kernel_evidence wrapper.
ws_kernel_probe() { printf '%s\n' '{"schema":1}'; }
FIXTURE_READY=true ws_serving_warm_status fixture - "$work/incomplete-schema.json"
jq -e '.status == "unknown" and .identity == null and (.reason|test("fresh current"))' \
  "$work/incomplete-schema.json" >/dev/null
if grep -F -- 'Traceback' "$work/incomplete-schema.json" >/dev/null; then
  printf 'warm status exposed a parser traceback\n' >&2
  exit 1
fi

# If the Pod changes while collecting fresh evidence, retain a stale result and
# do not parse the old runtime/compiler records as current warmth.
ws_serving_evidence() {
  mkdir -m 700 -- "$2"
  : >"$work/pod-now-changed"
  ws_serving_pod "$1" >"$2/pod.json"
  printf '%s\n' '{"schema":1}' >"$2/runtime.json"
}
ws_kernel_probe() { printf '%s\n' '{"schema":1}'; }
rm -f -- "$work/pod-now-changed"
ws_serving_pod() {
  local uid=11111111-2222-3333-4444-555555555555
  [[ -e $work/pod-now-changed ]] && uid=66666666-2222-3333-4444-555555555555
  jq -n --argjson ready "${FIXTURE_READY:-false}" --arg node "${FIXTURE_NODE:-fixture-node}" --arg uid "$uid" '{uid:$uid,
    started_at:"2026-09-11T00:00:00Z",container_id:"containerd://fixture",restart_count:0,
    node:$node,image:("fixture@sha256:"+("a"*64)),image_id:"containerd://fixture",ready:$ready}'
}
FIXTURE_READY=true ws_serving_warm_status fixture - "$work/changed-pod.json"
jq -e '.status == "stale" and .identity == null and (.reason|test("Pod changed"))' "$work/changed-pod.json" >/dev/null

# The bundle wrapper passes cache authority only from loaded configuration,
# never from a bundle argument. It does not run an actual dispatcher here.
bundle_python() { printf '%s\n' "$*" >"$work/bundle-args"; }
ws_measure_python() { printf 'bundle_python\n'; }
unset INFERENCE_CACHE_ROOT INFERENCE_CACHE_FREE_RESERVE_MIB
ws_performance_bundle_config
[[ $INFERENCE_CACHE_FREE_RESERVE_MIB == 20480 ]]
INFERENCE_CACHE_ROOT="$work/cache"
INFERENCE_CACHE_FREE_RESERVE_MIB=20480
mkdir "$INFERENCE_CACHE_ROOT"
ws_performance_bundle export bundle manifest "$work/output" target "$(printf 'a%.0s' {1..64})"
grep -F -- "--cache-root $work/cache" "$work/bundle-args" >/dev/null
grep -F -- '--cache-free-reserve-mib 20480' "$work/bundle-args" >/dev/null
ws_performance_export bundle manifest "$work/output-bridge" --target target --source-revision "$(printf 'a%.0s' {1..64})" --owner bridge-owner
grep -F -- '--owner bridge-owner' "$work/bundle-args" >/dev/null
if ws_performance_export bundle 2>/dev/null; then
  printf 'short performance export invocation was accepted\n' >&2
  exit 1
fi
INFERENCE_CACHE_ROOT=relative
if ws_performance_bundle export bundle manifest "$work/output2" target "$(printf 'a%.0s' {1..64})" 2>/dev/null; then
  printf 'relative cache root was accepted\n' >&2
  exit 1
fi

# Exercise the public CLI's fixed Bridge export argument sequence with no
# configuration file. A deliberately incomplete loading bundle reaches the
# real dispatcher and must leave only its private failure summary/report.
bundle="$work/public-bundle"
mkdir -m 700 "$bundle"
revision=$(printf 'a%.0s' {1..64})
printf '%s\n' '{"schema":1,"kind":"loading","deployment":"missing-deployment.json","evidence":"missing-evidence","threads":[1],"reserve_mib":1024,"per_thread_mib":64}' >"$bundle/spec.json"
chmod 600 "$bundle/spec.json"
WORKSTATION_PYTHON="$HOME_LAB_PYTHON" bash "$root/bin/workstationctl" performance seal "$bundle" loading fixture-target "$revision" >/dev/null
manifest=$(shasum -a 256 "$bundle/manifest.json" | awk '{print $1}')
WORKSTATION_PYTHON="$HOME_LAB_PYTHON" bash "$root/bin/workstationctl" performance export "$bundle" "$manifest" "$work/public-output" --target fixture-target --source-revision "$revision" --owner bridge-owner >/dev/null
jq -e '.status == "failed" and (.artifacts|length == 1 and .[0].name == "failure.json")' "$work/public-output/summary.json" >/dev/null

printf 'Serving runtime wrappers preserve current/stale status and config-only cache authority\n'
