#!/usr/bin/env bash
# Opt-in disposable hosted Linux test. No workstation target or GPU access.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_OS:-} == Linux && -z ${DOCKER_HOST:-} ]] || {
  printf 'Run only in the explicitly selected disposable Linux CI runner\n' >&2
  exit 2
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
source "$root/lib/common.sh"
umask 077
scratch=$(mktemp -d)
mkdir -p "$root/test-results"
evidence=$(mktemp -d "$root/test-results/native-rag.XXXXXX")
printf 'phase\texit_status\telapsed_seconds\n' >"$evidence/summary.tsv"
image=$(common::lock_get "$root/versions.lock" OPEN_WEBUI_IMAGE)
revision=$(common::lock_get "$root/versions.lock" OPEN_WEBUI_COMMIT)
nonce="rag-fixture-$$"
seed='' app='' dense='' client_started=0
phase() {
  local name=$1 duration=$2 status started=$SECONDS
  shift 2
  if timeout --kill-after=10s "$duration" "$@" >"$scratch/$name.log" 2>&1; then status=0; else status=$?; fi
  printf '%s\t%s\t%s\n' "$name" "$status" "$((SECONDS - started))" >>"$evidence/summary.tsv"
  ((status == 0)) || {
    printf 'FAIL: %s exit=%s (private raw logs retained)\n' "$name" "$status" >&2
    return "$status"
  }
}
cleanup() {
  local status=$? identifier suffix cleanup_status=0 started=$SECONDS
  trap - EXIT
  set +e
  for identifier in "$app" "$dense"; do
    [[ -n $identifier ]] || continue
    suffix=''
    [[ $identifier != "$dense" ]] || suffix=-dense
    # Fixed fields and error tokens only; never export raw server logs.
    timeout 10s docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.RestartCount}}' "$identifier" >"$scratch/state$suffix.log" 2>/dev/null
    if [[ $(cat "$scratch/state$suffix.log") =~ ^(created|running|paused|restarting|removing|exited|dead)\ [0-9]+\ (true|false)\ [0-9]+$ ]]; then
      cp "$scratch/state$suffix.log" "$evidence/container-state$suffix.txt"
    fi
    timeout 10s docker logs --tail 200 "$identifier" >"$scratch/server$suffix.log" 2>&1
    grep -oE 'PermissionError|Read-only file system|Permission denied|ModuleNotFoundError|ImportError|FileNotFoundError|RuntimeError|ValueError|OutOfMemoryError|OfflineModeIsEnabled|LocalEntryNotFoundError|ConnectionError|\[Errno [0-9]{1,3}\]|/app/backend/open_webui/static|/app/backend/data|/rag-models|/root/\.cache|/app/backend/\.webui_secret_key' "$scratch/server$suffix.log" | sort | uniq -c >"$evidence/startup-tokens$suffix.txt"
  done
  if [[ -n $app ]]; then
    if ((client_started == 0)); then
      # Before any HTTP client/auth call, this isolated container's only secret
      # is the freshly generated 64-hex signing key. Redact it and bound output.
      LC_ALL=C sed -E 's/[0-9A-Fa-f]{64}/[redacted-64-hex]/g' "$scratch/server.log" | head -c 65536 >"$evidence/startup-sanitized.txt"
    fi
    # Only fixed JSON receipts are exported; DB/auth/key/raw logs stay private.
    for kind in output failure; do
      timeout 10s docker cp "$app:/app/backend/data/.rag-smoke-$kind.json" "$evidence/$kind.json" >"$scratch/export-$kind.log" 2>&1
      [[ -s $evidence/$kind.json ]] || rm -f "$evidence/$kind.json"
    done
    timeout 10s docker cp "$app:/app/backend/data/.rag-smoke-rows.jsonl" "$evidence/rows.jsonl" >"$scratch/export-rows.log" 2>&1
    [[ -s $evidence/rows.jsonl ]] || rm -f "$evidence/rows.jsonl"
  fi
  for identifier in "$app" "$dense" "$seed"; do
    [[ -n $identifier ]] || continue
    if [[ $(timeout 10s docker inspect --format '{{index .Config.Labels "workstation.rag-fixture"}}' "$identifier" 2>/dev/null) == "$nonce" ]]; then
      timeout --kill-after=5s 35s docker rm -f "$identifier" >"$scratch/cleanup-$identifier.log" 2>&1 || cleanup_status=1
    else cleanup_status=1; fi
  done
  ((status != 0 || cleanup_status == 0)) || status=1
  printf 'cleanup\t%s\t%s\nresult\t%s\t%s\n' "$cleanup_status" "$((SECONDS - started))" "$status" "$SECONDS" >>"$evidence/summary.tsv"
  printf 'Sanitized evidence: %s\n' "$evidence"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
phase context 15s docker context inspect --format '{{.Endpoints.docker.Host}}'
[[ $(cat "$scratch/context.log") == unix://* ]]
[[ $(df -B1 --output=avail "$scratch" | tail -1) -ge $((12 * 1024 * 1024 * 1024)) ]]
phase image-pull 300s docker pull --platform linux/amd64 "$image"
[[ $(docker image inspect --format '{{.Architecture}}/{{.Os}}' "$image") == amd64/linux ]]
[[ $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") == "$revision" ]]
phase model-stage 360s bash "$root/bin/workstationctl" rag stage-models "$scratch/models"
phase corpus 30s bash "$root/bin/workstationctl" rag corpus "$scratch/corpus" \
  docs/AI-PERFORMANCE.md docs/ROCM.md docs/HOME-LAB.md docs/SERVING-INTEGRATION.md
mkdir "$scratch/data" "$scratch/static" "$scratch/tiktoken"
seed=$(docker create --platform linux/amd64 --label "workstation.rag-fixture=$nonce" --network none --user 1000:1000 "$image" true)
phase static-copy 30s docker cp "$seed:/app/backend/open_webui/static/." "$scratch/static/"
phase tokenizer-copy 30s docker cp "$seed:/app/backend/data/cache/tiktoken/." "$scratch/tiktoken/"
[[ $(du -sk "$scratch/static" | cut -f1) -le 65536 && $(du -sk "$scratch/tiktoken" | cut -f1) -le 4096 ]]
# Narrow fixture ownership on the disposable runner; no host/service data.
sudo -n chown -R 1000:1000 "$scratch/data" "$scratch/static" "$scratch/tiktoken" "$scratch/models"
chmod 711 "$scratch" # Read-only fixture directories are mounted separately.
chmod -R a+rX "$scratch/corpus"
# The dense arm uses the rag-dense profile: rag.env with only the global hybrid
# setting changed (tests/home-lab/check-rag.rb checks the same rendered delta).
# Open WebUI 0.11.4 ignores a request-level hybrid=false while that setting is true.
grep -Fxq '      - ENABLE_RAG_HYBRID_SEARCH=false' "$root/apps/overlays/rag-dense/kustomization.yaml"
[[ $(grep -cx 'ENABLE_RAG_HYBRID_SEARCH=true' "$root/apps/overlays/rag/rag.env") == 1 ]]
sed 's/^ENABLE_RAG_HYBRID_SEARCH=true$/ENABLE_RAG_HYBRID_SEARCH=false/' "$root/apps/overlays/rag/rag.env" >"$scratch/rag-dense.env"
[[ $(diff "$root/apps/overlays/rag/rag.env" "$scratch/rag-dense.env" | grep -c '^[<>]') == 2 ]]
create_app() {
  docker create --platform linux/amd64 --label "workstation.rag-fixture=$nonce" \
    --network none --user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 256 --cpus 2 --memory 6g --memory-swap 6g \
    --tmpfs /tmp:rw,nosuid,nodev,size=512m,mode=1777 \
    --env-file "$1" \
    --env WEBUI_AUTH=true --env ENABLE_SIGNUP=false --env ENABLE_OLLAMA_API=false \
    --env ENABLE_OPENAI_API=false --env OFFLINE_MODE=true --env HF_HUB_OFFLINE=1 \
    --env HOME=/app/backend/data --env XDG_CACHE_HOME=/app/backend/data/cache \
    --env HF_HOME=/app/backend/data/cache/huggingface \
    --env "RAG_FIXTURE_IMAGE=$image" --env "RAG_FIXTURE_REVISION=$revision" \
    --env "RAG_EMBEDDING_QUERY_PREFIX=$(printf 'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:')" \
    --mount "type=bind,source=$scratch/models,target=/rag-models,readonly" \
    --mount "type=bind,source=$scratch/data,target=/app/backend/data" \
    --mount "type=bind,source=$scratch/static,target=/app/backend/open_webui/static" \
    --mount "type=bind,source=$scratch/tiktoken,target=/runtime-tiktoken" \
    --mount "type=bind,source=$scratch/corpus,target=/rag-fixture/corpus,readonly" \
    --mount "type=bind,source=$root/tests/fixtures/rag/questions.json,target=/rag-fixture/questions.json,readonly" \
    --mount "type=bind,source=$root/tests/rag/client.py,target=/rag-fixture/client.py,readonly" \
    --mount "type=bind,source=$root/tests/rag/entrypoint.sh,target=/rag-fixture/entrypoint.sh,readonly" \
    --entrypoint bash "$image" /rag-fixture/entrypoint.sh
}
app=$(create_app "$root/apps/overlays/rag/rag.env")
phase start 20s docker start "$app"
phase readiness 120s bash "$root/tests/rag/wait-ready.sh" "$app"
client_started=1
phase client-initial 480s docker exec --user 1000:1000 "$app" python -B /rag-fixture/client.py initial
# Only one application process may use the Chroma/SQLite volume at a time.
phase stop-hybrid 45s docker stop --timeout 30 "$app"
dense=$(create_app "$scratch/rag-dense.env")
phase dense-start 20s docker start "$dense"
phase dense-readiness 120s bash "$root/tests/rag/wait-ready.sh" "$dense"
phase client-dense 180s docker exec --user 1000:1000 "$dense" python -B /rag-fixture/client.py dense
phase stop-dense 45s docker stop --timeout 30 "$dense"
phase restart 45s docker start "$app"
phase restart-readiness 120s bash "$root/tests/rag/wait-ready.sh" "$app"
phase client-restart 180s docker exec --user 1000:1000 "$app" python -B /rag-fixture/client.py restart
printf 'PASS: native CPU RAG, separate dense/hybrid profiles, isolation and same-volume restart\n'
