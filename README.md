# Arch Linux Threadripper AI Workstation

This repository contains a hardware-specific, guarded bootstrap for an Arch
Linux workstation built around a Gigabyte TRX50 AI TOP, AMD Threadripper 9960X,
two NVMe RAID0 members, and two Radeon AI PRO R9700 AI TOP GPUs with 32 GiB each.
The current memory configuration is 64 GiB DDR5-5600 ECC RDIMM. It also provisions an
on-demand AlmaLinux K3s lab under KVM. New installations are headless by default;
a host desktop and local gaming packages are explicit options.

The repository is executable infrastructure. Review every configuration and
lock-file change before use. `bootstrap-arch --execute install` permanently erases both
configured disks.

## Safety model

- Installation accepts only explicit `/dev/disk/by-id` devices and configured
  serial numbers.
- Preflight requires an Arch live ISO, root privileges, native UEFI, two
  distinct unmounted whole disks, and equal RAID-member sizes.
- Destructive work requires `--execute` and confirmation of resolved paths and serials.
- Installation defaults to `--dry-run`, runs every preflight guard, and prints the approved high-level
  storage/boot plan without executing it.
- Secret values are never stored in repository configuration. LUKS, AWS,
  Route53, Restic, and login credentials are entered or enrolled separately.
- External images and source repositories are pinned in `versions.lock`.
- Stable and LTS recovery UKIs are established before any git kernel is built.
- No swap, zram or hibernation is configured. Argon2id calibration is a separate
  in-memory benchmark.

## Repository commands

| Command | Purpose |
| --- | --- |
| `bin/bootstrap-arch` | Live-ISO preflight, destructive installation, and installed-system verification |
| `bin/workstationctl` | Hardware/resource plans, native builds, ccache, ROCm tests, gated sessions, snapshots and development tools |
| `bin/k3s-lab` | KVM/AlmaLinux/K3s lifecycle, backup, upgrade, and restore-test operations |
| `bash infrastructure/iso/release.sh` | Fresh ISO package builds and explicitly authorized ISO assembly |
| `make check` | Offline syntax, shell, Ansible, and Kubernetes-source validation |

## Build the installer ISO on macOS

Start Docker Desktop, review the checkout and run this from the repository root:

```sh
bash infrastructure/iso/release.sh packages             # Preview; changes nothing
bash infrastructure/iso/release.sh --execute packages   # Build fresh unsigned packages
```

The coordinator builds/checks the tools image, snapshots the current checkout and
builds packages in a new run directory. It handles source paths, Docker job names
and output paths together, so retries cannot accidentally select an old bundle.
On success, paste the printed `ISO_RUN=...` line back into your terminal and press
Enter. This sets the path to that build directory; printing it does not set the
variable for you. Follow the [ISO_RUN explanation and three-stage guide](docs/ISO.md#select-your-build-directory)
to select the run, sign its packages and assemble the ISO.
Signing and ISO mount privileges remain explicit; neither command above creates
an ISO, writes USB media or installs the workstation.

Docker image construction and userspace checks have passed on this Mac. See the
guide's [qualification status](docs/ISO.md#qualification) before treating an image
as recovery media. The [reference](docs/ISO-REFERENCE.md) covers manual stages,
native Arch builds and disposable UEFI tests; those are not additional steps in
the main Docker workflow. `make check` never runs Docker builds or boots a VM.

## Install the workstation

A signed `arch-workstation-boot` runtime package is required before real
installation. The custom ISO bundles it; official-ISO users supply
`--boot-package FILE`. Building the ISO does not require the configuration below.

Start by copying the non-secret examples:

```sh
cp config/install.conf.example config/install.conf
cp config/workstation.conf.example config/workstation.conf
cp config/k3s-lab.conf.example config/k3s-lab.conf
```

Replace every `REPLACE_...` or unresolved value. Do not put passwords, tokens,
private keys, kubeconfigs, or AWS secret keys in these files.

On the Arch live ISO:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf preflight
sudo ./bin/bootstrap-arch --config config/install.conf --dry-run install
sudo ./bin/bootstrap-arch --config config/install.conf --execute install
```

The install command stops at manual boundaries for credentials and Secure Boot
key enrolment. Follow [the installation runbook](docs/INSTALLATION.md) before
rebooting.

After the first boot, use `workstationctl` for optional layers. Create the K3s
VM only after host recovery, networking, backups, and KVM validation pass. See
[workstation setup](docs/WORKSTATION.md), [operations](docs/OPERATIONS.md),
[architecture](docs/ARCHITECTURE.md), and
[security boundaries](docs/SECURITY.md). The research baseline is preserved in
[primary references](docs/SOURCES.md).

The intended first-boot order is:

```sh
sudo ./bin/workstationctl --config config/workstation.conf dev setup server templates/workstation/packages.pacman
./bin/workstationctl --config config/workstation.conf ccache configure
./bin/workstationctl --config config/workstation.conf makepkg configure
./bin/workstationctl --config config/workstation.conf zsh setup
sudo ./bin/workstationctl virtualization configure developer
./bin/workstationctl virtualization validate
```

Keep JetBrains and Codex on the client Mac for remote development unless the
host needs them. Host Toolbox and the desktop-keyring Codex configuration remain
optional; see [workstation setup](docs/WORKSTATION.md). Existing installations
are not converted or stripped of packages by changing the default.

For the AI optimization audit, build controls and one/two-GPU measurements,
start with [AI-PERFORMANCE.md](docs/AI-PERFORMANCE.md). The
[ROCm build guide](docs/ROCM.md) distinguishes implemented native-kernel,
ccache and pinned llama.cpp HIP/Vulkan builds from the pending complete TheRock
dependency lock, ROCm source build and PKGBUILD packaging. Hardware
reports collected away from the Linux workstation are explicitly `pending`.
The retained `llm` and `ai validate` commands are legacy Intel workflows;
use `rocm validate` and `rocm inference` for the R9700s.

The additive [bare-metal home-lab profile](docs/HOME-LAB.md) provides
`infrastructure/` host/K3s/operator configuration and `apps/` Kustomize bases
and overlays. It reuses this installer with `/dev/md0`, TPM2+PIN enrollment
planning and a 4 GiB Argon2id memory ceiling. The EL9/KVM lab remains separate.
Workload replicas are disabled pending image/hardware qualification. Gaming now
has a non-root KWin/Wayland candidate, but still requires image promotion and
target validation of GPU nodes, capture, input, audio, network exposure and
egress before it can be enabled. See [the Sunshine runbook](docs/SUNSHINE.md).

The [model defaults](docs/MODELS.md) select Qwen3.8-27B-FP8 across both R9700s
through `apps/overlays/default`. The RAG profile uses the same chat model with
CPU Qwen3 embeddings. Model weights are staged separately, not bundled in the ISO.

K3s replaces RKE2 for new installations. The bare-metal profile uses SQLite;
the optional KVM lab keeps embedded etcd for its snapshot and restore commands.
Both use the same checksum-pinned K3s release. Use `bin/k3s-lab` and
`config/k3s-lab.conf` for new VMs; existing RKE2 configurations and data require
a separate migration. See the [selection rationale and backup requirements](docs/HOME-LAB.md#3-package-and-configure-bare-metal-k3s).

These commands do not log in to JetBrains, Codex, or AWS, enrol a YubiKey,
enable backup timers, build AUR packages, or make the git kernel preferred.
Those remain explicit trust and recovery boundaries.

## Supported posture

This is a development workstation and lab cluster, not a production platform.
Arch on an AMD host with dual Radeon GPUs, AlmaLinux as a K3s guest, a git
kernel, and development GPU stacks have different vendor support boundaries.
The scripts keep those layers independently reversible; they do not make the
combination vendor-certified.

RAID0 has no redundancy. Either NVMe failure destroys root. Maintain and test
the documented S3/Restic and K3s restore paths.
