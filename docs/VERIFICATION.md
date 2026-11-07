# Component verification record

Recorded 3 November 2026. Graphiti is the primary persistent-memory component
under qualification; GraphRAG remains optional on-demand document analysis.
Qwen serving work was excluded from this pass. No language model or GPU was
started, no paid provider was called and no application was deployed to K3s.

## Observed results

| Component | Passing result | Boundary |
| --- | --- | --- |
| Workstation software | 162 tests across 11 suites on native CPython 3.14.7 | API boundaries, tool adapters, Exa/Tavily adapter, creative staging, assistant composition, backup gates and telemetry/monitoring/dashboard/tariff logic; synthetic upstreams |
| LiteLLM gateway/PostgreSQL | 48 client checkpoints, including restart | Actual pinned images with deterministic completions; authentication, ownership, expiry/revocation, quotas, concurrency, streams, tools, cancellation, failure and maintenance |
| Graphiti 0.31.2 / native FalkorDB 6.0.3 | Five storage phases | Seed, restart, scoped deletion, restart after deletion and restore of a saved snapshot; explicit graceful shutdown passed in every phase |
| Grafana 13.2.1 / Prometheus 3.14.0 | 23 checks before restart and 23 after | Eight provisioned dashboards, datasource definitions, login/assets, access refusal and a real query for a healthy fixture series |
| Telemetry validators | Pinned image parsers and rule/privacy fixtures | Prometheus, Loki, Tempo, cluster/host Alloy; scope stripping, linked-span refusal and dummy secret minimisation |
| Loki / Alloy / Tempo | Ten integration checks | Exact synthetic log ingestion/query and filtered trace ingestion/query; rejected linked span and dummy secret fields were absent |
| Alertmanager 0.34.1 | Real routing/inhibition/resolution test | Loopback receiver and service/backup rule fixtures; no owner notification destination |
| Coding-client setup | Offline configuration tests | Bundle generation, endpoint/limit validation and separate retrieval credentials; no agent inference or live MCP session |

The workstation software run used a frozen working tree based on
`dac7de3052971d1e098c1f8076732dfee85e8e07`, including uncommitted and untracked
project source: 662 files / 7,182,202 bytes. Its file-manifest SHA-256 is
`885607e8401dcf5a496241fbe968e1ff9f258e9d9f6ec54ceddd03c1faa8f43f`.
The counts describe that snapshot, not every file in a later publication.
Private receipts retain exact inputs, failed attempts and process identities.
Credentials, private fixture databases and raw application logs are not published.

## Graphiti storage qualification

The unchanged storage/search/temporal/provenance assertions passed with the
official [FalkorDB 6.0.3 module](https://github.com/FalkorDB/FalkorDB/releases/tag/v6.0.3),
verified as SHA-256 `df83e89595c537de67cfa3466bb824c671dddf96f543b5d1e4c162f1b4ac93fa`.
An explicit process owner ran it with Redis 8.10.2 from the existing isolated
FalkorDBLite 0.11.0 environment. No installed module was replaced. The test used
Graphiti 0.31.2, FalkorDB Python client 1.8.1 and Redis Python client 8.1.0 on
CPython 3.14.7, private Unix sockets and separate PID/network namespaces.

The six-fact seed recorded about 153 MiB sampled combined process RSS and a
42,858-byte snapshot. This excludes real model extraction, semantic embeddings,
concurrent serving and production growth. It is not a deployment resource limit.
Logical partition filters are not authenticated user isolation. See
[memory qualification](MEMORY.md#observed-workstation-checks-3-november-2026).

Both earlier unmodified FalkorDBLite wheels remain unqualified: 0.11.0 bundled
engine 4.22.1 and 0.10.0 bundled engine 4.18.3. Facts survived reload, but a
labelled indexed UUID lookup used by Graphiti missed them. Rebuilding indexes
did not correct it. Their default `Graphiti.close()` also left the embedded
server running. Passing the separate native 6.0.3 candidate does not fix or
qualify those wheels. Deployment packaging and shared memory integration remain
open. Graphiti is Apache-2.0; the Lite wrapper is BSD-3-Clause. The engine's
[SSPLv1 licence](https://github.com/FalkorDB/FalkorDB/blob/v6.0.3/LICENSE)
must be considered separately.

## WebUI retrieval evidence

An earlier isolated workstation test exercised the pinned Open WebUI 0.12.5
image with CPU Qwen3-Embedding-0.6B vectors at 1,024 dimensions. All 112 API calls,
private-file/collection access checks, grant revocation and 20 retained
question/mode rankings across application restart passed. Peak cgroup memory
was about 1.09 GiB, with no swap or OOM kill.

Expected-source hit@3 was six of eight positive questions (75%); mean source
recall@3 was 68.75%. The `rocm-version` and `storage-quota` misses remain open.
The two requested dense/hybrid flags produced identical rankings because both
ran hybrid search: with the global setting on, the pinned WebUI's
`query_collection` re-enters hybrid search when the request sets `hybrid=false`
(0.11.4 `retrieval/utils.py:706-721`; 0.12.5 `:705-712`). This is not a
dense/hybrid comparison. The corrected protocol runs the dense arm on the
`rag-dense` profile; see [RAG](RAG.md). No serving model
was called. This was an application restart within one temporary Pod, not a
container restart, PVC recovery, browser-rendering or generated-answer test.

## Platform, lifecycle and remaining gates

Workstation CPU tests used the existing diagnostic lock, two CPU cores, private
writable fixture directories and no real home, credentials or GPU device mount.
The Kubernetes wrapper allowed only local `kustomize` rendering. Container tests
used pinned linux/amd64 images in a local Docker Desktop Linux VM on an ARM
controller. These are emulated component tests, not workstation performance
results. Integration networks were internal, with no public ports or host socket.

The controller's PostgreSQL init-script bind mount refused execution. A private
test image copied the same script bytes into the pinned PostgreSQL base; client
assertions and authentication/role SQL were unchanged. The production image and
Compose source were not changed. Grafana's 46 assertions passed, then its cleanup
driver failed on a newline-free CID file. The driver was corrected and the exact
owned containers, volume and network were removed explicitly. Earlier setup
failures and successful receipts are retained. No failed acceptance assertion
was removed to obtain a pass.

The test processes stopped and the workstation diagnostic lock was released.
An isolated snapshot restore does not establish off-machine disaster recovery.
Passing datasource definitions does not establish live workstation scrape health,
browser rendering or a long resource soak.

Remaining acceptance gates:

- Live Exa search/extraction needs a protected API-key file reference. Adapter
  fixtures and the prepared two-request probe do not qualify provider transport.
- The selected native Qwen endpoint, tokenization, context/output limits,
  reasoning/tool-call parsing, generated answers and serving coexistence require
  their own qualification. These tests used the retained container-profile IDs.
- Graphiti still needs model extraction/correction/recall, semantic embeddings,
  authenticated tools and independent browser/coding-session tests.
- Deployed DNS/TLS/routing, actual client streaming, real PVC recovery and
  off-machine backup restore remain separate from these component fixtures.

Use [assistant scope](ASSISTANT.md), [memory](MEMORY.md), [RAG](RAG.md),
[web search](WEB-SEARCH.md), [monitoring](TELEMETRY.md) and
[service recovery](SERVICE-RECOVERY.md) for the applicable next gates.
