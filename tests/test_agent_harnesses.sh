#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$root/lib/workstation/runtime.sh"
# shellcheck source=lib/k3s/common.sh
source "$root/lib/k3s/common.sh"
work=$(mktemp -d)
work=$(cd -- "$work" && pwd -P)
trap 'rm -rf -- "$work"' EXIT

[[ $(common::lock_get "$root/versions.lock" QWEN_CODE_VERSION) =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
for key in QWEN_CODE_COMMIT DSH_COMMIT HERMES_AGENT_COMMIT; do
  [[ $(common::lock_get "$root/versions.lock" "$key") =~ ^[a-f0-9]{40}$ ]]
  k3s_allowed_key "$key"
  k3s_validate_value "$key" "$(common::lock_get "$root/versions.lock" "$key")"
  if (k3s_validate_value "$key" unreviewed) >"$work/invalid-lock.log" 2>&1; then exit 1; fi
done
k3s_allowed_key QWEN_CODE_VERSION
k3s_validate_value QWEN_CODE_VERSION "$(common::lock_get "$root/versions.lock" QWEN_CODE_VERSION)"
if (k3s_validate_value QWEN_CODE_VERSION unreviewed) >"$work/invalid-lock.log" 2>&1; then exit 1; fi
if k3s_allowed_key AGENT_UNREVIEWED; then exit 1; fi

reject() {
  if ("$@") >"$work/rejected.log" 2>&1; then
    printf 'unexpected success: %s\n' "$*" >&2
    exit 1
  fi
}

file_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

sed \
  -e 's|^AGENT_HARNESS=.*|AGENT_HARNESS=dsh|' \
  -e 's|^AGENT_BASE_URL=.*|AGENT_BASE_URL=http://localhost:18001/v1|' \
  -e 's|^AGENT_MODEL=.*|AGENT_MODEL=Fixture-Model-1|' \
  -e 's|^AGENT_CONTEXT_TOKENS=.*|AGENT_CONTEXT_TOKENS=8192|' \
  -e 's|^AGENT_MAX_OUTPUT_TOKENS=.*|AGENT_MAX_OUTPUT_TOKENS=1024|' \
  "$root/config/workstation.conf.example" >"$work/agent.conf"

# --config is authoritative: inherited environment settings must not affect a
# portable bundle. Configure performs no client, model or network operation.
env AGENT_HARNESS=hermes AGENT_BASE_URL=http://127.0.0.1:19999/v1 \
  AGENT_MODEL=Ignored-Environment "$root/bin/workstationctl" --config "$work/agent.conf" \
  agent configure "$work/bundle" >/dev/null
jq -e '.schema == 1 and .status == "configured-not-qualified" and
  .default_harness == "dsh" and .preference_order == ["qwen","dsh","hermes"] and
  .automatic_fallback == false and .base_url == "http://localhost:18001/v1" and
  .model == "Fixture-Model-1" and .context_tokens == 8192 and .max_output_tokens == 1024 and
  .output_limit_support == {"qwen":"request","dsh":"request-default","hermes":"unsupported-provider-owned"} and
  (.sources.qwen.commit|test("^[a-f0-9]{40}$")) and
  (.sources.dsh.commit|test("^[a-f0-9]{40}$")) and
  (.sources.hermes.commit|test("^[a-f0-9]{40}$"))' "$work/bundle/bundle.json" >/dev/null
jq -e '.model.name == "Fixture-Model-1" and
  .modelProviders.openai[0].baseUrl == "http://localhost:18001/v1" and
  .modelProviders.openai[0].generationConfig.contextWindowSize == 8192 and
  .modelProviders.openai[0].generationConfig.samplingParams.max_tokens == 1024 and
  .modelProviders.openai[0].envKey == "WORKSTATION_AGENT_API_KEY"' "$work/bundle/qwen/settings.json" >/dev/null
ruby -e 'require "yaml"; y=YAML.load_file(ARGV.fetch(0)); abort unless y.dig("agent-default-model", "model") == "Fixture-Model-1" && y.dig("llm-pi-ai", "providers", "workstation", "baseURL") == "http://localhost:18001/v1"' "$work/bundle/dsh/settings.yaml"
# shellcheck disable=SC2016 # The literal placeholder is required in the native config.
ruby -e 'require "yaml"; y=YAML.load_file(ARGV.fetch(0)); abort unless y.dig("model", "provider") == "custom" && y.dig("model", "base_url") == "http://localhost:18001/v1" && y.dig("model", "api_key") == "${WORKSTATION_AGENT_API_KEY}" && y.dig("model", "context_length") == 8192 && y.dig("approvals", "mode") == "manual" && y.fetch("fallback_providers") == []' "$work/bundle/hermes/config.yaml"
for harness_file in qwen/settings.json dsh/settings.yaml hermes/config.yaml; do
  harness=${harness_file%%/*}
  [[ $(jq -er --arg harness "$harness" '.config_sha256[$harness]' "$work/bundle/bundle.json") == "$(common::sha256_file "$work/bundle/$harness_file")" ]]
done
[[ $(file_mode "$work/bundle") == 700 && $(file_mode "$work/bundle/qwen/settings.json") == 600 &&
$(file_mode "$work/bundle/dsh/settings.yaml") == 600 && $(file_mode "$work/bundle/hermes/config.yaml") == 600 &&
$(file_mode "$work/bundle/bundle.json") == 600 ]]
if grep -Rq 'fixture-secret\|WORKSTATION_AGENT_API_KEY=' "$work/bundle"; then exit 1; fi

# Existing paths and malformed URLs fail before a partial output directory exists.
reject "$root/bin/workstationctl" --config "$work/agent.conf" agent configure "$work/bundle"
ln -s "$work/missing-target" "$work/dangling"
reject "$root/bin/workstationctl" --config "$work/agent.conf" agent configure "$work/dangling"
for invalid_url in http://example.test/v1 http://127.0.0.1/v1?query=1 https://user@example.test/v1 https://example.test/v2 https://example.test:65536/v1; do
  sed "s|^AGENT_BASE_URL=.*|AGENT_BASE_URL=$invalid_url|" "$work/agent.conf" >"$work/invalid.conf"
  reject "$root/bin/workstationctl" --config "$work/invalid.conf" agent configure "$work/invalid-output"
  [[ ! -e $work/invalid-output ]]
done
for invalid_setting in AGENT_HARNESS=unknown AGENT_CONTEXT_TOKENS=2047 AGENT_CONTEXT_TOKENS=262145 AGENT_MAX_OUTPUT_TOKENS=127 AGENT_MAX_OUTPUT_TOKENS=5000 AGENT_MODEL=../not-a-model; do
  setting_key=${invalid_setting%%=*}
  setting_value=${invalid_setting#*=}
  sed "s|^$setting_key=.*|$setting_key=$setting_value|" "$work/agent.conf" >"$work/invalid.conf"
  reject "$root/bin/workstationctl" --config "$work/invalid.conf" agent configure "$work/invalid-output"
  [[ ! -e $work/invalid-output ]]
done

# Legacy configurations omit the agent keys and use the bounded defaults, even
# when an inherited environment contains an invalid endpoint.
sed '/^AGENT_/d' "$root/config/workstation.conf.example" >"$work/legacy.conf"
env AGENT_BASE_URL=http://example.test/v1 "$root/bin/workstationctl" --config "$work/legacy.conf" \
  agent configure "$work/default-bundle" >/dev/null
jq -e '.default_harness == "qwen" and .base_url == "http://127.0.0.1:18000/v1" and
  .model == "Qwen3.8-27B-FP8" and .context_tokens == 32768 and .max_output_tokens == 4096' \
  "$work/default-bundle/bundle.json" >/dev/null
# Keep the client default aligned with the existing dual-GPU server profile.
grep -Fxq "      - SERVED_MODEL_NAME=$(jq -r '.model' "$work/default-bundle/bundle.json")" \
  "$root/apps/overlays/dual-gpu/kustomization.yaml"
grep -Fxq "      - CONTEXT_LENGTH=$(jq -r '.context_tokens' "$work/default-bundle/bundle.json")" \
  "$root/apps/overlays/dual-gpu/kustomization.yaml"

# Launches use fixture functions only. They record native arguments and isolated
# homes instead of starting a real client. The loopback marker is public and not
# stored in the bundle.
ws_require_user() { :; }
qwen() { :; }
dsh() { :; }
hermes() { :; }
# shellcheck disable=SC2329 # Called indirectly by the tested launch implementation.
ws_agent_qwen_policy_path() { printf '%s\n' "$work/no-managed-qwen-policy"; }
ws_agent_exec() {
  printf '%s\n' "$@" >"$work/client-args"
  printf 'called\n' >"$work/client-called"
  # shellcheck disable=SC2031 # This mock intentionally observes launch-subshell exports.
  printf 'QWEN_HOME=%s\nQWEN_RUNTIME_DIR=%s\nQWEN_CODE_SYSTEM_SETTINGS_PATH=%s\nDSH_HOME=%s\nDSH_TELEMETRY_MODE=%s\nHERMES_HOME=%s\nOPENAI_BASE_URL=%s\nOPENAI_MODEL=%s\nKEY_LENGTH=%s\n' \
    "${QWEN_HOME:-}" "${QWEN_RUNTIME_DIR:-}" "${QWEN_CODE_SYSTEM_SETTINGS_PATH:-}" "${DSH_HOME:-}" "${DSH_TELEMETRY_MODE:-}" "${HERMES_HOME:-}" "${OPENAI_BASE_URL:-}" "${OPENAI_MODEL:-}" \
    "${#WORKSTATION_AGENT_API_KEY}" >"$work/client-env"
}
unset WORKSTATION_AGENT_API_KEY
unset QWEN_CODE_SYSTEM_SETTINGS_PATH
(ws_agent_launch "$work/bundle" qwen acp) >"$work/acp-stdout"
[[ ! -s $work/acp-stdout ]]
grep -Fxq qwen "$work/client-args"
grep -Fxq -- '--auth-type' "$work/client-args"
grep -Fxq -- '--acp' "$work/client-args"
grep -Fxq "QWEN_HOME=$work/bundle/qwen" "$work/client-env"
grep -Fxq "QWEN_RUNTIME_DIR=$work/bundle/qwen" "$work/client-env"
grep -Fxq "QWEN_CODE_SYSTEM_SETTINGS_PATH=$work/bundle/qwen/settings.json" "$work/client-env"
grep -Fxq 'OPENAI_BASE_URL=http://localhost:18001/v1' "$work/client-env"
grep -Fxq 'OPENAI_MODEL=Fixture-Model-1' "$work/client-env"
grep -Fxq 'KEY_LENGTH=12' "$work/client-env"
(ws_agent_launch "$work/bundle" dsh acp) >/dev/null
cmp <(printf '%s\n' dsh --profile acp) "$work/client-args"
grep -Fxq "DSH_HOME=$work/bundle/dsh" "$work/client-env"
grep -Fxq 'DSH_TELEMETRY_MODE=DISABLED' "$work/client-env"
(ws_agent_launch "$work/bundle" hermes cli) >/dev/null
cmp <(printf '%s\n' hermes chat) "$work/client-args"
grep -Fxq "HERMES_HOME=$work/bundle/hermes" "$work/client-env"
reject ws_agent_launch "$work/bundle"
(ws_agent_launch "$work/default-bundle") >/dev/null
grep -Fxq qwen "$work/client-args"
reject ws_agent_launch "$work/bundle" qwen unsupported-mode
reject "$root/bin/workstationctl" agent launch "$work/bundle" qwen cli unexpected-argument

# Bundle route metadata must agree with the selected, checksum-pinned native
# config. Changing metadata alone cannot redirect a launch.
cp "$work/bundle/bundle.json" "$work/bundle-manifest-original"
jq '.base_url = "http://127.0.0.1:19999/v1"' "$work/bundle/bundle.json" >"$work/bundle-manifest-tampered"
mv "$work/bundle-manifest-tampered" "$work/bundle/bundle.json"
mv "$work/client-called" "$work/client-called-before-metadata-tamper"
reject ws_agent_launch "$work/bundle" qwen cli
[[ ! -e $work/client-called ]]
mv "$work/bundle-manifest-original" "$work/bundle/bundle.json"

# The selected native file is pinned to the manifest. Refuse a changed file or
# symlink before the client execution boundary, then restore fixture state.
cp "$work/bundle/qwen/settings.json" "$work/qwen-settings-original"
printf 'tampered\n' >>"$work/bundle/qwen/settings.json"
reject ws_agent_launch "$work/bundle" qwen cli
[[ ! -e $work/client-called ]]
cp "$work/qwen-settings-original" "$work/bundle/qwen/settings.json"
mv "$work/bundle/qwen/settings.json" "$work/qwen-settings-for-symlink"
ln -s "$work/qwen-settings-for-symlink" "$work/bundle/qwen/settings.json"
reject ws_agent_launch "$work/bundle" qwen cli
[[ ! -e $work/client-called ]]
mv "$work/bundle/qwen/settings.json" "$work/qwen-settings-symlink"
mv "$work/qwen-settings-for-symlink" "$work/bundle/qwen/settings.json"

# A managed system policy or a conflicting process policy must be left intact.
printf 'managed policy\n' >"$work/managed-qwen-policy"
# shellcheck disable=SC2329 # This fixture replaces the policy-path dependency.
ws_agent_qwen_policy_path() { printf '%s\n' "$work/managed-qwen-policy"; }
reject ws_agent_launch "$work/bundle" qwen cli
grep -Fxq 'managed policy' "$work/managed-qwen-policy"
# shellcheck disable=SC2329 # Restore the non-existent policy fixture for later launches.
ws_agent_qwen_policy_path() { printf '%s\n' "$work/no-managed-qwen-policy"; }
if (
  QWEN_CODE_SYSTEM_SETTINGS_PATH="$work/different-policy"
  export QWEN_CODE_SYSTEM_SETTINGS_PATH
  ws_agent_launch "$work/bundle" qwen cli
) >"$work/conflicting-policy.log" 2>&1; then exit 1; fi

# An HTTPS bundle cannot launch without an owner-provided environment credential.
sed 's|^AGENT_BASE_URL=.*|AGENT_BASE_URL=https://agents.example.test/v1|' "$work/agent.conf" >"$work/https.conf"
"$root/bin/workstationctl" --config "$work/https.conf" agent configure "$work/https-bundle" >/dev/null
unset WORKSTATION_AGENT_API_KEY
reject ws_agent_launch "$work/https-bundle" qwen cli
(
  WORKSTATION_AGENT_API_KEY=fixture-secret
  export WORKSTATION_AGENT_API_KEY
  ws_agent_launch "$work/https-bundle" qwen cli
) >/dev/null
grep -Fxq 'KEY_LENGTH=14' "$work/client-env"
if grep -Rq 'fixture-secret' "$work/https-bundle"; then exit 1; fi

# Client failure is returned as-is, never retried through a second harness.
ws_agent_exec() {
  printf '%s\n' "$1" >>"$work/failed-client-calls"
  return 37
}
if (ws_agent_launch "$work/default-bundle") >"$work/failed-client-stdout" 2>"$work/failed-client-stderr"; then
  exit 1
else
  [[ $? == 37 ]]
fi
[[ $(wc -l <"$work/failed-client-calls") -eq 1 && ! -s $work/failed-client-stdout ]]
grep -Fxq qwen "$work/failed-client-calls"

printf 'Agent harness bundle, validation and fixture-only launch tests passed (no client or endpoint used)\n'
