# Whole-project audit follow-up

> Dated evidence for the revisions named below, not a current installation
> procedure. Use [ISO.md](../ISO.md), [INSTALLATION.md](../INSTALLATION.md) and the
> relevant workload runbook for new work. Preserve the recorded failures and skips.


Started from clean `fb6dfc66cae4d0ba85473c6e41834cb105bebc7d` on 9 September
2026. This is an implementation increment, **not completion of the whole
audit**. The development host is macOS/arm64. No target performance gain,
installed-system recovery, GPU execution or streaming qualification is claimed.
The reviewed numerical CSV parser is unchanged.

## Coverage and compatibility

| Audit work | This increment | Qualification boundary |
| --- | --- | --- |
| Executable-only llama identity | `lib/workstation/llama_runtime.py` records all four executables, built shared libraries and versioned links, resolved linked dependencies, installed Vulkan ICDs, package inventory and amdgpu/kernel metadata. `rocm.sh` seals and checks the manifest before and after runs. | Real local library-replacement regression; Linux loader/GPU integration pending. This is not a complete trace of runtime `dlopen`, JIT or GPU code-object loading. |
| Benchmark/quality split mismatch | Shared effective configuration; explicit split, equal tensor shares and main GPU in both pinned CLIs. Optional paired-run binding rejects changed configuration, model, source, runtime and physical identities. | HIP/Vulkan invocation fixtures, not model execution. Context remains separately recorded because the pinned bench has no context flag. |
| Kernel verification | Initial install keeps stable-first policy. Installed verification accepts stable, LTS or git first, retaining all six identity-checked recovery entries. Git also needs matching, signed copies on both ESPs. | Mock select/verify regression; no firmware access. One-shot trial operation remains deferred. |
| AMD SMI | Provider-aware read-only inventory in `hardware.sh`; Arch prefers available legacy tooling, modern provider prefers AMD SMI. Missing, failed and malformed results remain explicit failures/pending observations. Post-workload capture uses the same path. | Both provider fixtures pass. Exact installed AMD SMI and Radeon sensor coverage pending. Run-aligned sampling still uses existing sysfs telemetry; optional CLI metric output is raw, not normalized performance evidence. |
| K3s patch | `versions.lock` selects 1.35.8+k3s1, preserving the 1.35 minor guard. | Release checksum, API digest and downloaded binary hash agree. No server started or live upgrade performed. |
| Qwen Code patch | 0.23.2 with npm gitHead `f56de980b316cd5410f067fbb62357481ebd66b8`; existing route, approvals and output-budget fields retained. | Source/schema review and local wrapper fixtures, not installed-client execution. |
| Strict software checks | `make check-strict` retains logs, exit status and every skip in a new private directory. Unexpected skips or failures fail the target. | Three named image/Linux exceptions are allowed but printed; passing this target never means all image/hardware checks passed. |
| Gaming delivery | Current Wayland recipe builds; existing image smoke test was executed. | Image smoke FAILED: packaged Xwayland does not advertise `-enable-ei-portal`. Linux process-group checks pass in the image. Gaming stays disabled. |
| NAS recovery | Existing Restic boundary inspected; no new backup workflow yet. | Requires an explicit backend selection, coherent quiescence/state capture, credential recovery and isolated restore implementation/drill. VM backup does not protect bare-metal SQLite/PVCs. |
| Signed ISO and UEFI installation | Existing guarded build path retained; package-build status is recorded below. | No signing fingerprint/public key selected for this run. No privileged ISO assembly or disk/UEFI VM installation performed. |
| SGLang, development llama, graphics and TheRock | Baselines retained, apart from the explicit maintenance pins above. | New Radeon image inspection, hybrid-state sweeps, matched scaling, newer immutable llama candidate, newer coherent KWin/Qt/Mesa/Xwayland, and complete TheRock sealing/build/packages remain unimplemented in this increment. |

Old llama candidates without a runtime manifest are intentionally rejected.
Rebuild into a new directory. Do not fabricate a manifest for an old result or
copy new library hashes into old evidence. Execute binaries in their retained
candidate location. System package changes require new candidate evidence.
Unset loader/Vulkan/GGML/llama environment overrides before measurement; do not
silently inherit `LD_PRELOAD`, `LD_LIBRARY_PATH`, `VK_*`, `GGML_BACKEND_*` or
`LLAMA_ARG_*` values. The manifest uses `ldd` only on trusted locally built
artifacts; it is not an inspection command for untrusted downloaded programs.

The pinned [llama common parser](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/common/arg.cpp)
accepts comma-separated device/tensor-share lists. Bench uses slash-delimited
lists. The shared resolved policy is translated at those command boundaries.
[AMD SMI CLI documentation](https://rocm.docs.amd.com/projects/amdsmi/en/latest/how-to/amdsmi-cli-tool.html)
defines the read-only `list`, `version` and `metric` commands. Unsupported JSON
schemas fail explicitly; an available binary alone is not successful telemetry.

## Reviewed updates

The [K3s release](https://github.com/k3s-io/k3s/releases/tag/v1.35.8%2Bk3s1)
updates containerd to 2.2.7-k3s1 and bundled local-path-provisioner to 0.0.37.
Its Traefik chart v40 update renames the ingress-nginx migration provider from
`kubernetesIngressNginx` to `kubernetesIngressNGINX`. Review existing
HelmChartConfig/custom values before any upgrade. Preserve matching datastore,
token and PVC backups first. Binary rollback alone is not a datastore rollback.
The default bare-metal NAS recovery gap is still a deployment blocker. The
separate VM retains its existing tested-source maintenance workflow.

The [Qwen CLI release](https://github.com/QwenLM/qwen-code/releases/tag/v0.23.2)
and [npm metadata](https://registry.npmjs.org/@qwen-code/qwen-code/0.23.2)
agree on the CLI version. Reviewed `settingsSchema.ts` and
`contentGenerator.ts` at the locked commit retain `modelProviders.openai`,
`envKey`, `generationConfig.contextWindowSize`, `streamIdleTimeoutMs` and
`samplingParams.max_tokens`. Node >=22 is required. No new remote daemon,
browser endpoint, agent service or automatic updater is enabled.

| Component | Baseline | Reviewed candidate/latest boundary |
| --- | --- | --- |
| K3s | 1.35.7 before this increment | 1.35.8 selected for builds; 1.36 migration not reviewed here |
| Qwen CLI | 0.23.0 before this increment | 0.23.2 selected; not its separately versioned SDK |
| llama.cpp | stable commit 427291b5 / 0.4.0 | Development candidate not yet sealed; no floating main |
| Native ROCm | official Arch provider; explicit ROCm 10 binary provider | Installed versions unknown; TheRock remains planning/inventory |
| AMD SGLang | locked 0.5.15.post1 ROCm 10 image | No newer image promoted or unpacked in this increment |
| Gaming | locked Sunshine/Mesa/KWin recipe | Built, but input packaging fails smoke; newer graphics closure remains required |

This table is a dated review record, not an automatic latest-release resolver.
Newest upstream, a coherent build and a measured winner remain different labels.

## Reproduce software checks

The existing prepared private environment remains usable:

```sh
source test-results/validation-e5281734.JEFO5V/environment-ansible.sh
make check-strict
```

For a new **CPython 3.11/macOS arm64** environment, the reviewed wheel closure is
checked in as `tests/validation-macos-arm64.lock`. Use the IDE-selected Python
to create a private virtual environment. Install with:

```sh
PATH_TO_PRIVATE_ENV/bin/python -m pip --isolated install \
  --index-url https://pypi.org/simple --only-binary=:all: --require-hashes \
  -r tests/validation-macos-arm64.lock
export HOME_LAB_PYTHON=PATH_TO_PRIVATE_ENV/bin/python
export PATH="PATH_TO_PRIVATE_ENV/bin:$PATH"
```

These are placeholders for an explicitly created private environment, not
system pip commands. Other platforms need a separately reviewed wheel closure;
never remove hash enforcement to make this lock fit them. Shell/Helm tools are
still separate prerequisites; the complete dependency-preparation record is in
[VALIDATION-e5281734.md](VALIDATION-e5281734.md). A cross-platform hermetic
validation image is not delivered by this lock.

## Target acceptance and rollback

Use a fresh hardware report and the same local GGUF and quality corpus for all
candidates. Replace paths and device names with observed values:

```sh
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/audit-hardware-01
./bin/workstationctl --config config/workstation.conf rocm build-llama /path/to/locked/llama.cpp artifacts/audit-hardware-01/hardware.json artifacts/audit-hip-01
./bin/workstationctl --config config/workstation.conf rocm build-llama-vulkan /path/to/locked/llama.cpp artifacts/audit-hardware-01/hardware.json artifacts/audit-vulkan-01
./bin/workstationctl --config config/workstation.conf rocm benchmark-llama artifacts/audit-hardware-01/hardware.json artifacts/audit-hip-01/build/bin/llama-bench artifacts/audit-vulkan-01/build/bin/llama-bench /path/to/model.gguf artifacts/audit-pair-01 ROCm0/ROCm1 Vulkan0/Vulkan1
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/audit-hardware-01/hardware.json artifacts/audit-hip-01 /path/to/model.gguf /path/to/corpus.txt artifacts/audit-quality-hip-01 ROCm0/ROCm1 artifacts/audit-pair-01
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/audit-hardware-01/hardware.json artifacts/audit-vulkan-01 /path/to/model.gguf /path/to/corpus.txt artifacts/audit-quality-vulkan-01 Vulkan0/Vulkan1 artifacts/audit-pair-01
sudo ./bin/bootstrap-arch --config config/install.conf verify
```

Expected: retained runtime manifests, identical matched effective configuration,
successful executable/telemetry/CSV checks, positive selected allocations and
no unselected allocation. `quality.json` still requires human model-quality
review. On one GPU, select one observed backend device on each side and use
`LLAMA_SPLIT_MODE=auto` or `none` in the configuration. For row splitting,
select `LLAMA_SPLIT_MODE=row` for both benchmark and qualification. Do not compare
different models as a scaling result.

Run the host/Pod IPC, peer-copy, RCCL, serving and encrypted scratch-file
procedures in [PERFORMANCE-VALIDATION.md](../PERFORMANCE-VALIDATION.md). Those
commands still require the actual workstation, model bundle, exact Pod and
owner-approved workload window. Restore kernel preference with the existing
`sudo ./bin/workstationctl kernel rollback stable` or `lts` only during an
approved target maintenance session, then rerun `bootstrap-arch verify`.
This source task does not issue those firmware writes.

Pin rollback is a reviewed source revert plus a new build/bundle; it does not
reverse a deployed K3s transaction or change an installed agent. Keep the old
signed packages, images and private state backups. No performance defaults,
driver ownership, storage layout, security mitigations or no-swap policy changed.

## Retained evidence

Private artifacts are under `test-results/audit-fb6dfc6.mL5BaQ/` and the dated
`test-results/strict-check.*` directories. They contain release metadata,
the checksum-verified K3s binary (never executed), image build logs/IDs and smoke
results. Do not publish raw host identities or credentials. No Git commit,
registry publication, cluster mutation, disk erase or firmware change occurred.

The first full check ran all 46 shell scripts, 15 measurement tests, three
runtime-manifest tests, six Bats cases and three Ansible syntax checks. It
failed ShellCheck on three new fixture annotations; those were corrected.

The final `make check-strict` completed with exit 0; evidence is in
`test-results/strict-check.8fk5V4/`. All 46 shell scripts returned success
(including the explicit gaming-image skip), 15 measurement tests, three runtime
tests, six Bats cases and three Ansible syntax checks passed. Shell syntax,
ShellCheck, shfmt, YAML parsing and Kubernetes rendering passed. Ansible printed
its expected empty-inventory warnings; no playbook ran against a host.

Exactly two checks were skipped inside that run:

- The complete gaming-image smoke gate: `HOME_LAB_GAME_IMAGE` was unset. Its
  separate execution **failed**, as described below; strict-suite success does
  not override that result.
- `systemd-analyze` verification: the development host is macOS. Linux service
  executable/dependency validation remains pending.

`HOME_LAB_WAYLAND_RUNTIME_IMAGE` selected the built image during the final suite,
so its isolated Linux process-group checks ran rather than being skipped. The
documentation clarity audit and `git diff --check` also passed. Physical GPU,
model-quality, performance, firmware, NAS restore and streaming qualification
remain **NOT RUN — target hardware unavailable**.

The runtime-manifest tests also pass on macOS/Python 3.11 and in an isolated
Linux/amd64 container with Python 3.14. Both execute a real shared-library
replacement with an unchanged executable. Host package, driver and dependency
probes are mocked; this is not GPU integration evidence. Earlier Linux attempts
exposed scratch-directory execution restrictions and test-environment/mock
differences. Their failures are retained alongside the corrected runs in
`linux-runtime-icd.log` and `macos-runtime-icd.log` (both exit 0).

The existing Wayland recipe completed with image ID
`sha256:36eed15bf9d78a2faf840688d242b7eb72431aac50b7a3bda2d636f56f2f34bd`.
The fresh `gaming-final-build.log` records exit 0. Its separate image smoke test
fails because Xwayland `2:24.1.6-1` lacks `-enable-ei-portal`; subsequent smoke
assertions did not execute. `wayland-final-process.log` records successful
isolated Linux process-group checks, not capture or input qualification.
Reproduce these distinct checks without pulling or publishing an image:

```sh
HOME_LAB_GAME_IMAGE=sha256:36eed15bf9d78a2faf840688d242b7eb72431aac50b7a3bda2d636f56f2f34bd bash tests/test_sunshine_image.sh
HOME_LAB_WAYLAND_RUNTIME_IMAGE=sha256:36eed15bf9d78a2faf840688d242b7eb72431aac50b7a3bda2d636f56f2f34bd bash tests/test_wayland_processes.sh
```

`bash infrastructure/iso/release.sh --execute packages` completed with exit 0.
It produced three unsigned packages, repository metadata, checksums and source
locks in `build/iso/run-20260909-160800-10377`. Their version is
`0.1.0.scecf401aaacf-1`; the recorded source snapshot identifies their inputs,
not later documentation edits. Retained Docker build artifacts were not pruned.
Continue at the explicit signing/trust gate in [ISO.md](../ISO.md), selecting that
run and an owner-controlled signing identity. Unsigned package assembly does
not qualify a signed ISO, installation, firmware unlock or recovery boot.

Next experiments, after correctness and recovery gates: same-model one/two-card
and independent-worker scaling (TTFT and useful aggregate throughput); pinned
newer llama HIP/Vulkan (prompt/decode spread and quality); coherent newer graphics
(working input, distinct-frame cadence and latency); SGLang hybrid-state pools
(long-context latency and peak memory). Keep only repeated improvements beyond
noise without stability, quality or memory regressions.

## Earlier AI performance records

The following earlier tranche notes are historical. In particular, the original
RAG base-overlay description is not the current model/deployment selection;
use [RAG.md](../RAG.md) and [MODELS.md](../MODELS.md) for current configuration.

### Local validation record

`HOME_LAB_PYTHON=/usr/local/bin/python3.11 make check` passed with 32 test
scripts on the macOS development host. This includes shell syntax/ShellCheck,
YAML and Kustomize rendering, Jinja-generated K3s configuration, exact offline
CPU-reservation predicates, installer/source-generation dry-runs, hardware and
build fixtures, and session failure/recovery tests. The peer diagnostic was
compiled against a deliberately simulated HIP header, not a ROCm installation.

Unavailable checks were reported, not treated as executed: shfmt, Bats,
Ansible syntax-check (the Homebrew launcher references a missing Python 3.8),
Linux `systemd-analyze`, a local pinned GPU-operator chart archive, real ccache
repeat compilation and real CMake/Ninja fixture generation. The selected
PyCharm Python 3.11 SDK did run the Jinja/YAML configuration checks. No installer,
cluster mutation, firmware action, real ROCm build, ISO assembly, disk benchmark
or performance workload ran on this development host.

## RAG and context tranche — 2026-09-08

The opt-in `apps/overlays/rag` composition extends `single-gpu` without changing
the other profiles. It pins Open WebUI 0.11.3 and a small CPU embedding model,
uses embedded Chroma, bounds threads/uploads/chunks, and keeps zero replicas
with pending qualification. It does not request a GPU or alter SGLang's context,
host packages, gaming allocation or no-swap policy.

`bin/workstationctl rag stage-models` prepares hash-verified offline assets;
`rag verify-models` checks them after transfer. `rag corpus` snapshots explicitly
selected Markdown documents with source hashes and nullable Git provenance.
The new `webui-rag-data` PVC isolates pilot settings/data from `webui-data`.
Git-managed settings override the Admin UI in this profile; read the migration
and rollback notes before activating it.

Tests are `tests/test_rag.sh` and `tests/home-lab/check-rag.rb`. The seed questions
in `tests/fixtures/rag/questions.json` include unknown-hardware and invented-gain
cases. They are test expectations, not measured answers. See [RAG.md](../RAG.md) for
the evidence/decision matrix, exact preparation commands, acceptance metrics,
privacy boundaries, storage retention and deferred pgvector/reranking/graph work.

Local validation passed on 2026-09-08: `make check` with the configured Python
SDK (37 test scripts), Kubernetes 1.35 schema validation (28 objects per RAG
overlay) and actual staging/verification of all 11 model files. Optional tooling
skips and the unrun application/hardware tests are listed in the
[RAG verification record](../RAG.md#local-verification-record--2026-09-08).
