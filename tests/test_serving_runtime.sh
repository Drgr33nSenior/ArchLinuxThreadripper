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

# Exercise the public CLI and real Pod projection. Only kubectl is replaced;
# the test does not replace the warm-status wrapper or use an old evidence
# directory. The fixture includes private Kubernetes messages, so the public
# report must use only its fixed, bounded state reason.
mkdir "$work/cli-bin"
cat >"$work/cli-bin/kubectl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *' get pod sglang-fixture -o json '*) ;;
  *' exec '*) [[ -z ${FIXTURE_EXEC_MARKER:-} ]] || : >"$FIXTURE_EXEC_MARKER"; exit 1 ;;
  *) printf 'unexpected Kubernetes fixture call\n' >&2; exit 1 ;;
esac
image="fixture@sha256:$(printf 'a%.0s' {1..64})"
state='{"waiting":{"reason":"ContainerCreating"}}'
last='null'
ready=false
image_id='null'
container_id='null'
restart=0
pod_uid=11111111-2222-3333-4444-555555555555
case "${FIXTURE_POD_STATE:-loading}" in
  loading) ;;
  crash-loop-oom)
    state='{"waiting":{"reason":"CrashLoopBackOff","message":"private OOM diagnostic"}}'
    last='{"terminated":{"reason":"OOMKilled","message":"private termination diagnostic"}}'
    restart=3
    ;;
  terminated)
    state='{"terminated":{"reason":"Error","message":"private termination diagnostic"}}'
    restart=1
    ;;
  image-failure)
    state='{"waiting":{"reason":"ImagePullBackOff","message":"private registry diagnostic"}}'
    ;;
  missing-identity)
    state='{"running":{"startedAt":"2026-09-11T00:00:00Z"}}'
    ready=true
    ;;
  running-historical | restarted-historical)
    state='{"running":{"startedAt":"2026-09-11T00:00:00Z"}}'
    last='{"terminated":{"reason":"OOMKilled","message":"private termination diagnostic"}}'
    ready=true
    image_id='"containerd://fixture-image"'
    container_id='"containerd://fixture-container"'
    [[ ${FIXTURE_POD_STATE:-} != restarted-historical ]] || restart=4
    ;;
  resumed-loading)
    state='{"waiting":{"reason":"ContainerCreating","message":"private resume diagnostic"}}'
    last='{"terminated":{"reason":"OOMKilled","message":"private termination diagnostic"}}'
    restart=4
    ;;
  missing-pod-identity)
    state='{"waiting":{"reason":"CrashLoopBackOff"}}'
    pod_uid=''
    ;;
  *) printf 'unknown Pod fixture state\n' >&2; exit 1 ;;
esac
jq -n --arg image "$image" --arg pod_uid "$pod_uid" --argjson state "$state" --argjson last "$last" --argjson ready "$ready" \
  --argjson image_id "$image_id" --argjson container_id "$container_id" --argjson restart "$restart" \
  '{metadata:{name:"sglang-fixture",uid:$pod_uid},
    spec:{nodeName:"fixture-node",containers:[{name:"sglang",image:$image}]},
    status:{phase:"Running",containerStatuses:[{name:"sglang",imageID:$image_id,containerID:$container_id,
      restartCount:$restart,ready:$ready,state:$state,lastState:$last}]}}'
EOF
chmod 700 "$work/cli-bin/kubectl"
cli_warm_status() {
  local state=$1 output=$2
  FIXTURE_POD_STATE=$state PATH="$work/cli-bin:$PATH" WORKSTATION_PYTHON="$HOME_LAB_PYTHON" \
    SESSION_CONTEXT=fixture SESSION_NODE=fixture-node SESSION_NAMESPACE=fixture \
    SESSION_AI_DEPLOYMENT=sglang SESSION_GAME_DEPLOYMENT=gaming SESSION_ENVIRONMENT=dev \
    FIXTURE_EXEC_MARKER="$output.exec" \
    bash "$root/bin/workstationctl" rocm serving-warm-status sglang-fixture - "$output" \
      >"$output.stdout" 2>"$output.stderr"
}
cli_warm_status loading "$work/cli-loading.json"
jq -e '.status == "model-loading" and .kubernetes_readiness == "model-loading" and .identity == null' \
  "$work/cli-loading.json" >/dev/null
cli_warm_status crash-loop-oom "$work/cli-crash-loop.json"
jq -e '.status == "unavailable" and .kubernetes_readiness == "unavailable" and
  (.reason == "current container is restarting after an out-of-memory termination")' "$work/cli-crash-loop.json" >/dev/null
cli_warm_status terminated "$work/cli-terminated.json"
jq -e '.status == "unavailable" and (.reason == "current container terminated; inspect the reviewed workload logs")' \
  "$work/cli-terminated.json" >/dev/null
cli_warm_status image-failure "$work/cli-image.json"
jq -e '.status == "unavailable" and (.reason == "current container image cannot be started")' "$work/cli-image.json" >/dev/null
cli_warm_status missing-identity "$work/cli-missing.json"
jq -e '.status == "unknown" and .kubernetes_readiness == "unknown" and
  (.reason == "current Pod process identity is incomplete")' "$work/cli-missing.json" >/dev/null
cli_warm_status missing-pod-identity "$work/cli-missing-pod.json"
jq -e '.status == "unknown" and .kubernetes_readiness == "unknown" and
  (.reason == "current Pod identity is incomplete")' "$work/cli-missing-pod.json" >/dev/null
[[ ! -e $work/cli-missing-pod.json.exec ]]
cli_warm_status running-historical "$work/cli-historical.json"
jq -e '.status == "unknown" and (.reason|test("fresh current"))' "$work/cli-historical.json" >/dev/null
[[ -e $work/cli-historical.json.exec ]]
cli_warm_status restarted-historical "$work/cli-restarted.json"
jq -e '.status == "unknown" and (.reason|test("fresh current"))' "$work/cli-restarted.json" >/dev/null
cli_warm_status resumed-loading "$work/cli-resumed.json"
jq -e '.status == "model-loading" and .kubernetes_readiness == "model-loading"' "$work/cli-resumed.json" >/dev/null
if rg -F -e 'private OOM diagnostic' -e 'private termination diagnostic' -e 'private registry diagnostic' \
  -e 'private resume diagnostic' "$work"/*.json >/dev/null; then
  printf 'warm status exposed a private Kubernetes diagnostic\n' >&2
  exit 1
fi

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
