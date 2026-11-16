#!/usr/bin/env bash
# Owner Mac launcher: per-user sandbox home datasets, exports and snapshots on the NAS (the NAS
# repository's ansible/owner-sandbox-homes.yml, sandbox account UID/GID 2002). Run in your Mac
# terminal, after make-sandbox-config.sh on the workstation.
#
#   nas-homes.sh NAS_COMMIT WORKSTATION_COMMIT
#
# 1. Exports NAS_COMMIT of the NAS repository and lib/workstation/sandbox{,_homes}.py of
#    WORKSTATION_COMMIT from your local clones into a fresh run directory under the NAS repo's
#    test-results/. Both commits must be merged: each must be on origin/main of its clone, which
#    the script fetches first.
# 2. Copies ~/ai-stack/private/sandbox-users.json from the workstation over ssh into a 0600
#    temporary file, prints `sandbox_homes.py nas-vars` (names, quotas and the homes' owner
#    UID/GID 2002) and deletes the copy. Account ids never reach the run directory or the terminal.
# 3. Runs the play with --check --diff, lists what would change, asks, then applies the same
#    inputs. 4. Prints the verification commands from the NAS docs.
# Site values (tools/README.md): WORKSTATION_SSH, NAS_REPO, NAS_COMMISSIONING_DIR (holds
# controller.inventory.ini and controller.config.yml), NAS_OWNER_PROFILE (the latest owner profile:
# microserver_owner_install, microserver_k8s_storage_enabled, microserver_k8s_nfs_client),
# NAS_SANDBOX_DATASET (parent dataset of the homes) and NAS_ANSIBLE_VENV (default ~/.venvs/nas-ansible).
# WORKSTATION_REPO (default: the clone holding this script) selects another workstation clone.
# Needs passwordless sudo on the NAS.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
[[ $# == 2 && $(uname -s) == Darwin && -t 0 && -t 1 ]] || {
  printf 'Run in your Mac terminal: nas-homes.sh NAS_COMMIT WORKSTATION_COMMIT (--help for details)\n' >&2
  exit 1
}
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
nas_commit=$1
ws_commit=$2
for commit in "$nas_commit" "$ws_commit"; do
  [[ $commit =~ ^[0-9a-f]{7,40}$ ]] || stop "$commit is not a commit id"
done
tools::load_site
tools::require_site WORKSTATION_SSH NAS_REPO NAS_COMMISSIONING_DIR NAS_OWNER_PROFILE NAS_SANDBOX_DATASET
tools::require_ssh_destination WORKSTATION_SSH
tools::require_dataset NAS_SANDBOX_DATASET
# The site values are assigned by tools::load_site (an allowlisted KEY=VALUE loader).
# shellcheck disable=SC2153
nas_repo=$NAS_REPO
ws_repo=${WORKSTATION_REPO:-$(git -C "$here" rev-parse --show-toplevel)}
workstation=$WORKSTATION_SSH
commissioning=$NAS_COMMISSIONING_DIR
profile=$NAS_OWNER_PROFILE
dataset=$NAS_SANDBOX_DATASET
venv=${NAS_ANSIBLE_VENV:-$HOME/.venvs/nas-ansible}/bin

[[ -x $venv/ansible-playbook ]] || stop "no Ansible at $venv (set NAS_ANSIBLE_VENV)"
for f in "$commissioning/controller.inventory.ini" "$commissioning/controller.config.yml" "$profile"; do
  [[ -f $f ]] || stop "missing $f"
done
# Both commits must be merged: on origin/main of their repository, freshly fetched.
git -C "$nas_repo" fetch -q origin || stop "cannot fetch origin in $nas_repo"
git -C "$ws_repo" fetch -q origin || stop "cannot fetch origin in $ws_repo"
git -C "$nas_repo" cat-file -e "$nas_commit^{commit}" 2>/dev/null || stop "NAS commit $nas_commit is not in $nas_repo"
git -C "$ws_repo" cat-file -e "$ws_commit^{commit}" 2>/dev/null || stop "workstation commit $ws_commit is not in $ws_repo"
git -C "$nas_repo" merge-base --is-ancestor "$nas_commit" origin/main || stop "NAS commit $nas_commit is not merged (not on origin/main)"
git -C "$ws_repo" merge-base --is-ancestor "$ws_commit" origin/main || stop "workstation commit $ws_commit is not merged (not on origin/main)"
nas_commit=$(git -C "$nas_repo" rev-parse --verify "$nas_commit^{commit}")
ws_commit=$(git -C "$ws_repo" rev-parse --verify "$ws_commit^{commit}")
python3 -I - "$profile" <<'PY' || stop 'the owner profile lacks the commissioned NAS and Kubernetes storage settings'
import json, sys
p = json.load(open(sys.argv[1]))
assert p.get("microserver_owner_install") is True and p.get("microserver_k8s_storage_enabled") is True
assert isinstance(p.get("microserver_k8s_nfs_client"), str)
print("owner profile: K3s node", p["microserver_k8s_nfs_client"])
PY

run=$nas_repo/test-results/sandbox-homes-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p -- "$run/source" "$run/ws"
git -C "$nas_repo" archive "$nas_commit" | tar -x -C "$run/source"
printf '%s\n' "$nas_commit" >"$run/source/.source-commit"
git -C "$ws_repo" archive "$ws_commit" lib/workstation/sandbox.py lib/workstation/sandbox_homes.py | tar -x -C "$run/ws"
[[ -f $run/source/ansible/owner-sandbox-homes.yml ]] || stop 'the NAS source has no owner-sandbox-homes.yml'

printf '== NAS variables from the workstation mapping\n'
users=$(mktemp "${TMPDIR:-/tmp}/sandbox-users.XXXXXXXX")
trap 'rm -f -- "$users"' EXIT
ssh -o BatchMode=yes "$workstation" 'cat ~/ai-stack/private/sandbox-users.json' >"$users" ||
  stop 'cannot read ~/ai-stack/private/sandbox-users.json on the workstation (run make-sandbox-config.sh there)'
python3 "$run/ws/lib/workstation/sandbox_homes.py" nas-vars "$users" >"$run/sandbox-homes.json" ||
  stop 'the mapping does not validate'
rm -f -- "$users"
python3 -I - "$run/sandbox-homes.json" "$dataset" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))
dataset = sys.argv[2]
assert v["microserver_sandbox_homes_enabled"] is True and v["microserver_sandbox_homes"]
# The homes' owner is the dedicated sandbox account, never 1000 (the admin login).
assert (v["microserver_sandbox_uid"], v["microserver_sandbox_gid"]) == (2002, 2002), "homes owner is not 2002"
print("  owner: sandbox account, UID/GID 2002 (created on the NAS if missing)")
total = sum(int(h["quota"][:-1]) for h in v["microserver_sandbox_homes"])
for h in v["microserver_sandbox_homes"]:
    print(f'  {dataset}/{h["name"]}  refquota {h["quota"]}  quota {int(h["quota"][:-1]) * 2}G')
print(f"  {len(v['microserver_sandbox_homes'])} homes, {total}G of refquota (parent quota 200G covers files and snapshots)")
PY

play() { # log extra-args...
  local log=$1 result
  shift
  set +e
  PATH="$venv:$PATH" ANSIBLE_CONFIG="$run/source/ansible.cfg" ANSIBLE_HOST_KEY_CHECKING=True \
    "$venv/ansible-playbook" -i "$commissioning/controller.inventory.ini" --limit nas \
    -e "@$commissioning/controller.config.yml" -e "@$profile" -e "@$run/sandbox-homes.json" \
    --diff "$@" "$run/source/ansible/owner-sandbox-homes.yml" >"$log" 2>&1
  result=$?
  set -e
  printf '%s\n' "$result" >"$log.exit"
  grep -A2 'PLAY RECAP' "$log" || true
  return "$result"
}

printf '\n== 1) Dry run (--check --diff): reads pool and dataset metadata, creates nothing\n'
play "$run/check.log" --check || stop "dry run failed; see $run/check.log"
printf '\nWould change:\n'
awk '/^TASK \[/ {t=$0} /^changed:/ {print t}' "$run/check.log" |
  sed -E 's/^TASK \[(debian_microserver : )?(.*)\] \**$/  - \2/' | sort -u
printf '\nSkipped in check mode (run for real on apply):\n'
awk '/^TASK \[/ {t=$0} /^skipping:/ && t ~ /Create only the missing|Apply the reviewed|Set ownership/ {print t}' "$run/check.log" |
  sed -E 's/^TASK \[(debian_microserver : )?(.*)\] \**$/  - \2/' | sort -u
printf '\nExports in the diff:\n'
grep -E '^\+/srv/microserver/sandbox/' "$run/check.log" | sed 's/^/  /' || printf '  (none)\n'
cat <<'EOF'

Expected on the first run, as changed: the sandbox group and user (UID/GID 2002, nologin), the
exports file (one +/srv/microserver/sandbox/<name> line per home, rw,all_squash,anonuid=2002,
anongid=2002 to the K3s node only), /etc/sanoid/sanoid.conf, the sanoid directory and package.
A UID, GID or `sandbox` name already held by another account stops the dry run before any change. The zfs create commands do not run in check mode, so dataset
creation, ownership, quotas and the timer show as skipped until the real run.
Anything else (other datasets, the nas-nfs export, a pool change): answer n and send the list.
EOF
read -r -p 'Apply now? Creates datasets and reloads the export table (exportfs -ra; mounted volumes do not stall). [y/N] ' ok
[[ $ok == y ]] || {
  printf 'Not applied. Logs: %s\n' "$run"
  exit 0
}
printf '\n== 2) Apply\n'
play "$run/apply.log" || stop "apply failed; see $run/apply.log (rerunning is safe: datasets are only created when missing)"
printf 'NAS_SANDBOX_HOMES=applied; logs: %s/{check,apply}.log\n' "$run"
cat <<EOF

Verify on the NAS (the NAS repository's docs/KUBERNETES-STORAGE.md):
  getent passwd sandbox; getent group sandbox   # 2002, /usr/sbin/nologin, no members
  zfs list -o name,used,refquota,quota,mountpoint -r $dataset
  sudo exportfs -v | grep -A1 /srv/microserver/sandbox
  systemctl list-timers sanoid.timer
  zfs list -t snapshot -r $dataset      # after the first timer run (15 minutes)
Then on the workstation: tools/sandbox/sandbox-golive.sh from the shipped tree (its preflight
checks that every home is exported to the node).
EOF
