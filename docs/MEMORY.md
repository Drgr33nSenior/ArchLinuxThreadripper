# Session continuity and persistent memory

Decision recorded on 3 November 2026: prioritise a **Graphiti pilot** for
persistent coding and home-assistant context. Keep Exa for current public web
evidence, local retrieval for private code/documents and a compact project
handoff for the active task. Microsoft GraphRAG remains an optional, on-demand
document-analysis experiment.

This is an architecture decision and pilot plan. Graphiti is not installed,
integrated or resource-qualified by this change. No memory service, background
writer, client hook or new API endpoint is introduced. The current 64 GB host
budget, serving ownership and installation/recovery gates still apply.

## Responsibilities

| Need | Selected direction | Current status |
| --- | --- | --- |
| Durable preferences, decisions and changing project facts | Graphiti, scoped by user and project | Preferred next pilot; integration and quality tests pending |
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

- Open WebUI 0.11.3 includes per-user memory, system-context injection and
  background review controls. Upstream background review defaults to off.
- Qwen Code 0.23.2 supports `AGENTS.md`/`QWEN.md` briefings and editable Markdown
  auto-memory. Upstream automatic extraction/consolidation defaults to on;
  team memory and its Git sync default to off. These are upstream capabilities,
  not observations of a configured or qualified client session.
- The two clients do not automatically share their stores. Their existing tool
  permissions do not grant access to a new shared memory service.

Inspect the exact pins in `versions.lock`; verify effective settings and model
behaviour before enabling automatic writes. Sources:
[pinned WebUI configuration](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/config.py),
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

Select and pin the graph backend after a bounded compatibility test. The MCP
server documents FalkorDB and Neo4j. The library's embedded FalkorDBLite option
is another candidate; compatibility with the chosen service and Python 3.14
remains untested. No database choice or allocation is made by this document.

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

## Acceptance before adoption

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
