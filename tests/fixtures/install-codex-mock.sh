#!/usr/bin/env bash
set -euo pipefail
fixture=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
if [[ ${1:-} == --version ]]; then
  printf 'codex-cli 0.153.2\n'
  exit 0
fi
[[ -n ${CODEX_HOME:-} && -f $CODEX_HOME/config.toml ]]
[[ -z ${OPENAI_API_KEY:-} && -z ${CODEX_API_KEY:-} && -z ${CODEX_ACCESS_TOKEN:-} ]]
grep -Fqx 'cli_auth_credentials_store = "file"' "$CODEX_HOME/config.toml"
grep -Fqx 'sandbox_mode = "read-only"' "$CODEX_HOME/config.toml"
printf '%s\n' "$CODEX_HOME" >>"$fixture/paths"
printf '%s\n' "$*" >>"$fixture/commands"
if [[ ${1:-} == login ]]; then
  case $2 in
    --with-api-key)
      IFS= read -r key || [[ -n $key ]]
      [[ $key == DUMMY_NOT_A_REAL_API_KEY_12345 ]]
      printf '%s' "$key" >"$CODEX_HOME/auth.json"
      unset key
      ;;
    --device-auth) printf 'dummy device session' >"$CODEX_HOME/auth.json" ;;
    status)
      [[ -s $CODEX_HOME/auth.json ]]
      # Deliberately hostile fixture output must not reach transcripts.
      printf 'DUMMY_NOT_A_REAL_API_KEY_12345\n' >&2
      ;;
    *) exit 2 ;;
  esac
  [[ ! -f $fixture/login-fail ]]
  exit
fi
[[ $* == "--cd "*" --sandbox read-only --ask-for-approval on-request" ]]
[[ -s $CODEX_HOME/auth.json && -f .agents/skills/workstation-install/SKILL.md ]]
printf 'READY\n'
if [[ -f $fixture/wait ]]; then exec sleep 60; fi
[[ ! -f $fixture/client-fail ]] || exit 7
