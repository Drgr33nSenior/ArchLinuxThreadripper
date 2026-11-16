#!/usr/bin/env bash
# Write the private sandbox config for the coding workspaces (docs/ASSISTANT.md, "Private coding
# sandbox"; docs/HOME-LAB.md, "Sandbox homes"). Run on the workstation as your login user.
#
#   make-sandbox-config.sh [--force] [--review FILE] SOURCE_DIR
#
# Reads Open WebUI's database read-only inside the open-webui pod (accounts, roles, groups),
# lets you review the account -> home mapping, picks two free service addresses and the API
# endpoint read-only, validates everything with the repository's own loaders, then writes
#   ~/ai-stack/private/sandbox-users.json   (one home per included account)
#   ~/ai-stack/private/sandbox.json         (every key sandbox.py requires)
# with mode 0600. Existing files are kept unless --force (then they are backed up first).
# Nothing in the cluster changes. The terminal shows names, roles and groups only: never an
# email, a token or an account id.
#
# --review FILE  use an already edited review file instead of the interactive review.
# SOURCE_DIR     the reviewed, shipped source tree (e.g. ~/src/archlinuxthreadripper-SHA12). Its
#                versions.lock names both images (ASSISTANT_SANDBOX_CANDIDATE_IMAGE,
#                SANDBOX_EGRESS_CANDIDATE_IMAGE); SANDBOX_IMAGE and SANDBOX_EGRESS_IMAGE, when set,
#                must equal them (a pin from your review).
# Test seams: KUBECTL (kubectl command), SANDBOX_PRIVATE_DIR, SANDBOX_WEBUI_DB (a local SQLite
# file read instead of the pod's database).
set -euo pipefail
umask 077

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
helper=$here/sandbox_config_helper.py
private=${SANDBOX_PRIVATE_DIR:-$HOME/ai-stack/private}
users_file=$private/sandbox-users.json
config_file=$private/sandbox.json
kubectl=${KUBECTL:-kubectl}
force=false review=''
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case $1 in
    -h | --help)
      tools::usage "$0"
      exit 0
      ;;
    --force) force=true ;;
    --review)
      review=${2:?--review needs a file}
      shift
      ;;
    -*) stop "unknown option $1" ;;
    *) break ;;
  esac
  shift
done
src=${1:?usage: make-sandbox-config.sh [--force] [--review FILE] SOURCE_DIR (--help for details)}
[[ -f $src/lib/workstation/sandbox.py && -f $src/versions.lock ]] || stop "no source tree at $src"
[[ -f $helper ]] || stop "missing $helper"
[[ -d $private && ! -L $private ]] || stop "no private directory $private"
# The source must run the workspaces as the dedicated sandbox account, never UID 1000.
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import sandbox; assert sandbox.SANDBOX_UID == 2002' \
  "$src/lib/workstation" 2>/dev/null || stop "$src does not run the sandbox as UID/GID 2002 (PR #215)"

if [[ $force == false ]]; then
  for f in "$users_file" "$config_file"; do
    [[ ! -e $f ]] || stop "$f exists; rerun with --force to replace it (the old file is kept as a .bak copy)"
  done
fi
image=$(common::lock_get "$src/versions.lock" ASSISTANT_SANDBOX_CANDIDATE_IMAGE)
egress_image=$(common::lock_get "$src/versions.lock" SANDBOX_EGRESS_CANDIDATE_IMAGE)
[[ $image == *@sha256:* && $egress_image == *@sha256:* ]] || stop "$src/versions.lock does not pin both sandbox images by digest"
[[ -z ${SANDBOX_IMAGE:-} || $SANDBOX_IMAGE == "$image" ]] || stop 'the sandbox image is not the one versions.lock records'
[[ -z ${SANDBOX_EGRESS_IMAGE:-} || $SANDBOX_EGRESS_IMAGE == "$egress_image" ]] || stop 'the egress image is not the one versions.lock records'

work=$(mktemp -d "${TMPDIR:-/tmp}/sandbox-config.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT

echo '== Open WebUI accounts and groups (database opened read-only)'
if [[ -n ${SANDBOX_WEBUI_DB:-} ]]; then
  python3 -I "$helper" extract "$SANDBOX_WEBUI_DB" >"$work/dump.json"
else
  $kubectl -n ai-home-lab exec -i deploy/open-webui -c open-webui -- \
    python3 -I - extract /app/backend/data/webui.db <"$helper" >"$work/dump.json" ||
    stop 'cannot read the Open WebUI database'
fi
python3 -I "$helper" propose "$work/dump.json" "$work/review.tsv"

if [[ -n $review ]]; then
  cp -- "$review" "$work/review.tsv"
  echo "Using the reviewed file $review"
else
  [[ -t 0 ]] || stop 'no terminal for the review; pass --review FILE'
  while true; do
    read -r -p "Accept these homes [a], edit them in ${EDITOR:-vi} [e], or quit [q]? " answer
    case $answer in
      a) break ;;
      e)
        "${EDITOR:-vi}" "$work/review.tsv"
        echo 'Edited:'
        grep -v '^#' "$work/review.tsv" | sed 's/^/  /'
        ;;
      q) stop 'nothing written' ;;
    esac
  done
fi

echo '== Free proxy addresses and the API endpoint (read-only)'
$kubectl get servicecidrs -o json >"$work/cidrs.json"
$kubectl get services -A -o json >"$work/services.json"
$kubectl get ipaddresses.networking.k8s.io -o json >"$work/addresses.json"
$kubectl -n default get endpointslices -l kubernetes.io/service-name=kubernetes -o json >"$work/slices.json"
ips=$(python3 -I "$helper" pick-ips "$work/cidrs.json" "$work/services.json" "$work/addresses.json")
api=$(python3 -I "$helper" api-endpoint "$work/slices.json")
for policy in family parents; do
  ip=$(python3 -I -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$ips" "$policy")
  # The IPAddress allocator object is the authority for a ClusterIP in use.
  if $kubectl get ipaddresses.networking.k8s.io "$ip" >/dev/null 2>&1; then
    stop "$ip is allocated"
  fi
done

echo '== Build and validate'
python3 -I "$helper" build "$work/dump.json" "$work/review.tsv" "$work/sandbox-users.json" "$work/sandbox.json" \
  --src "$src" --homes "$users_file" --ips "$ips" --api "$api" --image "$image" --egress-image "$egress_image" ||
  stop 'validation failed; nothing written'

stamp=$(date -u +%Y%m%dT%H%M%SZ)
install_private() { # temporary target
  if [[ -e $2 ]]; then
    [[ $force == true ]] || stop "$2 appeared meanwhile"
    cp -p -- "$2" "$2.bak-$stamp"
    echo "kept the previous file as $2.bak-$stamp"
  fi
  install -m 0600 -- "$1" "$2.new-$stamp"
  mv -f -- "$2.new-$stamp" "$2"
}
install_private "$work/sandbox-users.json" "$users_file"
install_private "$work/sandbox.json" "$config_file"
ls -l -- "$users_file" "$config_file"
cat <<EOF
SANDBOX_CONFIG=written
Next:
  1. On the Mac: nas-homes.sh (creates and exports the NAS datasets).
  2. Here: add MODE_SANDBOX_CONFIG=$config_file to /etc/workstation/workstation.conf
     (sudoedit), then run sandbox-golive.sh.
EOF
