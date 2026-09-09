#!/usr/bin/env bash
# Explicit offline-pilot preparation. No model execution, cluster or NAS access.

ws_rag_new_output() {
  [[ -n $1 && ! -e $1 && ! -L $1 ]] || ws_die 'RAG output directory must not already exist'
  mkdir -p -- "$(dirname -- "$1")"
  mkdir -m 0700 -- "$1"
}

ws_rag_verify_models() {
  local inputs
  inputs="$(ws_repo_root)/apps/overlays/rag"
  bash "$inputs/verify-models.sh" "$1" "$inputs/SHA256SUMS"
}

ws_rag_validate_model_manifest() {
  local manifest=$1 hash relative extra count=0 seen='|'
  while read -r hash relative extra; do
    [[ $hash =~ ^[a-f0-9]{64}$ && -z $extra && $relative =~ ^Qwen3-Embedding-0.6B/(1_Pooling/)?[A-Za-z0-9_.-]+$ && $relative != *..* ]] ||
      ws_die 'invalid RAG checksum entry'
    case $relative in
      Qwen3-Embedding-0.6B/1_Pooling/config.json | Qwen3-Embedding-0.6B/config.json | Qwen3-Embedding-0.6B/config_sentence_transformers.json | Qwen3-Embedding-0.6B/generation_config.json | Qwen3-Embedding-0.6B/merges.txt | Qwen3-Embedding-0.6B/model.safetensors | Qwen3-Embedding-0.6B/modules.json | Qwen3-Embedding-0.6B/tokenizer.json | Qwen3-Embedding-0.6B/tokenizer_config.json | Qwen3-Embedding-0.6B/vocab.json) ;;
      *) ws_die 'RAG checksum entry is outside the reviewed Qwen allowlist' ;;
    esac
    case $seen in *"|$relative|"*) ws_die 'RAG checksum manifest has duplicate entries' ;; esac
    seen="${seen}${relative}|"
    count=$((count + 1))
    ((count <= 10)) || ws_die 'RAG model manifest exceeds the reviewed ten-file bound'
  done <"$manifest"
  ((count == 10)) || ws_die 'RAG model manifest is incomplete'
}

ws_rag_stage_models() (
  set -euo pipefail
  umask 077
  local output=$1 root repository revision hash relative extra url total=0 bytes remaining max_file
  ws_require_user
  root=$(ws_repo_root)
  repository=$(ws_read_lock RAG_EMBEDDING_REPOSITORY)
  revision=$(ws_read_lock RAG_EMBEDDING_REVISION)
  [[ $repository == Qwen/Qwen3-Embedding-0.6B && $revision == 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 ]] ||
    ws_die 'RAG model lock is not the reviewed Qwen3 embedding model'
  common::require_command curl
  common::require_command jq
  ws_rag_validate_model_manifest "$root/apps/overlays/rag/SHA256SUMS"
  ws_rag_new_output "$output"
  output=$(cd -- "$output" && pwd -P)
  while read -r hash relative extra; do
    [[ $hash =~ ^[a-f0-9]{64}$ && -z $extra && $relative =~ ^Qwen3-Embedding-0.6B/(1_Pooling/)?[A-Za-z0-9_.-]+$ && $relative != *..* ]] ||
      ws_die 'invalid RAG checksum entry'
    mkdir -p -- "$output/$(dirname -- "$relative")"
    url="https://huggingface.co/$repository/resolve/$revision/${relative#*/}"
    remaining=$((1400000000 - total))
    ((remaining > 0)) || ws_die 'RAG model inputs exceed the reviewed 1.4 GB total budget'
    max_file=1342177280
    ((max_file <= remaining)) || max_file=$remaining
    curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 \
      --max-time 1800 --max-filesize "$max_file" --output "$output/$relative" "$url" ||
      ws_die 'RAG model download failed; partial output retained, use a new directory to retry'
    common::verify_sha256 "$output/$relative" "$hash"
    bytes=$(wc -c <"$output/$relative")
    total=$((total + bytes))
    ((total <= 1400000000)) || ws_die 'RAG model inputs exceed the reviewed 1.4 GB total budget'
    chmod 0644 "$output/$relative"
  done <"$root/apps/overlays/rag/SHA256SUMS"
  ws_rag_verify_models "$output"
  cp "$root/apps/overlays/rag/SHA256SUMS" "$output/SHA256SUMS"
  jq -n --arg model "$repository" --arg revision "$revision" \
    --arg image "$(ws_read_lock OPEN_WEBUI_IMAGE)" \
    --arg checksums "$(common::sha256_file "$output/SHA256SUMS")" \
    '{schema:1,status:"staged-inputs-not-qualified",embedding_model:$model,embedding_revision:$revision,
      webui_image:$image,checksums_sha256:$checksums,device:"cpu",dimensions:1024}' >"$output/provenance.json"
  ws_note "wrote $output; no image pull, model execution or deployment was performed"
)

ws_rag_corpus() (
  set -euo pipefail
  umask 077
  local output=$1 root source directory file hash name total=0 bytes commit captured lock_hash
  shift
  (($# > 0 && $# <= 20)) || ws_die 'select 1..20 reviewed docs/*.md files explicitly'
  common::require_command jq
  root=$(ws_repo_root)
  # Validate every source before creating output. No directory crawler or symlinks.
  for source in "$@"; do
    [[ $source =~ ^docs/([A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md$ && $source != *..* ]] ||
      ws_die 'corpus inputs must be explicit repository-relative docs/*.md paths'
    file="$root/$source"
    [[ -f $file && ! -L $file ]] || ws_die "corpus source is not a regular non-symlink file: $source"
    directory=$(cd -- "$(dirname -- "$file")" && pwd -P)
    [[ $directory == "$root/docs" || $directory == "$root/docs/"* ]] || ws_die 'corpus source escapes docs/'
    bytes=$(wc -c <"$file")
    ((bytes <= 2097152)) || ws_die 'corpus source exceeds the 2 MiB pilot file budget'
    total=$((total + bytes))
    ((total <= 20971520)) || ws_die 'corpus exceeds the 20 MiB pilot budget'
  done
  ws_rag_new_output "$output"
  output=$(cd -- "$output" && pwd -P)
  mkdir "$output/documents"
  commit=$(git -C "$root" rev-parse --verify HEAD 2>/dev/null) || commit=''
  captured=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  lock_hash=$(common::sha256_file "$root/versions.lock")
  for source in "$@"; do
    file="$root/$source"
    # Snapshot bytes first: provenance must describe the copy even in a dirty tree.
    cp "$file" "$output/document.pending"
    hash=$(common::sha256_file "$output/document.pending")
    name="${source//\//__}--$hash.md"
    mv "$output/document.pending" "$output/documents/$name"
    jq -cn --arg source "$source" --arg sha256 "$hash" --arg file "documents/$name" \
      '{source_path:$source,source_sha256:$sha256,file:$file}' >>"$output/sources.jsonl"
  done
  jq -s --arg captured "$captured" --arg commit "$commit" --arg lock "$lock_hash" \
    '{schema:1,status:"prepared-not-ingested",captured_at:$captured,
      git_head:(if $commit == "" then null else $commit end),
      revision_basis:"file SHA-256, not a clean-tree claim",versions_lock_sha256:$lock,sources:.}' \
    "$output/sources.jsonl" >"$output/corpus.json"
  ws_note "wrote $output; review documents before upload. This is not a secret scanner or an automatic ingestion job"
)
