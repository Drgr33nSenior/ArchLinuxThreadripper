# ISO reference: manual stages, implementation and recovery

Start with [the three-stage build guide](ISO.md). This reference describes the
underlying commands, security boundaries, native Arch fallback and acceptance
tests. You do not need to run the manual build commands as well as `release.sh`.

This adds a network-assisted Archiso release path to the existing installer.
It is not a new distribution or a second installation implementation. The primary
build workflow uses a pinned amd64 Arch container on Docker Desktop. On Apple
Silicon, Docker runs x86_64 userspace through emulation. Native x86_64 Arch builds
remain an alternative. No ISO has yet been boot-qualified on the workstation.

See [current qualification](ISO.md#qualification) in the build guide. The image
build uses the existing pacman compatibility exception described below; this is
not qualification with all downloader sandbox controls enabled.

Observed with Docker Engine 29.6.2 and Buildx 0.35.0-desktop.2. Docker's separate
`--check --platform=linux/amd64` validation also reported `InvalidBaseImagePlatform`
(expected arm64). This earlier warning remains unresolved; the separate Dockerfile
check has not been requalified. The actual amd64 build now succeeds. Package hooks
also reported an uninitialized `/etc/` tmpfiles rule and skipped system-manager
reloads because the container root is not booted. These did not fail the build.

## Components and boundaries

- `infrastructure/packages/bootstrap/`: allowlisted source bundle and split
  PKGBUILD. `arch-workstation-bootstrap` provides the live CLI, templates and
  docs. `arch-workstation-boot` owns the installed UKI sync helper and hook.
  The optional `arch-workstation-backup` package owns the Restic helper, units
  and policy; it is not installed on the live image or base host.
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

Review `source.lock`, `project/SOURCE-MANIFEST.sha256`, `PKGBUILD` and the two
launchers. The archive is normalized for file order, ownership and timestamps.
This is source-archive repeatability, not a claim of bit-identical ISO builds.
No private signing key is an input to source preparation.

Build the Docker tools image and check its userspace. First review the dry run;
`--execute` is required to create an image or start a container:

```sh
bash infrastructure/iso/docker.sh image
bash infrastructure/iso/docker.sh --execute image &&
bash infrastructure/iso/docker.sh --execute check
```

If image construction reports `archiso: /usr/share/doc (No such file or directory)`
and the equivalent warning for `/usr/share/man`, the failure is from
`pacman -Qkk archiso`, not seccomp. The official base image's
[NoExtract rules](https://github.com/archlinux/archlinux-docker/blob/master/pacman-conf.d-noextract.conf)
omit documentation. Builder setup restores these two shared parent directories
before package installation, with mode `0755`. Documentation contents remain
subject to `NoExtract`; package-file verification still stops on other mismatches.
Do not suppress the check with `|| true`. Rebuild with the commands above; the
changed recipe generates a new image tag without pruning existing images.

The check verifies the amd64 Arch tools. It does not test mount permissions, build
an ISO or boot a kernel. Build the three split packages in a new isolated job:

```sh
bash infrastructure/iso/docker.sh packages candidate-01 \
  build/iso/source-01 build/iso/packages-01
bash infrastructure/iso/docker.sh --execute packages candidate-01 \
  build/iso/source-01 build/iso/packages-01
```

Only the source tarball, generated PKGBUILD and exported `source.lock` are
mounted, read-only. `makepkg` checks
dependencies and source integrity; it does not install dependencies or run as root.

The intended outputs are three `*.pkg.tar.zst` files: bootstrap, boot and backup.
Inspect `.PKGINFO`, `.BUILDINFO` and archive contents. None should contain an
install scriptlet. The boot package must contain its helper and shared validator
under `/usr/lib` and its hook under `/usr/share/libalpm/hooks`. The optional
backup package contains `/usr/lib/arch-workstation-backup/run`, vendor systemd
units and pacman-backed-up policy files under `/etc/restic`. Neither runtime
package installs into `/usr/local` or starts a service.

The job also exports an unsigned `arch-workstation.db.tar.gz` repository database,
checksums, a package inventory and the builder image ID. Use your existing approved
signing process outside Docker to produce detached `.sig` files for all three packages
and that exact database. Keep these artifacts in one release directory. These are
operator signing actions, not agent-run publication.
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

Packages used to **install Arch** still come from the dated online repository.
ISO assembly consumes only the signed bootstrap and boot archives. Its signed
database can also describe the optional backup package, but that archive is not
bundled or installed by the ISO. Retain it in the external signed release set
for explicit post-install use. Bundling scripts
and rescue tools does not provide a fully offline installation. The installer
preserves the same mirror snapshot inside the new system for its initial chroot
transactions. After acceptance, perform a reviewed coherent system update; do
not leave an old snapshot pinned indefinitely.

## 3. Build, inspect and sign

```sh
bash infrastructure/iso/docker.sh iso candidate-01 \
  build/iso/packages-01 /absolute/path/to/project-public.asc \
  YOUR_REVIEWED_FINGERPRINT build/iso/release-01
# Operator-run, after reviewing the plan and the privilege boundary above:
bash infrastructure/iso/docker.sh --execute --allow-iso-mounts iso candidate-01 \
  build/iso/packages-01 /absolute/path/to/project-public.asc \
  YOUR_REVIEWED_FINGERPRINT build/iso/release-01
```

ISO assembly checks mount-namespace support before downloads. If mounts, chroots
or amd64 execution fail, stop and inspect the retained job. Do not add blanket
privileges or change the VM kernel to work around the failure automatically.
Use the native Arch fallback if the Docker environment cannot support this stage.

The build refuses existing job/work/output state. Compression is limited to four
workers. Work files and package caches stay on the job's disk-backed volume, not
in a large tmpfs. No ROCm source build or native CPU tuning runs in the container.

Review the ISO contents, generated package list, `SHA256SUMS`, source manifest and
builder inventory. Sign the final ISO through the approved release-signing process.
Artifact signing and Secure Boot signing are different controls. No script writes
the resulting image to USB, enrolls firmware keys, publishes it or reboots a host.

For each subsequent release, increase `BOOTSTRAP_PACKAGE_VERSION` in the ISO lock
and review the snapshot and source epoch together. The source hash distinguishes
build inputs; its lexical order does not provide a package upgrade sequence.

### Native Arch fallback

On an isolated x86_64 Arch builder prepared against the same snapshot, the original
scripts remain supported. Transfer the source bundle and use the same reviewed
checkout. Run `makepkg --cleanbuild` as an ordinary user from the prepared source
directory, with `SOURCE_DATE_EPOCH` from `source.lock`. Create the repository
database with `repo-add`, then sign it and all three packages through the approved process.
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
