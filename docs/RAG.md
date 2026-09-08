# Local RAG pilot and context experiments

Reviewed on 2026-09-08. This is an opt-in, CPU-only Open WebUI pilot for the
Threadripper workstation. It does not start workloads, ingest documents, create
accounts or contact a cluster automatically. Physical testing is pending.

Retrieval-augmented generation (RAG) supplies selected source passages to a model.
It does not retrain the model, guarantee correct answers or enlarge its context
window. Persistent agent memory stores decisions across sessions. A knowledge
graph stores relationships. A KV/prefix cache reuses inference computation;
it is not a substitute for any of those stores.

## Decisions and follow-ups

| Candidate | Decision for this tranche | Evidence or acceptance condition |
| --- | --- | --- |
| Hybrid RAG over project docs/runbooks | Implement a disabled pilot with semantic retrieval and BM25; use source-hashed uploads | [Open WebUI RAG](https://docs.openwebui.com/features/chat-conversations/rag/); compare exact identifiers and conceptual questions |
| Local Chroma | Reuse the embedded database with one worker and one replica at most | [Open WebUI deployment guidance](https://docs.openwebui.com/getting-started/essentials/); no separate database service needed for this pilot |
| PostgreSQL/pgvector | Next candidate when chat and IDE tools need a shared store | [pgvector](https://github.com/pgvector/pgvector); keep structured benchmark numbers and provenance alongside vectors, with separate application-owned schemas |
| Qdrant | Alternative to pgvector, not an additional default service | Open WebUI currently treats its connector as non-core; qualify the connector before upgrades |
| Cross-encoder reranking | Deferred until the baseline has measured retrieval failures | Pin a model and test citation/retrieval accuracy versus added latency; no cross-encoder is loaded in this pilot |
| Qwen3-Embedding-0.6B | Replace MiniLM for the disabled CPU pilot | [Author model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B); use the native query prefix, 1,024 dimensions and a new index |
| Persistent project memory | Design notes only; no automatic memory writer or IDE endpoint | Store approved decisions with source, date, scope, supersession and deletion. Do not treat generated summaries as measured facts |
| GraphRAG | Defer until questions need relationships across sources | [Microsoft GraphRAG](https://microsoft.github.io/graphrag/); test multi-document relationship questions before accepting the indexing cost |
| PDF/diagram ingestion | Defer OCR, Docling and multimodal models; pilot accepts Markdown/text | Qualify extraction and bound peak ingestion RAM before adding document types |
| Prefix/HiCache | Separate experiment; do not enable host KV offload here | [SGLang HiCache](https://docs.sglang.io/docs/advanced_features/hicache_best_practices); verify the pinned AMD backend and measure cache hits, RAM, bandwidth and time to first token |

IDE agents will need an authenticated retrieval-tool integration. This pilot
does not expose raw database access or add a new agent framework. A local
database does not keep snippets local if an agent sends them to a cloud model.

## Inputs and configuration

Canonical sources are `apps/overlays/rag/`, `lib/workstation/rag.sh` and
`versions.lock`. The `rag` overlay extends `dual-gpu`, changes only Open WebUI
and adds its configuration and two PVCs. Other overlays are unchanged.

- Open WebUI: **0.11.3**, source commit
  `2a960a59fe1dbbd35282f0556b3666d81102e781`. The Linux/amd64 image manifest is
  digest-pinned; its registry configuration identifies the same source revision.
- Embeddings: `Qwen/Qwen3-Embedding-0.6B`, revision
  `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1,024 dimensions, CPU. The
  allowlisted safetensors, tokenizer and configuration files have individual
  SHA-256 hashes in `SHA256SUMS`. No pickle weights or remote model code are
  enabled. The model is approximately 1.2 GB; model staging has a 1.25 GiB
  per-file and 1.4 GB total input bound.
- Licensing: retain the upstream Open WebUI branding/license and the model's
  Apache-2.0 attribution. No dependency manager or host ROCm package changes.

The settings were checked against the pinned
[configuration source](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/config.py),
[retrieval implementation](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/routers/retrieval.py)
and [image recipe](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/Dockerfile).
Do not assume settings from a later documentation release work with this pin.

| Resource or setting | Pilot baseline |
| --- | --- |
| Open WebUI resources | Request 2 CPUs / 4 GiB; limit 2 CPUs / 6 GiB; no GPU request |
| Workers and threads | One Uvicorn worker; two BLAS/OpenMP threads; embedding batch 1 (qualify 2 separately) |
| Chunking | Local Qwen tokenizer, 192 tokens with 32 overlap; retain this initial comparison shape |
| Retrieval | Three hits per retriever and three final hits per merged query; no full-document context bypass |
| Uploads | One file per request, up to 2 MiB, `md` or `txt`; start with one small knowledge collection |
| Persistent data | `webui-rag-qwen3-data`: 20 GiB requested for the new chat DB, source uploads, Chroma and caches |
| Model assets | `webui-rag-qwen3-models`: 3 GiB requested, read-only to init/application containers |
| Startup assets | Disk-backed, bounded 64 MiB static assets and 4 MiB tiktoken cache, copied from the pinned image as UID 1000 |

The application remains Burstable, not CPU-Manager-exclusive. The init container
validates pilot settings and model hashes before startup. Missing assets stop
startup rather than causing an online download. The copied tiktoken cache avoids
hiding the image's preloaded tokenizer behind an empty data PVC. The embedding
splitter uses the staged Qwen tokenizer, not tiktoken. Open WebUI's pinned
retrieval code applies `RAG_EMBEDDING_QUERY_PREFIX` to query embeddings; the
deployment supplies Qwen's native `Instruct: ...\nQuery:` prefix as an actual
newline value. Documents have no instruction/content prefix.

These upload and batch controls are not a global ingestion queue, corpus quota
or prompt-token limiter. Serialize uploads during the pilot. Chroma's application
hybrid path can read a collection into RAM for BM25. Limit the pilot to 20 MiB of
reviewed source text and measure peak memory before expanding it. A short file
can still be pathological; stop on OOM/restart or sustained pressure.

The dual-GPU serving profile has a 32,768-token context. First smoke-test RAG at
4,096 tokens, then qualify 32,768 separately. Do not compare results across
those two serving contexts as if they were equivalent. Embedding tokens are not
serving-model tokens. Measure the final prompt with the serving model's tokenizer,
including instructions, citations, history and answer allowance. Use a fresh
chat per evaluation question. Do not claim that the chunk settings enforce a
hard end-to-end context budget.

On the 64 GiB/two-DIMM host, stop compilation and unneeded AI workloads during
bulk ingestion. Budget RAG's 6 GiB limit alongside host/K3s reserves and model
allocations. The reviewed dual-GPU serving limit is 38 GiB, so its 38 GiB plus
RAG's 6 GiB uses the 44 GiB workload budget, preserving 2 GiB for other pods
inside the planner's 46 GiB allocatable budget after the 18 GiB host reserve.
This is an admission cap, not evidence that the CPU embedding load fits every
concurrent workload. Do not enable every scaffold at once. No swap, RAM-backed
corpus, GPU sharing or new ROCm dependency is introduced. Rediscover trained
speed and topology after the four-DIMM upgrade, then repeat the worker/batch
sweep.

## Prepare without installing or deploying

Run from the full reviewed checkout. Each output path must be new. The model
command performs bounded HTTPS downloads of approximately 1.2 GB; verification
and corpus preparation are local. No authentication token is needed for this
model. It never runs at install time.

```sh
./bin/workstationctl rag stage-models artifacts/rag-models-01
./bin/workstationctl rag verify-models artifacts/rag-models-01
./bin/workstationctl rag corpus artifacts/rag-corpus-01 \
  docs/AI-PERFORMANCE.md docs/ROCM.md docs/HOME-LAB.md
kubectl kustomize apps/overlays/rag > artifacts/rag-pilot.yaml
bash tests/test_rag.sh
```

`stage-models` writes `provenance.json` only after checksum verification. A failed
download leaves partial output for inspection; use a new directory to retry.
`verify-models` always uses the repository's checksums, not a checksum file from
an untrusted download directory.

`corpus` accepts only explicitly named repository `docs/*.md` files, rejects
escaping paths/symlinks and limits each preparation to 20 files / 20 MiB. It
copies the document bytes without inserting instructions. Filenames retain the
source path and full content hash; `corpus.json` records those identities, the
capture time and the lock hash. Git HEAD is nullable and is not a claim that the
worktree was clean. This is provenance, not secret detection. Review every
selected document; do not upload credentials, customer data or private incident
material without authority. `corpus.json` is a sidecar record, not automatically
enforced vector metadata or access control.

## Workstation qualification procedure — NOT RUN

Preconditions: installed/qualified SGLang and its model, reviewed private ingress,
working storage/backups and sufficient memory headroom. Do not run the OS
installer or relax namespace security to enable RAG.

1. Render the overlay above. Review its image digest, zero replicas, unchanged
   NetworkPolicies and retained authentication/signup settings. Recheck pins
   and upstream security notices before the first deployment.
2. Through the existing operator-controlled storage/deployment process, provision
   `webui-rag-qwen3-models` and copy the staged `Qwen3-Embedding-0.6B` directory to that
   volume's root. Preserve the directory name. Make the files readable by UID/GID
   1000; do not make the model mount writable to the application. Validate the
   transferred bytes with `rag verify-models` against the mounted volume path.
   Local-path may defer provisioning until a consumer is scheduled; populate
   through a reviewed temporary volume consumer if required. No downloader Job
   or internet egress exception is supplied by this overlay.
3. Bootstrap the administrator on the new `webui-rag-qwen3-data` database using the
   isolated process in [HOME-LAB.md](HOME-LAB.md). Existing accounts are not copied.
   Set up restricted test accounts and test collection access before exposure.
4. Enable only the qualified application through the existing maintenance
   process. Confirm the init container accepts inputs, the process runs as UID
   1000, health/readiness pass and embeddings load without network access. Keep
   other unqualified replicas at zero. Ordinary tests do not perform this step.
5. Upload only `documents/*.md` from the prepared corpus, one file at a time, to
   a new private knowledge collection. Save `corpus.json` beside the test report.
   Use a collection name containing the capture date/revision and select only
   that collection for the run; do not mix superseded versions silently.
6. Confirm indexed passages have the expected source/hash filename and usable
   citations. Check exact error names as well as paraphrased queries. Confirm
   a second restricted account cannot retrieve the private collection. Treat
   retrieved instructions as untrusted document content, not tool authority.
7. Restart through maintenance and verify document/index persistence. Recheck
   offline loading and perform an off-array backup/restore test while stopped.

Settings in `rag.env` take precedence on restart because
`ENABLE_PERSISTENT_CONFIG=false`. This is deliberate and applies to all
Open WebUI persistent settings, not just RAG. Admin UI changes are not a durable
configuration source in this pilot. Export/review needed settings before reuse;
never point this profile at an existing WebUI database without a migration review.
See [configuration precedence](https://docs.openwebui.com/reference/env-configuration/).

## Paired evaluation and acceptance

Start with `tests/fixtures/rag/questions.json`, then expand to roughly 50 reviewed
questions. The checked-in answers are expectations for the document snapshot,
not observed model outputs. Update them deliberately when the source changes.
Add literal identifiers, paraphrases, obsolete-version traps, no-evidence
questions, restricted-document checks and malicious instructions in test data.

For the dense comparison, `rag-dense` changes only
`ENABLE_RAG_HYBRID_SEARCH=false`. Both profiles use `RAG_TOP_K=3` and
`RAG_TOP_K_RERANKER=3`; the general limit also bounds the upstream merge across
collections. Render both without contacting a cluster:

```sh
kubectl kustomize apps/overlays/rag-dense > artifacts/rag-dense.yaml
kubectl kustomize apps/overlays/rag > artifacts/rag-hybrid.yaml
```

Activate only one reviewed composition through maintenance; both select the
same Deployment and pilot PVCs, not independent concurrent test services.
Keep image, model, corpus, chunking, prompt, generation settings and question
order matched. Pin the qualified SGLang model and record its revision/quantization.
Clear conversation history between questions, not the stored documents.

Warm up both variants, alternate their order and repeat each question at least
three times. Separate first-load/cold-cache results from warmed queries. Record:

- Retrieved source IDs; recall@3 against labelled relevant passages; whether
  every factual answer claim has a correct supporting citation; correct abstention.
- Retrieval time, end-to-end time to first token and total answer time; median,
  p95 and min/max across repetitions. Keep failed and timed-out runs.
- Ingestion time, peak host/pod RAM, restart/OOM counts, CPU load, disk usage and
  serving-model VRAM; temperatures/power where existing telemetry supplies them.
- Image/source/model hashes, corpus manifest, rendered profile, software versions,
  question IDs, generation settings, cache state and concurrency.

Keep hybrid search only if retrieval/citation results justify its latency and RAM.
Next, compare a pinned CPU cross-encoder, then a shared pgvector store if another
client needs it. Changing embeddings, dimensions or chunking requires a new
index/reingestion; do not query old vectors with new embeddings. A memory/graph
experiment must beat this baseline on its intended questions. No published
percentage is an expected local gain.

## Rollback and retention

Keep the previous rendered deployment and configuration before activation.
Stop the pilot through maintenance, restore that reviewed configuration and
leave unqualified replicas at zero. The original `webui-data` PVC is unchanged.
The previous MiniLM `webui-rag-data` and `webui-rag-models` PVCs are retained;
the Qwen overlay intentionally uses distinct PVCs and requires a new index and
reingestion. Do not delete either generation of pilot PVCs to switch profiles:
the StorageClass may use `Delete` reclaim policy. Retain/export useful sources
and results first.

PVC sizes are advisory, not filesystem quotas. Monitor the 20 GiB data and
3 GiB asset budgets. Use the existing off-array NAS/Restic backup approach;
quiesce SQLite/Chroma for a consistent copy. Never run automatic cleanup over
the original documents, vector database or shared model directory.

## Local verification record — 2026-09-08

- `bash -n lib/workstation/rag.sh apps/overlays/rag/verify-models.sh
  apps/overlays/rag/validate-profile.sh tests/test_rag.sh`: passed.
- `bash tests/test_rag.sh`: passed. It exercises the Qwen lock, native query
  prefix in rendered manifests, distinct PVCs, CPU resource envelope, synthetic
  staging/corruption, model-path symlink rejection and RAG/dense isolation.
- `kubectl kustomize apps/overlays/rag` and `apps/overlays/rag-dense`: passed
  through the focused test; no cluster access occurred.
- Hugging Face metadata and the small pinned files were checked over HTTPS. The
  1,191,586,416-byte `model.safetensors` SHA-256 is pinned from the revision's
  LFS metadata; its weights were not downloaded.

NOT RUN: `rag stage-models`, `rag verify-models` against real model files, Open
WebUI container startup, embedding execution, retrieval/answer evaluation,
account isolation, persistence/restore on K3s and physical RAM/GPU performance.
Keep replicas at zero until the workstation qualification procedure passes;
source validation is not promotion.
