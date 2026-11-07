# Session continuity and persistent memory

Decision recorded on 3 November 2026: Graphiti is the **primary persistent-memory
component under qualification** for coding and home-assistant context. Keep Exa
for current public web evidence, local retrieval for private code/documents and
a compact project handoff for the active task. Microsoft GraphRAG remains an
optional, on-demand document-analysis experiment.

This is an architecture decision with partial pilot verification. An opt-in,
deployable memory profile now exists (see [Deploy memory](#deploy-memory-opt-in)).
It is inactive by default and is not yet a qualified client integration. There
is no background writer: memory is written only when a user explicitly asks
through the `remember` tool. The current 64 GB host budget, serving ownership
and installation/recovery gates still apply.

## Responsibilities

| Need | Selected direction | Current status |
| --- | --- | --- |
| Durable preferences, decisions and changing project facts | Graphiti on Neo4j Community, scoped by user | Opt-in `assistant-memory` profile packaged; Neo4j storage/restart test, model and client qualification pending |
| Current public web information | Existing Exa adapter | Source integration exists; full target acceptance remains separate |
| Exact code, private documentation and book passages | Repository tools and local hybrid retrieval | Existing retrieval path; book conversion remains planned |
| Active goal, constraints, last result and next steps | Short project handoff | Shared handoff storage/loading not implemented |
| Model execution and reusable prompt computation | SGLang and its prefix/state caches | Separate from semantic memory and retrieval |
| Questions across a document collection | Optional [GraphRAG pilot](GRAPHRAG.md) | Real-library fixture workflows passed; live Qwen quality/resource tests pending |

Graphiti fits evolving context because its graph records entities, relationships,
source episodes and temporal validity. It is a framework: the application must
provide ingestion, user/project isolation, access control and operations. Its
MCP server is experimental. It does not automatically connect browser and coding
sessions or make extracted facts correct. See the
[Graphiti overview](https://github.com/getzep/graphiti) and
[MCP documentation](https://help.getzep.com/graphiti/getting-started/mcp-server).

## Built-in baseline

Compare Graphiti with the memory already available in the pinned clients plus a
short handoff. Do not introduce a second store solely to duplicate those features.

- Open WebUI 0.11.4 (`service`) and 0.12.5 (`assistant`) include per-user memory,
  system-context injection and background review controls. Upstream background
  review defaults to off in both pins.
- Qwen Code 0.23.2 supports `AGENTS.md`/`QWEN.md` briefings and editable Markdown
  auto-memory. Upstream automatic extraction/consolidation defaults to on;
  team memory and its Git sync default to off. These are upstream capabilities,
  not observations of a configured or qualified client session.
- The two clients do not automatically share their stores. Their existing tool
  permissions do not grant access to a new shared memory service.

Inspect the exact pins in [versions.lock](../versions.lock) and the
[assistant component](../apps/components/assistant/kustomization.yaml); verify
effective settings and model behaviour before enabling automatic writes. Sources:
[WebUI 0.11.4 configuration](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/config.py),
[WebUI 0.12.5 configuration](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/config.py),
[pinned Qwen memory guide](https://github.com/QwenLM/qwen-code/blob/f56de980b316cd5410f067fbb62357481ebd66b8/docs/users/features/memory.md).

## What to retain

Start with selected decisions, corrections, preferences and meaningful task
milestones. Retain source pointers and dates. Record planned hardware separately
from observed installed capacity, and distinguish attempted work from passing
results. Preserve superseded facts as history with their validity and provenance.

Keep the immediate project handoff compact; 500–1,000 tokens is an initial
evaluation target, not an implemented limit. Include the goal, confirmed
constraints, current state, last verified result, unresolved questions and next
steps. Link to the original conversation, code revision and evidence. Recheck
volatile Git, hardware and service state when work resumes.

Do not automatically ingest every chat turn, full book or Exa page. Public web
results remain temporary evidence unless deliberately retained with a source,
retrieval time and project scope. Memory content is data; it does not authorize
commands or rewrite repository instructions. Keep accounts/projects separate,
support corrections and deletion, and retain a way to inspect the source of a
remembered claim. See the [book-ingestion plan](RAG.md#programming-books-and-later-model-adaptation).

## Pilot boundaries and resource selection

Use one owner and one project first. Process one memory-ingestion job at a time
and measure interference with interactive requests. Use selected local
completion and embedding providers only after qualification; self-hosting the
graph does not by itself keep model calls local. Do not fall back to a paid
model or embedding API when a local provider fails.

Graphiti supports OpenAI-compatible completion endpoints, but extraction and
deduplication depend on structured output. Test the selected Qwen/SGLang route
for schema validity, factual accuracy and correction behaviour. Valid JSON alone
does not qualify memory quality. See [local-model guidance](https://github.com/getzep/graphiti#using-graphiti-with-openai-compatible-providers-and-local-llms).

Select and pin the graph backend after it passes the compatibility and restart
tests. The MCP server documents FalkorDB and Neo4j. The library's embedded
FalkorDBLite option was tested on Python 3.14; its bundled 4.x engines failed
the restart gate. A separate native FalkorDB 6.0.3 candidate passed the storage
fixture described below.

Selected on 7 November 2026: **Neo4j Community 5.26 LTS (GPLv3)** instead of
FalkorDB (SSPLv1). Licence clarity matters because this setup may support
software work. Neo4j is also Graphiti's primary backend, and its driver is
already in the lock. It costs about 1 GiB more memory than FalkorDB. Neo4j
Community has one database, so users are separated by Graphiti's `group_id`
inside it, enforced by the memory service. The FalkorDB storage fixture
does not transfer; repeat the storage and restart test on Neo4j. The graph
store stays on the workstation. It can move to the MicroServer later through
`MEMORY_GRAPH_URI` (with `bolt+s://`) if measured memory is tight; the
embedding server needs the Threadripper's CPU and stays there.

Graphiti is not assumed to save RAM or disk compared with the current GraphRAG
pilot. That pilot runs on demand and stores files without a resident graph
database service. Graphiti adds graph/index storage, embeddings and extraction
work. Retained source episodes, persistence and backups also consume space.
Measure these costs before setting deployment limits or expanding the corpus.

Expose only reviewed memory operations through an authenticated client path.
Derive identity from the caller's credentials; a supplied graph namespace is
not authorization. Qualify read/write scopes independently from the existing
read-only document and web tools. Broad graph-clear/delete tools are not part
of the initial client scope. No direct client database access or new public
port is required by this plan.

## Deploy memory (opt-in)

The `assistant-memory` experience adds memory to the assistant profile. It is
inactive until you raise replicas, and it is absent from every other profile.

| Component | Source | Limits | Purpose |
| --- | --- | --- | --- |
| `neo4j` | `neo4j:5.26.32-community`, pinned digest; Bolt only, HTTP off; 1 GiB heap, 256 MiB page cache | 2 CPU, 2 GiB | Graph store on PVC `graphiti-neo4j` (retained, backed up) |
| `memory-embedding` | Text Embeddings Inference CPU 1.9.2, pinned digest | 4 CPU, 4 GiB | Serves the hash-verified RAG `Qwen3-Embedding-0.6B` files read-only |
| `graphiti-memory` | Local image from [infrastructure/graphiti](../infrastructure/graphiti/Containerfile): Graphiti 0.31.2, hashed lock | 1 CPU, 1 GiB | Internal memory API |

The service-tools MCP server exposes the tools to Open WebUI and Qwen Code:

| Tool | Scope | Switch |
| --- | --- | --- |
| `recall_memory` | Read: facts with dates, marked current or historical | `MEMORY_ENABLED` |
| `remember` | Write one explicit fact; a corrected statement makes the older fact historical | `MEMORY_WRITE_ENABLED` |
| `forget_memory` | Delete one episode by its ID | `MEMORY_WRITE_ENABLED` |

Each WebUI user gets a separate memory group. service-tools derives the group
from the caller's own authenticated identity; a tool argument can never select
it, and administrator tokens are refused. The memory service passes that group
to every search, drops any result without it, and checks an episode's group
before deleting it. Recall results say that current repository,
file or tool evidence overrides memory. NetworkPolicies allow only
service-tools → graphiti-memory → Neo4j, the embedding server and the
gateway. Nothing is exposed outside the cluster.

To enable it on the installed workstation:

1. Build the image and record its digest in
   `apps/components/memory/kustomization.yaml`:
   `podman build -f infrastructure/graphiti/Containerfile -t localhost/workstation/graphiti-memory infrastructure/graphiti`.
2. Render with `service render <site.json> <new-dir> --experience assistant-memory`.
   Run `service plan` with fresh resources. Memory adds about 7 GiB of declared
   limits. These are estimates, not measurements, and `service plan` counts
   limits, so measure real use on the running host before adjusting them.
3. Create the `memory-service` Secret from the files `service credentials`
   writes: `service-key` from `memory-service-key` and `neo4j-auth` from
   `memory-neo4j-auth`. Add `llm-key` from a `service provision` key for client
   `graphiti-memory`, restricted to the profile's chat model.
4. Raise replicas in this order: `neo4j`, `memory-embedding`,
   `graphiti-memory`, then restart service-tools.

Backups capture `graphiti-neo4j` when it exists. The capture stops
graphiti-memory, then stops Neo4j cleanly, which checkpoints the store, before
the copy.
Deployments without memory are unaffected.

Offline checks: `tests/workstation/test_memory.py` covers identity binding,
tool switches, group-scoped recall and deletion, the HTTP service key, the
rendered policies and pins, the lock and the backup claim. These use fakes; they
do not test extraction or recall quality.

## Acceptance before adoption

### Observed workstation checks, 3 November 2026

Graphiti 0.31.2 installed and ran on CPython 3.14.7 with FalkorDBLite 0.11.0
and, in a separate environment, 0.10.0. Both environments passed `pip check`.
Tests used six synthetic facts across three user/project partitions, deterministic
fixture vectors and clients that reject model calls. Network and PID namespaces
isolated the processes; the database used an owner-only Unix socket with TCP off.
No GPU devices, paid API or real model were used.
The [component verification record](VERIFICATION.md) preserves the broader
source identity, platform limits and remaining acceptance gates.

- Fresh writes, hybrid and full-text search, explicit current/historical validity
  filters, source-episode references and partition-scoped searches passed.
  Scoped deletion passed in a fresh database and retained the other partitions.
  These checks do not establish authenticated user isolation or model quality.
- Restart acceptance failed with both FalkorDBLite versions. The persisted facts
  remained visible in a graph scan, but the labelled, indexed UUID lookup used by
  `EntityEdge.get_by_uuid` returned no record. On the 0.11.0 snapshot, removing
  endpoint labels or using a scan predicate found the same fact. Rebuilding the
  indexes did not correct the labelled lookup. No query workaround was adopted.
- `Graphiti.close()` left the embedded server running in both environments.
  Explicit save/shutdown stopped each owned server. No database process remained
  after the tests, and the workstation diagnostic lock was released.
- The two fresh seed phases recorded about 191–193 MiB sampled combined process
  RSS and about 17 kB persisted snapshots. This tiny fixture excludes real model
  extraction, semantic embeddings, production growth and concurrent serving.
  It is not a deployment resource limit or an end-to-end footprint estimate.

FalkorDBLite 0.11.0 bundled FalkorDB engine 4.22.1 with Redis 8.10.2; 0.10.0
bundled engine 4.18.3 with Redis 8.6.2. Both tests used FalkorDB Python client
1.8.1 and Redis Python client 8.1.0. The restart failure does not establish that
other Graphiti backends fail, or that Python 3.14 caused the failure.

The unmodified embedded wheels remain unqualified. A later isolated test used
the official FalkorDB 6.0.3 module, verified against its release SHA-256, with the
existing Redis 8.10.2 binary and Graphiti 0.31.2 on CPython 3.14.7. An explicit
process owner saved and stopped the database; no installed module was replaced.
The original fixture's writes, lookup, search, temporal and provenance assertions
passed across all five phases: seed, process restart, scoped deletion, restart
after deletion and restore of a separate saved snapshot. Each phase used a new
PID/network namespace. All five database shutdowns were graceful.

This makes native FalkorDB 6.0.3 the next backend candidate for packaging. It
does not qualify the unchanged FalkorDBLite wheels or fix their default close
behaviour. The six-fact seed recorded about 153 MiB sampled combined RSS and a
42,858-byte snapshot. These are tiny-fixture observations, not a comparison of
production resource use or a deployment limit. An isolated snapshot restore
also does not establish off-machine disaster recovery or Kubernetes PVC recovery.
See the [official engine release](https://github.com/FalkorDB/FalkorDB/releases/tag/v6.0.3).

Next qualify extraction, correction detection, semantic recall and authenticated
browser/coding integration with the selected local completion and embedding
endpoints. No shared Graphiti service was deployed during these tests.

### Remaining adoption gates

Use the same tasks and evidence for native client memory plus handoff, then
Graphiti plus handoff. A new chat and a new coding session must be separate
test cases; continuing an existing transcript does not prove cross-session recall.

1. Retain and recover confirmed preferences, decisions and unfinished work with
   usable source references. Do not invent a memory when evidence is absent.
2. Correct an outdated fact, distinguish planned from observed state, and
   retrieve the correct current and historical answers.
3. Test browser/coding integration, restart persistence, deletion and isolated
   backup/restore. Confirm another user/project cannot read or change the records.
4. Measure idle/peak resident memory, persisted and backup bytes, growth per
   episode, model calls/tokens and interactive latency during ingestion.
5. Compare useful recall and operating cost with the baseline. Set explicit
   corpus, retention, request, concurrency and resource bounds before deployment.

Keep GraphRAG available for document-analysis comparisons without making it a
standing service or installation dependency. QLoRA remains optional post-install
work. Neither graph system replaces exact repository access or proves a better
assistant merely by starting successfully.
