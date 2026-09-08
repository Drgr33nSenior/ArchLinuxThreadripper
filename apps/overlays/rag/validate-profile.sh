#!/usr/bin/env bash
# Pilot guardrails, not a full Open WebUI configuration schema.
set -euo pipefail
fail() { printf 'Invalid RAG pilot setting: %s\n' "$1" >&2; exit 1; }

[[ ${UVICORN_WORKERS:-} == 1 && ${VECTOR_DB:-} == chroma ]] || fail 'single-worker local Chroma required'
for key in ENABLE_PERSISTENT_CONFIG USE_CUDA_DOCKER RAG_FULL_CONTEXT RAG_EMBEDDING_MODEL_AUTO_UPDATE RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE RAG_RERANKING_MODEL_AUTO_UPDATE RAG_RERANKING_MODEL_TRUST_REMOTE_CODE ENABLE_ASYNC_EMBEDDING; do
  [[ ${!key} == false ]] || fail "$key must remain false"
done
[[ ${RAG_EMBEDDING_MODEL:-} == /rag-models/Qwen3-Embedding-0.6B && ${RAG_TOKENIZER_MODEL:-} == "$RAG_EMBEDDING_MODEL" ]] || fail 'locked embedding/tokenizer paths required'
expected_query_prefix=$'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
[[ ${RAG_EMBEDDING_QUERY_PREFIX:-} == "$expected_query_prefix" ]] || fail 'Qwen query prefix must use the reviewed retrieval format'
[[ -z ${RAG_EMBEDDING_CONTENT_PREFIX+x} ]] || fail 'Qwen content prefix must remain unset'
[[ ${RAG_EMBEDDING_ENGINE:-} == '' && ${RAG_RERANKING_ENGINE:-} == '' && ${RAG_RERANKING_MODEL:-} == '' ]] || fail 'external engines and cross-encoder need separate qualification'
[[ ${RAG_TEXT_SPLITTER:-} == token_transformers ]] || fail 'token_transformers required'
[[ ${ENABLE_RAG_HYBRID_SEARCH:-} == true || ${ENABLE_RAG_HYBRID_SEARCH:-} == false ]] || fail 'hybrid search must be true or false'
for key in CHUNK_SIZE CHUNK_OVERLAP RAG_TOP_K RAG_TOP_K_RERANKER RAG_EMBEDDING_BATCH_SIZE; do
  [[ ${!key} =~ ^[1-9][0-9]{0,2}$ ]] || fail "$key must be a positive integer below 1000"
done
((CHUNK_SIZE <= 256 && CHUNK_OVERLAP < CHUNK_SIZE)) || fail 'chunk size exceeds the reviewed pilot or overlap exceeds chunk'
# Upstream merges results across collections using top_k, not top_k_reranker.
((RAG_TOP_K <= 3 && RAG_TOP_K_RERANKER <= RAG_TOP_K)) || fail 'retrieval candidate/context budget exceeded'
((RAG_EMBEDDING_BATCH_SIZE <= 2)) || fail 'embedding batch budget exceeded'
for key in OMP_NUM_THREADS MKL_NUM_THREADS OPENBLAS_NUM_THREADS; do
  [[ ${!key} == 1 || ${!key} == 2 ]] || fail "$key exceeds the two-CPU pilot budget"
done
[[ ${RAG_FILE_MAX_COUNT:-} == 1 && ${RAG_FILE_MAX_SIZE:-} == 2 && ${RAG_ALLOWED_FILE_EXTENSIONS:-} == md,txt ]] || fail 'one 2 MiB Markdown/text upload per request required'
printf 'RAG pilot settings passed; prompt token count and total corpus size still require measurement\n'
