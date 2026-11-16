#!/usr/bin/env bash
# Go live with the assistant coding sandbox in family mode (docs/ASSISTANT.md, "Owner rollout
# after merge and qualification"; docs/HOME-LAB.md, "Sandbox homes"). Run on the workstation as
# your login user, in a terminal, after make-sandbox-config.sh and nas-homes.sh.
#
#   sandbox-golive.sh SHA12                full go-live from the shipped tree of merged main SHA12
#   sandbox-golive.sh --plan-only SHA12    steps 1-4 only: checks, render, capacity, diffs; no change
#   sandbox-golive.sh --probe-only SHA12   only the network probe of running workspaces (step 9)
#
# SHA12 names the shipped tree $WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12 (default ~/src).
# Site values (tools/README.md): NAS_ADDRESS (the NAS address the node mounts homes from) and
# WORKSTATION_LAN_ADDRESS (the probe checks the workspaces cannot reach it).
#
# Steps, each fail-closed:
#   1. preflight: private config validates with the installed runtime (which must equal the
#      source tree), MODE_SANDBOX_CONFIG is set, both images verify (signature, SBOM, vuln
#      attestation), every home is exported to this node (NFSv4 lookup), family session ready,
#      nothing in flight (pictures, video, research, Jobs, rocm-dev, session lock)
#   2. plan-only `workstationctl mode family` render; static checks of sandbox.yaml
#   3. fresh resource plan + `service plan --node-pods`: the capacity figures; must fit
#   4. diff against live: open-webui may change only TERMINAL_SERVER_CONNECTIONS (plus env
#      names listed in EXPECT_ENV); other drift is shown, not applied
#   5. one confirmation, with the rollback commands (also saved to the log directory)
#   6. sandbox bundle: Namespace, then server dry-run + allowlist diff, then apply
#   7. homes: allowlist diff (new PV/PVCs only), apply, wait for Bound
#   8. controller and both proxies to one replica; wait for rollout
#   9. Open WebUI: re-diff, apply the one Deployment, wait; then the network probe in a
#      Family and a Parents workspace that you open in WebUI
# Logs and copies of everything applied: ~/ai-stack/sandbox/golive-<UTC stamp>-<pid>/.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
export KUBECONFIG=${KUBECONFIG:-$HOME/.kube/config}

probe_only=false plan_only=false
case ${1:-} in
  --probe-only) probe_only=true && shift ;;
  --plan-only) plan_only=true && shift ;;
esac
sha=${1:?usage: sandbox-golive.sh [--plan-only | --probe-only] SHA12 (--help for details)}
tools::load_site
tools::require_site NAS_ADDRESS WORKSTATION_LAN_ADDRESS
export NAS_ADDRESS WORKSTATION_LAN_ADDRESS
src=$(tools::source_tree "$sha")
conf=/etc/workstation/workstation.conf
runtime=/usr/lib/bridge/workstation-runtime
private=$HOME/ai-stack/private
config=$private/sandbox.json
users=$private/sandbox-users.json
ns=ai-home-lab
sns=assistant-sandbox
nas=$NAS_ADDRESS
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log=$HOME/ai-stack/sandbox/golive-$stamp-$$
read -r -a expect_env <<<"${EXPECT_ENV:-}"

say() { printf '%s %s\n' "$(date -u +%T)" "$*"; }
stop() {
  printf 'STOP: %s\n' "$*" >&2
  [[ ! -f ${log:-/nonexistent}/ROLLBACK.txt ]] || printf 'Rollback commands: %s/ROLLBACK.txt\n' "$log" >&2
  exit 1
}
check() { python3 -I "$here/golive_check.py" "$@"; }
# diff_to <file> <out.json>: server-side dry-run diff; exit 0/1 is fine, anything else stops.
diff_to() {
  local rc=0
  GOLIVE_DIFF_OUT=$2 KUBECTL_EXTERNAL_DIFF=$here/kdiff.sh kubectl diff -f "$1" >/dev/null || rc=$?
  ((rc <= 1)) || stop "kubectl diff failed for $1"
  [[ -f $2 ]] || : >"$2"
}

[[ $EUID != 0 ]] || stop 'run as your login user, not root (sudo is used where needed)'
[[ -t 0 ]] || stop 'run in a terminal: the script asks before every live change'
for f in golive_check.py kdiff.sh nfs4_probe.py; do [[ -f $here/$f ]] || stop "missing $here/$f"; done
[[ -f $src/versions.lock && -f $src/tests/workstation/probe_sandbox_network.py ]] || stop "no source tree at $src"
mkdir -p -- "$(dirname -- "$log")"
mkdir -m 700 -- "$log"
exec > >(tee -a "$log/console.log") 2>&1

# probe_workspaces both|any: both = one family and one parents workspace must run (go-live);
# any = probe whatever family or parents workspaces run (--probe-only, e.g. the admin account).
probe_workspaces() {
  local need=$1
  say '== network probe in live workspaces (docs/ASSISTANT.md, "Network probe")'
  cat <<'EOF'
Open two workspaces now, each from its own browser session:
  - a Family-only account (the child's account): Family policy
  - the owner's daily account (Parents and Family; most-permissive gives Parents)
In a native-tool chat select "Private coding workspace" and ask it to run `echo ready`.
The quota allows two workspaces at once; test the admin account afterwards with --probe-only.
EOF
  local answer pods failed=0 tested=0 group pod ip other spec rc
  while true; do
    read -r -p 'Press Enter when the workspaces run (or type skip): ' answer
    [[ $answer != skip ]] || break
    kubectl -n "$sns" get pods -l app.kubernetes.io/name=assistant-workspace -o json >"$log/workspaces.json"
    pods=$(jq -r '.items[] | select(.status.phase == "Running") | [.metadata.name, .metadata.labels["workstation.ai/sandbox-egress"] // "none", .status.podIP] | @tsv' "$log/workspaces.json")
    printf '%s\n' "${pods:-  (no running workspace)}"
    jq -e --arg need "$need" '[.items[] | select(.status.phase == "Running") | .metadata.labels["workstation.ai/sandbox-egress"]]
      | if $need == "both" then (index("family") != null) and (index("parents") != null)
        else (index("family") != null) or (index("parents") != null) end' "$log/workspaces.json" >/dev/null && break
    say "need running workspaces ($need: family and parents)"
  done
  [[ $answer != skip ]] || {
    say 'PROBES=skipped'
    return 1
  }
  while IFS=$'\t' read -r pod group ip; do
    other=$(jq -r --arg pod "$pod" '[.items[] | select(.status.phase == "Running" and .metadata.name != $pod) | .status.podIP][0] // empty' "$log/workspaces.json")
    spec=$(check probe "$group" "$config" "$other")
    say "-- $group workspace $pod ($ip); other workspace ${other:-none}"
    rc=0
    kubectl -n "$sns" exec -i "$pod" -- python -I - "$spec" <"$src/tests/workstation/probe_sandbox_network.py" || rc=$?
    say "home: $(kubectl -n "$sns" exec "$pod" -- df -P /home/user | tail -1 | awk '{print $1, $2 " KiB"}')"
    # The workspace runs as the sandbox account (2002), never the owner's 1000; a mapped NAS
    # home is owned by it (the NAS squashes every write to 2002:2002).
    # shellcheck disable=SC2016 # expanded inside the workspace
    ids=$(kubectl -n "$sns" exec "$pod" -- sh -c 'echo "$(id -u):$(id -g) $(stat -c %u:%g /home/user)"') || ids='unknown'
    say "ids and home owner: $ids"
    if [[ ${ids%% *} != 2002:2002 ]]; then
      say "workspace runs as ${ids%% *}, expected 2002:2002"
      rc=1
    elif jq -e --arg pod "$pod" '.items[] | select(.metadata.name == $pod) | .spec.volumes[] | select(.name == "home") | has("persistentVolumeClaim")' "$log/workspaces.json" >/dev/null &&
      [[ ${ids#* } != 2002:2002 ]]; then
      say "the mapped home is owned by ${ids#* }, expected 2002:2002"
      rc=1
    fi
    tested=$((tested + 1))
    ((rc == 0)) || failed=$((failed + 1))
  done < <(jq -r '.items[] | select(.status.phase == "Running") | select(.metadata.labels["workstation.ai/sandbox-egress"] == "family" or .metadata.labels["workstation.ai/sandbox-egress"] == "parents") | [.metadata.name, .metadata.labels["workstation.ai/sandbox-egress"], .status.podIP] | @tsv' "$log/workspaces.json")
  say "PROBES=$((tested - failed))/$tested as expected"
  local min=1
  [[ $need != both ]] || min=2
  ((failed == 0 && tested >= min))
}

if [[ $probe_only == true ]]; then
  [[ -f $config ]] || stop "no $config"
  probe_workspaces any || stop 'a probe failed or was skipped'
  exit 0
fi

say "== 1. preflight (log $log)"
for f in "$config" "$users"; do
  [[ -f $f && ! -L $f ]] || stop "missing $f (run make-sandbox-config.sh)"
  [[ $(stat -c '%a %U' "$f") == "600 $(id -un)" ]] || stop "$f must be mode 0600 and yours"
done
# The mode render runs the installed runtime; it must be the reviewed source.
for f in lib/workstation/mode.sh lib/workstation/sandbox.py lib/workstation/sandbox_homes.py lib/workstation/service.py \
  lib/workstation/service_mode.py lib/workstation/session.sh infrastructure/service/sandbox-egress.json; do
  cmp -s "$runtime/$f" "$src/$f" || stop "installed runtime $f differs from $src; install the runtime package from main first"
done
homes=$(
  python3 -I - "$runtime/lib/workstation" "$config" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import sandbox
config = sandbox.read_config(sys.argv[2])
assert config["family_enabled"] is True and config["group_resolution"] == "most-permissive", "unexpected policy"
print(" ".join(user["name"] for user in config["users"]))
PY
) || stop 'sandbox.json does not validate with the installed runtime'
[[ -n $homes ]] || stop 'the homes mapping is empty'
say "config valid; homes: $homes"
image=$(jq -r .image "$config")
egress_image=$(jq -r .egress_image "$config")
grep -qx "ASSISTANT_SANDBOX_CANDIDATE_IMAGE=$image" "$src/versions.lock" || stop 'sandbox.json image differs from versions.lock'
grep -qx "SANDBOX_EGRESS_CANDIDATE_IMAGE=$egress_image" "$src/versions.lock" || stop 'sandbox.json egress image differs from versions.lock'
sudo grep -qx "MODE_SANDBOX_CONFIG=$config" "$conf" ||
  stop "add the line MODE_SANDBOX_CONFIG=$config to $conf (sudoedit $conf), then rerun"
for ref in "$image" "$egress_image"; do
  bash "$src/infrastructure/ai-stack/signing/verify-image.sh" "$ref" | tail -1 || stop "image $ref does not verify"
done
exports=()
for name in $homes; do exports+=("/srv/microserver/sandbox/$name"); done
sudo python3 -I "$here/nfs4_probe.py" "$nas" "${exports[@]}" || stop 'a home is not exported to this node; run nas-homes.sh on the Mac first'
[[ $(kubectl get node -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}') == True ]] || stop 'node not Ready'
state=$(sudo jq -c '{mode, phase}' /var/lib/workstation-session/state.json)
[[ $state == '{"mode":"shared","phase":"ready"}' ]] || stop "session is $state; switch to family mode first"
sudo flock -n /var/lib/workstation-session/lock true || stop 'a session switch holds the lock'
tools=$(kubectl -n "$ns" get svc service-tools -o jsonpath='{.spec.clusterIP}')
health=$(curl -s -m 5 "http://$tools:8000/health") || stop 'service-tools does not answer'
jq -e '.image_active == 0 and .video_active == false and .image_busy == false and .deep_research.active == 0' <<<"$health" >/dev/null ||
  stop 'a picture, video or research run is in flight'
gateway=$(curl -s -m 5 "http://$(kubectl -n "$ns" get svc litellm -o jsonpath='{.spec.clusterIP}'):4000/_service/status")
jq -e '.phase == "ready"' <<<"$gateway" >/dev/null || stop "gateway is not ready: $gateway"
active=$(kubectl get jobs -A -o json | jq -r '[.items[] | select((.status.active // 0) > 0) | .metadata.namespace + "/" + .metadata.name] | join(" ")')
[[ -z $active ]] || stop "Jobs are running: $active"
[[ $(kubectl -n "$ns" get deploy rocm-dev -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0) == 0 ]] || stop 'rocm-dev is up'
live_terminal=$(kubectl -n "$ns" get deploy open-webui -o json | jq -r '.spec.template.spec.containers[0].env[] | select(.name == "TERMINAL_SERVER_CONNECTIONS") | .value')
[[ $live_terminal == '[]' ]] || say "NOTE: open-webui already has a terminal connection; this is a rerun"
if kubectl get namespace "$sns" >/dev/null 2>&1; then say "NOTE: namespace $sns exists; this is a rerun and the diff decides"; fi
say "gateway $gateway"

say '== 2. plan-only family render with MODE_SANDBOX_CONFIG'
sudo workstationctl --config "$conf" mode family 2>&1 | tee "$log/mode-family-plan.log" || stop 'the plan-only family render failed'
render=$(sed -n 's/^render: //p' "$log/mode-family-plan.log" | tail -1)
if [[ -z $render ]] || ! sudo test -f "$render/sandbox.yaml" -a -f "$render/apply.json"; then stop 'no family render'; fi
sudo cp -r -- "$render" "$log/render"
sudo chown -R "$(id -u):$(id -g)" "$log/render"
check bundle "$log/render/sandbox.yaml" "$log/render/evidence.json" "$config" "$users" || stop 'the sandbox bundle is not as expected'
python3 "$runtime/lib/workstation/sandbox_homes.py" render "$users" >"$log/homes.yaml"
check namespace "$log/render/sandbox.yaml" "$log/namespace.yaml"

say '== 3. capacity: fresh resource plan and service plan --node-pods'
hardware=/var/lib/workstation-evidence/boot-$(tr -d '-' </proc/sys/kernel/random/boot_id)/hardware.json
sudo workstationctl --config "$conf" resources plan "$hardware" "$log/resources" >/dev/null || stop 'resource plan failed'
sudo chown -R "$(id -u):$(id -g)" "$log/resources"
telemetry=${TELEMETRY_RENDER:-}
if [[ -z $telemetry ]]; then
  # shellcheck disable=SC2012 # names are telemetry-<sha12>; newest by modification time
  telemetry=$(ls -1dt "$HOME"/ai-stack/deploy/telemetry-*/ | head -1)
fi
telemetry=${telemetry%/}
workstationctl telemetry verify "$telemetry" >/dev/null || stop "telemetry render $telemetry does not verify"
say "telemetry render $telemetry (set TELEMETRY_RENDER to choose another)"
kubectl get pods -A -o json >"$log/node-pods.json"
workstationctl service plan "$log/resources/resource-plan.json" "$log/render" "$telemetry" "$log/plan" --node-pods "$log/node-pods.json" || true
[[ -f $log/plan/capacity.json ]] || stop 'service plan produced no capacity.json'
check capacity "$log/plan/capacity.json" || stop 'the family composition with the sandbox pool does not fit'

say '== 4. diff against live'
check pick "$log/render/apply.json" open-webui "$log/open-webui.json"
diff_to "$log/open-webui.json" "$log/webui-diff.json"
check allow "$log/webui-diff.json" webui TERMINAL_SERVER_CONNECTIONS "${expect_env[@]}" ||
  stop 'open-webui would change beyond the terminal connection (EXPECT_ENV="NAME ..." allows reviewed env changes)'
kubectl -n "$ns" get deploy open-webui -o json >"$log/live-open-webui.json"
say "other Deployments the next 'mode family --execute' would change (not applied here):"
kubectl -n "$ns" get deploy -o json >"$log/live-deployments.json"
check drift "$log/render/workloads.yaml" "$log/live-deployments.json"
for name in $homes; do
  if kubectl get pv "sandbox-home-$name" >/dev/null 2>&1; then say "NOTE: PersistentVolume sandbox-home-$name exists; the homes diff decides"; fi
done

cat >"$log/ROLLBACK.txt" <<EOF
Sandbox rollback (run on the workstation as your login user). Homes on the NAS are never touched.
A. Stop offering the workspace in Open WebUI (the value before go-live), then wait:
   kubectl -n $ns set env deploy/open-webui -c open-webui 'TERMINAL_SERVER_CONNECTIONS=[]'
   kubectl -n $ns rollout status deploy/open-webui --timeout=10m
B. Remove the line MODE_SANDBOX_CONFIG=$config from $conf (sudoedit $conf),
   so later mode switches render without the sandbox.
C. Stop the pool. Workspaces end with their pods (a mapped home keeps its files):
   kubectl -n $sns delete pods -l app.kubernetes.io/name=assistant-workspace
   kubectl -n $sns scale deploy/assistant-terminals deploy/sandbox-egress-family deploy/sandbox-egress-parents --replicas=0
D. Only to remove everything (volumes are Retain; the NAS datasets stay):
   kubectl delete -f $log/homes.yaml
   kubectl delete -f $log/render/sandbox.yaml
EOF

if [[ $plan_only == true ]]; then
  say "PLAN_ONLY=done; nothing changed. Review $log, then rerun without --plan-only."
  exit 0
fi
say '== 5. confirm'
cat <<EOF
Will apply, in order (each step stops on failure):
  6. namespace $sns, then the sandbox bundle ($(grep -c '^kind:' "$log/render/sandbox.yaml") objects, Deployments at 0)
  7. homes: $(grep -c '^kind: PersistentVolume$' "$log/homes.yaml") volumes and claims ($homes)
  8. assistant-terminals, sandbox-egress-family, sandbox-egress-parents -> 1 replica (pulls both images)
  9. open-webui with the terminal connection (Recreate: WebUI restarts, about a minute; chat
     and pictures keep running), then the network probe in two workspaces you open.
Rollback: $log/ROLLBACK.txt
EOF
sed 's/^/  /' "$log/ROLLBACK.txt"
read -r -p 'Go live? [y/N] ' ok
[[ $ok == y ]] || stop 'not applied; nothing changed'

say '== 6. sandbox bundle'
kubectl apply -f "$log/namespace.yaml"
kubectl apply --dry-run=server -f "$log/render/sandbox.yaml" >/dev/null || stop 'server dry-run of the sandbox bundle failed (only the namespace exists: rollback D)'
diff_to "$log/render/sandbox.yaml" "$log/sandbox-diff.json"
check allow "$log/sandbox-diff.json" sandbox || stop 'the sandbox bundle would change objects outside its namespace'
kubectl apply -f "$log/render/sandbox.yaml" | sed 's/^/  /'
kubectl -n "$sns" get deploy,svc,resourcequota,networkpolicy --no-headers | sed 's/^/  /'

say '== 7. homes'
diff_to "$log/homes.yaml" "$log/homes-diff.json"
check allow "$log/homes-diff.json" homes || stop 'the homes render would change existing volumes'
kubectl apply --dry-run=server -f "$log/homes.yaml" >/dev/null || stop 'server dry-run of the homes failed'
kubectl apply -f "$log/homes.yaml" | sed 's/^/  /'
for name in $homes; do
  kubectl -n "$sns" wait --for=jsonpath='{.status.phase}'=Bound "pvc/sandbox-home-$name" --timeout=120s >/dev/null ||
    stop "claim sandbox-home-$name is not Bound"
done
kubectl -n "$sns" get pvc --no-headers | sed 's/^/  /'

say '== 8. controller and proxies to one replica'
pool=(sandbox-egress-family sandbox-egress-parents assistant-terminals)
for d in "${pool[@]}"; do kubectl -n "$sns" scale "deploy/$d" --replicas=1; done
for d in "${pool[@]}"; do
  kubectl -n "$sns" rollout status "deploy/$d" --timeout=900s || stop "$d did not become ready (rollback C)"
done
kubectl -n "$sns" get pods -o wide --no-headers | sed 's/^/  /'
restarts=$(kubectl -n "$sns" get pods -o json | jq '[.items[].status.containerStatuses[]?.restartCount] | add // 0')
((restarts == 0)) || stop "pool pods restarted $restarts times (rollback C)"

say '== 9. Open WebUI'
diff_to "$log/open-webui.json" "$log/webui-diff-apply.json"
check allow "$log/webui-diff-apply.json" webui TERMINAL_SERVER_CONNECTIONS "${expect_env[@]}" ||
  stop 'live open-webui changed since step 4; nothing applied to it (rollback C leaves the pool idle)'
kubectl apply -f "$log/open-webui.json" | sed 's/^/  /'
kubectl -n "$ns" rollout status deploy/open-webui --timeout=600s || stop 'open-webui did not roll out (rollback A)'
kubectl -n "$ns" get deploy open-webui -o json >"$log/open-webui-after.json"
check webui "$log/open-webui-after.json" "$config" || stop 'open-webui does not carry the rendered connection (rollback A)'
say "gateway $(curl -s -m 5 "http://$(kubectl -n "$ns" get svc litellm -o jsonpath='{.spec.clusterIP}'):4000/_service/status")"

probe_workspaces both || stop 'a probe failed or was skipped: review the output above; rollback A stops new workspaces'
cat <<EOF
SANDBOX_GOLIVE=done ($log)
Still to check by hand (docs/ASSISTANT.md step 6): a Parents chat builds and serves a small HTML
game with Playwright; a Family account is refused an adult site; a file in a mapped home
survives a teardown (15 idle minutes, or delete the pod); the admin account
(sandbox-golive.sh --probe-only with its workspace and one other running).
EOF
