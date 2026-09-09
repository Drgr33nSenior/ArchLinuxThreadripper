# Build the installer ISO on your Mac

Follow this guide in order: build packages, bundle Bridge, sign, assemble,
verify and write USB media. The default installation includes Bridge; its
services remain inactive until owner setup after first boot.

Run from the reviewed checkout on the controller. These steps do not install
Arch on the Mac, enrol firmware keys or reboot a machine. For booting the ISO,
Wi-Fi, Codex and target installation, use [INSTALLATION.md](INSTALLATION.md).
Advanced build internals and disposable tests are in [ISO-REFERENCE.md](ISO-REFERENCE.md).

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
checkout and builds all four unsigned installer packages. Each stage stops on failure.
Package building runs as UID 1000 without network or Linux capabilities.

### Select your build directory

Paste the successful command's printed `ISO_RUN=...` assignment into your
terminal. It selects one completed run, not the newest timestamp:

```text
build/iso/run-<timestamp>-<pid>/
├── source/       # Frozen source archive, recipe and manifest
├── packages/     # Four unsigned installer packages
└── bundled/      # After Bridge selection: five packages + official dependencies
```

```sh
printf 'Selected build: %s\n' "${ISO_RUN:?Paste the successful run assignment first}"
ls "$ISO_RUN/source/source.lock" "$ISO_RUN/packages/source.lock"
```

Keep this assignment through bundling, signing and assembly. In a new terminal,
restore the same reviewed absolute path. Missing manifests mean an incomplete
or wrong selection; do not create them or rebuild merely to restore context.
Changed source requires a new package run. Failed jobs and prior artifacts remain
available for inspection.

## 1a. Bundle Bridge, unless explicitly opting out

The default `INSTALL_BRIDGE=true` requires this stage before signing. For media
without Bridge, skip this stage and set `INSTALL_BRIDGE=false` in the install
configuration; omission means true, including in older configurations.

Select one reviewed local or successful CI build of
`spry-ai-workstation-bridge`, with its exact `PKGBUILD` and
`spry-bridge-*-src.tar.gz`. A version label alone does not identify the source.
For a local build, follow Bridge's `docs/ARCH-PACKAGING.md`:

```sh
BRIDGE_REPO=/absolute/reviewed/Spry.ai-workstation-bridge
cd "$BRIDGE_REPO"
make check
go run ./cmd/bridge-arch-package --version v0.0.0 --output /absolute/new/bridge-source
# In a prepared unprivileged Arch environment, in that generated directory:
makepkg --verifysource
makepkg --cleanbuild
```

Choose a reviewed version label; `v0.0.0` is only a local candidate example.
Return to the installer checkout, retain the printed `ISO_RUN`, and select
the directory containing that single package, source archive and recipe:

```sh
BRIDGE_ARTIFACTS=/absolute/reviewed/bridge-artifacts
bash infrastructure/iso/release.sh bridge "$ISO_RUN" "$BRIDGE_ARTIFACTS"
bash infrastructure/iso/release.sh --execute bridge "$ISO_RUN" "$BRIDGE_ARTIFACTS"
```

The stage creates `ISO_RUN/bundled`: five local packages, a rebuilt repository,
`bridge-bundle.json` and the official runtime dependency closure for the ISO's
Arch snapshot. Go remains build-only. Official packages retain their Arch
signatures. The selected Bridge binary checks the matching runtime/reference
catalog; no service starts and no target approvals are generated.

Review the recorded source hashes, package metadata and dependency evidence.
Missing, ambiguous or changed inputs stop the stage. Existing output is refused;
retain failed jobs and use a fresh reviewed run for changed inputs. Do not fall
back to another run's packages. Signed offline transaction validation remains
pending until owner signing and installation preflight.

See [payload and contract details](ISO-REFERENCE.md#bridge-payload-and-candidate-contract).
Transporting these archives does not make the whole OS installation offline.

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
package contents. Expect bootstrap, boot, backup and Bridge-runtime packages,
plus Bridge after bundling; no `.INSTALL`
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
artifact_dir="$run/packages"
expected_count=4
if [[ -d $run/bundled ]]; then artifact_dir="$run/bundled"; expected_count=5; fi
cd "$artifact_dir"
shopt -s nullglob
packages=(./*.pkg.tar.zst)
((${#packages[@]} == expected_count)) || { echo 'Unexpected package count.' >&2; exit 1; }
artifacts=("${packages[@]}" ./arch-workstation.db.tar.gz)
if ((expected_count==5)); then artifacts+=(./bridge-bundle.json); fi
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
  checks:** see the [signature-verification retry](ISO-REFERENCE.md#signature-verification-retry)
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

## Qualification

Use the [dated validation records](validation/VALIDATION-BRIDGE-ISO.md) for exact
source/package identities, completed checks, failures and pending acceptance.
A package build, signature or USB readback does not qualify ISO boot, live Secure
Boot, disk unlock, recovery or physical hardware.

Do not rebuild or resign a successful old run merely to continue. Documentation
changes affect future source packages, not existing signed media. Keep reviewed
recovery media and follow the [disposable tests](ISO-REFERENCE.md#4-disposable-uefi-installation-tests)
before relying on a new ISO.
