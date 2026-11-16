#!/usr/bin/env bash
# Set the tokens-a-minute limit on chosen gateway keys with `service limits` from a shipped source tree,
# through a loopback port-forward to LiteLLM, then list every key's stored limits. Key values are never
# read or printed; the master key is passed to service.py as a file. Run on the workstation.
#
#   key-limits.sh SHA12 TPM
#
# SHA12 names the shipped tree $WORKSTATION_SRC_DIR/archlinuxthreadripper-SHA12 (default ~/src).
# Site value GATEWAY_KEY_ALIASES: the key aliases to change, separated by spaces. Keys not listed keep
# their limits. Master key file: ~/ai-stack/private/service-credentials/master-key.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=tools/lib/tools.sh
source "$here/../lib/tools.sh"
if tools::help_requested "$@"; then
  tools::usage "$0"
  exit 0
fi
sha=${1:?usage: key-limits.sh SHA12 TPM (--help for details)}
tpm=${2:?usage: key-limits.sh SHA12 TPM (--help for details)}
[[ $tpm =~ ^[1-9][0-9]*$ ]] || {
  echo "STOP: TPM must be a positive whole number" >&2
  exit 1
}
tools::load_site
tools::require_site GATEWAY_KEY_ALIASES
read -r -a aliases <<<"$GATEWAY_KEY_ALIASES"
src=$(tools::source_tree "$sha")
master=$HOME/ai-stack/private/service-credentials/master-key
# Required feature: the tree's service.py has the `limits` command.
python3 "$src/lib/workstation/service.py" limits --help >/dev/null 2>&1 || {
  echo "STOP: $src/lib/workstation/service.py has no limits command" >&2
  exit 1
}

kubectl -n ai-home-lab port-forward --address 127.0.0.1 service/litellm 14000:4000 >/dev/null 2>&1 &
pf=$!
trap 'kill "$pf" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  curl -s -m 1 -o /dev/null http://127.0.0.1:14000/health/liveliness && break
  sleep 0.5
done

for alias in "${aliases[@]}"; do
  python3 "$src/lib/workstation/service.py" limits --base-url http://127.0.0.1:14000 --master-file "$master" \
    --alias "$alias" --tpm "$tpm"
done

echo '== stored limits (alias | rpm | tpm | concurrency)'
# shellcheck disable=SC2016 # $POSTGRES_USER expands inside the database pod
kubectl -n ai-home-lab exec deploy/gateway-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d litellm -At -F " | " -c "select key_alias, rpm_limit, tpm_limit, max_parallel_requests from \"LiteLLM_VerificationToken\" order by key_alias"'
