# Focused correctness revalidation — 9 September 2026

> Dated evidence for the revisions named below, not a current installation
> procedure. Use [ISO.md](../ISO.md), [INSTALLATION.md](../INSTALLATION.md) and the
> relevant workload runbook for new work. Preserve the recorded failures and skips.


Starting checkouts were clean:

- Installer: `67a506090e8ecf696190e0be55f865e3ce054d0e`.
- Spry.ai-workstation-bridge: `deb6a93fcbb35fe7ea44f085b78d1ac174729d1b`.

Results below cover those revisions plus the changes described here. During
validation, Bridge moved externally to
`0d775872b18676147b10128781ad21ad432386cb` (`GPT-6 Audit`), containing these ten
reconciliation files; its working tree was then clean. The agent did not commit
or push. Installer edits remain uncommitted at `67a5060`.

No install, live workload change or firmware operation was performed. The host
is macOS/arm64. AI workload defaults and model/image selections,
cache mounts and qualification gates remain unchanged.

## Changes and evidence

| Finding | Change | Validation boundary |
| --- | --- | --- |
| Temporary compiler evidence leaked | `lib/workstation/performance.sh` removes its three named temporary files, retains unknown contents, preserves the primary exit status, and cleans owned workload/tunnel process groups. | Real shell orchestration with harmless external processes; both modes cover success, exit 7, INT/130, TERM/143 and unrelated process/file preservation. Kubernetes, HTTP workloads and telemetry are mocked. |
| Bounded profiler stopped twice | `kernel_run.py` recognizes only a pending generic-500 case; the exact source contract, same-session per-rank GPU trace exports and specific completion/error logs must verify it before success. `tests/hardware/sglang-profile-evidence.py` validates source hashes and bounded traces inside the selected Pod. | Stateful fixtures cover automatic completion and unknown/auth/transport/error-body/trace failures. Exact source methods also executed with fake Torch and transport; no image/GPU execution. |
| Discovery could report false success | `tests/run.sh` and `tests/syntax.sh` now check rg availability, pipeline exit and a nonempty discovered set before execution. | Missing tool, failed/partially failed discovery, empty set and valid set fixtures. Strict skip policy retained. |
| Standalone inference looked sealed | `rocm inference` now writes `smoke.json` and explicitly reports an unsealed all-card, layer-split smoke test. `docs/ROCM.md` directs measured comparisons to the existing sealed benchmark/quality commands. | Existing inference fixture checks the new status. Its fixed split and implicit thread/KV defaults are not changed. AMD SMI/legacy SMI documentation now follows provider selection. |
| Bridge Qwen drift | Bridge `internal/catalog/catalog.go` now matches 0.23.2 / `f56de980b316cd5410f067fbb62357481ebd66b8`. Generated metadata uses these pins. Added installer-derived fixtures, stale-version/commit refusals and `scripts/test-installer-contract.sh`. | All three native bundle schemas and hashes compare against a fresh export from installer commit `67a5060`. No earlier candidate patch was assumed. Import refusal and installed runtime hash ownership are unchanged. |

The SGLang source layer was rehashed as
`c31ff2cd4a0364cfb637ffc627ddef4f0de7a2f415301ff2982b775b0f84d86f`.
Its default scheduler exports at step 32 and then reports not-in-progress on
another stop. The tokenizer propagates that result as RuntimeError; the HTTP
route has no special success mapping. Generic HTTP 500 alone is therefore not
accepted. The new probe rejects a different profiler source or V2 mode. Trace
and log matching assumes an exclusive owner-approved profiling session. Missing
logs, custom log formats or insufficient bounded evidence fail closed.

Qwen's exact [settings schema](https://github.com/QwenLM/qwen-code/blob/f56de980b316cd5410f067fbb62357481ebd66b8/packages/cli/src/config/settingsSchema.ts)
and [generation configuration](https://github.com/QwenLM/qwen-code/blob/f56de980b316cd5410f067fbb62357481ebd66b8/packages/core/src/core/contentGenerator.ts)
retain the fields used in both exports. Source/schema review does not establish
installed-client execution or model/tool quality.

## Executed checks

The documented private dependency environment was reused; no dependencies were
installed. Python preflight identified `/usr/local/bin/python3.11`; the prepared
private Python supplies Jinja2/PyYAML through `HOME_LAB_PYTHON`.

```sh
source test-results/validation-e5281734.JEFO5V/environment-ansible.sh
bash tests/test_discovery.sh
bash tests/test_performance.sh
make check-strict
```

Both strict runs exited 0. The final run, including the error-response-body
regression and packaged documentation link, retained logs in
`test-results/strict-check.nGcJvo/`; the earlier run is in
`test-results/strict-check.ii77yC/`.
It completed 47 shell scripts (including the two explicitly skipped image
scripts), 37 Python tests, six Bats cases and three Ansible syntax checks.
Shell syntax, ShellCheck, shfmt, YAML parsing and Kubernetes rendering passed.
Ansible emitted empty-inventory warnings; no playbook ran against a host.

Exactly three strict-suite skips:

- Gaming-image smoke: `HOME_LAB_GAME_IMAGE` was unset.
- Isolated Wayland process checks: `HOME_LAB_WAYLAND_RUNTIME_IMAGE` was unset.
- `systemd-analyze` verification requires Linux.

Bridge commands, run in its separate checkout:

```sh
go test -count=1 -v ./internal/catalog ./internal/client
bash scripts/test-installer-contract.sh /Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI
make check
go test -count=1 -json ./...
```

All exited 0. `make check` included vet, tests, race tests, generated-source and
OpenAPI checks, manifest validation, builds, module integrity and govulncheck
(no vulnerabilities found). The fresh JSON run recorded 112 top-level test
passes (211 pass events including subtests), with one explicit skip:
`TestPackageFunctionInstallsOnlyReviewedPayloadOnLinux` requires GNU install on
Linux. Eight packages reported no test files; their compilation is not a test.
The systemd target reported compatible Linux systemd unavailable. Manifest
validation explicitly did not verify live Kubernetes enforcement.

Darwin build tags also excluded these Linux tests: adapter cross-UID staging,
staging seccomp/sandbox (amd64/arm64 variants), config deployment permissions,
host-executor peer credentials, and worker cgroup/namespace containment.
The exact excluded filenames are retained in `bridge-platform-exclusions.txt`.
The optional browser suite was not run; this increment changes no web UI.

Private artifacts are in `test-results/revalidate-67a5060.I1G1vC/`. The initial
model test exposed a fixture missing its run-kind field; that fixture was
corrected. The first source-method probe lacked the `os` mock namespace import;
its corrected execution passed. The source probe's initial failure log is
retained; the model-fixture failure was observed in console output. Neither
failure was a GPU observation. Documentation audits and final
diff checks are separate from performance qualification.

## Remaining acceptance and rollback

**NOT RUN — target workstation unavailable:** complete AMD image execution,
real model warmup/compilation, automatic profiler stop and per-rank exports,
restart cache reuse, numerical GPU correctness, sustained performance and
thermal/memory qualification. Use the exact model/workload and reviewed session
configuration from [MODEL-KERNELS.md](../MODEL-KERNELS.md):

```sh
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/kernel-hardware-01
./bin/workstationctl --config config/workstation.conf rocm kernel-evidence sglang-EXACT-POD artifacts/kernel-evidence-01
./bin/workstationctl --config config/workstation.conf rocm kernel-warmup artifacts/interactive.json artifacts/kernel-evidence-01 artifacts/kernel-warmup-01
./bin/workstationctl --config config/workstation.conf rocm kernel-profile artifacts/interactive.json artifacts/kernel-evidence-01 artifacts/kernel-profile-01
```

Use new output paths and the actual Ready Pod. Expected profile evidence:
`measured-not-qualified`, `profile_stop` equal to `explicit-completion-verified`
or `automatic-completion-verified`, matching nonempty GPU trace hashes for every
TP rank, checked memory, and unchanged Pod/runtime identity. Any missing evidence
must prevent success. The trace files remain private on the existing cache PVC;
the command does not copy arbitrary Pod logs or alter the workload.

Backup/restore (including bare-metal K3s/NAS consistency), signed ISO assembly
and disposable UEFI recovery boots, and gaming input/capture/audio/network
acceptance are **separate unfinished deliverables**. Earlier unsigned package
assembly and the recorded Xwayland input smoke failure are not superseded by
this source suite. See [AUDIT-FOLLOWUP-2026-09-09.md](AUDIT-FOLLOWUP-2026-09-09.md).

No tuning default was promoted. Roll back this increment through a reviewed
source change in each repository. Restore the matching Bridge catalog and
installer pin together; do not disable drift refusal. Re-export client bundles
to a new directory and separately review installed runtime hashes before any
deployment. Do not rewrite old evidence or delete persistent caches.
