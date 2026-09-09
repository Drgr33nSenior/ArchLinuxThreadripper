#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
source "$root/lib/workstation/runtime.sh"
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT

# The separate legacy K3s parser must accept only the new exact lock keys/values.
source "$root/lib/k3s/common.sh"
for key in OPEN_WEBUI_VERSION OPEN_WEBUI_COMMIT OPEN_WEBUI_IMAGE RAG_EMBEDDING_REPOSITORY RAG_EMBEDDING_REVISION; do
  k3s_allowed_key "$key"
  k3s_validate_value "$key" "$(common::lock_get "$root/versions.lock" "$key")"
  if (k3s_validate_value "$key" unreviewed) >"$scratch/invalid-lock.log" 2>&1; then exit 1; fi
done
if k3s_allowed_key OPEN_WEBUI_UNREVIEWED; then exit 1; fi

profile_check() (
  set -a
  source "$root/apps/overlays/rag/rag.env"
  export RAG_EMBEDDING_QUERY_PREFIX=$'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
  env "$@" bash "$root/apps/overlays/rag/validate-profile.sh"
)
profile_check >/dev/null
profile_check ENABLE_RAG_HYBRID_SEARCH=false RAG_TOP_K=3 CHUNK_SIZE=160 CHUNK_OVERLAP=24 RAG_EMBEDDING_BATCH_SIZE=2 >/dev/null
for invalid in UVICORN_WORKERS=2 VECTOR_DB=pgvector USE_CUDA_DOCKER=true ENABLE_PERSISTENT_CONFIG=true \
  RAG_FULL_CONTEXT=true RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE=true RAG_RERANKING_MODEL=unreviewed \
  RAG_EMBEDDING_CONTENT_PREFIX=unreviewed RAG_EMBEDDING_MODEL=/rag-models/other \
  CHUNK_SIZE=512 CHUNK_OVERLAP=192 RAG_TOP_K=0 RAG_TOP_K=1000 RAG_EMBEDDING_BATCH_SIZE=3 \
  RAG_TOP_K_RERANKER=10 RAG_TEXT_SPLITTER=token OMP_NUM_THREADS=48 RAG_FILE_MAX_SIZE=100 \
  RAG_ALLOWED_FILE_EXTENSIONS=pdf RAG_TOP_K=4; do
  if profile_check "$invalid" >"$scratch/invalid.log" 2>&1; then
    printf 'Unexpectedly accepted %s\n' "$invalid" >&2
    exit 1
  fi
done

# Synthetic bytes exercise checksum/staging safety, not actual embedding quality.
mkdir -p "$scratch/repo/apps/overlays/rag" "$scratch/repo/docs"
cp "$root/apps/overlays/rag/verify-models.sh" "$scratch/repo/apps/overlays/rag/"
cp "$root/versions.lock" "$scratch/repo/versions.lock"
printf 'synthetic model bytes\n' >"$scratch/fixture"
fixture_hash=$(common::sha256_file "$scratch/fixture")
while read -r _ relative; do
  printf '%s  %s\n' "$fixture_hash" "$relative"
done <"$root/apps/overlays/rag/SHA256SUMS" >"$scratch/repo/apps/overlays/rag/SHA256SUMS"
ws_repo_root() { (cd -- "$scratch/repo" && pwd -P); }
ws_require_user() { :; }
curl() {
  local destination='' arg url=''
  [[ $* == *"--proto =https --proto-redir =https"* && $* == *"--max-filesize 1342177280"* ]] || return 3
  while (($#)); do
    arg=$1
    shift
    if [[ $arg == --output ]]; then
      destination=$1
      shift
    else url=$arg; fi
  done
  [[ $url == https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/* ]] || return 4
  [[ ${fail_download:-false} != true ]] || return 22
  cp "$scratch/fixture" "$destination"
  if [[ ${corrupt_download:-false} == true ]]; then printf 'bad bytes\n' >>"$destination"; fi
}
ws_rag_stage_models "$scratch/staged" >/dev/null
ws_rag_verify_models "$scratch/staged" >/dev/null
jq -e '.status == "staged-inputs-not-qualified" and .device == "cpu" and .dimensions == 1024' "$scratch/staged/provenance.json" >/dev/null
cp "$scratch/repo/apps/overlays/rag/SHA256SUMS" "$scratch/manifest-before-overflow"
printf '%s  %s\n' "$fixture_hash" 'Qwen3-Embedding-0.6B/overflow.json' >>"$scratch/repo/apps/overlays/rag/SHA256SUMS"
if ws_rag_stage_models "$scratch/manifest-overflow" >"$scratch/manifest-overflow.log" 2>&1; then exit 1; fi
[[ ! -e $scratch/manifest-overflow ]]
cp "$scratch/manifest-before-overflow" "$scratch/repo/apps/overlays/rag/SHA256SUMS"
head -n 9 "$scratch/manifest-before-overflow" >"$scratch/repo/apps/overlays/rag/SHA256SUMS"
head -n 1 "$scratch/manifest-before-overflow" >>"$scratch/repo/apps/overlays/rag/SHA256SUMS"
if ws_rag_stage_models "$scratch/manifest-duplicate" >"$scratch/manifest-duplicate.log" 2>&1; then exit 1; fi
[[ ! -e $scratch/manifest-duplicate ]]
if bash "$root/apps/overlays/rag/verify-models.sh" "$scratch/staged" "$scratch/repo/apps/overlays/rag/SHA256SUMS" >"$scratch/duplicate.log" 2>&1; then exit 1; fi
cp "$scratch/manifest-before-overflow" "$scratch/repo/apps/overlays/rag/SHA256SUMS"
if (ws_rag_stage_models "$scratch/staged") >"$scratch/reuse.log" 2>&1; then exit 1; fi
cmp "$scratch/fixture" "$scratch/staged/Qwen3-Embedding-0.6B/model.safetensors"
if (
  fail_download=true
  ws_rag_stage_models "$scratch/download-failure"
) >"$scratch/failure.log" 2>&1; then exit 1; fi
[[ -d $scratch/download-failure && ! -e $scratch/download-failure/provenance.json ]]
if (
  corrupt_download=true
  ws_rag_stage_models "$scratch/corruption"
) >"$scratch/corruption.log" 2>&1; then exit 1; fi
[[ ! -e $scratch/corruption/provenance.json ]]
printf 'tampered\n' >>"$scratch/staged/Qwen3-Embedding-0.6B/config.json"
if ws_rag_verify_models "$scratch/staged" >"$scratch/tamper.log" 2>&1; then exit 1; fi
cp "$scratch/fixture" "$scratch/staged/Qwen3-Embedding-0.6B/config.json"
mv "$scratch/staged/Qwen3-Embedding-0.6B/config.json" "$scratch/saved-config"
ln -s "$scratch/saved-config" "$scratch/staged/Qwen3-Embedding-0.6B/config.json"
if ws_rag_verify_models "$scratch/staged" >"$scratch/symlink.log" 2>&1; then exit 1; fi
if ws_rag_verify_models "$scratch/missing" >"$scratch/missing.log" 2>&1; then exit 1; fi

printf '# Approved public runbook\nUse the locked image.\n' >"$scratch/repo/docs/runbook.md"
ws_rag_corpus "$scratch/corpus" docs/runbook.md >/dev/null
jq -e '.status == "prepared-not-ingested" and .git_head == null and (.sources | length) == 1' "$scratch/corpus/corpus.json" >/dev/null
prepared=$(jq -r '.sources[0].file' "$scratch/corpus/corpus.json")
hash=$(jq -r '.sources[0].source_sha256' "$scratch/corpus/corpus.json")
[[ $prepared == *"$hash.md" && $hash == "$(common::sha256_file "$scratch/corpus/$prepared")" ]]
cmp "$scratch/repo/docs/runbook.md" "$scratch/corpus/$prepared"
if (ws_rag_corpus "$scratch/corpus" docs/runbook.md) >"$scratch/corpus-reuse.log" 2>&1; then exit 1; fi
for invalid in ../README.md docs/../versions.lock docs/missing.md versions.lock /etc/passwd; do
  if (ws_rag_corpus "$scratch/rejected" "$invalid") >"$scratch/source-error.log" 2>&1; then exit 1; fi
  [[ ! -e $scratch/rejected ]]
done
ln -s "$scratch/repo/docs/runbook.md" "$scratch/repo/docs/link.md"
if (ws_rag_corpus "$scratch/rejected" docs/link.md) >"$scratch/link.log" 2>&1; then exit 1; fi
# Sparse test file: no raw-device or storage benchmark writes.
dd if=/dev/zero of="$scratch/repo/docs/large.md" bs=1 count=1 seek=2097152 2>/dev/null
if (ws_rag_corpus "$scratch/rejected" docs/large.md) >"$scratch/large.log" 2>&1; then exit 1; fi
[[ ! -e $scratch/rejected ]]
bash "$root/bin/workstationctl" help | grep -Fq 'rag stage-models'
bash "$root/bin/workstationctl" rag corpus "$scratch/cli-corpus" docs/AI-PERFORMANCE.md >/dev/null

if command -v kubectl >/dev/null && command -v ruby >/dev/null; then
  kubectl kustomize "$root/apps/overlays/dual-gpu" >"$scratch/dual.yaml"
  kubectl kustomize "$root/apps/overlays/rag" >"$scratch/rag.yaml"
  kubectl kustomize "$root/apps/overlays/rag" >"$scratch/repeated.yaml"
  kubectl kustomize "$root/apps/overlays/rag-dense" >"$scratch/dense.yaml"
  cmp "$scratch/rag.yaml" "$scratch/repeated.yaml"
  ruby "$root/tests/home-lab/check-rag.rb" "$root" "$scratch/dual.yaml" "$scratch/rag.yaml" "$scratch/dense.yaml"
else
  printf 'SKIP: RAG manifest checks require kubectl and Ruby\n'
fi
printf 'RAG profile, staging/corruption fixtures, corpus provenance and offline render tests passed\n'
