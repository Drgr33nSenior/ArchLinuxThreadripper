#!/usr/bin/env bash
# Portable client configuration; never install clients, start a model or contact K3s.

ws_agent_config_validate() {
  : "${AGENT_HARNESS:=qwen}"
  : "${AGENT_BASE_URL:=http://127.0.0.1:18000/v1}"
  : "${AGENT_MODEL:=Qwen3.8-27B-FP8}"
  : "${AGENT_CONTEXT_TOKENS:=32768}"
  : "${AGENT_MAX_OUTPUT_TOKENS:=4096}"
  case $AGENT_HARNESS in qwen|dsh|hermes) ;; *) ws_die 'AGENT_HARNESS must be qwen, dsh or hermes' ;; esac
  # Deliberately narrow API URLs: no credentials, query strings or redirects.
  # Plain HTTP is only for an owner-established loopback tunnel.
  local authority port
  if [[ $AGENT_BASE_URL =~ ^http://(127\.0\.0\.1|localhost)(:[1-9][0-9]{0,4})?/v1$ ]]; then
    :
  elif [[ $AGENT_BASE_URL =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[1-9][0-9]{0,4})?/v1$ ]]; then
    :
  else
    ws_die 'AGENT_BASE_URL must be a loopback HTTP or authenticated HTTPS /v1 URL without credentials, query or fragment'
  fi
  authority=${AGENT_BASE_URL#*://}; authority=${authority%/v1}
  if [[ $authority == *:* ]]; then
    port=${authority##*:}
    ((port <= 65535)) || ws_die 'AGENT_BASE_URL port exceeds 65535'
  fi
  [[ $AGENT_MODEL =~ ^[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}$ && $AGENT_MODEL != *..* ]] \
    || ws_die 'AGENT_MODEL must be a served model identifier, not a path or command'
  if [[ ! $AGENT_CONTEXT_TOKENS =~ ^[1-9][0-9]{3,5}$ ]] \
    || ((AGENT_CONTEXT_TOKENS < 2048 || AGENT_CONTEXT_TOKENS > 262144)); then
    ws_die 'AGENT_CONTEXT_TOKENS must be 2048..262144 and match the qualified server'
  fi
  if [[ ! $AGENT_MAX_OUTPUT_TOKENS =~ ^[1-9][0-9]{2,5}$ ]] \
    || ((AGENT_MAX_OUTPUT_TOKENS < 128 || AGENT_MAX_OUTPUT_TOKENS > AGENT_CONTEXT_TOKENS / 2)); then
    ws_die 'AGENT_MAX_OUTPUT_TOKENS must be 128..half the context window'
  fi
}

ws_agent_hermes_config() {
  jq -n --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" --argjson context "$AGENT_CONTEXT_TOKENS" \
    '{model:{provider:"custom",default:$model,base_url:$url,
        api_key:"${WORKSTATION_AGENT_API_KEY}",context_length:$context},
      approvals:{mode:"manual"},terminal:{backend:"local"},fallback_providers:[],
      compression:{enabled:true,threshold:0.5,target_ratio:0.2,protect_last_n:20},
      auxiliary:{compression:{provider:"main"},title_generation:{enabled:false},
        background_review:{enabled:false}},mcp_servers:{}}' > "$1"
}

ws_agent_configure() (
  set -euo pipefail
  umask 077
  local output=$1 qwen_commit dsh_commit hermes_commit qwen_version
  common::require_command jq
  ws_agent_config_validate
  [[ -n $output && ! -e $output && ! -L $output ]] || ws_die 'agent output directory must not already exist'
  qwen_commit=$(ws_read_lock QWEN_CODE_COMMIT)
  qwen_version=$(ws_read_lock QWEN_CODE_VERSION)
  dsh_commit=$(ws_read_lock DSH_COMMIT)
  hermes_commit=$(ws_read_lock HERMES_AGENT_COMMIT)
  [[ $qwen_commit =~ ^[a-f0-9]{40}$ && $dsh_commit =~ ^[a-f0-9]{40}$ && $hermes_commit =~ ^[a-f0-9]{40}$ \
    && $qwen_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || ws_die 'agent source pins are invalid'
  mkdir -p -- "$(dirname -- "$output")"
  mkdir -m 0700 -- "$output"
  mkdir -m 0700 -- "$output/qwen" "$output/dsh" "$output/hermes"
  # JSON is also valid YAML. No evaluation, interpolation of credential values,
  # writes to existing dotfiles, or secondary model services are involved.
  jq -n --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" \
    '{general:{enableAutoUpdate:false},privacy:{usageStatisticsEnabled:false},telemetry:{enabled:false},
      security:{auth:{selectedType:"openai"},folderTrust:{enabled:true}},
      tools:{approvalMode:"default"},context:{fileName:["AGENTS.md","QWEN.md"]},
      model:{name:$model},modelProviders:{openai:[{id:$model,name:"Workstation local Qwen",
        baseUrl:$url,envKey:"WORKSTATION_AGENT_API_KEY",generationConfig:{contextWindowSize:$context,
          timeout:120000,streamIdleTimeoutMs:180000,maxRetries:1,
          samplingParams:{max_tokens:$output}}}]}}' > "$output/qwen/settings.json"
  jq -n --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" \
    '{"agent-default-model":{provider:"workstation",model:$model},
      "llm-pi-ai":{providers:{workstation:{api:"openai-completions",baseURL:$url,
        apiKeyEnv:"WORKSTATION_AGENT_API_KEY",models:[{id:$model,contextWindow:$context,
          maxTokens:$output,input:["text"]}]}}}}' > "$output/dsh/settings.yaml"
  ws_agent_hermes_config "$output/hermes/config.yaml"
  jq -n --arg selected "$AGENT_HARNESS" --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" \
    --arg qwen "$qwen_commit" --arg version "$qwen_version" --arg dsh "$dsh_commit" --arg hermes "$hermes_commit" \
    --arg qwen_config "$(common::sha256_file "$output/qwen/settings.json")" \
    --arg dsh_config "$(common::sha256_file "$output/dsh/settings.yaml")" \
    --arg hermes_config "$(common::sha256_file "$output/hermes/config.yaml")" \
    '{schema:1,status:"configured-not-qualified",default_harness:$selected,
      preference_order:["qwen","dsh","hermes"],automatic_fallback:false,
      base_url:$url,model:$model,context_tokens:$context,max_output_tokens:$output,
      output_limit_support:{qwen:"request",dsh:"request-default",hermes:"unsupported-provider-owned"},
      config_sha256:{qwen:$qwen_config,dsh:$dsh_config,hermes:$hermes_config},
      sources:{qwen:{repository:"https://github.com/QwenLM/qwen-code",commit:$qwen,version:$version},
        dsh:{repository:"https://github.com/deepseek-ai/deepseek-harness",commit:$dsh},
        hermes:{repository:"https://github.com/NousResearch/hermes-agent",commit:$hermes}}}' > "$output/bundle.json"
  ws_note "wrote $output; primary=$AGENT_HARNESS, manual preference=qwen,dsh,hermes; no clients installed or started"
)

ws_agent_exec() { exec "$@"; }

ws_agent_qwen_policy_path() {
  case $(uname -s) in
    Darwin) printf '%s\n' '/Library/Application Support/QwenCode/settings.json' ;;
    Linux) printf '%s\n' /etc/qwen-code/settings.json ;;
    *) ws_die 'agent launch is supported on macOS and Linux clients' ;;
  esac
}

ws_agent_launch() {
  local bundle=$1 selected=${2:-} mode=${3:-cli} native_file expected_hash policy_file
  ws_require_user
  common::require_command jq
  [[ -d $bundle && ! -L $bundle && -f $bundle/bundle.json && ! -L $bundle/bundle.json ]] \
    || ws_die 'agent bundle must contain a regular bundle.json'
  bundle=$(cd -- "$bundle" && pwd -P)
  jq -e '.schema == 1 and .automatic_fallback == false and .preference_order == ["qwen","dsh","hermes"] and
    (.base_url|type) == "string" and (.model|type) == "string" and
    (.context_tokens|type) == "number" and (.max_output_tokens|type) == "number"' "$bundle/bundle.json" >/dev/null \
    || ws_die 'invalid agent bundle metadata; regenerate the bundle'
  # The bundle is authoritative at launch. --config affects generation only.
  AGENT_HARNESS=${selected:-$(jq -er '.default_harness' "$bundle/bundle.json")}
  AGENT_BASE_URL=$(jq -er '.base_url' "$bundle/bundle.json")
  AGENT_MODEL=$(jq -er '.model' "$bundle/bundle.json")
  AGENT_CONTEXT_TOKENS=$(jq -er '.context_tokens' "$bundle/bundle.json")
  AGENT_MAX_OUTPUT_TOKENS=$(jq -er '.max_output_tokens' "$bundle/bundle.json")
  ws_agent_config_validate
  case $mode in cli|acp) ;; *) ws_die 'agent launch mode must be cli or acp' ;; esac
  [[ $AGENT_HARNESS != dsh || $mode == acp ]] \
    || ws_die 'the pinned DSH has no interactive CLI profile; select dsh acp in an ACP-capable IDE'
  case $AGENT_HARNESS in
    qwen) native_file=qwen/settings.json ;;
    dsh) native_file=dsh/settings.yaml ;;
    hermes) native_file=hermes/config.yaml ;;
  esac
  [[ -d $bundle/$AGENT_HARNESS && ! -L $bundle/$AGENT_HARNESS \
    && -f $bundle/$native_file && ! -L $bundle/$native_file ]] || ws_die 'native agent configuration is missing or symlinked'
  expected_hash=$(jq -er --arg client "$AGENT_HARNESS" '.config_sha256[$client]' "$bundle/bundle.json") \
    || ws_die 'agent configuration checksum is missing; regenerate the bundle'
  [[ $expected_hash =~ ^[a-f0-9]{64}$ ]] || ws_die 'agent configuration checksum is invalid'
  common::verify_sha256 "$bundle/$native_file" "$expected_hash"
  jq -e --slurpfile bundle "$bundle/bundle.json" --arg client "$AGENT_HARNESS" '
    $bundle[0] as $b |
    if $client == "qwen" then
      .modelProviders.openai[0] as $p | .model.name == $b.model and $p.id == $b.model and
      $p.baseUrl == $b.base_url and $p.generationConfig.contextWindowSize == $b.context_tokens and
      $p.generationConfig.samplingParams.max_tokens == $b.max_output_tokens
    elif $client == "dsh" then
      .["llm-pi-ai"].providers.workstation as $p |
      .["agent-default-model"].provider == "workstation" and .["agent-default-model"].model == $b.model and
      $p.baseURL == $b.base_url and $p.models[0].id == $b.model and
      $p.models[0].contextWindow == $b.context_tokens and $p.models[0].maxTokens == $b.max_output_tokens
    else
      .model.provider == "custom" and .model.default == $b.model and
      .model.base_url == $b.base_url and .model.context_length == $b.context_tokens
    end' "$bundle/$native_file" >/dev/null || ws_die 'native agent route or budget differs from bundle metadata; regenerate the bundle'
  if [[ $AGENT_HARNESS == qwen ]]; then
    # The pinned client supports a process-local system settings path. Use it
    # to prevent project settings from replacing the reviewed model route, but
    # NEVER override an existing managed/system policy to do so.
    policy_file=$(ws_agent_qwen_policy_path)
    [[ ! -e $policy_file && ! -L $policy_file ]] \
      || ws_die 'existing Qwen system policy requires an owner-reviewed merge; it has not been overridden'
    [[ -z ${QWEN_CODE_SYSTEM_SETTINGS_PATH:-} || $QWEN_CODE_SYSTEM_SETTINGS_PATH == "$bundle/qwen/settings.json" ]] \
      || ws_die 'existing QWEN_CODE_SYSTEM_SETTINGS_PATH requires owner review; it has not been overridden'
    export QWEN_CODE_SYSTEM_SETTINGS_PATH="$bundle/qwen/settings.json"
  fi
  common::require_command "$AGENT_HARNESS"
  if [[ -z ${WORKSTATION_AGENT_API_KEY:-} ]]; then
    [[ $AGENT_BASE_URL == http://* ]] || ws_die 'HTTPS agent endpoint requires WORKSTATION_AGENT_API_KEY from the owner credential store'
    # Public SDK marker, not an authentication credential. The tunnel provides
    # transport authentication; this does not create an unauthenticated ingress.
    WORKSTATION_AGENT_API_KEY=local-tunnel
  fi
  export WORKSTATION_AGENT_API_KEY
  # Route shared SDK environment variables to this endpoint too. Do not inherit
  # an unrelated cloud endpoint or place secret values in command-line arguments.
  export OPENAI_BASE_URL="$AGENT_BASE_URL" OPENAI_API_KEY="$WORKSTATION_AGENT_API_KEY" OPENAI_MODEL="$AGENT_MODEL"
  printf 'workstationctl: starting %s (%s); no automatic fallback; tools run on this client, not the GPU server\n' "$AGENT_HARNESS" "$mode" >&2
  case $AGENT_HARNESS in
    qwen)
      export QWEN_HOME="$bundle/qwen" QWEN_RUNTIME_DIR="$bundle/qwen" QWEN_MODEL="$AGENT_MODEL"
      export QWEN_USAGE_STATISTICS_ENABLED=false QWEN_TELEMETRY_ENABLED=false
      if [[ $mode == acp ]]; then
        ws_agent_exec qwen --auth-type openai --model "$AGENT_MODEL" --approval-mode default --acp
      else
        ws_agent_exec qwen --auth-type openai --model "$AGENT_MODEL" --approval-mode default
      fi
      ;;
    dsh)
      export DSH_HOME="$bundle/dsh" DSH_PERMISSION_MODE=workspace-write DSH_TELEMETRY_MODE=DISABLED
      ws_agent_exec dsh --profile acp
      ;;
    hermes)
      export HERMES_HOME="$bundle/hermes"
      printf 'workstationctl: Hermes output-token cap is provider-owned; AGENT_MAX_OUTPUT_TOKENS is not enforced by this client\n' >&2
      if [[ $mode == acp ]]; then ws_agent_exec hermes acp; else ws_agent_exec hermes chat; fi
      ;;
  esac
}
