# Build the installer ISO on your Mac

Use three stages: **build packages → sign packages → assemble the ISO**.
You choose a signing key, but you no longer choose source-directory or Docker-job
names. The coordinator creates a fresh source snapshot for every package build
and prints the one run directory needed for the remaining stages.

These commands build local artifacts only. They do not install Arch on your Mac,
write a USB drive, change firmware or reboot anything. The resulting live ISO is
not yet boot-qualified or Secure Boot qualified.

The signed bootstrap package now includes the installation skill and console
launcher; the ISO selects a recorded Codex package from its Arch snapshot.
See [CODEX-INSTALL.md](CODEX-INSTALL.md) for Wi-Fi, explicit authentication,
private RAM-session cleanup and live-ISO smoke tests. No credentials enter the ISO.

## Before you start

- Run from the repository root: the directory containing `README.md`, `bin/`
  and `infrastructure/`.
- Start Docker Desktop with working `linux/amd64` execution. The default context
  is `desktop-linux`; if necessary, add `--context NAME` to every coordinator call.
- Have Bash, libarchive `bsdtar`, gzip and a SHA256 tool on the Mac. Signing also
  requires GnuPG and an existing signing key whose full fingerprint you trust.
  Do not supply private keys to Docker or store them in this repository.
- Review the checkout, especially the [packaging recipe](../infrastructure/packages/bootstrap/PKGBUILD),
  [source allowlist](../infrastructure/packages/bootstrap/source.files),
  [version lock](../infrastructure/iso/versions.lock) and
  [Docker security boundary](ISO-REFERENCE.md#docker-prerequisites-and-boundaries).
  The existing builder compatibility exception disables pacman's syscall and
  filesystem downloader filters; signatures and Docker's security controls remain.

You do **not** need workstation disk paths, installation credentials, ROCm,
Ansible or Kubernetes configuration to build the ISO. Configure the workstation
installation separately, after building and validating the media.

The home lab now uses K3s. Kubernetes is configured after the Arch installation;
the ISO does not install or start a cluster. The [bare-metal profile](HOME-LAB.md#3-package-and-configure-bare-metal-k3s)
uses SQLite, and the separate [KVM lab](../ansible/K3S-OPERATIONS.md) retains
embedded etcd for its snapshot/restore workflow. Both use `K3S_VERSION` from
the root `versions.lock`. Secure Boot and ISO package signing are unchanged.
Existing ISO files remain frozen artifacts. To include these revised guides,
build a fresh package run and repeat signing and assembly; retrying assembly
with an old package run does not incorporate checkout changes.

## AI tuning and the installed system

The live ISO keeps a generic Arch kernel. Native CPU compilation belongs on the
installed Threadripper; the Mac's ISO builder must not select CPU-native flags
for the target machine. The home-lab install example now explicitly selects
`TUNED_PROFILE=accelerator-performance`, independently of `HOST_PROFILE=headless`.
The generic install example retains `auto` and its previous balanced default.
This configures the installed host, not the running live ISO.

After installation, the reviewed full checkout provides native git-kernel
builds, memory-limited compilation and same-revision llama.cpp HIP/Vulkan builds. The
bootstrap package does not bundle these post-install workstation commands.
Follow [the AI performance audit and qualification runbook](AI-PERFORMANCE.md).
It also explains the explicit move from the ISO's dated package mirror to
rolling Arch after recovery tests pass. Existing media are not modified by
editing this checkout.

The [first-tranche tracking matrix](AI-PERFORMANCE.md#first-tranche-implementation-record)
also covers target topology, K3s whole-core resource plans, persistent caches and
gated AI/gaming handover. These are post-install commands and source changes;
they do not run benchmarks, configure K3s or discover target hardware in the ISO
builder. Gaming remains disabled until image, device and security qualification.
The [Sunshine runbook](SUNSHINE.md) covers the pinned gaming-image recipe,
VA-API/Vulkan Video profiles, allocated-GPU diagnostics and paired measurements.
These are post-install tools; rebuilding the ISO does not qualify game streaming.

## 1. Build fresh packages

Preview the workflow without contacting Docker or creating files:

```sh
bash infrastructure/iso/release.sh packages
```

Then execute it:

```sh
bash infrastructure/iso/release.sh --execute packages
```

This builds or reuses the pinned tools image, checks it, snapshots the current
checkout and builds all three unsigned packages. Each stage stops on failure.
Package building runs as UID 1000 without network or Linux capabilities.

### Select your build directory

`ISO_RUN` is a shell variable containing the **absolute path to one build run on
your Mac**. It selects the packages you will sign and use for ISO assembly. Set
it to the parent directory containing both `source/` and `packages/`, not to one
of those subdirectories, a Docker job name or an `.iso` file:

```text
build/iso/run-<timestamp>-<pid>/   ← ISO_RUN points here
├── source/       # Frozen source archive, recipe and source manifest
└── packages/     # Three packages, repository database, checksums and build records
```

At the end of a successful package build, the command prints an `ISO_RUN=...`
line. **Paste that entire line into your terminal and press Enter.** Printing the
line does not set the variable: the build script runs in a separate shell.

For example, from the repository root, a run named `run-20260905-191820-34503`
would be selected like this. Use your successful run's name:

```sh
ISO_RUN="$(pwd)/build/iso/run-20260905-191820-34503"
```

Check the selected directory before continuing:

```sh
printf 'Selected build: %s\n' "${ISO_RUN:?Set ISO_RUN first}"
ls "$ISO_RUN/source/source.lock" "$ISO_RUN/packages/source.lock"
```

Both manifest paths should exist. If either is missing, check your selection and
whether stage 1 completed. This check does not create a directory or rebuild anything.

Keep the same value through stages 2 and 3. It lasts only in the terminal session
where you set it; in a new tab or terminal, run the assignment again. No `export`
is needed because the commands below pass the value as an argument. `$ISO_RUN`
substitutes the selected path; `${ISO_RUN:?}` also stops the command if the variable
is unset or empty. Neither form chooses a run automatically.

Do not rerun this stage just to continue signing or ISO assembly. To build edited
source, rerun the same command: it creates a new snapshot and run automatically.
Old runs, including failed containers and volumes, remain available for inspection.

## 2. Review and sign this run's packages

This is a manual trust boundary. The coordinator never accesses your signing key.
If you do not have an established signing key, stop here and arrange that setup
before continuing. Do not bypass package signatures.

Set the full, uppercase, 40-character **primary** fingerprint of your reviewed
OpenPGP key, not an email address or a short key ID:

```sh
SIGNING_FINGERPRINT='REPLACE_WITH_YOUR_REVIEWED_PRIMARY_FINGERPRINT'
```

Review the generated PKGBUILD, source manifest, `.PKGINFO`, `.BUILDINFO` and
package contents. Expect bootstrap, boot and backup packages; no `.INSTALL`
scriptlets. The [package-content checklist](ISO-REFERENCE.md#1-prepare-the-source-package)
describes their intended files. Then run the following in the same terminal.
It checks the recorded checksums, exports only the public key, and creates a
detached signature for each package and the repository database. It refuses
existing signatures or public-key output; it does not overwrite them.

```sh
bash -s -- "${ISO_RUN:?Copy the ISO_RUN assignment from stage 1}" \
  "${SIGNING_FINGERPRINT:?Set the reviewed fingerprint}" <<'BASH'
set -euo pipefail
run=$1
fingerprint=$2
[[ $fingerprint =~ ^[A-F0-9]{40}$ ]] || { echo 'Use the full uppercase primary fingerprint.' >&2; exit 1; }
cd "$run/packages"
shopt -s nullglob
packages=(./*.pkg.tar.zst)
((${#packages[@]} == 3)) || { echo 'Expected three packages.' >&2; exit 1; }
artifacts=("${packages[@]}" ./arch-workstation.db.tar.gz)
[[ ! -e ../signing-key.asc && ! -L ../signing-key.asc ]] || { echo 'Public-key output already exists; inspect this run.' >&2; exit 1; }
for artifact in "${artifacts[@]}"; do
  [[ -f $artifact && ! -L $artifact && ! -e $artifact.sig && ! -L $artifact.sig ]] || { echo 'Missing artifact or existing signature; inspect this run.' >&2; exit 1; }
done
shasum -a 256 -c SHA256SUMS
gpg --armor --output ../signing-key.asc --export "$fingerprint"
for artifact in "${artifacts[@]}"; do
  gpg --local-user "$fingerprint" --output "$artifact.sig" --detach-sign "$artifact"
  gpg --verify "$artifact.sig" "$artifact"
done
BASH
```

GnuPG may prompt for your key's passphrase through pinentry. Do not put the
passphrase in a command, config file or chat. These are standard
[GnuPG detached-signature operations](https://gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html).
If signing stops partway through, inspect the partial outputs; do not delete
signatures or overwrite artifacts merely to rerun the block.

## 3. Assemble the ISO

Preview this run's ISO build:

```sh
bash infrastructure/iso/release.sh iso "${ISO_RUN:?}" \
  "$ISO_RUN/signing-key.asc" "${SIGNING_FINGERPRINT:?}"
```

After reviewing the preview, explicitly permit the container mounts needed by
Archiso. This stage runs as **container root with `CAP_SYS_ADMIN`**, not as root
on macOS. It does not use `--privileged`, expose host disks, or change Docker's
security profiles. It downloads packages and verifies package/database signatures.

```sh
bash infrastructure/iso/release.sh --execute --allow-iso-mounts iso \
  "${ISO_RUN:?}" "$ISO_RUN/signing-key.asc" "${SIGNING_FINGERPRINT:?}"
```

The command prints a fresh output directory inside `ISO_RUN`. Expect an `.iso`,
`SHA256SUMS` and build manifests. ISO attempts use new job/output names, so you can
retry this stage with the same signed packages without rebuilding or resigning.
Never treat partial output from a failed attempt as a completed release.

Stop here before writing USB media. Review the image and manifests, sign the final
ISO using your reviewed release-signing process, and perform the
[disposable UEFI boot and recovery tests](ISO-REFERENCE.md#4-disposable-uefi-installation-tests).
Package signing is not Secure Boot qualification for the live medium.

## 4. Write a USB installer on macOS

Use the separate [USB writer](../infrastructure/iso/usb.sh) after the flash drive
arrives. ISO assembly never selects or writes a USB device. The writer uses
macOS `diskutil`, `ioreg`, `plutil`, `dd`, `head` and `cmp`, plus the project's
existing `jq` dependency. It does not need Docker or rebuild the ISO.

**Writing destroys existing contents of the selected USB disk. Back up the
drive first.** This is an installer copy, not a persistent Arch installation
or a secure erase of the drive's unused capacity.

1. Select the exact completed ISO. Keep its original `SHA256SUMS` beside it.
   Do not select an incomplete attempt or generate a replacement checksum merely
   to bypass a mismatch. Checksum verification detects corruption; it does not
   authenticate an untrusted ISO and checksum pair. Review/sign the release as
   described above.
2. Connect the flash drive and list candidates. Check its name and capacity
   against the physical drive; disconnect unrelated external disks if practical.

```sh
bash infrastructure/iso/usb.sh list
```

3. Set the two explicit paths below. Replace `/dev/diskN` with the **whole disk**
   shown by the list command, not a partition such as `/dev/disk4s1` or the raw
   `/dev/rdisk4` alias. The placeholders deliberately do not select a disk.

```sh
ISO_FILE='/absolute/path/to/completed/arch-workstation-2026.09.04-x86_64.iso'
USB_DISK='/dev/diskN'
bash infrastructure/iso/usb.sh write "$ISO_FILE" "$USB_DISK"
```

The default is a read-only preview. It checks the ISO hash, source location,
capacity, disk properties and live IOKit media identity, then prints the write
and verification plan. It does not unmount, write, synchronize or eject anything.
The ISO must be on a local disk other than the selected USB. Spaces are allowed
in directory paths; the ISO basename must contain only letters, digits, dots,
underscores and hyphens.

The writer refuses internal, virtual, read-only, non-USB and unidentified disks,
disk0, partitions, undersized media and an ISO not aligned to the device block
size. It also refuses APFS, CoreStorage, RAID and macOS system-volume layouts.
These conservative refusals apply even to an external drive previously used by
macOS. Review such a drive separately; this tool does not reformat it or offer a
force/bypass option.

4. Review the preview. Close applications using the USB, keep the Mac awake,
   and leave drives and hubs connected throughout the operation. Then run:

```sh
sudo bash infrastructure/iso/usb.sh --execute write "$ISO_FILE" "$USB_DISK"
```

Type the exact `ERASE ...` phrase displayed on the controlling terminal. It
includes the selected disk, its capacity and an ISO checksum prefix. There is
no automatic yes option. After confirmation, the writer rechecks the image and
media identity. It performs a **non-forced** unmount, checks that volumes stayed
unmounted, and rechecks identity. It then writes through a held raw-device
descriptor, synchronizes, compares exactly the ISO's byte count, and ejects.
The IOKit registry identity helps detect unplug/replug
and disk-number reuse; it is not a permanent manufacturer serial. Do not hotplug
devices during writing or verification. Press Ctrl-T for macOS `dd` progress;
Ctrl-C aborts and leaves an incomplete copy.

Success means the image bytes compared equal and ejection succeeded. Remove
the drive only after that message. USB boot, recovery and Secure Boot behaviour
still need separate workstation tests; readback is not boot qualification.

If an operation fails, later stages stop. A write/readback failure can leave the
USB overwritten but unverified; do not boot it or assume its previous files are
recoverable. There is no automatic retry, formatting, force-unmount or rollback.
Resolve the cause, inspect the current device list, and repeat preview and
confirmation. If only ejection fails, close open users and eject manually before
removing the drive. An interrupted writer normally releases its per-disk lock;
after a forced termination, inspect any remaining
`/var/run/arch-workstation-usb-diskN.lock` and confirm that no writer is running
before manually removing that exact empty directory.

`tests/test_iso_usb.sh` exercises the safety and copy-verification path with
mocked OS adapters and regular temporary files. No test unmounts or writes a
real device. Physical USB write/readback and target boot tests remain **NOT RUN**
until the flash drive is available.

## If a stage fails

- **Repeated `GPGME error: Inappropriate ioctl for device` during package integrity
  checks:** see the [signature-verification retry](#signature-verification-retry)
  below. Do not delete packages or change your signing key based on this message.
- **Missing `/work/source/launch-bootstrap`:** a manually selected bundle contains
  the older recipe. Run stage 1 above; changing only `candidate-01` to `candidate-02`
  does not refresh a bundle. Do not patch generated recipes or copy loose launchers.
- **Job state already exists:** a lower-level command reused an old job name. The
  coordinator generates fresh names. Existing jobs are evidence, not disposable
  clutter to prune automatically.
- **Docker or pacman errors:** stop at that stage. Use the exact log command printed
  for the job. Do not add privileges, disable more sandbox controls or weaken
  signature checks. See the [reference and native Arch fallback](ISO-REFERENCE.md).
- **Lost terminal variables:** repeat the [build-directory assignment](#select-your-build-directory)
  and set the same reviewed `SIGNING_FINGERPRINT`. If you lost the printed path,
  inspect `build/iso/` and select your successful run, not simply the newest directory.
  Do not create an empty directory or rebuild packages just to restore the variable.

### Signature-verification retry

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

Keep `ISO_RUN` set to your existing successful package run, then retry stage 3:

```sh
bash infrastructure/iso/release.sh --execute --allow-iso-mounts iso \
  "${ISO_RUN:?}" "$ISO_RUN/signing-key.asc" "${SIGNING_FINGERPRINT:?}"
```

This builder-only fix does not require rebuilding or resigning those packages.
The coordinator creates a new ISO attempt and retains the failed job. If signature
verification still fails, stop and inspect the new log; do not use `SigLevel = Never`
or assume that all signature failures have this cause.

## Qualification

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
remain pending. See the [reference](ISO-REFERENCE.md) for retained warnings, implementation details,
manual stage commands and recovery procedures.
