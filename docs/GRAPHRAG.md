# GraphRAG pilot

This opt-in pilot tests whether relationships across project documents improve
answers enough to justify graph construction. It uses Microsoft GraphRAG 3.3.0,
the selected local SGLang candidate and a private document snapshot. It adds no
installation gate, shared service or automatic ingestion.

[Graphiti is the preferred next pilot for persistent session memory](MEMORY.md).
This GraphRAG pilot is retained for optional, on-demand document analysis; it
is not the selected continuity layer. No existing artifacts or checks are removed.

The first comparison uses GraphRAG **standard indexing and global search** versus
a direct-context answer from the same model and documents. Global search uses
community reports; it does not need vector embeddings. The workflow omits
embedding generation and uses no embedding model. The existing Open WebUI
Chroma/BM25 pilot remains a separate retrieval comparison in [RAG.md](RAG.md).
This pilot does not yet measure GraphRAG against that running retrieval service.

The [programming-book plan](RAG.md#programming-books-and-later-model-adaptation)
records chapter conversion, source/version metadata, retrieval comparisons and
later QLoRA adaptation. Kindle/EPUB/PDF ingestion remains a follow-up; this
pilot's explicit Markdown corpus and bounds are unchanged.

Canonical inputs are [profile.json](../infrastructure/graphrag/profile.json),
[questions.json](../infrastructure/graphrag/questions.json) and the
[dependency lock](../infrastructure/graphrag/requirements-python314.lock).
The controller is `lib/workstation/graphrag_pilot.py`; the isolated worker is
`lib/workstation/graphrag_worker.py`.

## Runtime and bounds

Python **3.14 is an explicit compatibility experiment** requested by the owner.
Upstream GraphRAG 3.3.0 declares Python `>=3.11,<3.14`. The isolated installation
therefore uses `--ignore-requires-python` with a complete wheel hash lock. That
option changes Python-version eligibility only; hashes, TLS and dependency
constraints still apply. Keep this environment separate from SGLang, the shared
gateway and the system interpreter. Fall back to a separately resolved Python
3.13 environment if actual workflow testing fails.

Observed on 2026-11-03: installation, dependency checks, compilation and real
GraphRAG index/global/direct fixture workflows passed on Python 3.14.7 (the Linux
workstation) and Python 3.14.8 (macOS). Native dependencies used published wheels;
this was not a source build of those dependencies. Actual Qwen answer quality
and resource use remain unqualified until the authenticated model route is live.

The 139-package lock was resolved with pip 26.2.1 for CPython 3.14. It retains PyPI
wheel hashes for Linux x86_64 and macOS arm64/universal2 where published. Runtime
preflight checks every locked package version. GraphRAG is MIT-licensed; its
dependency closure is isolated because it includes its own LiteLLM, storage and
document-processing dependencies even when those features are unused.

| Control | Initial bound |
| --- | --- |
| Documents | Explicit repository Markdown paths; 8 files / 64 KiB total |
| Evaluation corpus | `docs/ASSISTANT.md`, `docs/MODELS.md`, `docs/WEB-SEARCH.md` |
| Graph chunks | 384 tiktoken tokens, overlap 48; not the Qwen tokenizer |
| Model calls | One at a time; 128 cumulative calls per prepared pilot |
| Request data | 128 KiB per call; 8 MiB cumulative per pilot |
| Model output | At most 2,048 tokens per call; non-thinking mode |
| Completion response | At most 2 MiB; reject truncation or changed model identity |
| Runtime | 30 minutes per index/query command; no automatic retries |
| Local artifacts | Sampled 256 MiB bound; not a filesystem quota |
| CPU libraries | Two BLAS/OpenMP threads; not a CPU or RSS limit |

Measure actual peak RAM and GPU use alongside the 64 GB host budget before
expanding this corpus. The request/byte limits bound this experiment; they do
not guarantee it fits every concurrent workload or completes within 128 calls.
Local inference uses no Exa, Tavily or paid model API. It still consumes local
compute, energy and time. Provider token counts are retained when supplied.
GraphRAG's reducer consumes a streaming interface. The controller first obtains
one complete, bounded upstream answer, then frames that answer as server-sent
events for GraphRAG. It does not stream an unfinished upstream response.

## Prepare the environment and snapshot

Run from the reviewed checkout. Each output directory must be new. The package
installation and tokenizer staging require internet access. Neither command
starts a model or contacts Kubernetes.

```sh
python3.14 -m venv artifacts/graphrag-python314
artifacts/graphrag-python314/bin/python -m pip --isolated install \
  --ignore-requires-python --only-binary=:all: --require-hashes \
  -r infrastructure/graphrag/requirements-python314.lock
artifacts/graphrag-python314/bin/python -m pip check

./bin/workstationctl graphrag stage-tokenizers artifacts/graphrag-tokenizers-01

./bin/workstationctl service candidate-plan \
  /reviewed/path/CANDIDATE-POINTER.json artifacts/graphrag-candidate-01 \
  --pointer-sha256 fe92916ca06f43ec719d3a66345e4345908106212596d7044c03713b575c3394

GRAPHRAG_CONTRACT_SHA=$(sha256sum artifacts/graphrag-candidate-01/candidate-contract.json | cut -d ' ' -f 1)
./bin/workstationctl graphrag prepare artifacts/graphrag-pilot-01 \
  docs/ASSISTANT.md docs/MODELS.md docs/WEB-SEARCH.md \
  --contract artifacts/graphrag-candidate-01/candidate-contract.json \
  --contract-sha256 "$GRAPHRAG_CONTRACT_SHA"
./bin/workstationctl graphrag verify artifacts/graphrag-pilot-01
```

Use the current reviewed selected-pointer digest if the candidate changes.
`prepare` requires the native 131,040-token, Quark/BF16 candidate contract. The
served alias comes from that contract; its historical `FP8` wording does not
describe the selected MXFP4 weights. Contract validation does not prove the
model is deployed. See [ASSISTANT.md](ASSISTANT.md) for the serving handoff.

The snapshot records document paths, byte hashes, model contract, worker,
controller, profile, questions and dependency lock. A changed worker, controller,
profile, dependency lock or document snapshot requires a new pilot. Preparation
copies only explicitly selected documents. Review those documents before
preparation; this is provenance, not a secret scanner.

The real-library smoke command below runs standard indexing, global search and
the direct baseline against deterministic loopback responses. It uses no GPU or
external model. A passing smoke establishes package/configuration/protocol
compatibility, not Qwen answer quality.

```sh
artifacts/graphrag-python314/bin/python -B tests/workstation/graphrag_runtime_smoke.py \
  artifacts/graphrag-runtime-smoke-01 \
  --tokenizer-cache artifacts/graphrag-tokenizers-01
```

## Run against the local candidate

First establish the existing authenticated serving route and an owner-controlled
loopback tunnel. The example below assumes that route is at port 18088; verify
the actual port before use. Do not substitute a public or cloud API URL.
The optional `WORKSTATION_AGENT_API_KEY` is read only by the controller from the
existing private environment. Never put its value on the command line, in the
corpus, in this checkout or in a report.

```sh
./bin/workstationctl graphrag index artifacts/graphrag-pilot-01 \
  --runtime-python artifacts/graphrag-python314/bin/python \
  --api-base http://127.0.0.1:18088/v1 \
  --tokenizer-cache artifacts/graphrag-tokenizers-01

./bin/workstationctl graphrag query artifacts/graphrag-pilot-01 search-and-knowledge \
  --mode global --runtime-python artifacts/graphrag-python314/bin/python \
  --api-base http://127.0.0.1:18088/v1 \
  --tokenizer-cache artifacts/graphrag-tokenizers-01

./bin/workstationctl graphrag query artifacts/graphrag-pilot-01 search-and-knowledge \
  --mode direct --runtime-python artifacts/graphrag-python314/bin/python \
  --api-base http://127.0.0.1:18088/v1 \
  --tokenizer-cache artifacts/graphrag-tokenizers-01
```

Repeat both query modes for all six IDs in the prepared `questions.json`.
The controller writes a private `runs/<id>/result.json` and `receipt.json`.
`state.json` records cumulative calls, bytes, reported tokens and uncertain
work. Graph tables and their hashes are retained separately. A failed worker
writes `worker-status.json` with the stage and exception class, without provider
exception text. GraphRAG can catch model failures internally; the controller
also rejects a run if its proxy recorded failed calls.

If an index fails, preserve its evidence and prepare a new directory. Do not
edit the journal to regain budget. Connection loss or interruption can leave
upstream inference running; an uncertain journal blocks further work on that
pilot. The runtime deadline does not cancel an already accepted model request.

## Judge the result

For each pair, review correctness, source support, citation traceability,
current-versus-legacy settings and the no-evidence answer. Trace GraphRAG report
citations through retained community/entity/text-unit/document tables to the
hashed source files. Record unsupported claims and compare useful answer
improvements with index calls, query calls and elapsed time. Do not treat
keyword matches, fixture responses or a model's self-score as acceptance.

Keep ordinary hybrid retrieval unless GraphRAG shows a repeatable improvement
on the relationship questions without weakening citations or inventing missing
facts. Evaluate the live hybrid retriever separately before choosing a shared
retrieval architecture.

This pilot is private to one owner. GraphRAG global summaries combine source
content, so they must not mix documents with different account permissions.
Nothing here exposes a family endpoint, ingests web results, writes persistent
agent memory or changes radix/prefix caching. Answers remain untrusted text.
The worker receives an ephemeral proxy credential; the upstream credential stays
in its parent. Python socket connections outside that proxy are rejected. This
check is not an operating-system sandbox for arbitrary native code.

Upstream references: [GraphRAG 3.3.0](https://github.com/microsoft/graphrag/releases/tag/v3.3.0),
[indexing methods](https://microsoft.github.io/graphrag/index/methods/),
[global search](https://microsoft.github.io/graphrag/query/global_search/),
[configuration](https://microsoft.github.io/graphrag/config/yaml/).
