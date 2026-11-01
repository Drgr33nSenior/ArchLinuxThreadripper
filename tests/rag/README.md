# Disposable native RAG test

This opt-in test runs the pinned Open WebUI image and real
Qwen3-Embedding-0.6B on a disposable Linux CI runner. It does not connect to the
workstation, a serving model, Kubernetes, personal accounts or a shared database.
It supplies functional evidence for the proposed CPU retrieval service. Its
timings do not predict workstation performance or answer quality.

## Frozen protocol

Use the image/source/model pins in `versions.lock` and model hashes in
`apps/overlays/rag/SHA256SUMS`. Keep the pilot's Qwen tokenizer, 192/32 chunking,
query-only native instruction prefix, batch 1, two CPU threads, three final
hits and no cross-encoder. Ingest full, source-hashed copies of these public
documents through the existing `rag corpus` and authenticated upload interfaces:

- `docs/AI-PERFORMANCE.md`
- `docs/ROCM.md`
- `docs/HOME-LAB.md`
- `docs/SERVING-INTEGRATION.md`

Run one warmup per retrieval mode. Run each of the ten checked-in questions three
times, alternating dense/hybrid order by question and repetition. This gives
60 measured queries in one startup, not independent startup pairs. Dense and
hybrid query the same index through the pinned API's per-request selector.

Report source-level hit@3 and source recall@3 for the eight positive questions.
The two no-evidence questions remain in the timing record but are excluded from
the retrieval-quality denominator. No generation occurs: answer correctness,
citation entailment, prompt-injection resistance and abstention are **NOT RUN**.
Record every latency and returned source/passage hash, including misses. Do not
tune or retry to improve retrieval scores. Four documents are a smoke corpus,
not a representative production evaluation.

Create one fixture administrator and two ordinary users with random credentials.
Keep credentials and the signing key private on the disposable data volume.
Test anonymous signup refusal, unshared collection/file refusal, and a foreign
directory ID. Restart the same image against the same volume; all 20 repeated
question/mode combinations must retain their ranked source IDs. Then remove a
file from the shared collection while retaining it privately, and revoke the
collection grant. The former reader must lose access; the administrator must
retain access to the undeleted file. These checks cover the scenarios in
[GHSA-9cxp-636w-4997](https://github.com/open-webui/open-webui/security/advisories/GHSA-9cxp-636w-4997)
and [GHSA-jx3w-3pqq-7f3w](https://github.com/open-webui/open-webui/security/advisories/GHSA-jx3w-3pqq-7f3w),
both fixed upstream in 0.11.4. They are not a complete security audit.

The complete protocol makes 110 HTTP calls: 22 setup/ingestion/access checks,
two warmups, 60 measured retrievals, 20 restart retrievals and six removal/revocation
calls. Health probes are separate. Actual indexed dimensions must be 1,024 and
cgroup OOM kills must be zero. Preserve all failed attempts. At most three fresh
technical attempts are allowed in the initial diagnosis; stop on an access-control
failure or exhausted budget. A diagnosed correction requires a new bounded
protocol and source identity; do not silently reset the budget or repeat quality
questions to select favorable answers.

## Resource and evidence bounds

The container has no network, published port, GPU, host credentials or service
account. Run as UID/GID 1000, with a read-only root, all capabilities dropped,
no-new-privileges, two CPUs, 6 GiB RAM, no container swap and 512 MiB temporary
space. The data/model directories are new and private. Model staging is bounded
by the existing 1.4 GB total limit; require at least 12 GiB free runner disk before
pulling the image. Model and corpus mounts are read-only. The default seccomp
profile remains active.

Stage allowances total 2,210 seconds, plus at most 120 seconds of stage termination
grace and 150 seconds of collection/disposal. The outer command stops after
43 minutes, with 45 seconds of kill grace; the job has a 45-minute deadline.
These are hard campaign limits, not predicted run times. Only exact container
IDs bearing this run's ownership label may be removed. A failed cleanup fails
the run. The ephemeral runner discards private DB/key/log files afterward.

Artifacts contain phase statuses/times and fixed JSON receipts with public
document provenance, retrieval measurements, allowlisted settings and resource
observations. If initial readiness fails before any client/account request,
retain at most 64 KiB of startup output with all 64-hex strings redacted. The
container's only secret then is its fresh 64-hex signing key. After the client
starts, export only fixed error tokens and container fields. Never upload the
auth file, signing key, DB or raw server/HTTP logs.

## Run and rollback

Use the existing **Workstation source checks** manual workflow on the reviewed
source branch, with `rag_pilot=true`. Ordinary PR/push runs do not start this test.
The script refuses direct workstation or non-CI execution. Unit/source checks:

```sh
HOME_LAB_PYTHON=/path/to/prepared/python bash tests/test_rag.sh
```

The workload is temporary; no persistent service or live deployment changes.
Rollback removes the opt-in CI job/test from the source branch. Keep old deployment
manifests and PVCs; do not downgrade an existing database without migration review.
The 0.11.3 image has known access-control advisories and is not a safe exposure
fallback. Workstation network policy, backup/restore, serving integration and
coexistence still require their existing qualification procedure.
