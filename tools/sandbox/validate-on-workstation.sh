#!/usr/bin/env bash
# Dry validation of the sandbox go-live steps on the workstation, with a FAKE sandbox config, in a
# scratch directory. Reads the cluster only (get, diff = server-side dry-run); writes nothing
# live and nothing under ~/ai-stack/private. Mirrors what `workstationctl mode family` renders
# (same arguments as lib/workstation/mode.sh) without the root render directory.
#
#   validate-on-workstation.sh SHA12
#
# SHA12 names the shipped tree $WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12 (default ~/src).
# Site values (tools/README.md): NAS_ADDRESS and WORKSTATION_LAN_ADDRESS. The render reads
# ~/ai-stack/private/site.json.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
sha=${1:?usage: validate-on-workstation.sh SHA12 (--help for details)}
tools::load_site
tools::require_site NAS_ADDRESS WORKSTATION_LAN_ADDRESS
export NAS_ADDRESS WORKSTATION_LAN_ADDRESS
src=$(tools::source_tree "$sha")
check() { python3 -I "$here/golive_check.py" "$@"; }
w=$(mktemp -d /tmp/sandbox-validate.XXXXXXXX)
trap 'rm -rf -- "$w"' EXIT
echo "scratch $w (removed at exit)"

cat >"$w/sandbox-users.json" <<'EOF'
{"users": [
 {"webui_user_id": "00000000-0000-4000-8000-000000000001", "name": "test-admin", "group": "Admin"},
 {"webui_user_id": "00000000-0000-4000-8000-000000000002", "name": "test-parent", "group": "Parents", "quota": "30G"},
 {"webui_user_id": "00000000-0000-4000-8000-000000000003", "name": "test-child", "group": "Family"}]}
EOF
image=$(sed -n 's/^ASSISTANT_SANDBOX_CANDIDATE_IMAGE=//p' "$src/versions.lock")
egress=$(sed -n 's/^SANDBOX_EGRESS_CANDIDATE_IMAGE=//p' "$src/versions.lock")
# The live API endpoint, read-only, as make-sandbox-config.sh picks it.
kubectl -n default get endpointslices -l kubernetes.io/service-name=kubernetes -o json >"$w/slices.json"
api=$(python3 -I "$here/sandbox_config_helper.py" api-endpoint "$w/slices.json")
cat >"$w/sandbox.json" <<EOF
{"image": "$image", "egress_image": "$egress",
 "parents_group_id": "00000000-0000-4000-8000-0000000000aa", "family_group_id": "00000000-0000-4000-8000-0000000000bb",
 "family_enabled": true, "group_resolution": "most-permissive",
 "egress_service_ips": {"family": "10.43.0.201", "parents": "10.43.0.202"},
 "api_server_cidr": "${api%:*}/32", "api_server_port": ${api##*:},
 "homes": "$w/sandbox-users.json", "family_dns": ["1.1.1.3", "1.0.0.3"]}
EOF

echo '== render (mode.sh arguments, shared, assistant-memory, live SGLang image, --sandbox-config)'
live_image=$(kubectl -n ai-home-lab get deploy sglang -o json | jq -er '[.spec.template.spec.containers[] | select(.name == "sglang") | .image][0]')
kubectl -n ai-home-lab get persistentvolumeclaims -o json >"$w/claims.json"
kubectl -n ai-home-lab get deployments -o json >"$w/live.json"
python3 "$src/lib/workstation/service.py" render "$HOME/ai-stack/private/site.json" "$w/render" \
  --mode shared --experience assistant-memory --existing-claims "$w/claims.json" \
  --serving-profile "$src/infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json" \
  --runtime-image "$live_image" --sandbox-config "$w/sandbox.json" >/dev/null
jq -c '{status, contract, arm: .selection.arm}' "$w/render/serving-contract.json"
python3 "$src/lib/workstation/service_mode.py" prepare "$w/render/workloads.yaml" "$w/live.json" "$w/render/apply.json" \
  --managed sglang,steam-headless,comfyui-gpu1,comfyui-gpu0 | jq -c '{not_live, managed}'

echo '== bundle checks'
check bundle "$w/render/sandbox.yaml" "$w/render/evidence.json" "$w/sandbox.json" "$w/sandbox-users.json"
check namespace "$w/render/sandbox.yaml" "$w/namespace.yaml" && grep -c . "$w/namespace.yaml"
python3 "$src/lib/workstation/sandbox_homes.py" render "$w/sandbox-users.json" >"$w/homes.yaml"
echo "homes.yaml: $(grep -c '^kind:' "$w/homes.yaml") objects; user ids in it: $(grep -c '00000000-0000-4000-8000-00000000000' "$w/homes.yaml" || true)"
python3 "$src/lib/workstation/sandbox_homes.py" nas-vars "$w/sandbox-users.json" | jq -c .
python3 "$src/lib/workstation/sandbox_homes.py" nas-vars "$w/sandbox-users.json" |
  jq -e '.microserver_sandbox_uid == 2002 and .microserver_sandbox_gid == 2002' >/dev/null

echo '== capacity (fresh resource plan in scratch, live telemetry render, --node-pods)'
hardware=/var/lib/workstation-evidence/boot-$(tr -d '-' </proc/sys/kernel/random/boot_id)/hardware.json
sudo workstationctl --config /etc/workstation/workstation.conf resources plan "$hardware" "$w/resources" >/dev/null
sudo chown -R "$(id -u):$(id -g)" "$w/resources"
jq -c '{allocatable_logical_cpus, memory: .memory.allocatable_mib, k3s_version}' "$w/resources/resource-plan.json"
# shellcheck disable=SC2012
telemetry=$(ls -1dt "$HOME"/ai-stack/deploy/telemetry-*/ | head -1)
telemetry=${telemetry%/}
workstationctl telemetry verify "$telemetry" >/dev/null && echo "telemetry $telemetry verifies"
kubectl get pods -A -o json >"$w/node-pods.json"
workstationctl service plan "$w/resources/resource-plan.json" "$w/render" "$telemetry" "$w/plan" --node-pods "$w/node-pods.json" || true
check capacity "$w/plan/capacity.json" || echo 'CAPACITY: does not fit (see above)'

echo '== diffs against live (server-side dry-run; nothing applied)'
check pick "$w/render/apply.json" open-webui "$w/open-webui.json"
rc=0
GOLIVE_DIFF_OUT=$w/webui-diff.json KUBECTL_EXTERNAL_DIFF=$here/kdiff.sh kubectl diff -f "$w/open-webui.json" >/dev/null || rc=$?
echo "kubectl diff exit $rc"
[[ -f $w/webui-diff.json ]] || : >"$w/webui-diff.json"
check allow "$w/webui-diff.json" webui TERMINAL_SERVER_CONNECTIONS && echo 'WEBUI_ALLOWLIST=pass' || echo 'WEBUI_ALLOWLIST=refused'
echo 'other Deployment drift (render from this source vs live):'
check drift "$w/render/workloads.yaml" "$w/live.json"
echo 'sandbox bundle diff (namespace does not exist yet, so only cluster-scoped and existing-namespace objects):'
rc=0
python3 - "$w/render/sandbox.yaml" "$w/sandbox-outside.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["metadata"].get("namespace") != "assistant-sandbox"]
open(sys.argv[2], "w").write(yaml.safe_dump_all(docs))
print(" ", [f'{d["kind"]} {d["metadata"].get("namespace", "")}/{d["metadata"]["name"]}' for d in docs])
PY
GOLIVE_DIFF_OUT=$w/sandbox-diff.json KUBECTL_EXTERNAL_DIFF=$here/kdiff.sh kubectl diff -f "$w/sandbox-outside.yaml" >/dev/null || rc=$?
echo "kubectl diff exit $rc"
check allow "$w/sandbox-diff.json" sandbox && echo 'SANDBOX_ALLOWLIST=pass'
rc=0
GOLIVE_DIFF_OUT=$w/homes-pv-diff.json KUBECTL_EXTERNAL_DIFF=$here/kdiff.sh kubectl diff \
  -f <(python3 -c 'import sys, yaml; print(yaml.safe_dump_all([d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"] == "PersistentVolume"]))' "$w/homes.yaml") >/dev/null || rc=$?
echo "kubectl diff (home volumes) exit $rc"
check allow "$w/homes-pv-diff.json" homes && echo 'HOMES_ALLOWLIST=pass'
echo 'negative check: a changed env outside the allowlist must be refused'
jq '(.spec.template.spec.containers[0].env[] | select(.name == "WEBUI_NAME") | .value) |= . + "x"' "$w/open-webui.json" >"$w/open-webui-bad.json"
rc=0
GOLIVE_DIFF_OUT=$w/webui-bad.json KUBECTL_EXTERNAL_DIFF=$here/kdiff.sh kubectl diff -f "$w/open-webui-bad.json" >/dev/null || rc=$?
if check allow "$w/webui-bad.json" webui TERMINAL_SERVER_CONNECTIONS; then echo 'NEGATIVE=FAILED (accepted)'; else echo 'NEGATIVE=pass (refused)'; fi

echo '== probe specs'
for group in family parents; do check probe "$group" "$w/sandbox.json" 10.42.0.99 | jq -c '{open, denied, allowed, closed: (.closed | keys)}'; done

echo '== NFS export probe for the fake homes (must fail: not exported)'
sudo python3 -I "$here/nfs4_probe.py" "$NAS_ADDRESS" /srv/microserver/sandbox/test-child /srv/microserver/k8s || echo "nfs4_probe exit $? (expected 1)"
echo 'VALIDATION=done'
