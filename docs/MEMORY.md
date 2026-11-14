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
| Durable preferences, decisions and changing project facts | Graphiti on Neo4j Community, scoped by user or by user and authorised project | Opt-in `assistant-memory` profile packaged; Neo4j storage/restart test, model and client qualification pending |
| Current public web information | Existing Exa adapter | Source integration exists; full target acceptance remains separate |
| Exact code, private documentation and book passages | Repository tools and local hybrid retrieval | Existing retrieval path; book conversion remains planned |
| Active goal, constraints, last result and next steps | [Task handoff](#task-handoffs) per user, project and task | Implemented in service-tools for the `assistant` experiences; client qualification pending |
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

- Open WebUI 0.13.3 (`service` and `assistant`) include per-user memory,
  system-context injection and background review controls. Upstream background
  review defaults to off in both pins.
- Qwen Code 0.27.1 supports `AGENTS.md`/`QWEN.md` briefings and editable Markdown
  auto-memory. At 0.23.2, upstream automatic extraction/consolidation defaulted
  to on and team memory and its Git sync to off; recheck these defaults for 0.27.1. These are upstream capabilities,
  not observations of a configured or qualified client session.
- The two clients do not automatically share their stores. Their existing tool
  permissions do not grant access to a new shared memory service.

Inspect the exact pins in [versions.lock](../versions.lock) and the
[assistant component](../apps/components/assistant/kustomization.yaml); verify
effective settings and model behaviour before enabling automatic writes. Sources:
[WebUI 0.11.4 configuration](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/config.py),
[WebUI 0.13.3 configuration](https://github.com/open-webui/open-webui/blob/b0e6098e940942281d9c83f3529ea117287a3a22/backend/open_webui/config.py),
[pinned Qwen memory guide](https://github.com/QwenLM/qwen-code/blob/c2eccf7459c8d6d4b2a5df3733d2f3833153763b/docs/users/features/memory.md).

## What to retain

Start with selected decisions, corrections, preferences and meaningful task
milestones. Retain source pointers and dates. Record planned hardware separately
from observed installed capacity, and distinguish attempted work from passing
results. Preserve superseded facts as history with their validity and provenance.

Keep the immediate task handoff compact. The [handoff tools](#task-handoffs)
enforce character limits sized for roughly 500–1,000 tokens. Include the goal,
confirmed constraints, current state, last observed verification, unresolved
work and next steps. Link to the code revision and evidence. Recheck volatile
Git, hardware and service state when work resumes.

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

The service-tools MCP server exposes these tools to Qwen Code and, through
the [Open WebUI tool server](#open-webui-tool-server), to browser chats:

| Tool | Scope | Switch |
| --- | --- | --- |
| `recall_memory` | Read: facts marked current or historical, with dates and source provenance | `MEMORY_ENABLED` |
| `inspect_memory_source` | Read: one episode's source description, times, a 2,000-character excerpt and the status of its facts | `MEMORY_ENABLED` |
| `remember` | Write one explicit fact; a corrected statement makes the older fact historical | `MEMORY_WRITE_ENABLED` |
| `forget_memory` | Delete one episode by its ID | `MEMORY_WRITE_ENABLED` |

Each tool takes an optional `project`. service-tools derives the memory group
from the caller's own authenticated identity and, when `project` is given, from
a project the caller can read now (see [Project scope](#project-scope)). A tool
argument never names a group, and administrator tokens are refused. The memory
service passes the group to every search, drops any fact or source episode
from another group, and checks an episode's group before showing or deleting
it. NetworkPolicies allow only service-tools → graphiti-memory → Neo4j, the
embedding server and the gateway. Nothing is exposed outside the cluster.

### Explicit writes and provenance

Memory is written only when the user explicitly asks; tool descriptions say so,
and there is no background ingestion of chats, files or web pages. Remembered
text is user-supplied evidence. It has no instruction or permission authority,
and current repository, file or tool evidence overrides it. Recall and
inspection results repeat this notice.

Each recalled fact has `status` (`current` or `historical`), `valid_at`,
`invalid_at`, `expired_at` and up to three `sources`: the episode ID, its
source description, its reference time and when it was recorded. Pass an
episode ID to `inspect_memory_source` to read a bounded excerpt of the stored
text and the facts extracted from it. Episode text longer than 2,000 characters
is reported with `excerpt_complete: false`.

### Project scope

A project is an Open WebUI knowledge collection that the caller can read: the
collection ID that `retrieve_project_passage` takes. The only authority is
Open WebUI's knowledge ACL (owner, user or group read grant, public read
grant). service-tools reads `GET /api/v1/knowledge/` with the caller's own
credential on every project-scoped call; pinned 0.13.3 applies the same owner
and read-grant rules to knowledge collections during retrieval. A client-supplied
project string is only a lookup key. A collection that is missing or not
readable is refused with `project_not_authorized`. The listing is read for at
most 300 collections; beyond that a call is refused with
`project_listing_incomplete`.

API keys reach the listing through the `/api/v1/knowledge/` entry in
`API_KEYS_ALLOWED_ENDPOINTS`. Open WebUI matches an entry exactly or as a
prefix followed by `/`, so the trailing slash admits only the read-only
listing, not `/api/v1/knowledge/<id>/...` write routes.

| Scope | Group | When |
| --- | --- | --- |
| User-wide (legacy) | `u_` + 32 hex of SHA-256(`workstation-memory:<user id>`) | `project` omitted |
| User and project | `p_` + 32 hex of SHA-256(`workstation-memory-project:<user id>\0<project id>`) | `project` given and readable |

The `u_` formula is unchanged, so memory written before project scope remains
reachable by omitting `project`. No data was moved, repartitioned or deleted.
Project memory is per user: two users of a shared collection have separate
groups. If access to a collection is revoked, its project memory is no longer
reachable, including for `forget_memory`; it is kept, not deleted. Moving
user-wide facts into a project would be a separate, non-destructive procedure:
re-`remember` the selected facts with `project`, verify recall, then
`forget_memory` the user-wide episodes individually. No such migration is
implemented.

### Enable memory

To enable it on the installed workstation:

1. The image comes from the lab registry, pinned by digest in
   `apps/components/memory/kustomization.yaml`. To rebuild it after changing
   `infrastructure/graphiti`, run the AI-stack `graphiti` stage, which builds it,
   runs an offline import smoke test, scans, signs and pushes it
   ([AI serving stack](../infrastructure/ai-stack/README.md#stages)), then record the
   pushed digest it prints:
   ```sh
   AI_STACK_PUSH=1 bash infrastructure/ai-stack/build-stage.sh graphiti
   ```
2. Render with `service render <site.json> <new-dir> --experience assistant-memory`.
   Run `service plan` with fresh resources. Memory adds about 9 GiB of declared
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
project authorisation and the legacy user scope, tool switches, group-scoped
recall, provenance, inspection and deletion, the HTTP service key, the rendered
policies and pins, the lock and the backup claim. `tests/workstation/test_handoff.py`
covers handoff scope, limits, verification status, conflicts and storage. These
use fakes; they do not test extraction, recall quality or a live browser.

## Task handoffs

`save_task_handoff`, `load_task_handoff` and `list_task_handoffs` keep a compact
record of one paused task. They are enabled by `HANDOFF_ENABLED` in the
`assistant` experiences and do not depend on Graphiti. A handoff is scoped to
the authenticated user, an authorised [project](#project-scope) and a task slug
(`[a-z0-9][a-z0-9._-]{0,63}`). Other users, including users of the same
collection, cannot read it.

| Field | Limit (characters) |
| --- | --- |
| `goal` (required) | 600 |
| `repository`, `branch`, `revision` | 200, 120, 80 |
| `next_action` | 400 |
| `completion_criteria`, `constraints`, `decisions` (accepted), `outstanding`, `evidence` | 6–8 items of 240 |
| `blockers` | 6 items of 240 |
| `changed_files` | 20 items of 200 |
| `verification` | `status`, `observed` 400, `observed_at` 40, `evidence` 240 |
| All text together | 4,000 |

Limits count characters, not tokens; 4,000 characters is roughly 1,000 English
tokens. Unknown fields and control characters are refused. Each user can keep
100 handoffs; there is no delete tool, so reuse task slugs.

`verification.status` is one of `passed`, `failed`, `partial`, `not_run` or
`unknown`. A missing verification is stored as `unknown`. `passed`, `failed` and
`partial` require the `observed` result text, for example `pytest: 42 passed`.
The service never infers or upgrades a status.

Every save carries `base_revision`: `0` for a new task, otherwise the revision
that was loaded. A save based on an older revision returns an error result
`handoff_conflict` with `current_revision` and `updated_at`, and nothing is
overwritten. Load the current handoff, merge, and save again. Handoff refusals
are MCP tool results with `isError: true`, so both Qwen Code and Open WebUI
show them to the model.

`load_task_handoff` returns the handoff, `revision`, `updated_at` and a notice
to recheck Git, runtime and hardware facts before acting. `list_task_handoffs`
without `project` lists handoffs in every collection the caller can currently
read, with those collections' IDs and names.

Handoffs are stored in `/state/task-handoffs.sqlite3` on the retained
`service-tools-state` claim, with a derived owner key instead of the WebUI user
ID. The backup capture stops `service-tools` and waits for its Pod to exit
before archiving that claim, so the closed SQLite file is consistent. Preserve
it during restore and rollback, as for the web request ledger.

## Open WebUI tool server

The `assistant` component configures one native MCP tool server in
`TOOL_SERVER_CONNECTIONS`: `http://service-tools:8000/mcp`, ID `workstation`,
named "Workstation tools". Its `auth_type` is `session`, so pinned Open WebUI
0.13.3 sends the signed-in user's own token with each MCP request. service-tools
checks that token through `/api/v1/users/user/status` as it checks a coding
client's API key. (Open WebUI 0.12 refuses API keys on `/api/v1/auths/`.) There is no shared credential. The public read grant only lists the server
in every user's tool menu; each call is authorised for the calling user.
The existing `open-webui` → `service-tools:8000` NetworkPolicy already allows
the flow. The connection's `function_name_filter_list` hides the server's `search_web`
from Open WebUI chats, which use Open WebUI's own web search instead
([one web search](ASSISTANT.md#one-web-search-in-a-chat)).

To use it, select "Workstation tools" in a chat's tool menu, or attach it to a
model in the admin model settings. The connection is owned by the deployment
([Admin Panel settings](ASSISTANT.md#admin-panel-settings)): change it in source,
not the admin UI.

Known gaps:

- Administrator accounts are refused by service-tools, as on the coding path.
- The forwarded session token is not limited by `API_KEYS_ALLOWED_ENDPOINTS`;
  service-tools only calls the read routes listed above with it.
- Memory and retrieval refusals are HTTP errors. Pinned Open WebUI shows them as
  a generic tool error, not the refusal code. Handoff refusals are tool results.
- Nothing is synchronised. Browser chat history, Open WebUI's own per-user
  memory, Qwen Code auto-memory and knowledge collections stay separate stores;
  only calls to these tools reach the shared handoff and Graphiti stores.
- Browser-side tool selection, model tool-call quality and the end-to-end flow
  have not been qualified on the workstation.

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
