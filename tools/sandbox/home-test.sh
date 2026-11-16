#!/usr/bin/env bash
# After sandbox go-live: prove one NAS-backed home works end to end. Runs a short-lived pod as the sandbox UID
# (2002) that mounts sandbox-home-<name>: write a file and a git commit, delete and recreate the pod, check the
# file, commit and 2002:2002 ownership survived, show the quota the home sees, and list the home's ZFS snapshots
# on the NAS (read-only). Leaves the test files in the home (.home-test/) and deletes only its own pod.
# Run on the workstation.
#
#   home-test.sh HOME_NAME      the name from sandbox-users.json; no workspace may be using that home
#
# The image comes from ~/ai-stack/private/sandbox.json. Site values NAS_SSH (an SSH destination with
# passwordless `sudo -n zfs` on the NAS) and NAS_SANDBOX_DATASET (the homes' parent dataset) enable the
# snapshot listing; without them it is skipped.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
name=${1:?usage: home-test.sh HOME_NAME (--help for details)}
[[ $name =~ ^[a-z][a-z0-9-]{0,31}$ ]] || {
  echo "STOP: $name is not a home name (lowercase DNS label)" >&2
  exit 1
}
tools::load_site
[[ -z ${NAS_SSH:-} ]] || tools::require_ssh_destination NAS_SSH
[[ -z ${NAS_SANDBOX_DATASET:-} ]] || tools::require_dataset NAS_SANDBOX_DATASET
ns=assistant-sandbox
claim=sandbox-home-$name
pod=home-test-$name
uid=2002
log() { printf '%s %s\n' "$(date +%T)" "$*"; }
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
cleanup() { kubectl -n $ns delete pod "$pod" --ignore-not-found --wait=true >/dev/null 2>&1 || true; }
trap cleanup EXIT

[[ $(kubectl -n $ns get pvc "$claim" -o jsonpath='{.status.phase}' 2>/dev/null) == Bound ]] || stop "$claim is not Bound"
users=$(kubectl -n $ns get pods -o json | python3 -c "
import json, sys
pods = json.load(sys.stdin)['items']
print(' '.join(p['metadata']['name'] for p in pods
               if any((v.get('persistentVolumeClaim') or {}).get('claimName') == '$claim' for v in p['spec'].get('volumes', []))))")
[[ -z $users ]] || stop "$claim is in use by: $users (close that workspace first)"
image=$(python3 -c 'import json, os; print(json.load(open(os.path.expanduser("~/ai-stack/private/sandbox.json")))["image"])')

run_pod() {
  kubectl -n $ns apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $pod, labels: {app.kubernetes.io/name: home-test}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: $uid
    runAsGroup: $uid
    fsGroup: $uid
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: test
      image: $image
      command: [sleep, "600"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources:
        requests: {cpu: 100m, memory: 128Mi, ephemeral-storage: 64Mi}
        limits: {cpu: 500m, memory: 256Mi, ephemeral-storage: 64Mi}
      volumeMounts:
        - {name: home, mountPath: /home/user}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: home
      persistentVolumeClaim: {claimName: $claim}
    - name: tmp
      emptyDir: {sizeLimit: 64Mi}
EOF
  kubectl -n $ns wait --for=condition=Ready "pod/$pod" --timeout=180s >/dev/null || {
    kubectl -n $ns describe pod "$pod" | tail -15
    stop "test pod did not start"
  }
}
in_pod() { kubectl -n $ns exec "$pod" -- sh -c "$1"; }

log "== round 1: write"
run_pod
in_pod 'id; mkdir -p ~/.home-test && cd ~/.home-test && date -u > stamp.txt && git init -q 2>/dev/null; git -c user.name=t -c user.email=t@example.invalid add stamp.txt && git -c user.name=t -c user.email=t@example.invalid commit -qm "home test" && git log --oneline -1 && stat -c "owner %u:%g mode %a %n" ~ ~/.home-test/stamp.txt'
stamp=$(in_pod 'cat ~/.home-test/stamp.txt')
log "quota seen by the home: $(in_pod 'df -h ~ | tail -1')"

log "== round 2: recreate the pod and check"
cleanup
run_pod
[[ $(in_pod 'cat ~/.home-test/stamp.txt') == "$stamp" ]] || stop "file did not survive the pod"
in_pod 'cd ~/.home-test && git log --oneline -1 && git status --short | wc -l | sed "s/^/uncommitted: /"; stat -c "owner %u:%g %n" ~/.home-test/stamp.txt'
owner=$(in_pod 'stat -c %u:%g ~/.home-test/stamp.txt')
[[ $owner == "$uid:$uid" ]] || stop "file owner is $owner, expected $uid:$uid"
log "persistence OK; owner $owner"

log "== NAS snapshots of this home (read-only)"
if [[ -n ${NAS_SSH:-} && -n ${NAS_SANDBOX_DATASET:-} ]]; then
  home=$NAS_SANDBOX_DATASET/$name
  ssh -o BatchMode=yes "$NAS_SSH" "sudo -n zfs list -H -t snapshot -o name,creation -s creation -r $home 2>/dev/null | tail -3; sudo -n zfs get -H -o property,value refquota,quota $home" ||
    log "could not read NAS snapshots (run on the NAS: zfs list -t snapshot -r $home)"
else
  log 'NAS snapshots skipped (set NAS_SSH and NAS_SANDBOX_DATASET)'
fi
log HOME_TEST=done
