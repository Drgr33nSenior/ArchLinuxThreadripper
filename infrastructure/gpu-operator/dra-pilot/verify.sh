#!/usr/bin/env bash
# Read-only DRA pilot checks. Prints PASS/FAIL per check; changes nothing.
# Usage: verify.sh [--probe]   (--probe also reads the dra-probe pod's log)
set -uo pipefail
ns=ai-home-lab; fail=0
pass() { printf 'PASS  %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fail=1; }
if kubectl -n kube-amd-gpu get daemonsets -o name | grep -q dra-driver; then pass 'DRA driver DaemonSet exists'; else bad 'DRA driver DaemonSet missing'; fi
if ! kubectl -n kube-amd-gpu get daemonsets -o name | grep -q device-plugin; then pass 'device plugin DaemonSet removed'; else bad 'device plugin DaemonSet still present'; fi
if kubectl get deviceclass gpu.amd.com >/dev/null 2>&1; then pass 'DeviceClass gpu.amd.com exists'; else bad 'DeviceClass gpu.amd.com missing'; fi
slices=$(kubectl get resourceslices -o json)
devices=$(jq '[.items[] | select(.spec.driver == "gpu.amd.com") | .spec.devices[]] | length' <<<"$slices")
if [[ $devices == 2 ]]; then pass "ResourceSlices publish 2 GPUs"; else bad "ResourceSlices publish $devices GPUs (expected 2)"; fi
jq -r '.items[] | select(.spec.driver == "gpu.amd.com") | .spec.devices[] |
  "      \(.name)  pciBusID=\(.attributes["resource.kubernetes.io/pciBusID"].string // "?")  deviceID=\(.attributes.deviceID.string // "?")  driverVersion=\(.attributes.driverVersion.version // "absent")"' <<<"$slices"
for c in gpu-0 gpu-1; do
  if kubectl -n $ns get resourceclaim $c >/dev/null 2>&1; then
    state=$(kubectl -n $ns get resourceclaim $c -o json | jq -r '(.status.allocation.devices.results // []) | map(.device) | join(",")')
    users=$(kubectl -n $ns get resourceclaim $c -o json | jq -r '(.status.reservedFor // []) | map(.name) | join(",")')
    pass "claim $c present (allocated: ${state:-none}; reserved for: ${users:-none})"
  else
    bad "claim $c missing"
  fi
done
alloc=$(kubectl get node gr33nstein -o jsonpath='{.status.allocatable.amd\.com/gpu}')
printf 'INFO  node allocatable amd.com/gpu=%s (0 or absent after the switch; pods still requesting it stay Pending)\n' "${alloc:-absent}"
kubectl -n $ns get pods -l app.kubernetes.io/name=sglang --no-headers | awk '{print "INFO  sglang pod " $1 " " $3}'
if [[ ${1:-} == --probe ]]; then
  phase=$(kubectl -n $ns get pod dra-probe -o jsonpath='{.status.phase}' 2>/dev/null)
  if [[ $phase == Succeeded ]]; then pass 'dra-probe succeeded'; else bad "dra-probe phase: ${phase:-missing}"; fi
  log=$(kubectl -n $ns logs dra-probe 2>/dev/null)
  printf '%s\n' "$log" | sed 's/^/      /'
  if grep -q '^render nodes: 1$' <<<"$log"; then pass 'probe sees exactly one render node'; else bad 'probe does not see exactly one render node'; fi
  if grep -q '^visible GPUs: 1$' <<<"$log" && grep -q 'gfx1201' <<<"$log"; then pass 'probe sees one gfx1201 GPU'; else bad 'probe does not see one gfx1201 GPU'; fi
fi
exit $fail
