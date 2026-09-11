#!/usr/bin/env bash
# Offline generation only. Credentials and deployment remain owner-run.
ws_telemetry_config_validate() {
  local key value
  for key in TELEMETRY_ENABLED TELEMETRY_GPU_EXPORTER TELEMETRY_SGLANG_TRACE TELEMETRY_KUBELET; do
    value=${!key:-false}
    [[ $value == true || $value == false ]] || ws_die "$key must be true or false"
  done
  [[ ${TELEMETRY_RESERVE_MIB:-6144} =~ ^[1-9][0-9]*$ ]] || ws_die 'TELEMETRY_RESERVE_MIB must be positive'
  [[ ${TELEMETRY_WORKLOADS:-sglang,open-webui} =~ ^[a-z0-9-]+(,[a-z0-9-]+)*$ ]] || ws_die 'TELEMETRY_WORKLOADS must contain comma-separated deployment names'
}

ws_telemetry() (
  set -euo pipefail
  ws_telemetry_config_validate
  local action=$1
  shift
  case $action in
    render)
      "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/telemetry.py" render "$@" \
        --root "$(ws_repo_root)" --enabled "${TELEMETRY_ENABLED:-true}" \
        --gpu-exporter "${TELEMETRY_GPU_EXPORTER:-false}" --trace "${TELEMETRY_SGLANG_TRACE:-false}" \
        --api-address "${TELEMETRY_API_ADDRESS:-}" --host-address "${TELEMETRY_WORKSTATION_ADDRESS:-}" \
        --kubelet "${TELEMETRY_KUBELET:-false}" --node-name "${TELEMETRY_NODE_NAME:-}" \
        --reserve-mib "${TELEMETRY_RESERVE_MIB:-6144}" --workloads "${TELEMETRY_WORKLOADS:-sglang,open-webui}"
      ;;
    plan | verify) "$(ws_measure_python)" "$(ws_repo_root)/lib/workstation/telemetry.py" "$action" "$@" ;;
    *) ws_die 'telemetry supports render, plan and verify; no deployment or installer resume' ;;
  esac
)
