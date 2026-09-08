#!/usr/bin/env bash
# Shared by workstationctl and the non-root, network-free init container.
set -euo pipefail

(($# == 2)) || { printf 'Usage: verify-models.sh MODEL_DIRECTORY SHA256SUMS\n' >&2; exit 2; }
directory=$1
manifest=$2
[[ -d $directory && ! -L $directory && -s $manifest ]] || { printf 'Missing RAG models or checksums\n' >&2; exit 1; }
count=0
seen='|'
while read -r expected relative extra; do
  [[ $expected =~ ^[a-f0-9]{64}$ && -z $extra && $relative =~ ^Qwen3-Embedding-0.6B/(1_Pooling/)?[A-Za-z0-9_.-]+$ && $relative != *..* ]] \
    || { printf 'Invalid RAG checksum entry\n' >&2; exit 1; }
  case $relative in
    Qwen3-Embedding-0.6B/1_Pooling/config.json|Qwen3-Embedding-0.6B/config.json|Qwen3-Embedding-0.6B/config_sentence_transformers.json|Qwen3-Embedding-0.6B/generation_config.json|Qwen3-Embedding-0.6B/merges.txt|Qwen3-Embedding-0.6B/model.safetensors|Qwen3-Embedding-0.6B/modules.json|Qwen3-Embedding-0.6B/tokenizer.json|Qwen3-Embedding-0.6B/tokenizer_config.json|Qwen3-Embedding-0.6B/vocab.json) ;;
    *) printf 'RAG checksum entry is outside the reviewed Qwen allowlist\n' >&2; exit 1 ;;
  esac
  case $seen in *"|$relative|"*) printf 'RAG checksum manifest has duplicate entries\n' >&2; exit 1 ;; esac
  seen="${seen}${relative}|"
  [[ -f $directory/$relative && ! -L $directory/$relative && ! -L $directory/Qwen3-Embedding-0.6B && ! -L $directory/Qwen3-Embedding-0.6B/1_Pooling ]] \
    || { printf 'Missing or symlinked RAG model input: %s\n' "$relative" >&2; exit 1; }
  if command -v sha256sum >/dev/null; then
    actual=$(sha256sum "$directory/$relative")
  else
    actual=$(shasum -a 256 "$directory/$relative")
  fi
  [[ ${actual%% *} == "$expected" ]] || { printf 'RAG checksum mismatch: %s\n' "$relative" >&2; exit 1; }
  count=$((count + 1))
done < "$manifest"
((count == 10)) || { printf 'Incomplete RAG model manifest\n' >&2; exit 1; }
printf 'Verified %s RAG input files; model execution remains unqualified\n' "$count"
