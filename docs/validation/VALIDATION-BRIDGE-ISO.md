# Bridge ISO integration validation — 9 September 2026

> Dated evidence for the revisions named below, not a current installation
> procedure. Use [ISO.md](../ISO.md), [INSTALLATION.md](../INSTALLATION.md) and the
> relevant workload runbook for new work. Preserve the recorded failures and skips.


## Selected source and artifacts

Both checkouts were clean before this increment. No commits, tags, signatures,
publications, host installations or service starts were performed.

| Input | Identity |
| --- | --- |
| Installer base HEAD | `0d5c5b1c396292d7273dc1619b1d289e19e867ff` |
| Bridge base HEAD | `0d775872b18676147b10128781ad21ad432386cb` |
| Installer source archive, including this implementation | `e57a6a35bb7c4397c2106a3be02f9ecb9a198b31aab2d1cd2439ba750db3b57f` |
| Bridge source archive, including this implementation | `aca7e6450d9cfc30b9a1f0e039c67d1663eddc1417492ac57c58d991c76581d0` |
| Installer split-package version | `0.1.0.se57a6a35bb7c-1`, `any` |
| Bridge candidate | `spry-ai-workstation-bridge 0.0.0-1`, `x86_64` |
| Bridge package SHA-256 | `046fde2dff3f77baeae0c2d82f40ed143e375ce555756ced4c4b22e92167edf8` |
| Target dependency snapshot | `2026/09/04` |

The candidate is a private local build, not a published `0.0.0` release. HEADs
alone do not identify these uncommitted changes. The archived source manifests
and package hashes identify the selected pair. Subsequent documentation/test
changes do not silently update the frozen artifacts.

Retained local outputs, relative to the installer checkout:

- `test-results/bridge-package-final/`: reviewed Bridge source archive, generated
  recipe, `.BUILDINFO` inside the successful binary package, and retained build state.
- `build/iso/run-20260909-200736-85200/source/`: frozen installer source and recipe.
- `build/iso/run-20260909-200736-85200/packages/`: four unsigned split packages.
- `build/iso/run-20260909-200736-85200/bundled/`: five unsigned local packages,
  fresh repository database, `bridge-bundle.json`, `SHA256SUMS`, and 116 official
  runtime dependency archives (approximately 99 MiB) with original Arch signatures.
- That bundle's `evidence/`: actual catalog/native checks, runtime/reference
  manifests, dependency URLs, Bridge recipe/source, and an explicit pending
  signed-transaction record. These are not target executable approvals.

## Implemented boundaries

The ISO release coordinator now has an explicit `bridge` stage between package
building and owner signing. It accepts one reviewed package/source/recipe set,
checks `.PKGINFO`, `.BUILDINFO` and source identity, executes the selected binary's
offline catalog checks, resolves runtime dependencies against the installer
snapshot and rebuilds the local repository. Go remains build-only.

The new `arch-workstation-bridge-runtime` split package owns the runtime and
reference trees. Exact source allowlists preserve the existing configuration
authority; no active policy, credentials, target approvals or mutable state is
included. The Bridge recipe now records its source archive hash. Bridge's
existing pinned installer fixture remains, alongside a candidate-pair check.

`INSTALL_BRIDGE` defaults to true, including configurations which omit it.
Explicit false skips the target payload. Enabled preflight verifies trusted
signatures, package/source/snapshot identities, exact database entries and an
offline dependency transaction before disk erasure. Installation repeats the
checks immediately before target-root pacman installation. No dependency is
installed into the live root, and an ephemeral live repository URL in the
target's pacman configuration is refused.

Bridge is not a live ISO package. Target installation does not start or enable
its services/sockets, import policy, create credentials or qualify workloads.

Material source changes:

- Installer release: `infrastructure/iso/{release,docker,prepare,bridge-bundle}.sh`
  and `infrastructure/iso/docker/{setup,entrypoint}.sh`.
- Packaging: `infrastructure/packages/bootstrap/PKGBUILD`, `prepare-source.sh`,
  `source.files`, `bridge-runtime.files` and `bridge-reference.files`.
- Target installation: `bin/bootstrap-arch`, `lib/bootstrap/{bridge,config,preflight,install}.sh`
  and both generic/home-lab install configuration examples.
- Owner guidance: `docs/{ISO,ISO-REFERENCE,INSTALLATION,CODEX-INSTALL,BRIDGE-ISO}.md`
  and `.agents/skills/workstation-install/SKILL.md`.
- Installer tests: `tests/test_bridge_bundle.sh`, the three ISO tests and
  `tests/bootstrap/run.sh`.
- Bridge: `packaging/arch/PKGBUILD.in`, `cmd/bridge-arch-package/main.go` and its
  test, `cmd/bridge-hostd/main.go`, `internal/catalog/installer_test.go`,
  `scripts/test-installer-contract.sh` and `docs/ARCH-PACKAGING.md`.

## Executed validation

The private prepared environment is selected by
`test-results/validation-e5281734.JEFO5V/environment-ansible.sh`, with
`HOME_LAB_PYTHON` containing Jinja2 and PyYAML. No system Python packages were
installed. Raw local logs remain outside Git under `test-results/`.

| Command/check | Observed scope and result |
| --- | --- |
| Installer `make check-strict` | Passed, exit 0; 51 shell scripts (including the two documented image skips), 42 Python tests, six Bats cases, three Ansible syntax checks, shell syntax/lint/format, YAML and local Kustomize rendering. Evidence: `test-results/strict-check.dHBeoI/`. |
| Bridge `GOTOOLCHAIN=local make check` | Passed on macOS: formatting, tests/race checks, vet, generated contracts, deployment source validation, builds, module verification and vulnerability checks. |
| Bridge `makepkg --verifysource` then `makepkg --cleanbuild --noconfirm` | Passed in isolated unprivileged amd64 Arch container; source/module integrity and Linux package tests ran. No `--skipinteg`, `--nocheck` or signing. |
| `bash scripts/test-installer-contract.sh --candidate INSTALLER_SOURCE_EXPORT` in Bridge | Passed for the frozen selected installer export and the retained known-good fixture. |
| `bash infrastructure/iso/release.sh --execute packages` | Passed: all four actual unsigned installer packages. |
| `bash infrastructure/iso/release.sh --execute bridge ISO_RUN REVIEWED_BRIDGE_DIRECTORY` | Passed: actual selected binary/catalog/native checks, file manifests, official signatures, dependency resolution, exact local repository verification and export. |
| `bash tests/test_bridge_bundle.sh` | Passed: synthetic archives, mocked signatures/pacman; enabled/disabled, wrong identity/version/architecture, stale database, missing dependencies, preflight tampering, target-root-only calls and repeated state preservation. Includes epoch filenames and failed signature-check cleanup. |
| `bash tests/test_iso_docker.sh` and `bash tests/test_iso_release.sh` | Passed: scoped input mounts, candidate ambiguity, stage selection, incomplete-state refusal, preview and retry boundaries. No Docker calls from these fixtures. |
| Skill validator and relative-reference check | Passed; all 19 relative references resolve. |
| Actual staged-package tamper check | Passed in network-disabled, unprivileged amd64 container: valid import, changed runtime manifest/checksum refusal and corrupted native bundle refusal. No pacman installation. |
| Real official Arch signature check | Passed in a network-disabled container: original jq archive accepted; altered owned copy rejected. |
| Final source identity comparison | All 162 allowlisted installer files match the final frozen export. Regenerating the Bridge source archive from its current checkout produced an identical archive. |
| `git diff --check` in both checkouts | Passed. |

The strict suite's complete skip list is:

- Streaming smoke: `HOME_LAB_GAME_IMAGE` not supplied.
- Isolated Linux Wayland process-group test: `HOME_LAB_WAYLAND_RUNTIME_IMAGE`
  not supplied.
- Host `systemd-analyze` verification: requires Linux, unavailable on this Mac.

There were no unexpected skips. Ansible emitted empty-host-list warnings during
syntax-only checks; no playbook contacted a target. Bridge's Mac suite separately
reports Linux systemd and real Kubernetes enforcement/qualification as not run.
The optional candidate test is skipped in its general suite when no candidate
is supplied; the explicit candidate command above passed both tests. Actual
Linux `makepkg` testing and staged binary execution are distinct from target
systemd or pacman-install acceptance.

Reproduce the strict software check from the prepared controller environment:

```sh
source test-results/validation-e5281734.JEFO5V/environment-ansible.sh
make check-strict
```

The actual Bridge build used Go **1.27.1** (required by its `.go-version`), in a
private build-only image layered on the installer's dated builder. The official
Linux amd64 Go archive hash was verified as
`63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445`.
The target closure contains no Go compiler. Dependency semantics follow the
[pacman manual](https://man.archlinux.org/man/pacman.8.en) and
[repo-add manual](https://man.archlinux.org/man/repo-add.8.en).

Earlier failed evidence is retained, not relabelled as successful: Linux Bridge
packaging exposed the new source-identity file missing from its expected-content
test; split-package metadata exposed a pre-existing `$srcdir`-dependent Codex
pin; official signature verification required dearmoring Arch's keyring; cached
pacman URLs required an empty private cache; unsigned packages correctly failed
the signed transaction check. These causes were corrected and the final source
pair rebuilt. Signed transaction validation remains after owner signing, not a
signature-policy exception during unsigned preparation. An attempted bundle
before package export completed also refused the incomplete run. The first broad
strict run found one Docker-wrapper formatting mismatch; the corrected wrapper
was included in a fresh package/source run rather than editing frozen artifacts.

## Owner commands and remaining acceptance

Do not rebuild just to continue signing this successful run:

```sh
ISO_RUN="$(pwd)/build/iso/run-20260909-200736-85200"
# Review bundled/bridge-bundle.json, source records and SHA256SUMS.
# Set your reviewed SIGNING_FINGERPRINT; run ISO.md section 2's exact signing block.
bash infrastructure/iso/release.sh iso "$ISO_RUN" "$ISO_RUN/signing-key.asc" "$SIGNING_FINGERPRINT"
# Owner-only assembly after signature review:
bash infrastructure/iso/release.sh --execute --allow-iso-mounts iso \
  "$ISO_RUN" "$ISO_RUN/signing-key.asc" "$SIGNING_FINGERPRINT"
```

For a fresh source build or different Bridge candidate, follow
[BRIDGE-ISO.md](../ISO.md#1a-bundle-bridge-unless-explicitly-opting-out). Do not combine
these signatures/packages with another run. The same document provides the
local-console installation, explicit opt-out, first-boot setup and rollback.

After booting verified media, connect locally and review the install configuration:

```sh
bootstrap-arch --config /run/install.conf preflight
bootstrap-arch --config /run/install.conf --dry-run install
# Owner handoff only, after reviewing exact disks and all prerequisites:
bootstrap-arch --config /run/install.conf --execute install
```

The following remain **BLOCKED or NOT RUN**, not fixture-qualified:

- Owner signatures/trust, signed offline pacman transaction and actual
  network-disabled installation into a disposable target root/VM. No signing
  authority was exercised. Package ownership, sysusers/hooks, installed-tree
  policy verification, repeated real installation and inactive units must be
  checked there using the exact selected signed pair.
- Signed ISO assembly, disposable UEFI installation, cold boots, physical unlock
  and stable/LTS/recovery acceptance. Existing signing/boot boundaries remain.
- First-boot management setup: actual UIDs/GIDs, policies and manifests, address/
  TLS, owner credential and deliberate activation. Package installation is not
  proof of a working Bridge API.
- Physical GPU qualification, AI/gaming handover and gaming input acceptance;
  backup/restore acceptance remains a separate deliverable.

The offline test applies only to Bridge installation on the documented prepared
baseline. It cannot establish that the entire OS installer works offline.

## Earlier ISO build observations

The following record predates the Bridge bundle; package counts and builder
identities apply only to that historical run.

### 5 September qualification

As of 2026-09-05, the Docker tools image and unprivileged userspace check passed
on this Mac with builder tag `arch-workstation-iso-builder:acba2b40e867f6e24826`.
Archiso 90-1 reported `198 total files, 0 altered files`. The coordinator also
built and exported all three unsigned packages from a fresh source snapshot;
their checksums, launcher contents and package metadata passed inspection.
The subsequent signed-package run passed preparation but failed during ISO package
verification. An offline regression reproduced GPGME process exhaustion with a
64-PID limit; the child-reaping adapter passed 100 operations under the same limit.
The updated builder `arch-workstation-iso-builder:0b1a2f52d00187889d4f` also passed
the userspace check. `arch-install-scripts` reported `21 total files, 0 altered
files`; the adapter does not modify the installed upstream package.
These checks do not qualify a complete ISO. ISO assembly and boot validation
remain pending. See the [reference](../ISO-REFERENCE.md) for retained warnings, implementation details,
manual stage commands and recovery procedures.

Observed with Docker Engine 29.6.2 and Buildx 0.35.0-desktop.2. Docker's separate
`--check --platform=linux/amd64` validation also reported `InvalidBaseImagePlatform`
(expected arm64). This earlier warning remains unresolved; the separate Dockerfile
check has not been requalified. The actual amd64 build now succeeds. Package hooks
also reported an uninitialized `/etc/` tmpfiles rule and skipped system-manager
reloads because the container root is not booted. These did not fail the build.
