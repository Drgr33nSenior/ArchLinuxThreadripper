#!/usr/bin/env bash
# Shared by workstationctl and the non-root, network-free init container.
# SET selects one reviewed allowlist: embedding (default, SHA256SUMS), reranker
# (RERANKER-SHA256SUMS) or whisper (WHISPER-SHA256SUMS). Each set is verified against
# its own manifest.
set -euo pipefail

(($# == 2 || $# == 3)) || {
  printf 'Usage: verify-models.sh MODEL_DIRECTORY SHA256SUMS [embedding|reranker|whisper]\n' >&2
  exit 2
}
directory=$1
manifest=$2
set_name=${3:-embedding}
case $set_name in
  embedding)
    label='RAG'
    model=Qwen3-Embedding-0.6B
    pattern='^Qwen3-Embedding-0.6B/(1_Pooling/)?[A-Za-z0-9_.-]+$'
    expected=10
    ;;
  reranker)
    label='RAG reranker'
    model=ms-marco-MiniLM-L6-v2
    pattern='^ms-marco-MiniLM-L6-v2/[A-Za-z0-9_.-]+$'
    expected=6
    ;;
  whisper)
    label='Speech-to-text'
    model=faster-whisper-base
    pattern='^faster-whisper-base/[A-Za-z0-9_.-]+$'
    expected=4
    ;;
  *)
    printf 'Usage: verify-models.sh MODEL_DIRECTORY SHA256SUMS [embedding|reranker|whisper]\n' >&2
    exit 2
    ;;
esac
allowed() {
  case $set_name:$1 in
    embedding:Qwen3-Embedding-0.6B/1_Pooling/config.json | embedding:Qwen3-Embedding-0.6B/config.json | embedding:Qwen3-Embedding-0.6B/config_sentence_transformers.json | embedding:Qwen3-Embedding-0.6B/generation_config.json | embedding:Qwen3-Embedding-0.6B/merges.txt | embedding:Qwen3-Embedding-0.6B/model.safetensors | embedding:Qwen3-Embedding-0.6B/modules.json | embedding:Qwen3-Embedding-0.6B/tokenizer.json | embedding:Qwen3-Embedding-0.6B/tokenizer_config.json | embedding:Qwen3-Embedding-0.6B/vocab.json) return 0 ;;
    reranker:ms-marco-MiniLM-L6-v2/config.json | reranker:ms-marco-MiniLM-L6-v2/model.safetensors | reranker:ms-marco-MiniLM-L6-v2/special_tokens_map.json | reranker:ms-marco-MiniLM-L6-v2/tokenizer.json | reranker:ms-marco-MiniLM-L6-v2/tokenizer_config.json | reranker:ms-marco-MiniLM-L6-v2/vocab.txt) return 0 ;;
    whisper:faster-whisper-base/config.json | whisper:faster-whisper-base/model.bin | whisper:faster-whisper-base/tokenizer.json | whisper:faster-whisper-base/vocabulary.txt) return 0 ;;
  esac
  return 1
}
[[ -d $directory && ! -L $directory && -s $manifest ]] || {
  printf 'Missing %s models or checksums\n' "$label" >&2
  exit 1
}
count=0
seen='|'
while read -r expected_hash relative extra; do
  [[ $expected_hash =~ ^[a-f0-9]{64}$ && -z $extra && $relative =~ $pattern && $relative != *..* ]] ||
    {
      printf 'Invalid %s checksum entry\n' "$label" >&2
      exit 1
    }
  allowed "$relative" || {
    printf '%s checksum entry is outside the reviewed %s allowlist\n' "$label" "$model" >&2
    exit 1
  }
  case $seen in *"|$relative|"*)
    printf '%s checksum manifest has duplicate entries\n' "$label" >&2
    exit 1
    ;;
  esac
  seen="${seen}${relative}|"
  [[ -f $directory/$relative && ! -L $directory/$relative && ! -L $directory/$model && ! -L $directory/$model/1_Pooling ]] ||
    {
      printf 'Missing or symlinked %s model input: %s\n' "$label" "$relative" >&2
      exit 1
    }
  if command -v sha256sum >/dev/null; then
    actual=$(sha256sum "$directory/$relative")
  else
    actual=$(shasum -a 256 "$directory/$relative")
  fi
  [[ ${actual%% *} == "$expected_hash" ]] || {
    printf '%s checksum mismatch: %s\n' "$label" "$relative" >&2
    exit 1
  }
  count=$((count + 1))
done <"$manifest"
((count == expected)) || {
  printf 'Incomplete %s model manifest\n' "$label" >&2
  exit 1
}
printf 'Verified %s %s input files; model execution remains unqualified\n' "$count" "$label"
