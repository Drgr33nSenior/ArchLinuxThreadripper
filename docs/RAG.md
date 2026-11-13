# Local RAG pilot and context experiments

This is an opt-in, CPU-only Open WebUI pilot for the
Threadripper workstation. It does not start workloads, ingest documents, create
accounts or contact a cluster automatically. An isolated workstation test of
Open WebUI 0.12.5 with CPU Qwen embeddings passed its API, access-control and
application-restart checks. Retrieval misses remain; see the
[verification record](VERIFICATION.md#webui-retrieval-evidence). This does not
qualify the deployed assistant or generated answers.

The assistant overlays add a pinned CPU cross-encoder reranker and an exact
source-read tool to this retrieval path; see
[Assistant reranker profile](#assistant-reranker-profile). The `rag`, `rag-dense`
and `service` overlays keep the baseline profile described below. The assistant
overlays, including the live `assistant-memory` shared render, run the reranker.

The optional [disposable Linux retrieval test](../tests/rag/README.md) exercises
real CPU embeddings, retrieval, access revocation and same-volume
restart without using the workstation or starting a language model. It is
separate from workstation, browser and generated-answer qualification.

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
| Cross-encoder reranking | Assistant overlays only: pinned `cross-encoder/ms-marco-MiniLM-L6-v2` on CPU; `rag`/`service` keep the baseline | [Assistant reranker profile](#assistant-reranker-profile); measure citation/retrieval accuracy against added latency before relying on it |
| Qwen3-Embedding-0.6B | Replace MiniLM for the disabled CPU pilot | [Author model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B); use the native query prefix, 1,024 dimensions and a new index |
| Persistent project memory | [Graphiti is primary](MEMORY.md); native 6.0.3 storage fixtures passed, shared integration remains planned | Compare with native client memory plus a compact handoff; qualify real-model corrections/recall, authenticated isolation and measured resource use |
| GraphRAG | Optional on-demand [document-analysis pilot](GRAPHRAG.md), standard indexing/global search with local completions | Retain cross-document comparisons; Graphiti is the preferred memory pilot. No standing graph service or installation gate |
| PDF/diagram ingestion | Defer OCR, Docling and multimodal models; pilot accepts Markdown/text | Qualify extraction and bound peak ingestion RAM before adding document types |
| Prefix/HiCache | Separate experiment; do not enable host KV offload here | [SGLang HiCache](https://docs.sglang.io/docs/advanced_features/hicache_best_practices); verify the pinned AMD backend and measure cache hits, RAM, bandwidth and time to first token |

IDE agents will need an authenticated retrieval-tool integration. This pilot
does not expose raw database access or add a new agent framework. A local
database does not keep snippets local if an agent sends them to a cloud model.

The [continuity decision](MEMORY.md) separates evolving user/project memory,
active task handoffs, public Exa evidence and exact local document retrieval.
Graphiti's small storage fixture does not qualify its shared integration or
production footprint. It is not assumed to be smaller than this file-based
GraphRAG experiment.

## Programming books and later model adaptation

Use programming books as a private reference corpus first. A 27B model's size
does not establish whether it knows a particular book, recalls its examples
accurately or distinguishes editions and dependency versions. Retrieved source
passages can supply that evidence without changing model weights.

Start with one chapter and representative coding questions. Compare the same
model without retrieval and with hybrid retrieval; add GraphRAG as an optional
cross-document comparison. Prefer exact source passages for code, API details
and version-specific instructions. Use
the graph comparison for relationships across chapters, authors or design
principles. The current GraphRAG pilot tests global community-report search;
it does not implement GraphRAG local search or replace original code with a
verified executable representation. See [GraphRAG query methods](https://microsoft.github.io/graphrag/query/overview/).

Kindle ingestion is planned, not implemented. Obtain a readable EPUB, PDF or
text copy before conversion. Amazon permits EPUB/PDF downloads for eligible
purchased DRM-free titles when the publisher enables that option; check
Manage Your Content and Devices or the publisher's download service. This
availability does not apply to every Kindle title. See
[Amazon's download guidance](https://kdp.amazon.com/en_US/help/topic/GDDXGH9VR22ACM8U).
Neither pilot reads a Kindle account or imports a Kindle library directly.

Before a chapter enters either corpus:

1. Review the selected content and its permitted use. Keep the book, extracted
   text and derived index within the owner's private collection.
2. Convert to Markdown/text and inspect headings, code indentation, tables,
   equations and diagram references. Preserve complete code examples and their
   surrounding explanation; record extraction omissions.
3. Retain title, author, edition, chapter/section, applicable language/library
   versions, source hash and access scope with each extract. Do not merge
   conflicting editions without identifying them.
4. Prepare a new explicit corpus through a reviewed ingestion/profile change.
   The GraphRAG pilot currently requires its three project documents and caps
   the corpus at 8 Markdown inputs / 64 KiB. Whole-book or EPUB/PDF ingestion
   needs separate implementation and resource qualification.

Evaluate supported answers, usable citations, executable code where relevant,
version accuracy, elapsed time, prompt size, indexing calls and peak memory.
Retrieving a few passages can reduce context compared with supplying complete
books. Retrieval and graph construction also add work; faster inference or
better answers are not established by ingestion alone.

QLoRA remains optional post-install work. It trains adapters through a frozen
quantized base model; it is not a document index or a guarantee of exact factual
recall. For personalisation, first collect reviewed coding tasks, tested
solutions, useful corrections and project conventions. Book passages can
support those examples. Training on raw prose is possible, but does not by
itself specify the desired coding-assistant behaviour. Keep evaluation tasks
separate from the training examples. Retrieval and adaptation can complement
one another; [RAFT](https://arxiv.org/abs/2403.10131) studies training models to
use retrieved evidence. See also the [QLoRA paper](https://arxiv.org/abs/2305.14314).

Qualify a separate training environment, quantization format, adapter-serving
path and memory budget before training. The native Quark AWQ MXFP4 inference
candidate is not an already-qualified QLoRA training checkpoint. No training,
dataset generation or ingestion runs during installation.

## Inputs and configuration

Canonical sources are `apps/overlays/rag/`, `lib/workstation/rag.sh` and
`versions.lock`. The `rag` overlay extends `dual-gpu`, changes only Open WebUI
and adds its configuration and two PVCs. Other overlays are unchanged.

- Open WebUI: **0.13.3** for every overlay (`rag`/`service` and the assistant
  overlays), source commit `b0e6098e940942281d9c83f3529ea117287a3a22`, Linux/amd64
  manifest `sha256:e731360d9e6eeb25482d90cf3d564ce81a1c4a10e6a84ed9e6c63aa7ac48b0b6`,
  recorded in `versions.lock` and digest-pinned in
  `apps/overlays/rag/kustomization.yaml` and
  `apps/components/assistant/kustomization.yaml` (13 November 2026; previously
  0.11.4 and 0.12.5). The reranker and speech-to-text profiles were reviewed
  against the 0.12.5 source; 0.13.3 ships the same sentence-transformers (5.5.1),
  faster-whisper (1.2.1), transformers, torch and chromadb versions, and its
  health, knowledge-listing, model-import and API-key rules are unchanged.
- Reranker (assistant overlays only): `cross-encoder/ms-marco-MiniLM-L6-v2`,
  revision `233902d25c440f23af6f7d6e94d2946bac0bee0a`, Apache-2.0, CPU; per-file
  SHA-256 hashes in `RERANKER-SHA256SUMS`. See
  [Assistant reranker profile](#assistant-reranker-profile).
- Embeddings: `Qwen/Qwen3-Embedding-0.6B`, revision
  `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`, 1,024 dimensions, CPU. The
  allowlisted safetensors, tokenizer and configuration files have individual
  SHA-256 hashes in `SHA256SUMS`. No pickle weights or remote model code are
  enabled. The model is approximately 1.2 GB; model staging has a 1.25 GiB
  per-file and 1.4 GB total input bound.
- Licensing: retain the upstream Open WebUI branding/license and the model's
  Apache-2.0 attribution. No dependency manager or host ROCm package changes.

The settings were checked against the pinned
[configuration source](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/config.py),
[retrieval implementation](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/routers/retrieval.py)
and [image recipe](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/Dockerfile).
Do not assume settings from a later documentation release work with this pin.

The 0.11.4 update addresses the upstream
[stale knowledge-file access](https://github.com/open-webui/open-webui/security/advisories/GHSA-9cxp-636w-4997)
and [foreign directory disclosure](https://github.com/open-webui/open-webui/security/advisories/GHSA-jx3w-3pqq-7f3w)
advisories affecting the previous 0.11.3 pin. Keep the pilot private; this patch
and its regression checks do not constitute a complete security audit.

As of 1 November 2026, upstream's current release is 0.12.5. The retained 0.11.4
test candidate is also affected by the later
[channel-message impersonation advisory](https://github.com/open-webui/open-webui/security/advisories/GHSA-3vvh-g22g-jq6r),
fixed in 0.12.2. This network-isolated fixture has no browser/channel traffic.
Keep deployment replicas at zero; review and qualify a current release before
exposing the application. Do not treat this historical-profile test as security
clearance for 0.11.4.

| Resource or setting | Pilot baseline |
| --- | --- |
| Open WebUI resources | Request 2 CPUs / 4 GiB; limit 2 CPUs / 6 GiB; no GPU request |
| Workers and threads | One Uvicorn worker; two BLAS/OpenMP threads; embedding batch 1 (qualify 2 separately) |
| Chunking | Local Qwen tokenizer, 192 tokens with 32 overlap; retain this initial comparison shape |
| Retrieval | Baseline: three hits per retriever and three final hits per merged query. Assistant: ten per retriever, up to 20 reranked, five final. No full-document context bypass |
| Uploads | One file per request, up to 8 MiB, `md` or `txt`; start with one small knowledge collection. Open WebUI applies the same size cap to files a tool returns, such as a chat video clip |
| Persistent data | `webui-rag-qwen3-data`: 20 GiB requested for the new chat DB, source uploads, Chroma and caches |
| Model assets | `webui-rag-qwen3-models`: 3 GiB requested, read-only to init/application containers |
| Startup assets | Disk-backed, bounded 64 MiB static assets and 4 MiB tiktoken cache, copied from the pinned image as UID 1000 |

The application remains Burstable, not CPU-Manager-exclusive. The init container
validates pilot settings and model hashes before startup. Missing assets stop
startup rather than causing an online download. The copied tiktoken cache avoids
hiding the image's preloaded tokenizer behind an empty data PVC. Its writable
mount is `/runtime-tiktoken`, selected by `TIKTOKEN_CACHE_DIR`. Keep it outside
the data mount: a runtime-created nested mount parent can be root-owned and
prevent UID 1000 from creating other application caches. The embedding
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

Historical 64 GiB admission example: stop compilation and unneeded AI workloads during
bulk ingestion. Budget RAG's 6 GiB limit alongside host/K3s reserves and model
allocations. The reviewed dual-GPU serving limit is 38 GiB, so its 38 GiB plus
RAG's 6 GiB uses the 44 GiB workload budget, preserving 2 GiB for other pods
inside the planner's 46 GiB allocatable budget after the 18 GiB host reserve.
This is an admission cap, not evidence that the CPU embedding load fits every
concurrent workload. Do not enable every scaffold at once. No swap, RAM-backed
corpus, GPU sharing or new ROCm dependency is introduced.

The current installed capacity is 64 GB. Recollect DIMM population, trained
speed, topology and MemTotal. Keep the 6 GiB cap
and current ingestion concurrency until a separate worker/batch sweep measures
pressure alongside the service budget. The authenticated read-only MCP path in
[AI-SERVICE.md](AI-SERVICE.md) reuses this store and its access checks; no second
vector database or automatic project-memory writer is introduced.

## Prepare without installing or deploying

Run from the full reviewed checkout. Each output path must be new. The model
command performs bounded HTTPS downloads of approximately 1.2 GB; verification
and corpus preparation are local. No authentication token is needed for this
model. It never runs at install time.

```sh
./bin/workstationctl rag stage-models artifacts/rag-models-01
./bin/workstationctl rag verify-models artifacts/rag-models-01
./bin/workstationctl rag stage-reranker artifacts/rag-reranker-01
./bin/workstationctl rag verify-reranker artifacts/rag-reranker-01
./bin/workstationctl rag stage-whisper artifacts/stt-whisper-01
./bin/workstationctl rag verify-whisper artifacts/stt-whisper-01
./bin/workstationctl rag corpus artifacts/rag-corpus-01 \
  docs/AI-PERFORMANCE.md docs/ROCM.md docs/HOME-LAB.md
kubectl kustomize apps/overlays/rag > artifacts/rag-pilot.yaml
bash tests/test_rag.sh
```

`stage-models` writes `provenance.json` only after checksum verification. A failed
download leaves partial output for inspection; use a new directory to retry.
`verify-models` always uses the repository's checksums, not a checksum file from
an untrusted download directory. `stage-reranker`/`verify-reranker` follow the
same contract for the six reranker files (about 92 MB, 100 MB staging bound)
against `apps/overlays/rag/RERANKER-SHA256SUMS`.

`stage-whisper`/`verify-whisper` do the same for local voice input: the four
`Systran/faster-whisper-base` files at the revision pinned in `versions.lock`
(about 148 MB, 160 MB staging bound, MIT) against
`apps/overlays/rag/WHISPER-SHA256SUMS`. Copy `faster-whisper-base/` beside
`Qwen3-Embedding-0.6B/` on the models volume.
- Open WebUI loads it with `WHISPER_MODEL=/rag-models/faster-whisper-base`: a
  directory, so faster-whisper reads it from disk on the CPU (int8) with no Hub
  lookup, and audio never leaves the workstation.
- The init container verifies it whenever the directory exists. Before it is staged,
  Open WebUI still starts and voice input reports the missing model.
- The assistant experience allows the microphone for every account
  (`USER_PERMISSIONS_CHAT_STT=true`).

`corpus` accepts only explicitly named repository `docs/*.md` files, rejects
escaping paths/symlinks and limits each preparation to 20 files / 20 MiB. It
copies the document bytes without inserting instructions. Filenames retain the
source path and full content hash, for example
`docs__RAG.md--rev-<commit>--<sha256>.md`. The `--rev-<commit>` part is present
only when the copied bytes equal the file's blob at HEAD; it names the last
commit that changed the file. Dirty, untracked or Git-less copies omit it.
`corpus.json` records those identities (`git_revision` per source), the capture
time and the lock hash. Git HEAD is nullable and is not a claim that the
worktree was clean. This is provenance, not secret detection. Review every
selected document; do not upload credentials, customer data or private incident
material without authority. `corpus.json` is a sidecar record, not automatically
enforced vector metadata or access control.

## Workstation qualification procedure

Preconditions: installed/qualified SGLang and its model, reviewed private ingress,
working storage/backups and sufficient memory headroom. Do not run the OS
installer or relax namespace security to enable RAG.

1. Render the overlay above. Review its image digest, zero replicas, unchanged
   NetworkPolicies and retained authentication/signup settings. Recheck pins
   and upstream security notices before the first deployment.
2. Through the existing operator-controlled storage/deployment process, provision
   `webui-rag-qwen3-models` and copy the staged `Qwen3-Embedding-0.6B` directory to that
   volume's root. For the assistant overlays, also copy the staged
   `ms-marco-MiniLM-L6-v2` directory beside it. Preserve the directory names.
   Make the files readable by UID/GID 1000; do not make the model mount
   writable to the application. Validate the
   transferred bytes with `rag verify-models` (and `rag verify-reranker`) against
   the mounted volume path. The 3 GiB request covers both models; no resize.
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

## Assistant reranker profile

`apps/components/rag-reranker` merges `reranker.env` over `rag.env` in the
`assistant` and `assistant-split` overlays (the memory overlays inherit it). Open
WebUI, browser chat and the service-tools MCP tools read the same merged
`webui-rag-profile` ConfigMap. Source checks render it; it has not been loaded,
measured or qualified on the workstation.

### Model

| Item | Value |
| --- | --- |
| Model | [`cross-encoder/ms-marco-MiniLM-L6-v2`](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2), revision `233902d25c440f23af6f7d6e94d2946bac0bee0a` (`versions.lock` `RAG_RERANKER_*`) |
| Licence | Apache-2.0 (model card); English MS MARCO passage ranking |
| Architecture | `BertForSequenceClassification`, 6 layers, 384 hidden, about 22.7 M parameters, one logit |
| Files | `config.json`, `model.safetensors` (90,870,598 bytes), `special_tokens_map.json`, `tokenizer.json`, `tokenizer_config.json`, `vocab.txt`; SHA-256 in `apps/overlays/rag/RERANKER-SHA256SUMS`. No pickle weights, ONNX/OpenVINO variants or remote code |
| Runtime path | `/rag-models/ms-marco-MiniLM-L6-v2` on the existing `webui-rag-qwen3-models` volume |
| Scoring | Open WebUI scores each (query, passage) pair with `CrossEncoder.predict`; sigmoid of the logit, 0 to 1 |
| Pair limit | 512 WordPiece tokens for query plus passage, fixed by the hash-pinned `tokenizer_config.json`; longer pairs are truncated. Open WebUI 0.12.5 has no max-length setting |

Open WebUI 0.12.5 (`routers/retrieval.py` `get_rf`) loads a local model with
`sentence_transformers.CrossEncoder(path, device="cpu", trust_remote_code=…,
backend="torch", activation_fn=Sigmoid())`. `get_model_path` returns an existing
local path unchanged, and `OFFLINE_MODE` forces local files. The pinned
sentence-transformers 5.5.1 treats a model whose architecture ends in
`ForCausalLM` as a generic yes/no-token scorer without that model's prompt
template, so Qwen3-Reranker is not used through this path. BERT sequence
classification is the plain supported case. `BAAI/bge-reranker-base` (278 M
parameters) would cost roughly ten times more CPU per pair;
`mxbai-rerank-*-v1` (DeBERTa) needs additional tokenizer qualification. No
adapter is needed.

### Settings

| Setting | Baseline (`rag.env`) | Assistant (`reranker.env`) | Meaning in 0.12.5 |
| --- | --- | --- | --- |
| `RAG_RERANKER_PROFILE` | `none` | `ms-marco-minilm-l6-v2` | Named mode enforced by `validate-profile.sh` and reported by service-tools |
| `RAG_RERANKING_MODEL` | empty | `/rag-models/ms-marco-MiniLM-L6-v2` | Loaded at startup only while `ENABLE_RAG_HYBRID_SEARCH=true` |
| `RAG_TOP_K` | 3 | 10 (max 16) | BM25 top-k and vector top-k; their deduplicated union, at most 2 × k, is scored. Also the cross-collection merge limit |
| `RAG_TOP_K_RERANKER` | 3 | 5 (max 8, at most `RAG_TOP_K`) | Final passages per collection after scoring |
| `RAG_RELEVANCE_THRESHOLD` | 0.0 | 0.0 | Drops scores below the value before the final cut; upstream skips 0 |
| `RAG_RERANKING_BATCH_SIZE` | unset | 8 (1..32) | Pairs per forward pass |
| `OMP/MKL/OPENBLAS_NUM_THREADS` | 2 | 2 | Shared by embeddings and reranking in the one WebUI process |
| `RAG_PASSAGE_CHAR_LIMIT` | 1600 | 1600 | MCP text per passage (characters) |
| `RAG_CONTEXT_CHAR_BUDGET` | 4800 | 8000 | MCP text across all returned passages |
| `RAG_EXPANSION_CHAR_LIMIT` | 800 | 800 | MCP neighbouring text per side for `expand` |
| `RAG_SOURCE_READ_CHAR_LIMIT` | 8000 | 8000 | Maximum `read_project_source` excerpt |

Open WebUI keeps `min(RAG_TOP_K, RAG_TOP_K_RERANKER)` per collection and merges
several collections to `RAG_TOP_K`. Browser chat queries each attached knowledge
item separately, so each item contributes up to five passages. The model runs
in the WebUI process on CPU; no GPU is requested. The application container keeps
its 2 CPU / 4 GiB request and 2 CPU / 6 GiB limit: the model adds about 91 MB of
weights plus activations for at most 8 pairs of 512 tokens. That estimate is
unmeasured; record peak memory and query latency during qualification. The init
container's limit rises to 2 GiB for the offline load check, still below the
application limit, so the admitted pod budget is unchanged.

Token and character limits are separate. Chunks are 192 Qwen embedding tokens.
Reranker pairs are at most 512 MiniLM tokens. Browser chat inserts whole chunks
(up to five per knowledge item) and has no character budget. MCP results are
bounded in characters by the four `RAG_*_CHAR_*` settings. None of these is the
serving model's context; measure the final prompt with the serving tokenizer.

### Startup checks and failure behaviour

The `rag-inputs` init container validates the profile, verifies the embedding
files and, when the profile is not `none`, verifies the six reranker files with
`verify-models.sh … reranker` and runs `check-reranker.py`. The script loads the
model with the same constructor arguments as Open WebUI, offline, checks the
512-token limit and scores a relevant pair above an irrelevant one. It prints one
JSON line (`kubectl logs <pod> -c rag-inputs`).

| Condition | Behaviour | Where it is visible |
| --- | --- | --- |
| Missing, symlinked or altered reranker file | Init container fails, naming the file; WebUI does not start | Init logs; MCP calls return `503 retrieval_unavailable` |
| Model or runtime cannot load or score | Init container fails with a fixed token such as `reranker_load_failed` | Init logs |
| Load fails in WebUI after the init check passed (for example OOM at startup) | Upstream logs `CrossEncoder: …` and `Error updating models`, then continues with embedding-cosine scoring | WebUI logs only. Not detectable by service-tools; check the logs after each start |
| Reranker error during an MCP query | WebUI returns 400; the tool fails with `503 retrieval_unavailable`. No fallback | `workstation_assistant_retrieval_requests_total{outcome="unavailable"}` |
| Reranker error during a browser chat query | Upstream falls back to vector-only search with `RAG_TOP_K` (10) passages | WebUI debug log only |
| `none` profile (rollback) | No cross-encoder; deliberate, documented mode | Tool result `retrieval.fallback: disabled_by_profile`; `workstation_assistant_reranker_fallback_total{reason="disabled_by_profile"}` per call; state gauge `disabled` |
| Profile keys invalid or inconsistent in service-tools | Results are labelled; WebUI's own validation refuses the same profile | State gauge `incompatible`; `reranker_fallback_total{reason="unavailable"}` |

The two upstream fallbacks in the table are Open WebUI behaviour, not settings
in this repository. Only WebUI logs show them. The service-tools MCP path never
retries or degrades on its own.

service-tools `/health` includes a `retrieval` object (profile, state, fallback,
score kind, counts and any invalid profile keys), and `/metrics` sets
`workstation_assistant_reranker_state{state}` from the mounted profile. That
state reflects configuration plus the init-container gate; it is not a probe of
the loaded model.

### MCP tools

`retrieve_project_passage(collection, query, expand?)` posts the query to
`/api/v1/retrieval/query/collection` without `k`, `k_reranker` or `r`, so Open
WebUI applies the same profile as browser chat. Each hit carries `rank`, `score`
and `score_kind` (`cross-encoder-sigmoid`, `embedding-cosine` or
`vector-similarity`), `file_id`, `source` (filename), `source_path`,
`git_revision`, `source_sha256`, `content_sha256`, `hash_verified`, `section`
(Markdown heading path), `location` (character offsets and line numbers),
`text_basis`, `truncated`, `incomplete`, `version_conflict`, `historical`,
`content_trust` and `citation`.

`snippet` is the exact source text when the chunk is found in the file. Open
WebUI's Markdown splitter strips indentation and heading metadata from stored
chunks, so the stored chunk is returned only as a fallback
(`text_basis: index-chunk`). `expand=neighbours` adds up to
`RAG_EXPANSION_CHAR_LIMIT` characters on each side, snapped to lines;
`expand=section` returns the enclosing heading section. Both are bounded by
`RAG_PASSAGE_CHAR_LIMIT + 2 × RAG_EXPANSION_CHAR_LIMIT` per hit and by
`RAG_CONTEXT_CHAR_BUDGET` overall. `historical` is always null: the index does
not record which version is current. `version_conflict` marks hits whose
`source_path` appears with different content hashes in the same result.

`read_project_source(file_id, offset?, limit?, section?)` returns an exact
excerpt of an authorised file's extracted text with the same identity fields,
`total_chars`, `offset`, `end`, line numbers and `next_offset`. `limit` is
1..`RAG_SOURCE_READ_CHAR_LIMIT`; `offset` and `section` are exclusive; the server
checks all bounds. Offsets count Unicode characters of the text Open WebUI
extracted, which equals the uploaded bytes for UTF-8 Markdown/text;
`hash_verified` confirms this for corpus files.

Access control applies at three points, always with the caller's restricted
WebUI API key:

1. Open WebUI checks read access to the requested collection before it retrieves
   or reranks anything. The cross-encoder runs inside WebUI on chunks of that
   collection only; no candidate text reaches service-tools before final
   selection.
2. service-tools re-reads each final hit's file (`GET /api/v1/files/{id}`) and
   drops 403/404 results before returning text.
3. Expansion and `read_project_source` use only that ACL-checked file record.

The tools share the existing two-request concurrency limit and 30 s deadline.
They count `workstation_assistant_retrieval_*{client="mcp"}` and
`workstation_assistant_source_reads_total`; browser chat retrieval runs inside
WebUI and is not counted there.

### Rollback to the baseline mode

Remove `../../components/rag-reranker` from the `components` list in
`apps/overlays/assistant/kustomization.yaml` and
`apps/overlays/assistant-split/kustomization.yaml`, render, and confirm that the
rendered `webui-rag-profile` equals the `service` render's profile
(`RAG_RERANKER_PROFILE=none`, three candidates, three final hits). Apply through
the existing maintenance process. Reranking does not change embeddings or chunks,
so no reindexing is needed in either direction; the staged reranker directory can
stay on the models volume.

## Paired evaluation and acceptance

Start with `tests/fixtures/rag/questions.json`, then expand to roughly 50 reviewed
questions. The checked-in answers are expectations for the document snapshot,
not observed model outputs. Update them deliberately when the source changes.
Add literal identifiers, paraphrases, obsolete-version traps, no-evidence
questions, restricted-document checks and malicious instructions in test data.

The open `rocm-version` and `storage-quota` misses are not missing content. The
`docs/HOME-LAB.md` answers are in its storage and ROCm 10 SGLang sections. The
retrieved `ROCM.md` opening shares the question's "native 10.2" and "checked-in"
wording but not the 10.0.0 answer. `docs/AI-PERFORMANCE.md` also states that
local-path PVC requests are not quotas, but that question labels only
`HOME-LAB.md`. Review both labels in a new frozen protocol version. Do not
relabel or tune to improve recorded scores.

For the dense comparison, `rag-dense` changes only
`ENABLE_RAG_HYBRID_SEARCH=false`. Both profiles use `RAG_TOP_K=3` and
`RAG_TOP_K_RERANKER=3`; the general limit also bounds the upstream merge across
collections. Render both without contacting a cluster:

```sh
kubectl kustomize apps/overlays/rag-dense > artifacts/rag-dense.yaml
kubectl kustomize apps/overlays/rag > artifacts/rag-hybrid.yaml
```

The global profile is what selects the mode. In the pinned release, a
request-level `hybrid=false` still runs hybrid search while the global setting is
true; see the [retrieval test](../tests/rag/README.md#frozen-protocol). Earlier
flag-only dense/hybrid records ran hybrid twice and are not a comparison. Record
each arm's config readback and query route with its results. Without a
cross-encoder, pinned hybrid search rescores BM25 and vector candidates by
embedding cosine similarity. It changes the top three only when a BM25-only
passage's recomputed cosine score exceeds a vector hit's score.

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
Compare the assistant cross-encoder profile with the baseline by the same
protocol (it changes candidate and final counts as well as scoring), then a shared
pgvector store if another client needs it. Changing embeddings, dimensions or chunking requires a new
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
and results first. The assistant reranker has its own
[rollback](#rollback-to-the-baseline-mode); it uses the same PVCs and index.

PVC sizes are advisory, not filesystem quotas. Monitor the 20 GiB data and
3 GiB asset budgets. Use the existing off-array NAS/Restic backup approach;
quiesce SQLite/Chroma for a consistent copy. Never run automatic cleanup over
the original documents, vector database or shared model directory.

## Qualification boundary

Source validation covers the lock, query bridge, bundle/refusal paths and RAG
profile rendering with synthetic fixtures. It does not stage real model files,
start Open WebUI or Chroma, ingest private content, establish retrieval quality,
or qualify resource use, network policy, backup or migration. Keep replicas at
zero until the workstation qualification procedure passes. The reranker profile,
its init-container load check and the MCP retrieval/source-read tools have the
same source-only status: the reranker has not been loaded in the pinned image,
and its latency, memory and retrieval effect are unmeasured.
