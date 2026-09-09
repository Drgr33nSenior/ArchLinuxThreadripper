#!/usr/bin/env bash
# Synthetic boundaries only; production serving shell orchestration is unmodified.
set -euo pipefail
source "$REPO_ROOT/lib/common.sh"
source "$REPO_ROOT/lib/workstation/runtime.sh"
SESSION_NODE=fixture
SESSION_CONTEXT=fixture
SESSION_NAMESPACE=fixture
ws_require_arch() { printf '%s\n' "$BASHPID" >"$CASE_ROOT/shell.pid"; }
ws_require_user() { :; }
ws_serving_pod() { cat "$CASE_ROOT/evidence/pod.json"; }
ws_rocm_boot_id_read() { printf 'fixture-boot\n'; }
ws_session_kubectl() {
  if [[ $1 == get ]]; then
    printf '{"status":{"nodeInfo":{"bootID":"fixture-boot"},"allocatable":{"amd.com/gpu":"2"}}}\n'
  else
    cat >/dev/null
    printf '{}\n'
  fi
}
ws_kernel_probe() { printf '{}\n'; }
ws_hardware_collect() {
  mkdir "$1"
  printf '{}\n' >"$1/hardware.json"
}
ws_detected_gpu_target() { printf 'gfx1201\n'; }
ws_serving_evidence() {
  mkdir "$2"
  cp "$CASE_ROOT/evidence/"*.json "$2/"
}
ws_measure_python() { printf '%s/helper-python\n' "$CASE_ROOT/bin"; }
mktemp() {
  local path
  path=$(command mktemp "$@")
  printf '%s\n' "$path" >"$CASE_ROOT/temp-path"
  if [[ ${FOREIGN_TEMP:-0} == 1 ]]; then printf 'retain\n' >"$path/foreign"; fi
  printf '%s\n' "$path"
}
ws_serving_benchmark "$CASE_ROOT/workload.json" "$CASE_ROOT/evidence" "$CASE_ROOT/output" "$CASE_MODE"
