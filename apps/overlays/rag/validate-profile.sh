#!/usr/bin/env bash
# Pilot guardrails, not a full Open WebUI configuration schema.
# RAG_RERANKER_PROFILE selects one reviewed retrieval mode:
#   none                   baseline: no cross-encoder, three candidates/three final hits
#   ms-marco-minilm-l6-v2  assistant: CPU cross-encoder over at most 2 x RAG_TOP_K
#                          hybrid candidates (asset hashes/load: init container)
set -euo pipefail
fail() {
  printf 'Invalid RAG pilot setting: %s\n' "$1" >&2
  exit 1
}

[[ ${UVICORN_WORKERS:-} == 1 && ${VECTOR_DB:-} == chroma ]] || fail 'single-worker local Chroma required'
for key in ENABLE_PERSISTENT_CONFIG USE_CUDA_DOCKER RAG_FULL_CONTEXT RAG_EMBEDDING_MODEL_AUTO_UPDATE RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE RAG_RERANKING_MODEL_AUTO_UPDATE RAG_RERANKING_MODEL_TRUST_REMOTE_CODE ENABLE_ASYNC_EMBEDDING; do
  [[ ${!key} == false ]] || fail "$key must remain false"
done
[[ ${RAG_EMBEDDING_MODEL:-} == /rag-models/Qwen3-Embedding-0.6B && ${RAG_TOKENIZER_MODEL:-} == "$RAG_EMBEDDING_MODEL" ]] || fail 'locked embedding/tokenizer paths required'
expected_query_prefix=$'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
[[ ${RAG_EMBEDDING_QUERY_PREFIX:-} == "$expected_query_prefix" ]] || fail 'Qwen query prefix must use the reviewed retrieval format'
[[ -z ${RAG_EMBEDDING_CONTENT_PREFIX+x} ]] || fail 'Qwen content prefix must remain unset'
[[ ${RAG_EMBEDDING_ENGINE:-} == '' && ${RAG_RERANKING_ENGINE:-} == '' ]] || fail 'external embedding/reranking engines need separate qualification'
[[ ${RAG_TEXT_SPLITTER:-} == token_transformers ]] || fail 'token_transformers required'
[[ ${TIKTOKEN_CACHE_DIR:-} == /runtime-tiktoken ]] || fail 'tokenizer cache must not create a nested data-volume mount'
[[ ${ENABLE_RAG_HYBRID_SEARCH:-} == true || ${ENABLE_RAG_HYBRID_SEARCH:-} == false ]] || fail 'hybrid search must be true or false'
for key in CHUNK_SIZE CHUNK_OVERLAP RAG_TOP_K RAG_TOP_K_RERANKER RAG_EMBEDDING_BATCH_SIZE; do
  [[ ${!key} =~ ^[1-9][0-9]{0,2}$ ]] || fail "$key must be a positive integer below 1000"
done
((CHUNK_SIZE <= 256 && CHUNK_OVERLAP < CHUNK_SIZE)) || fail 'chunk size exceeds the reviewed pilot or overlap exceeds chunk'
[[ ${RAG_RELEVANCE_THRESHOLD:-} =~ ^0(\.[0-9]{1,3})?$ ]] || fail 'RAG_RELEVANCE_THRESHOLD must be a decimal in [0, 1)'
# Upstream keeps min(RAG_TOP_K, RAG_TOP_K_RERANKER) per collection and merges
# collections with RAG_TOP_K, so final <= candidates keeps both counts meaningful.
case ${RAG_RERANKER_PROFILE:-} in
  none)
    [[ ${RAG_RERANKING_MODEL:-} == '' ]] || fail 'the none profile must not load a cross-encoder'
    ((RAG_TOP_K <= 3 && RAG_TOP_K_RERANKER <= RAG_TOP_K)) || fail 'retrieval candidate/context budget exceeded'
    ;;
  ms-marco-minilm-l6-v2)
    [[ ${RAG_RERANKING_MODEL:-} == /rag-models/ms-marco-MiniLM-L6-v2 ]] || fail 'reranker path must be the reviewed MiniLM cross-encoder'
    # Open WebUI 0.12.5 loads RAG_RERANKING_MODEL only while hybrid search is on.
    [[ $ENABLE_RAG_HYBRID_SEARCH == true ]] || fail 'the cross-encoder profile requires hybrid search'
    ((RAG_TOP_K <= 16)) || fail 'reranker candidate budget exceeded: RAG_TOP_K above 16 (32 scored pairs)'
    ((RAG_TOP_K_RERANKER <= 8 && RAG_TOP_K_RERANKER <= RAG_TOP_K)) || fail 'final passages must be at most 8 and at most RAG_TOP_K'
    [[ ${RAG_RERANKING_BATCH_SIZE:-} =~ ^[1-9][0-9]?$ ]] && ((RAG_RERANKING_BATCH_SIZE <= 32)) ||
      fail 'RAG_RERANKING_BATCH_SIZE must be 1..32'
    [[ ${SENTENCE_TRANSFORMERS_CROSS_ENCODER_BACKEND:-torch} == torch ]] || fail 'reranker backend must remain torch'
    [[ -z ${SENTENCE_TRANSFORMERS_CROSS_ENCODER_MODEL_KWARGS:-} ]] || fail 'reranker model kwargs need separate qualification'
    [[ ${SENTENCE_TRANSFORMERS_CROSS_ENCODER_SIGMOID_ACTIVATION_FUNCTION:-true} == true ]] ||
      fail 'reranker scores must keep the sigmoid [0, 1] scale used by RAG_RELEVANCE_THRESHOLD'
    ;;
  *) fail 'RAG_RERANKER_PROFILE must be none or ms-marco-minilm-l6-v2' ;;
esac
((RAG_EMBEDDING_BATCH_SIZE <= 2)) || fail 'embedding batch budget exceeded'
for key in OMP_NUM_THREADS MKL_NUM_THREADS OPENBLAS_NUM_THREADS; do
  [[ ${!key} == 1 || ${!key} == 2 ]] || fail "$key exceeds the two-CPU pilot budget"
done
[[ ${RAG_FILE_MAX_COUNT:-} == 1 && ${RAG_FILE_MAX_SIZE:-} == 2 && ${RAG_ALLOWED_FILE_EXTENSIONS:-} == md,txt ]] || fail 'one 2 MiB Markdown/text upload per request required'
# Returned-text allowance for the service-tools MCP tools (characters).
for key in RAG_PASSAGE_CHAR_LIMIT RAG_CONTEXT_CHAR_BUDGET RAG_EXPANSION_CHAR_LIMIT RAG_SOURCE_READ_CHAR_LIMIT; do
  [[ ${!key:-} =~ ^(0|[1-9][0-9]{0,4})$ ]] || fail "$key must be a non-negative integer"
done
((RAG_PASSAGE_CHAR_LIMIT >= 200 && RAG_PASSAGE_CHAR_LIMIT <= 4000)) || fail 'RAG_PASSAGE_CHAR_LIMIT must be 200..4000'
((RAG_CONTEXT_CHAR_BUDGET >= RAG_PASSAGE_CHAR_LIMIT && RAG_CONTEXT_CHAR_BUDGET <= 24000)) ||
  fail 'RAG_CONTEXT_CHAR_BUDGET must be at least one passage and at most 24000'
((RAG_EXPANSION_CHAR_LIMIT <= 4000)) || fail 'RAG_EXPANSION_CHAR_LIMIT must be at most 4000'
((RAG_SOURCE_READ_CHAR_LIMIT >= 1000 && RAG_SOURCE_READ_CHAR_LIMIT <= 16000)) || fail 'RAG_SOURCE_READ_CHAR_LIMIT must be 1000..16000'
printf 'RAG pilot settings passed (reranker profile %s); prompt token count and total corpus size still require measurement\n' "$RAG_RERANKER_PROFILE"
