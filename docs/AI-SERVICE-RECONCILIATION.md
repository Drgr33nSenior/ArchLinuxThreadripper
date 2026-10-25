# Shared AI service reconciliation

Review baseline: `92dea182b930c7dd7f8e7b6514bbfad781dd2e61` (2026-10-22).
The earlier `2512495` review is historical evidence. No reset was performed.
Existing IDE and scratch work is excluded from this change.

This is the final reconciliation for the source change. It does not identify a deployed service.
`source-validated`, `integration-tested`, `hardware-validated`, `deployed`, and
`NOT RUN` are separate evidence labels; one does not imply the next.

| Finding | Current evidence/path | Final disposition and change | Validation | Remaining gate |
| --- | --- | --- | --- | --- |
| 128 GB baseline | `hardware.sh`, `resources.sh`, `tests/test_resource_plan.sh`; refreshed HOME-LAB/STACK/RAG/TELEMETRY; `service.py` | Observed-memory planning and 128 GiB fixtures **already satisfied**; stale current-hardware prose corrected; service/build/VM admission **implemented** | **source-validated**: existing 128 GiB topology fixture, firmware-reserved service fixture, CPU/RAM/GPU budget rejection. Limits and 18 GiB reserves retained | **NOT RUN**: current DIMMs, trained speed, MemTotal and pressure measurements |
| Application recovery | `service_backup.py`, Restic units, `SERVICE-RECOVERY.md` | **Implemented**: baseline/full captures; canonical lock and writer shutdown; PostgreSQL dump; SQLite/token/config; gaming homes; independent-SSD local snapshot and exact-ID off-site copy; protected UID/replica journal | **source-validated**: storage/writer gates, failure/retry/freshness fixtures and isolated restore staging. **integration-tested**: native Restic copy/retry/check/restore using two disposable local repositories. Existing PVs require Retain migration | **NOT RUN**: physical SSD, S3, K3s token/bootstrap, independent recovery credentials, PostgreSQL and application restore. Synthetic restore is not disaster recovery |
| Shared model API | `apps/base/shared-api/`, `service.py` | **Implemented**: pinned LiteLLM OSS/PostgreSQL, native key policy, private per-person WebUI translation, bounded ASGI admission/status, scoped DB role/migration, content-free operational signals | **source-validated** against selected source; **integration-tested** boundary/adapter with deterministic upstreams and native LiteLLM/PostgreSQL on a hosted runner, including identity denial, expiry, quotas, streaming/tools, cancellation and restart persistence | **NOT RUN**: target service acceptance with actual clients and the GPU backend. Native hosted evidence covers commit `3f8fa579ca13aa41800501ce9bc8e9d5e2f674c8`; local Docker remains unavailable |
| Private ingress and clients | `apps/overlays/service/`, `infrastructure/service/traefik.yaml`, `agents.sh` | **Implemented**: existing Traefik NodePort 32443, actual-site renderer, TLS/consumer routes, exact flows, normal HTTPS client bundles | **source-validated**: Kustomize/site rendering, no public admin route, union of policies admits required flows and denies lateral access; Qwen separate retrieval-key fixture | **NOT RUN**: real LAN/DNS/TLS, Flannel enforcement, browser WebSocket and full proxy streaming/disconnect |
| GPU service modes | `session.sh`, `service_session.py`, `test_session.sh` | Existing locks, identity and qualification **retained**; large/split admission, bounded drain, restart-unavailable and recoverable failure **implemented**; no automatic image-triggered switch | **source-validated**: session lock, split/failure and zero-device-release fixtures. Conservative split plan correctly fails at 43.25 CPU limits even with metrics-only telemetry versus 42 allocatable CPUs | **NOT RUN**: physical handover. Split activation **blocked** until a measured CPU/profile adjustment is qualified; external Bridge v1 catalog has no new split operation |
| Browser images | `service_tools.py`, fixed SDXL workflow; `infrastructure/creative/` | Narrow Swarm adapter **implemented**; Swarm remains sole Comfy owner; pinned source/model/runtime contract and offline package preparation added | **integration-tested** with a deterministic job API. Checks cover access permissions, busy/unavailable responses, bounded artifacts and unresolved cancellation across restart | **NOT RUN**: complete NuGet/wheel closure, offline Linux image build, native Swarm/Comfy behavior, real image quality/persistence/GPU use. Image stays UNQUALIFIED |
| Shared knowledge for IDEs | Existing RAG pilot + `service_tools.py`, `agents.sh` | Pilot/provenance/embedded store **already present**; authenticated read-only MCP **implemented**, using caller WebUI ACLs and bounded cited snippets. No new vector DB | **integration-tested** with deterministic WebUI APIs: allowed/private/deleted/no-evidence results, source instructions remain data, no arbitrary write/URL tool. Existing RAG tests retained | **NOT RUN**: actual Qwen MCP handshake, deployed document retrieval and target quality evaluation |
| CI and notifications | `.github/workflows/source-checks.yml`, `tests/ci/`, Alertmanager/observer sources | **Implemented**: pinned source checks and automatic bounded native smoke with sanitized artifacts; separate local/off-site backup alerts; chosen webhook route and independent observer | **source-validated** local and hosted strict checks, including Linux systemd verification; **integration-tested** hosted native gateway job and real Alertmanager/local receiver with maintenance inhibition. Prometheus fixtures distinguish local/off-site freshness and remote-only mode | **NOT RUN**: selected-owner delivery and second-device heartbeat. Image qualification gates remain. No workstation credentials enter PR jobs |
| AMD DMA-buffer recommendation | `linux-git.config`, `runtime.sh`, `KERNEL-REQUIREMENTS.md` | **Implemented** explicit `CONFIG_DMA_SHARED_BUFFER=y` and effective-package refusal. `CONFIG_DMABUF_MOVE_NOTIFY` is absent from pinned 7.3-rc6; move notification is unconditional in that source | **source-validated** against exact Linux source; kernel-profile and missing-symbol negative tests pass | **NOT RUN**: rebuilding/installing/booting a new kernel in this task |

## Authoritative model inputs

`versions.lock`, `apps/base/sglang/` and the selected overlay remain authoritative.
Dual GPU: Qwen3.8-27B-FP8 revision `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`,
32,768 context tokens. Single GPU: Qwen3.5-9B BF16 revision
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`, 4,096 context tokens.
The single-GPU local image differs from the dual-GPU ROCm 10 image deliberately.
All application replicas are gated at zero in the reviewed source. Image pins,
previous tuning receipts and rendered manifests do not qualify a shared service.

## Unavailable target evidence

This checkout is on the owner's Mac. No classified, authenticated workstation
target or current 128 GB hardware collection has been supplied for this task.
DIMM count, trained speed, Linux-visible RAM, physical GPU handover and deployed
application state remain **NOT RUN**. Collect these on the workstation using
the existing `workstationctl hardware collect` and `resources plan` commands.
The final acceptance runbook supplies the exact sequence and remaining site inputs.

## Validation record, 2026-10-22

Local checks used the IDE-selected CPython 3.11.1 through a private validation
environment, with `tests/validation-service.lock`. No system interpreter or global
packages were changed. Hosted CI selects Python 3.11.17, whose Ubuntu 24.04 x64
asset was verified in the [Actions Python release](https://github.com/actions/python-versions/releases/tag/3.11.17-36876195594).
Hosted execution itself is **NOT RUN**.

| Command/check | Observed result |
| --- | --- |
| `make -C test-results/ai-service-20261022/delivery-source check-strict` with the private Python/tool PATH and pinned GPU chart | **PASS**, exit 0; 65 test files, six Bats cases, shell syntax/lint/format, YAML, Ansible syntax, manifest and package/source inventory gates. Includes the final creative paths, policy test and CI pin |
| `HOME_LAB_PYTHON=<private-validation-python> bash tests/test_shared_service.sh` | **PASS**, 17 boundary/recovery/resource/policy tests plus five adapter tests, standalone and in the final strict run |
| `bash tests/test_agent_harnesses.sh` | **PASS**, including HTTPS retrieval bundle, separate credential requirement and secret-free bundle output; no real client launched |
| `bash tests/test_session.sh` | **PASS** in strict run; original and split/drain/failure/locking fixtures; no hardware |
| `bash tests/test_kernel_native.sh`; `bash tests/test_kernel_profile.sh` | **PASS** standalone and in strict run, including missing DMA-buffer support refusal |
| `bash tests/test_service_alerts.sh` with pinned binaries | **PASS** standalone and in strict run; real loopback receiver and Alertmanager inhibition/resolution; `promtool check rules` passes nine rules |
| `make yaml`; `git diff --check -- ':!.idea'` | **PASS**, including the final CI Python pin and documentation edits |
| `gitleaks dir <source-snapshot> --redact ...` | Exit 1: three **existing public SHA-256 fields**, verified unchanged from HEAD (`k3s_rpm_gpg_key_sha256`, `K3S_RPM_GPG_KEY_SHA256`, and a `shape_key.py` source hash). An intermediate delivery scan also flagged ordinary prose after `API:`; that sentence was clarified. No suppression or scanner weakening; both reports retained |
| `gitleaks stdin --redact ... < service-integration.patch` | **PASS**, no leaks in the implemented diff; existing whole-tree checksum findings remain recorded above |
| Native gateway/PostgreSQL smoke, creative container build, live services and isolated application restore | **NOT RUN**; unavailable local Docker daemon or target/build/site prerequisites, as specified above |

The first strict run failed Ruby YAML parsing on unquoted colon-containing flow
arguments. Those arguments were quoted and the full strict run then passed. Both
receipts remain under `test-results/ai-service-20261022/`. The final strict receipt
is `delivery-source/test-results/strict-check.NjQMqc/`; earlier passing evidence
is retained at `source-check-final/test-results/strict-check.biDPsE/`.
The separate service-test receipt is `shared-service-final.log`. A fresh bounded
local Docker ping still timed out; `docker-availability.json` records that limit.
Only this evidence record and prose corrections changed after the final strict run.

Preserved strict skips:

- `HOME_LAB_GAME_IMAGE` streaming smoke: no qualified local image supplied.
- `HOME_LAB_WAYLAND_RUNTIME_IMAGE` process-group tests: no local image supplied.
- `systemd-analyze verify`: Linux required; this checkout is on macOS.

Ansible reports absent live inventory/host patterns during syntax-only checks.
The existing Hermes warning remains: its output-token cap is provider-owned.
Neither warning was hidden or treated as live qualification.

## Delivery boundary

The [component/credential/state map, rollout, target checks and rollback](AI-SERVICE.md)
and [consistent capture/isolated restore procedure and restore record](SERVICE-RECOVERY.md)
are implemented alongside the code. Source/export inventories include the new
runtime, tests, workflow, configuration and documentation. Existing IDE/scratch
changes remain untouched; no live service, kernel, firmware, encryption or OS
installation was changed.

The family-user end-to-end milestone is **NOT RUN**, not complete. Remaining inputs
are grouped in `AI-SERVICE.md`: current hardware evidence and classified target;
actual private address/CIDRs/DNS/TLS; PV identities and staging capacity; remote
backup and independent recovery credentials; WebUI identities/key policy; a Linux
creative build with reviewed dependency closure; measured split admission; and
the owner-selected notification destination and observer device. Supply nonsecret
values or protected file paths, never credential values in chat or Git.

## Installation-readiness review, 2026-10-25

The reviewed publication candidate includes the pending service implementation
and the following corrections. Experimental inference and IDE files are excluded.

- Local-first capture includes installed host-policy paths and every present
  gaming home. It refuses incorrect/shared SSD storage, repository symlinks or
  nested mounts, unexpected PVC writers, and device changes during capture.
- Off-site copy verifies the exact source and destination snapshots, records
  their separate IDs and preserves a usable local copy after network failure.
  Freshness follows the captured recovery point; retrying an old snapshot or a
  successful systemd result cannot make it fresh.
- WebUI uses the selected release's canonical API-key settings and correct file
  endpoint prefix. Retrieval fixtures apply the native path-matching semantics.
- A submitted image without proven completion retains its durable recovery
  marker even if Swarm acknowledges interruption with zero session counters.
- Native gateway CI runs on PR, main push and manual events, with bounded stages,
  disposable credentials and sanitized phase/container artifacts. No target
  credentials or raw startup/migration logs enter hosted artifacts.
- [Final installation acceptance](INSTALL-READINESS.md) includes package/boot,
  physical recovery, actual clients, images, Sunshine input/networking, both GPU
  handover directions and independent SSD/S3 restores. Unqualified workloads
  remain disabled.

Observed checks:

| Check | Result and scope |
| --- | --- |
| `make check-strict` on a clean publication-source candidate | **PASS**, exit 0, including 35 focused backup tests, native Alertmanager receiver tests and Prometheus local/off-site freshness fixtures. Candidate files matched the working source by SHA-256; permitted image/Linux exceptions remain |
| Native Restic 0.19.1 | **integration-tested**: two disposable local repositories; exact copy, different destination ID, idempotent retry, full read-data integrity checks and matching isolated synthetic restores from both copies |
| Independent Astra review at maximum reasoning | All material findings resolved; source review covers native WebUI contracts, image cancellation, storage/writer boundaries, snapshot joins and recovery-point metrics |
| Gitleaks 8.30.1 | Staged publication patch **PASS**. Selected-file scan identifies one existing public `K3S_RPM_GPG_KEY_SHA256`; verified unchanged from HEAD. No scanner rule or suppression changed |
| Native gateway containers | **integration-tested**, hosted run `40221003029` at commit `3f8fa579ca13aa41800501ce9bc8e9d5e2f674c8`: initial requests, restart persistence and cleanup all exit 0. **NOT RUN** locally: bounded Docker daemon check timed out after 20 seconds |
| Hosted strict source checks | **PASS**, exit 0, in [run `40221003029`](https://github.com/Drgr33nSenior/ArchLinuxThreadripper/actions/runs/40221003029) at commit `3f8fa579ca13aa41800501ce9bc8e9d5e2f674c8`: 66 test files, six Bats cases, Ansible/YAML/manifests and Linux systemd verification; only the two image gates below were skipped |
| Physical acceptance | **NOT RUN**: actual SSD, S3 transport/recovery credentials, K3s/PostgreSQL/application restore, Sunshine, image backend, LAN/TLS, notification delivery and GPU handover |

The first strict attempt in the original working directory was stopped after
discovery included unrelated untracked experiment archives. The full unchanged
gates then passed on a clean source candidate; no research files were removed or
checks weakened. Receipts are retained under
`test-results/install-readiness-20261025.lKhZgr/` and the separate private clean
validation directory's `test-results/strict-check.l1yQoF/`. This local receipt
precedes the hosted CI corrections below.

Hosted run `40220171336` failed during the initial client phase. Its phase-only
artifact did not identify the failing assertion. Source review then identified
that the pinned native API returns 404 for an unauthorized lookup. The corrected
fixture first requires both users' self-lookups to return 200 with the matching
`user_id`, then requires the cross-user lookup to return 401, 403 or 404. The
subsequent instrumented run confirmed both self-lookups and the 404 denial.
Hosted source validation also
exposed missing `xmllint` and Restic manual-page prerequisites and setup-python's
compiler search environment. Commit `3f8fa579ca13aa41800501ce9bc8e9d5e2f674c8`
adds those prerequisites and removes `LD_LIBRARY_PATH` and `PKG_CONFIG_PATH` only
for the strict-check command; the compiler-environment rejection guard remains.

The native gateway job in hosted run `40221003029` **passed** on 2026-10-25.
Sanitized evidence is retained at
`test-results/install-readiness-20261025.lKhZgr/native-gateway-second/native-service.IRMTp5/`.
Its phase/checkpoint and container records confirm native key creation, self-user
identity and cross-user denial, expiry/revocation, model restrictions, RPM/TPM and
concurrency limits, streaming/tool round trips, disconnect cancellation, bounded
backend failure, maintenance refusal and key persistence after gateway/database
restart. Setup, migration, both client phases and cleanup exited 0. The upstream
inference service was a deterministic fixture; this does not qualify a GPU model,
deployed clients or physical recovery. The source job in that run also **passed**.
Its strict receipt, including `make.exit` = 0, is retained at
`test-results/install-readiness-20261025.lKhZgr/hosted-source-second/strict-check.lqrJPN/`.
The only hosted strict skips were:

- `SKIP: set HOME_LAB_GAME_IMAGE to an already-built local image ID for streaming smoke tests`
- `SKIP: set HOME_LAB_WAYLAND_RUNTIME_IMAGE for isolated Linux process-group tests`
