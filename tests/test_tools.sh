#!/usr/bin/env bash
# Owner tools under tools/: usage of every script, the site-value loader, the Python helpers'
# unit tests and the offline make-sandbox-config test. Nothing here contacts a cluster, the
# workstation or the NAS. shellcheck and shfmt cover tools/ through `make shellcheck shfmt`.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
: "${HOME_LAB_PYTHON:?select the prepared validation Python}"
# The tools call python3; use the validation Python, as CI puts it first on PATH.
PATH=$(dirname -- "$HOME_LAB_PYTHON"):$PATH
export PATH
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}
# No site file of the person running the tests: every site value comes from this test.
export WORKSTATION_SITE_ENV=$work/site.env
unset WORKSTATION_SRC_DIR WORKSTATION_SSH WORKSTATION_LAN_ADDRESS NAS_ADDRESS NAS_SSH NAS_SANDBOX_DATASET \
  NAS_REPO NAS_COMMISSIONING_DIR NAS_OWNER_PROFILE NAS_ANSIBLE_VENV PROMETHEUS_URL GATEWAY_KEY_ALIASES CHAT_PROBE

# 1. Every script prints its usage for --help and changes nothing.
scripts=$(cd "$root" && rg --files tools -g '*.sh' | sort)
[[ -n $scripts ]] || fail 'no tool scripts found'
while IFS= read -r script; do
  [[ -x $root/$script ]] || fail "$script is not executable"
  out=$(cd "$work" && bash "$root/$script" --help) || fail "$script --help failed"
  grep -q "$(basename -- "$script")" <<<"$out" || fail "$script --help does not name the script: $out"
done < <(grep -v '^tools/lib/' <<<"$scripts")
for tool in tools/kv-trial/kv_trial_report.py tools/measure/comfy_mem.py tools/measure/comfy_summary.py \
  tools/measure/parallel_tools_probe.py tools/sandbox/sandbox_config_helper.py; do
  "$HOME_LAB_PYTHON" -I "$root/$tool" --help >"$work/help.txt" || fail "$tool --help failed"
  grep -qi 'usage' "$work/help.txt" || fail "$tool --help prints no usage"
done

# 2. Scripts that need arguments stop with their usage line before doing anything.
for script in tools/rollout/install-runtime.sh tools/rollout/rollout-main.sh tools/rollout/key-limits.sh \
  tools/sandbox/sandbox-golive.sh tools/sandbox/home-test.sh tools/sandbox/validate-on-workstation.sh; do
  if out=$(bash "$root/$script" 2>&1 </dev/null); then fail "$script ran without arguments"; fi
  grep -q "usage: $(basename -- "$script")" <<<"$out" || fail "$script: no usage line: $out"
done
if out=$(bash "$root/tools/sandbox/nas-homes.sh" 2>&1 </dev/null); then fail 'nas-homes.sh ran without arguments'; fi
grep -q 'nas-homes.sh NAS_COMMIT WORKSTATION_COMMIT' <<<"$out" || fail "nas-homes.sh: no usage line: $out"
if out=$(bash "$root/tools/rollout/key-limits.sh" 0123456789ab 2x 2>&1); then fail 'key-limits.sh accepted TPM 2x'; fi
grep -q 'TPM must be a positive whole number' <<<"$out" || fail "key-limits.sh: $out"

# 3. Site values: the environment wins, the file fills the rest, unknown keys and missing
# values stop, and source trees are named by commit only.
site() { # KEY... : print each loaded value after sourcing the helpers
  # shellcheck disable=SC2016 # expanded by the inner shell
  bash -c 'set -euo pipefail; source "$1"; shift; tools::load_site; for k; do printf "%s=%s\n" "$k" "${!k:-}"; done' \
    site "$root/tools/lib/tools.sh" "$@"
}
printf '# site file\nNAS_ADDRESS=10.0.30.10\nPROMETHEUS_URL=https://prometheus.example.invalid\n' >"$work/site.env"
[[ $(site NAS_ADDRESS PROMETHEUS_URL NAS_SSH) == $'NAS_ADDRESS=10.0.30.10\nPROMETHEUS_URL=https://prometheus.example.invalid\nNAS_SSH=' ]] ||
  fail "site file values: $(site NAS_ADDRESS PROMETHEUS_URL NAS_SSH)"
[[ $(NAS_ADDRESS=10.9.9.9 site NAS_ADDRESS) == NAS_ADDRESS=10.9.9.9 ]] || fail 'the environment did not win over the site file'
[[ $(WORKSTATION_SITE_ENV=$work/absent.env site NAS_ADDRESS) == NAS_ADDRESS= ]] || fail 'a missing site file is not optional'
printf 'NAS_ADRESS=typo\n' >"$work/typo.env"
if WORKSTATION_SITE_ENV=$work/typo.env site NAS_ADDRESS >/dev/null 2>&1; then fail 'an unknown site key was accepted'; fi
# shellcheck disable=SC2016 # a literal command substitution the loader must not run
printf 'NAS_ADDRESS=$(touch %s/ran)\n' "$work" >"$work/code.env"
[[ $(WORKSTATION_SITE_ENV=$work/code.env site NAS_ADDRESS) == "NAS_ADDRESS=\$(touch $work/ran)" && ! -e $work/ran ]] ||
  fail 'a site value was evaluated as shell'
# shellcheck disable=SC2016 # expanded by the inner shell
if out=$(bash -c 'source "$1"; tools::load_site; tools::require_site NAS_ADDRESS NAS_SSH' site "$root/tools/lib/tools.sh" 2>&1); then
  fail 'a missing site value did not stop'
fi
grep -q "STOP: set NAS_SSH in the environment or in $work/site.env" <<<"$out" || fail "wrong missing-value message: $out"
# shellcheck disable=SC2016 # expanded by the inner shell
[[ $(WORKSTATION_SRC_DIR=/srv/trees bash -c 'source "$1"; tools::source_tree 0123456789ab' site "$root/tools/lib/tools.sh") == /srv/trees/archlinuxthreadripper-0123456789ab ]] || fail 'source tree path'
for bad in ../x 0123456789AB ''; do
  # shellcheck disable=SC2016 # expanded by the inner shell
  if bash -c 'source "$1"; tools::source_tree "$2"' site "$root/tools/lib/tools.sh" "$bad" >/dev/null 2>&1; then
    fail "accepted source tree id '$bad'"
  fi
done
if out=$(bash "$root/tools/sandbox/sandbox-golive.sh" --plan-only 0123456789ab 2>&1 </dev/null); then
  fail 'sandbox-golive.sh ran without NAS_ADDRESS'
fi
grep -q 'STOP: set WORKSTATION_LAN_ADDRESS' <<<"$out" || fail "sandbox-golive.sh: $out"

# 4. No site literals in the tools: addresses and home paths come from site values.
# /home/user is the workspace's home inside the sandbox image.
if rg -n '192\.168\.|/Users/|/home/[a-z]' "$root/tools" | grep -v '/home/user\b'; then
  fail 'a site literal is in tools/'
fi

# 5. Python helpers.
for test in "$root"/tests/tools/test_*.py; do
  "$HOME_LAB_PYTHON" "$test"
done

# 6. make-sandbox-config.sh end to end with a fake database and a read-only fake kubectl.
bash "$root/tests/tools/make-sandbox-config-offline.sh" "$root" >"$work/make-config.log" 2>&1 ||
  fail "make-sandbox-config offline test: $(tail -20 "$work/make-config.log")"
tail -1 "$work/make-config.log"
echo 'PASS: owner tools'
