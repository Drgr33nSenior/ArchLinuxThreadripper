#!/usr/bin/env bash
# Portable client configuration; never install clients, start a model or contact K3s.

ws_agent_config_validate() {
  : "${AGENT_HARNESS:=qwen}"
  : "${AGENT_BASE_URL:=http://127.0.0.1:18000/v1}"
  : "${AGENT_MODEL:=Qwen3.8-27B-FP8}"
  : "${AGENT_CONTEXT_TOKENS:=32768}"
  : "${AGENT_MAX_OUTPUT_TOKENS:=4096}"
  : "${AGENT_RETRIEVAL_ENABLED:=false}"
  : "${AGENT_WEB_SEARCH_ENABLED:=false}"
  : "${AGENT_MEMORY_ENABLED:=false}"
  : "${AGENT_HANDOFF_ENABLED:=false}"
  # Request defaults from the serving contract (service render client.conf).
  # Empty means the client sends nothing and the server default applies.
  : "${AGENT_ENABLE_THINKING:=}" "${AGENT_REASONING_EFFORT:=}" "${AGENT_SERVING_CONTRACT:=}"
  : "${AGENT_TEMPERATURE:=}" "${AGENT_TOP_P:=}" "${AGENT_TOP_K:=}"
  case $AGENT_RETRIEVAL_ENABLED in true | false) ;; *) ws_die 'AGENT_RETRIEVAL_ENABLED must be true or false' ;; esac
  case $AGENT_WEB_SEARCH_ENABLED in true | false) ;; *) ws_die 'AGENT_WEB_SEARCH_ENABLED must be true or false' ;; esac
  case $AGENT_MEMORY_ENABLED in true | false) ;; *) ws_die 'AGENT_MEMORY_ENABLED must be true or false' ;; esac
  case $AGENT_HANDOFF_ENABLED in true | false) ;; *) ws_die 'AGENT_HANDOFF_ENABLED must be true or false' ;; esac
  case $AGENT_ENABLE_THINKING in '' | true | false) ;; *) ws_die 'AGENT_ENABLE_THINKING must be true, false or empty' ;; esac
  # The gateway sends high as the template's xhigh level.
  case $AGENT_REASONING_EFFORT in '' | low | medium | high | xhigh) ;; *) ws_die 'AGENT_REASONING_EFFORT must be low, medium, high, xhigh or empty' ;; esac
  [[ -z $AGENT_SERVING_CONTRACT || $AGENT_SERVING_CONTRACT =~ ^[A-Za-z0-9._:-]{1,64}$ ]] ||
    ws_die 'AGENT_SERVING_CONTRACT must be the rendered contract identity'
  [[ -z $AGENT_TEMPERATURE || $AGENT_TEMPERATURE =~ ^(0|1|2)(\.[0-9]{1,4})?$ ]] || ws_die 'AGENT_TEMPERATURE must be 0..2'
  [[ -z $AGENT_TOP_P || $AGENT_TOP_P =~ ^(0(\.[0-9]{1,4})?|1(\.0{1,4})?)$ ]] || ws_die 'AGENT_TOP_P must be 0..1'
  [[ -z $AGENT_TOP_K || $AGENT_TOP_K =~ ^(-1|[1-9][0-9]{0,3})$ ]] || ws_die 'AGENT_TOP_K must be -1 or 1..9999'
  [[ $AGENT_HANDOFF_ENABLED != true || $AGENT_BASE_URL == https://* ]] || ws_die 'task handoffs require the HTTPS service endpoint'
  [[ $AGENT_HANDOFF_ENABLED != true || $AGENT_HARNESS == qwen ]] || ws_die 'task handoffs currently support Qwen Code; other clients need separate MCP qualification'
  [[ $AGENT_MEMORY_ENABLED != true || $AGENT_BASE_URL == https://* ]] || ws_die 'shared memory requires the HTTPS service endpoint'
  [[ $AGENT_MEMORY_ENABLED != true || $AGENT_HARNESS == qwen ]] || ws_die 'shared memory currently supports Qwen Code; other clients need separate MCP qualification'
  [[ $AGENT_RETRIEVAL_ENABLED != true || $AGENT_BASE_URL == https://* ]] || ws_die 'shared retrieval requires the HTTPS service endpoint'
  [[ $AGENT_WEB_SEARCH_ENABLED != true || $AGENT_BASE_URL == https://* ]] || ws_die 'shared web search requires the HTTPS service endpoint'
  [[ $AGENT_WEB_SEARCH_ENABLED != true || $AGENT_HARNESS == qwen ]] || ws_die 'shared web search bundle currently supports Qwen Code; other clients need separate MCP qualification'
  case $AGENT_HARNESS in qwen | dsh | hermes) ;; *) ws_die 'AGENT_HARNESS must be qwen, dsh or hermes' ;; esac
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
  authority=${AGENT_BASE_URL#*://}
  authority=${authority%/v1}
  if [[ $authority == *:* ]]; then
    port=${authority##*:}
    ((port <= 65535)) || ws_die 'AGENT_BASE_URL port exceeds 65535'
  fi
  [[ $AGENT_MODEL =~ ^[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}$ && $AGENT_MODEL != *..* ]] ||
    ws_die 'AGENT_MODEL must be a served model identifier, not a path or command'
  if [[ ! $AGENT_CONTEXT_TOKENS =~ ^[1-9][0-9]{3,5}$ ]] ||
    ((AGENT_CONTEXT_TOKENS < 2048 || AGENT_CONTEXT_TOKENS > 262144)); then
    ws_die 'AGENT_CONTEXT_TOKENS must be 2048..262144 and match the qualified server'
  fi
  if [[ ! $AGENT_MAX_OUTPUT_TOKENS =~ ^[1-9][0-9]{2,5}$ ]] ||
    ((AGENT_MAX_OUTPUT_TOKENS < 128 || AGENT_MAX_OUTPUT_TOKENS > AGENT_CONTEXT_TOKENS / 2)); then
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
        background_review:{enabled:false}},mcp_servers:{}}' >"$1"
}

ws_agent_configure() (
  set -euo pipefail
  umask 077
  local output=$1 qwen_commit dsh_commit hermes_commit qwen_version retrieval='' defaults
  common::require_command jq
  ws_agent_config_validate
  [[ $AGENT_RETRIEVAL_ENABLED != true && $AGENT_WEB_SEARCH_ENABLED != true && $AGENT_MEMORY_ENABLED != true &&
    $AGENT_HANDOFF_ENABLED != true ]] || retrieval="${AGENT_BASE_URL%/v1}/mcp"
  [[ -n $output && ! -e $output && ! -L $output ]] || ws_die 'agent output directory must not already exist'
  qwen_commit=$(ws_read_lock QWEN_CODE_COMMIT)
  qwen_version=$(ws_read_lock QWEN_CODE_VERSION)
  dsh_commit=$(ws_read_lock DSH_COMMIT)
  hermes_commit=$(ws_read_lock HERMES_AGENT_COMMIT)
  [[ $qwen_commit =~ ^[a-f0-9]{40}$ && $dsh_commit =~ ^[a-f0-9]{40}$ && $hermes_commit =~ ^[a-f0-9]{40}$ &&
    $qwen_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || ws_die 'agent source pins are invalid'
  mkdir -p -- "$(dirname -- "$output")"
  mkdir -m 0700 -- "$output"
  mkdir -m 0700 -- "$output/qwen" "$output/dsh" "$output/hermes"
  # Contract request defaults; only set values are sent. The pinned Qwen Code
  # passes samplingParams keys to the request unchanged (0.27.1 pipeline.ts).
  defaults=$(jq -n --arg thinking "$AGENT_ENABLE_THINKING" --arg effort "$AGENT_REASONING_EFFORT" \
    --arg temperature "$AGENT_TEMPERATURE" --arg top_p "$AGENT_TOP_P" --arg top_k "$AGENT_TOP_K" \
    '(if $effort == "" then {} else {reasoning_effort:$effort} end) +
     (if $thinking == "" then {} else {chat_template_kwargs:{enable_thinking:($thinking == "true")}} end) +
     (if $temperature == "" then {} else {temperature:($temperature|tonumber)} end) +
     (if $top_p == "" then {} else {top_p:($top_p|tonumber)} end) +
     (if $top_k == "" then {} else {top_k:($top_k|tonumber)} end)')
  # JSON is also valid YAML. No evaluation, interpolation of credential values,
  # writes to existing dotfiles, or secondary model services are involved.
  # Web access goes only through the workstation (search_web/read_web_page on the gateway's
  # Exa adapter). Qwen's built-in web_fetch fetched from the client machine instead and
  # failed behind TLS-inspecting networks (DEPTH_ZERO_SELF_SIGNED_CERT, 2026-11-11); a
  # whole-tool permissions.deny removes it from the registry. Its built-in web_search is
  # registered only for Qwen-cloud auth, which this bundle never selects.
  # tools.eager keeps only the coding core's schemas in every request (Qwen Code 0.27.1
  # names). Other eager-by-default built-ins (skill, agent, memory, plan mode, web) are
  # deferred: still registered, loaded on demand through the tool_search/tool_call bridge,
  # so a local model's context is not spent on tools a session may never use.
  # Client timeouts cover the longest gateway deadline (large mode, 1200 s; a cold 235K-token
  # prompt streams nothing for ~240 s), so the gateway, not the client, ends a request.
  # "$version" is Qwen Code's settings schema (SETTINGS_VERSION 4 since 0.27);
  # without it the client migrates the file on first run and the launcher's
  # SHA-256 check then rejects the bundle.
  jq -n --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --arg retrieval "$retrieval" \
    --argjson retrieval_enabled "$AGENT_RETRIEVAL_ENABLED" --argjson search_enabled "$AGENT_WEB_SEARCH_ENABLED" \
    --argjson memory_enabled "$AGENT_MEMORY_ENABLED" --argjson handoff_enabled "$AGENT_HANDOFF_ENABLED" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" --argjson defaults "$defaults" \
    '{"$version":4,general:{enableAutoUpdate:false},privacy:{usageStatisticsEnabled:false},telemetry:{enabled:false},
      security:{auth:{selectedType:"openai"},folderTrust:{enabled:true}},
      tools:{approvalMode:"default",eager:["read_file","edit","write_file","list_directory","grep_search","glob",
        "run_shell_command","todo_write","ask_user_question"]},permissions:{deny:["WebFetch"]},context:{fileName:["AGENTS.md","QWEN.md"]},
      model:{name:$model},modelProviders:{openai:[{id:$model,name:"Workstation local Qwen",
        baseUrl:$url,envKey:"WORKSTATION_AGENT_API_KEY",generationConfig:{contextWindowSize:$context,
          timeout:1200000,streamIdleTimeoutMs:1200000,maxRetries:0,
          samplingParams:({max_tokens:$output} + $defaults)}}]} } +
      (if $retrieval=="" then {} else {mcpServers:{workstationKnowledge:{httpUrl:$retrieval,
        headers:{Authorization:"Bearer ${WORKSTATION_RETRIEVAL_API_KEY}"},trust:false,timeout:35000,
        includeTools:((if $retrieval_enabled then ["retrieve_project_passage","read_project_source"] else [] end) +
                      (if $search_enabled then ["search_web","read_web_page"] else [] end) +
                      (if $memory_enabled then ["recall_memory","remember","forget_memory","inspect_memory_source"] else [] end) +
                      (if $handoff_enabled then ["save_task_handoff","load_task_handoff","list_task_handoffs"] else [] end))}}} end)' >"$output/qwen/system-settings.json"
  jq -n --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" \
    '{"agent-default-model":{provider:"workstation",model:$model},
      "llm-pi-ai":{providers:{workstation:{api:"openai-completions",baseURL:$url,
        apiKeyEnv:"WORKSTATION_AGENT_API_KEY",models:[{id:$model,contextWindow:$context,
          maxTokens:$output,input:["text"]}]}}}}' >"$output/dsh/settings.yaml"
  ws_agent_hermes_config "$output/hermes/config.yaml"
  jq -n --arg selected "$AGENT_HARNESS" --arg url "$AGENT_BASE_URL" --arg model "$AGENT_MODEL" \
    --argjson context "$AGENT_CONTEXT_TOKENS" --argjson output "$AGENT_MAX_OUTPUT_TOKENS" \
    --arg qwen "$qwen_commit" --arg version "$qwen_version" --arg dsh "$dsh_commit" --arg hermes "$hermes_commit" --arg retrieval "$retrieval" \
    --arg qwen_config "$(common::sha256_file "$output/qwen/system-settings.json")" \
    --arg dsh_config "$(common::sha256_file "$output/dsh/settings.yaml")" \
    --arg hermes_config "$(common::sha256_file "$output/hermes/config.yaml")" \
    --arg contract "$AGENT_SERVING_CONTRACT" --argjson defaults "$defaults" \
    '{schema:1,status:"configured-not-qualified",default_harness:$selected,
      preference_order:["qwen","dsh","hermes"],automatic_fallback:false,
      base_url:$url,model:$model,context_tokens:$context,max_output_tokens:$output,retrieval_url:$retrieval,
      serving_contract:(if $contract == "" then null else $contract end),
      request_defaults:$defaults,
      request_defaults_support:{qwen:"request",dsh:"unsupported-server-default",hermes:"unsupported-server-default"},
      output_limit_support:{qwen:"request",dsh:"request-default",hermes:"unsupported-provider-owned"},
      config_sha256:{qwen:$qwen_config,dsh:$dsh_config,hermes:$hermes_config},
      sources:{qwen:{repository:"https://github.com/QwenLM/qwen-code",commit:$qwen,version:$version},
        dsh:{repository:"https://github.com/deepseek-ai/deepseek-harness",commit:$dsh},
        hermes:{repository:"https://github.com/NousResearch/hermes-agent",commit:$hermes}}}' >"$output/bundle.json"
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
  [[ -d $bundle && ! -L $bundle && -f $bundle/bundle.json && ! -L $bundle/bundle.json ]] ||
    ws_die 'agent bundle must contain a regular bundle.json'
  bundle=$(cd -- "$bundle" && pwd -P)
  jq -e '.schema == 1 and .automatic_fallback == false and .preference_order == ["qwen","dsh","hermes"] and
    (.base_url|type) == "string" and (.model|type) == "string" and
    (.context_tokens|type) == "number" and (.max_output_tokens|type) == "number"' "$bundle/bundle.json" >/dev/null ||
    ws_die 'invalid agent bundle metadata; regenerate the bundle'
  # The bundle is authoritative at launch. --config affects generation only.
  AGENT_HARNESS=${selected:-$(jq -er '.default_harness' "$bundle/bundle.json")}
  AGENT_BASE_URL=$(jq -er '.base_url' "$bundle/bundle.json")
  AGENT_MODEL=$(jq -er '.model' "$bundle/bundle.json")
  AGENT_CONTEXT_TOKENS=$(jq -er '.context_tokens' "$bundle/bundle.json")
  AGENT_MAX_OUTPUT_TOKENS=$(jq -er '.max_output_tokens' "$bundle/bundle.json")
  # The bundle, not the inherited environment, supplies request defaults.
  AGENT_SERVING_CONTRACT=$(jq -r '.serving_contract // ""' "$bundle/bundle.json")
  AGENT_REASONING_EFFORT=$(jq -r '.request_defaults.reasoning_effort // ""' "$bundle/bundle.json")
  # jq's // treats false as missing, so test for null explicitly.
  AGENT_ENABLE_THINKING=$(jq -r '.request_defaults.chat_template_kwargs.enable_thinking |
    if . == null then "" else tostring end' "$bundle/bundle.json")
  AGENT_TEMPERATURE='' AGENT_TOP_P='' AGENT_TOP_K='' AGENT_HANDOFF_ENABLED=false
  ws_agent_config_validate
  case $mode in cli | acp) ;; *) ws_die 'agent launch mode must be cli or acp' ;; esac
  [[ $AGENT_HARNESS != dsh || $mode == acp ]] ||
    ws_die 'the pinned DSH has no interactive CLI profile; select dsh acp in an ACP-capable IDE'
  case $AGENT_HARNESS in
    qwen) native_file=qwen/system-settings.json ;;
    dsh) native_file=dsh/settings.yaml ;;
    hermes) native_file=hermes/config.yaml ;;
  esac
  [[ -d $bundle/$AGENT_HARNESS && ! -L $bundle/$AGENT_HARNESS &&
    -f $bundle/$native_file && ! -L $bundle/$native_file ]] || ws_die 'native agent configuration is missing or symlinked'
  expected_hash=$(jq -er --arg client "$AGENT_HARNESS" '.config_sha256[$client]' "$bundle/bundle.json") ||
    ws_die 'agent configuration checksum is missing; regenerate the bundle'
  [[ $expected_hash =~ ^[a-f0-9]{64}$ ]] || ws_die 'agent configuration checksum is invalid'
  common::verify_sha256 "$bundle/$native_file" "$expected_hash"
  jq -e --slurpfile bundle "$bundle/bundle.json" --arg client "$AGENT_HARNESS" '
    $bundle[0] as $b |
    if $client == "qwen" then
      .modelProviders.openai[0] as $p | .model.name == $b.model and $p.id == $b.model and
      $p.baseUrl == $b.base_url and $p.generationConfig.contextWindowSize == $b.context_tokens and
      $p.generationConfig.samplingParams.max_tokens == $b.max_output_tokens and
      ($p.generationConfig.samplingParams | del(.max_tokens)) == ($b.request_defaults // {})
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
    if [[ $(jq -r '.retrieval_url // ""' "$bundle/bundle.json") != '' ]]; then
      [[ -n ${WORKSTATION_RETRIEVAL_API_KEY:-} ]] || ws_die 'retrieval requires the caller WebUI API key from the credential store'
      export WORKSTATION_RETRIEVAL_API_KEY
    fi
    # The pinned client supports a process-local system settings path. Use it
    # to prevent project settings from replacing the reviewed model route, but
    # NEVER override an existing managed/system policy to do so.
    policy_file=$(ws_agent_qwen_policy_path)
    [[ ! -e $policy_file && ! -L $policy_file ]] ||
      ws_die 'existing Qwen system policy requires an owner-reviewed merge; it has not been overridden'
    [[ -z ${QWEN_CODE_SYSTEM_SETTINGS_PATH:-} || $QWEN_CODE_SYSTEM_SETTINGS_PATH == "$bundle/qwen/system-settings.json" ]] ||
      ws_die 'existing QWEN_CODE_SYSTEM_SETTINGS_PATH requires owner review; it has not been overridden'
    # The reviewed, hash-checked configuration is Qwen's *system* settings, which
    # override every other scope. QWEN_HOME/settings.json stays Qwen's own user file:
    # "Always allow" choices are written there and must not break the bundle hash.
    export QWEN_CODE_SYSTEM_SETTINGS_PATH="$bundle/qwen/system-settings.json"
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
      # Folder trust (which projects may load MCP tools) is the user's decision, not
      # the bundle's: keep it in one stable file so regenerating a bundle keeps it.
      # Qwen Code reads QWEN_CODE_TRUSTED_FOLDERS_PATH; an explicit value wins.
      export QWEN_CODE_TRUSTED_FOLDERS_PATH="${QWEN_CODE_TRUSTED_FOLDERS_PATH:-${XDG_CONFIG_HOME:-$HOME/.config}/workstation-agent/qwen-trustedFolders.json}"
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
