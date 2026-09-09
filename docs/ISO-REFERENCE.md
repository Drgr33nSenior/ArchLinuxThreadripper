# ISO reference: manual stages, implementation and recovery

Use [ISO.md](ISO.md) for the normal controller workflow and
[INSTALLATION.md](INSTALLATION.md) at the workstation. This reference is for
manual/native builds, trust boundaries, diagnostics and disposable acceptance.
Do not run these lower-level steps in addition to the release coordinator.

Dated build results and failures are retained under [validation/](validation).
They do not establish a boot-qualified or Secure Boot-qualified workstation.

## Components and boundaries

- `infrastructure/packages/bootstrap/`: allowlisted source bundle and split
  PKGBUILD. `arch-workstation-bootstrap` provides the live CLI, templates and
  docs. `arch-workstation-boot` owns the installed UKI sync helper and hook.
  The optional `arch-workstation-backup` package owns the Restic helper, units
  and policy; it is not installed on the live image or base host.
  `arch-workstation-bridge-runtime` supplies the target-owned runtime/reference
  closure. Follow [Bridge bundling](ISO.md#1a-bundle-bridge-unless-explicitly-opting-out) to select the separate reviewed
  Bridge package and seal dependencies before signing. Neither Bridge package
  is installed in the live root.
- `infrastructure/iso/profile/`: reviewed changes to Archiso's `releng` profile.
- `infrastructure/iso/docker/`: pinned builder image and fixed container actions.
- `infrastructure/iso/docker.sh`: dry-run wrapper for image creation, userspace
  checks, package builds and ISO assembly. It only accepts a local Docker Desktop
  context, classified as a development builder. It never changes the active context.
- `infrastructure/iso/release.sh`: coordinates those stages with a fresh source
  snapshot and one run directory. It never signs artifacts or deletes old jobs.
- `infrastructure/iso/prepare.sh`: verifies local signed inputs and creates a
  fresh profile. It never installs packages, builds an ISO or changes host trust.
- `infrastructure/iso/build.sh`: prints the build command by default. Explicit
  `--execute` runs mkarchiso on the isolated Arch builder.
- `infrastructure/iso/vm/test-vm.sh`: creates/boots disposable file-backed QEMU
  fixtures only with `--execute`. It never uses libvirt, VFIO, host USB, shared
  directories, port forwarding or physical block devices.

The source bundle uses exact file paths, rejects symlinks and records per-file
SHA256 hashes. It does not copy the entire working tree. Package versions include
the source archive hash, including the packaging recipe and launchers. The
repository currently has no project-wide license: local evaluation is supported,
but redistribution requires an owner licensing decision.

The prepared PKGBUILD contains literal archive, version and epoch pins. It checks
its own rendering against the canonical template inside the verified archive.
Launchers are installed only from that archive, never from adjacent mutable
files. The exported `source.lock` is an audit record, not an undeclared makepkg
dependency; the generated recipe and declared tarball are self-contained inputs
for standard source packaging. Review the recipe before executing makepkg:
self-checks do not make arbitrary PKGBUILDs trusted code.

The ISO retains an explicit local shell login. It does not run the installer at
boot. SSH, cloud-init and releng's `script=` startup execution are disabled.
The menu is started by the operator with `arch-workstation-live`. It inspects
hardware, previews the existing installation, starts an explicitly confirmed
installation, or opens this recovery guide. It never records passwords or PINs.

The installed host now defaults to `HOST_PROFILE=headless`, including the
disposable VM and home-lab examples. It boots to a multi-user target without
GNOME/GDM or host Steam. Set `HOST_PROFILE=desktop` only when a local desktop is
wanted. The live ISO remains a text installer in either case. This default does
not remove packages or change services on an already installed host.

The live medium uses its own UEFI systemd-boot menu. The installed host retains
direct UKI boot. This initial live medium is **not Secure Boot qualified**. Do
not assume that signatures on the host UKIs authenticate the live filesystem.
Signed live boot and payload verification require a separate release milestone.
Retain trusted alternate recovery media until that path has been tested.

## Docker prerequisites and boundaries

Use Docker Desktop with working `linux/amd64` containers. The wrapper explicitly
selects `desktop-linux`; use `--context NAME` before the action if your local
Desktop context has another name. Remote/TCP/SSH contexts and non-Desktop engines
are refused. Do not disable Enhanced Container Isolation, seccomp or other
Desktop controls to make a build pass.

The Dockerfile pins the official Arch base image by its amd64 manifest digest.
The existing `versions.lock` selects the Arch repository snapshot and Archiso
version. Image tags include a hash of all builder inputs. Changing the recipe or
lock requires rebuilding the builder image; package manifests record its image ID.
The Docker build context is allowlisted and does not include the repository root.

Package builds run as UID 1000, with no network or Linux capabilities. ISO assembly
runs as container root with `CAP_SYS_ADMIN` for package-installation/chroot mounts.
It requires both `--execute` and `--allow-iso-mounts`. This is a significant privilege
boundary, not a general-purpose sandbox. The default Docker security profiles stay
enabled. No stage uses `--privileged`, host devices, host namespaces, the Docker
socket, your personal GnuPG directory or writable source bind mounts.

The existing `setup.sh` command uses `--disable-sandbox-syscalls` to get past
`error restricting syscalls via seccomp: 22` during image construction. Inspection
of the built image also confirmed that the pinned base image already enables
`DisableSandboxFilesystem`. Together, these disable both pacman downloader filters
for that package transaction. `DownloadUser = alpm` and package-signature checks
remain enabled, as do Docker's own security controls. This is a builder-only
compatibility exception, not a host security policy. Do not copy the builder's
pacman configuration into the live ISO or installed host; their configuration
comes from separate project templates. No sandbox setting was changed by the
documentation-directory fix.

If pacman's download sandbox fails, retain the error and stop. Do not add
`DisableSandbox`, `--disable-sandbox`, weak-signature exceptions or an unconfined
Docker security profile. Use a backend that supports the required sandbox
operations, or the native x86_64 Arch fallback. Changes to Docker's VM/backend or
use of another machine require a separate, explicitly reviewed setup.

Jobs use Linux-backed named volumes and export artifacts with `docker cp`.
They have a 6 GiB memory limit, no container swap, four CPU equivalents and a
512-process limit. Leave memory for the Docker VM itself and keep sufficient disk
space for downloaded packages and the unpacked live filesystem. These limits do
not disable macOS swap or change Desktop settings. Image construction uses
Desktop's configured build resources.

Use a fresh job name and output directory for each attempt. Job containers and
volumes are retained after success or failure. No command prunes or deletes them.
Their names are printed; inspect each exact resource before any manual removal.
Signing keys are never supplied to containers. Temporary pacman trust keys exist
only in tmpfs, not in image layers or exported artifacts.

## 1. Prepare the source package

On a trusted checkout, with Bash, libarchive `bsdtar`, gzip and a SHA256 tool:

```sh
mkdir -p build/iso
bash infrastructure/packages/bootstrap/prepare-source.sh build/iso/source-01
```

Review `source.lock`, `project/SOURCE-MANIFEST.sha256`, `PKGBUILD` and all
packaged launchers. The archive is normalized for file order, ownership and timestamps.
This is source-archive repeatability, not a claim of bit-identical ISO builds.
No private signing key is an input to source preparation.

For Docker builds, use the package command in [ISO.md](ISO.md#1-build-fresh-packages);
the coordinator prepares the source, checks userspace and exports all four
split packages. Native Arch source builds are described below.

If image construction reports `archiso: /usr/share/doc (No such file or directory)`
and the equivalent warning for `/usr/share/man`, the failure is from
`pacman -Qkk archiso`, not seccomp. The official base image's
[NoExtract rules](https://github.com/archlinux/archlinux-docker/blob/master/pacman-conf.d-noextract.conf)
omit documentation. Builder setup restores these two shared parent directories
before package installation, with mode `0755`. Documentation contents remain
subject to `NoExtract`; package-file verification still stops on other mismatches.
Do not suppress the check with `|| true`. Rebuild with the commands above; the
changed recipe generates a new image tag without pruning existing images.

The intended outputs are four `*.pkg.tar.zst` files: bootstrap, boot, backup and
Bridge runtime/reference payload.
Inspect `.PKGINFO`, `.BUILDINFO` and archive contents. None should contain an
install scriptlet. The boot package must contain its helper and shared validator
under `/usr/lib` and its hook under `/usr/share/libalpm/hooks`. The optional
backup package contains `/usr/lib/arch-workstation-backup/run`, vendor systemd
units and pacman-backed-up policy files under `/etc/restic`. Neither runtime
package installs into `/usr/local` or starts a service.

The job also exports an unsigned `arch-workstation.db.tar.gz` repository database,
checksums, a package inventory and the builder image ID. Use your existing approved
signing process outside Docker. Keep matching packages and database in one release directory. For the default Bridge-enabled ISO, first complete the bundle
stage; sign its five local packages, new database and manifest instead, retaining
the official dependency signatures. Use ISO.md's signing block for that run.
Signing is owner-run; never treat it as agent-run publication.
Never copy a private key or a complete personal GnuPG home into build artifacts.

## 2. Review signed release inputs

Use the reviewed inputs in `infrastructure/iso/versions.lock`: Archiso 90-1,
the recorded v90 source commit and the 2026/09/04 Arch repository snapshot.
The Dockerfile upgrades only its container filesystem coherently against that
snapshot. Preparation checks the installed Archiso version and package-file
integrity and records the builder inventory. No host packages are installed.

Export the selected project **public** key as armored text. Supply its full
uppercase 40-character primary fingerprint through a trusted channel. ISO assembly
creates an ephemeral pacman keyring, then calls the existing `prepare.sh` and
`build.sh`. Preparation verifies all package/database signatures against that
fingerprint. Do not weaken signature verification. Only the selected artifacts
and public key are mounted; the signing directory itself is not mounted.

The live image contains only the selected public signing key. A live-only unit
adds it to the ephemeral pacman keyring after `pacman-init.service`. If that unit
fails, installation must stop before disk erasure. This trust is specific to a
release whose ISO signature you verified; it is not permission to trust random
package keys. Verify the ISO through an independent trusted channel before use.

Packages used to install the OS still come from the dated online repository.
The default Bridge bundle carries five local archives plus official runtime
dependencies. Only bootstrap and boot are installed in the live root; Bridge and
its runtime/reference package are target payloads, and backup remains opt-in.
Without Bridge, assembly carries bootstrap and boot only, and target installation
must select `INSTALL_BRIDGE=false`. Neither path makes the whole OS installer
offline. Keep the snapshot through initial transactions, then follow the
[full-upgrade procedure](OPERATIONS.md#move-from-the-installation-snapshot-to-rolling-arch).

## 3. Manual and native assembly

Use [ISO.md](ISO.md#3-assemble-the-iso) for coordinated Docker assembly.
It checks mount-namespace support, refuses existing job/output state and limits
compression to four workers on disk-backed storage. Do not add privileges to
work around mount or emulation failures. Review the ISO contents, manifests and
checksums, then sign the ISO separately from package and Secure Boot signing.

For a subsequent release, review `BOOTSTRAP_PACKAGE_VERSION`, snapshot and
source epoch together. Source hashes distinguish inputs but do not establish
package upgrade order.

### Native Arch fallback

On an isolated x86_64 Arch builder prepared against the same snapshot, the original
scripts remain supported. Transfer the source bundle and use the same reviewed
checkout. Run `makepkg --cleanbuild` as an ordinary user from the prepared source
directory, with `SOURCE_DATE_EPOCH` from `source.lock`. Create the repository
database with `repo-add`. For the default target, run the same
`infrastructure/iso/bridge-bundle.sh PACKAGES BRIDGE_INPUTS NEW_OUTPUT` in that
prepared unprivileged builder before signing; retain the matching `source.lock`.
Sign the five bundled local packages, new database and manifest through
[ISO.md's owner procedure](ISO.md#2-review-and-sign-this-runs-packages).
For an explicit Bridge-disabled release, sign the four split packages and
database instead.
Also test `makepkg --source` and rebuild from its extracted archive without
adjacent launcher files or `source.lock`; offline reconstruction tests do not
replace this real Arch acceptance check.

Initialize/populate a dedicated builder pacman keyring, add the reviewed public
key and locally sign its exact fingerprint. From the repository root:

```sh
bash infrastructure/iso/prepare.sh \
  /absolute/path/to/signed-packages /absolute/path/to/project-public.asc \
  YOUR_REVIEWED_FINGERPRINT /absolute/path/to/dedicated-builder-keyring \
  build/iso/prepared-01
bash infrastructure/iso/build.sh build/iso/prepared-01
sudo bash infrastructure/iso/build.sh build/iso/prepared-01 --execute
```

The dedicated keyring is not copied into the ISO. Native build configuration paths
must not contain spaces or delimiters.

## 4. Disposable UEFI installation tests

Boot tests are separate from Docker ISO assembly. Run the harness as a non-root
user on a machine with QEMU and OVMF installed. The
default is a printed plan, with TCG, four virtual CPUs and 8 GiB RAM. Installation
uses two standalone, sparse 64 GiB raw files with fixed QEMU NVMe serials. Disk
paths must be absolute, owned regular files. Raw format is forced so disk contents
cannot specify backing chains or external data files. Do not point the harness at
existing VM disks or a real firmware store.

```sh
bash infrastructure/iso/vm/test-vm.sh create \
  /absolute/path/to/new-test-vm /usr/share/edk2/x64/OVMF_VARS.4m.fd
```

Inspect the plan, then add `--execute` to create those disposable files. Boot:

```sh
bash infrastructure/iso/vm/test-vm.sh boot \
  /absolute/path/to/new-test-vm /usr/share/edk2/x64/OVMF_CODE.4m.fd \
  /absolute/path/to/reviewed.iso --network
```

Again, only `--execute` starts QEMU. `--network` enables outbound user-mode NAT
for installation, with no inbound forwarding. Without it there is no network.
No transcript is saved: enter test passphrases only in the guest console.

Acceptance procedure:

1. Confirm native UEFI boot and that no installation or SSH service started.
2. Copy the packaged `infrastructure/iso/vm/install.conf.example` to `/root/vm.conf`.
   Set both disk paths from the guest's actual `/dev/disk/by-id/` links. Verify
   serials `ARCHLAB_TEST_A` and `ARCHLAB_TEST_B` and model `QEMU NVMe Ctrl`.
3. Run `bootstrap-arch --config /root/vm.conf preflight`, then the default dry-run
   install. Inspect the layout. Reject an incorrect serial or confirmation and
   verify that no storage operation follows the rejection.
4. Confirm the live signing-key unit completed. Run the explicit `--execute`
   installation and enter disposable disk-unlock, named-user and root-recovery
   passwords interactively. Verify two ESPs,
   RAID0, LUKS2/Argon2id, XFS and signed stable/LTS/recovery UKIs. Use `pacman --root
   /mnt -Qo` on the runtime helper and hook to confirm package ownership.
5. Shut down the guest manually. Boot the same fixture with `-` instead of the ISO
   path. Test passphrase unlock and stable, LTS and fallback UKI boot separately.
   Copy the reviewed config/installer into the installed guest if needed for
   `bootstrap-arch verify`. Record `/proc/swaps`, `findmnt /`, `mdadm --detail`
   and signature/BootOrder verification without recording credentials.
   Verify authenticated emergency-console access, the multi-user default target,
   absence of GDM/Steam, and TuneD's balanced profile for the headless fixture.
   Test a reviewed initramfs-triggering update and confirm signing precedes
   synchronization, including rejection of a missing or wrong ESP mount.
6. Refuse installation over the existing array. Rerunning an interrupted storage
   installation is not an idempotent repair operation. Inspect it manually or use
   a newly created disposable fixture; never reset disks automatically.

`VM_TEST_MODE=true` is accepted only with QEMU/KVM virtualization, QEMU DMI and
both exact synthetic NVMe identities. It bypasses physical GPU package selection,
not disk safety or encryption. An installed marker identifies the VM test. It
does not validate the R9700s, physical TPM, PCIe/IOMMU topology or CPU optimization.

## 5. Recovery test

Run `test-vm.sh recover DIR OVMF_CODE ISO` and review the plan before adding
`--execute`. Recovery disables networking and uses temporary disk overlays; writes
inside that guest do not change the original two fixture images. Firmware changes
remain confined to this fixture's copied OVMF variable file. No real TPM is exposed.

In the guest, inspect the two members before assembly. For a read-only filesystem
test, explicitly assemble mdadm read-only, open LUKS read-only with the recovery
passphrase, and mount XFS with `ro,norecovery` at an unused mountpoint. An unclean
XFS log may require a separate writable **disposable-copy** recovery experiment;
do not perform repair against the original data to obtain a passing result.
Confirm the installed system files and backup ESP UKIs are available. Shut down,
then boot the original installed fixture again.

On the real workstation, recovery must also include an offline LUKS header backup
and tested passphrase fallback. Do not enroll TPM PCRs from the live ISO. Physical
TPM+PIN, early-PCR11 policy, Secure Boot and multi-GPU tests remain target-hardware
acceptance gates. Refer to [HOME-LAB.md](HOME-LAB.md) and [INSTALLATION.md](INSTALLATION.md).

## Verification and maintenance

`make check` includes source allowlist/archive-repeatability, recipe/metadata and
launcher integrity, split-package staging and source-only reconstruction,
CLI/headless-profile safety, failed-probe and ESP-identity regressions,
synthetic Docker context/privilege checks and VM command-boundary
tests. It never contacts Docker, creates a real VM or builds an ISO.
Real makepkg, mkarchiso and QEMU/UEFI tests are separate operator-run acceptance
steps. Keep a last-known-good ISO, source bundle and package set off the RAID0.

New installations require the signed runtime package before erasure. On an
official ISO, pass it explicitly with `--boot-package FILE` and establish its
public-key trust separately. The installed host uses pacman ownership, but future
local-package updates still require a reviewed signing-key/repository setup.
Existing unowned helpers remain verifiable; migration is manual. Inspect and
archive the old `/etc/pacman.d/hooks` hook before removing it, because it otherwise
overrides the packaged hook with the same name. Never overwrite a working recovery
path implicitly.

Primary references:

- [Archiso v90 sources](https://github.com/archlinux/archiso/tree/v90)
- [Archiso profiles](https://github.com/archlinux/archiso/blob/v90/docs/README.profile.rst)
- [Arch Linux Archive](https://wiki.archlinux.org/title/Arch_Linux_Archive)
- [Arch Secure Boot installation media](https://wiki.archlinux.org/title/Secure_Boot#Booting_an_installation_medium)
- [OVMF package files](https://archlinux.org/packages/extra/any/edk2-ovmf/files/)
- [Official Arch container](https://hub.docker.com/_/archlinux)
- [Pinned base-image metadata](https://github.com/docker-library/repo-info/blob/master/repos/archlinux/remote/base-20260830.0.582275.md)
- [Docker amd64 emulation](https://docs.docker.com/build/building/multi-platform/)
- [Docker capability boundaries](https://docs.docker.com/engine/containers/run/#runtime-privilege-and-linux-capabilities)

## Bridge payload and candidate contract

`bridge-bundle.json` records schema, selected installer source-archive hash,
Bridge source-archive hash, snapshot, package names/versions/releases,
architectures/digests and repository database digest. The source archive hash
identifies dirty source too; HEAD alone does not. The existing source lock must
match throughout the selected run.

`arch-workstation-bridge-runtime` installs root-owned files at:

- `/usr/lib/bridge/workstation-runtime`: the existing workstation CLI, libraries,
  templates, locks, deployment inputs and explicitly listed diagnostic sources.
- `/usr/lib/bridge/workstation-reference`: catalog locks, example configuration,
  deployment profiles and RAG integrity inputs from that same source archive.

The exact lists are `infrastructure/packages/bootstrap/bridge-runtime.files` and
`bridge-reference.files`. They are included in the established source allowlist
and checksum workflow. Nothing under `/etc/bridge` or mutable application state
is generated. Examples are not active configuration. GPU qualification and
target system-executable approvals are never fabricated from package hashes.

The bundle executes the **selected package's** offline `bridge-hostd
--check-reference`/`--check-native` against the staged reference tree and the
actual runtime-generated native harness bundle. It also collects manifests with
`bridge-hostd --manifest` and verifies the payload's per-file checksums. These
commands do not start a service, contact a cluster or authorize hashes.
Bridge's `scripts/test-installer-contract.sh --candidate SOURCE_DIRECTORY`
adds source tests for the actual installer export; the historical pinned fixture
remains independently checked. Catalog drift still refuses import.

Bundling resolves the runtime dependency closure against the installer's snapshot
using an empty private package database/cache. Official packages retain their Arch
signatures. Go is build-only; optional GPU SDKs, compilers and model weights are
not added merely to satisfy optional capabilities. Unsigned preparation cannot
perform the signed offline install test.

### Offline Bridge acceptance

Before relying on the package, use a disposable Arch root/VM at the selected
snapshot, with normal Arch keyring, filesystem, package manager and service hooks.
After the owner establishes the reviewed signing key, disconnect networking.
Run the Bridge installation transaction from the live installer against **only
that disposable root**, or pass the two Bridge/runtime archives plus the recorded
official dependency archives to `pacman --root TARGET --gpgdir KEYRING --needed -U`.
Keep signature policy Required. Do not pass real disks or the development host root.
Run the ownership/checksum/contract commands in [the first-boot handoff](INSTALLATION.md#bridge-package-and-owner-handoff) inside that target. Inspect
actual sysusers allocation and service/socket inactivity. Repeat installation with
`--needed`; verify owner test configuration and mutable-state sentinel files did
not change. A later started service is outside this installation test.

Change one runtime file in a separate disposable copy and confirm manifest
verification fails. Check the same-model/source pair, not the old fixture alone.
Preserve failed evidence. Unsigned candidates cannot complete this signed-install
test; owner signing is a blocker, not permission to use `--skipinteg` or weaken
pacman trust. VM package acceptance does not qualify physical GPU handover.

## Codex package and live-console acceptance

The ISO lock selects the exact `openai-codex` version, archive hash and Arch
snapshot. The builder checks package and executable versions and signatures.
The bootstrap package depends on that pin and owns the launcher, skill,
references and generated guardrail adapter. No unpinned npm/AUR install runs at
boot; no credentials enter the source package or ISO. A changed snapshot requires
a reviewed rebuild. See [the recorded CLI checks](validation/VALIDATION-CODEX-INSTALL.md).

Follow the [two file-backed disk VM procedure](#4-disposable-uefi-installation-tests) with outbound networking;
never pass host disks through. On the booted ISO, before installation:

1. Run `codex --version`, `pacman -Q openai-codex arch-workstation-bootstrap`, and
   inspect `BUILD-IDENTITY`. Match the release lock; verify the packaged manifest.
2. Run `arch-workstation-network mirrors` and the selected `codex-api` or
   `codex-device` probe. A wired VM does not qualify physical Wi-Fi.
3. Run the selected launcher locally. Owner authentication is optional/manual;
   no real credentials belong in automation. Confirm actual `/skills` discovery,
   interactive startup, read-only sandbox and approval policy. Request a harmless
   response only if you explicitly accept the account's usage charge.
4. Exit, repeat with INT/TERM, and verify that only the owned RAM session is removed
   without printing its contents. Confirm unrelated user auth remains unchanged.
5. Exercise disconnected DNS/mirrors and prove preflight refuses before erasure.
6. For the separately authorized disposable install, follow exact disk confirmations,
   then first-boot NetworkManager connection, fresh auth, verification and recovery
   tests. Do not infer that a VM Ethernet connection qualifies target Wi-Fi.

Record separately: source/fixture checks; actual package/CLI execution; login;
model access; ISO boot; Wi-Fi; installation; unlock/firmware/recovery. None of these
is a performance measurement. Remaining physical acceptance is owner-run.

## Signature-verification retry

The 2026-09-05 failure was reproduced in the builder's GPGME library. The pinned
`pacstrap` runs pacman as PID 1 in a nested PID namespace. GPGME leaves orphaned
child processes that pacman does not reap. These zombies exhaust the process
limit, after which verification reports misleading `ioctl` and corrupt-package
errors. This is distinct from a bad signature or a signing-passphrase prompt.

The builder now uses a [pacstrap adapter](../infrastructure/iso/docker/pacstrap.sh)
that keeps Bash as PID 1 to reap children. It changes only the nested process
launcher, refuses an unexpected upstream launcher, and leaves `/usr/bin/pacstrap`
unchanged. Package signatures, the 512-PID limit and Docker security controls
remain enabled. Docker's outer `--init` alone would not fix this nested namespace.

Build and check the updated tools image from the repository root:

```sh
bash infrastructure/iso/docker.sh --execute image
bash infrastructure/iso/docker.sh --execute check
```

Keep `ISO_RUN` set to your existing successful package run, then retry [ISO assembly](ISO.md#3-assemble-the-iso):

```sh
bash infrastructure/iso/release.sh --execute --allow-iso-mounts iso \
  "${ISO_RUN:?}" "$ISO_RUN/signing-key.asc" "${SIGNING_FINGERPRINT:?}"
```

This builder-only fix does not require rebuilding or resigning those packages.
The coordinator creates a new ISO attempt and retains the failed job. If signature
verification still fails, stop and inspect the new log; do not use `SigLevel = Never`
or assume that all signature failures have this cause.
