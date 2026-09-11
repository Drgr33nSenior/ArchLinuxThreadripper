# Workstation setup

Run this procedure only after stable and LTS boots, Secure Boot recovery, disk
unlock, and the first offline LUKS-header copy have passed.
For the AI-specific audit and measurement sequence, see
[AI-PERFORMANCE.md](AI-PERFORMANCE.md). If installed from the custom ISO, review
its dated mirrorlist and the explicit transition to rolling Arch first.

Use [Telemetry](TELEMETRY.md) for local Prometheus/Grafana/Loki/Tempo/Alloy,
host sensors, Bridge instrumentation and capacity planning. The server profile
includes the host Alloy binary but does not activate collectors.

## Packages and services

New installations use `HOST_PROFILE=headless`: no GNOME/GDM, host Steam,
32-bit gaming libraries or graphical login target. The host still needs GPU
drivers and compute libraries. A remote gaming workload supplies its own
display session; headless does not mean that Sunshine can encode without one.
The disabled gaming manifests are not yet a qualified streaming deployment.
Use [SUNSHINE.md](SUNSHINE.md) to build the streaming candidate, configure its
encoder and validate the allocated GPU without changing the host display stack.

Install optional server tools in one complete Arch transaction. The `server`
profile includes the AMD graphics/ROCm baseline, Podman, shell and cloud clients,
command-line KVM/libvirt tools, Ansible and Restic:

```sh
sudo ./bin/workstationctl --config config/workstation.conf \
  dev setup server templates/workstation/packages.pacman
```

The virtualization manifest uses Arch's [headless `qemu-base` package](https://archlinux.org/packages/extra/x86_64/qemu-base/).
Gaming containers can provide their own desktop and streaming session, as
described by [Steam Headless upstream](https://github.com/Steam-Headless/docker-steam-headless).
This reference does not qualify or replace the project's locked image candidate.

The narrower `gpu`, `ai`, `gaming`, `shell`, `cloud`, `virtualization`, and `backup`
profiles are available when the full set is not wanted. The command always uses
`pacman -Syu`; it never performs a partial upgrade.
The `gaming` selector needs `templates/workstation/packages-desktop.pacman`
as its manifest; the server manifest deliberately contains no local gaming set.

For a local host desktop, select `HOST_PROFILE=desktop` before a new installation.
For optional desktop packages on an existing server, first review and enable
Arch's multilib repository, then explicitly select the additional manifest:

```sh
sudo ./bin/workstationctl dev setup workstation templates/workstation/packages-desktop.pacman
```

This post-install command does not enable GDM or change the default target.
Choose those service changes separately. The `server` profile rejects this
desktop manifest before calling pacman. Neither profile removes existing packages;
do not rerun the destructive installer to change a host's role.

Arch's signed packages are the host GPU baseline: upstream `amdgpu`, AMD
firmware and Mesa/RADV. Matching 32-bit Vulkan libraries are desktop/gaming
options, not server requirements. The AI profile adds
the official ROCm HIP SDK, ROCm monitoring tools, RCCL and ROCm PyTorch.
Keep these packages as a coherent generation. Native llama.cpp builds can
explicitly select the reviewed [ROCm 10 AUR SDK provider](ROCM.md#reviewed-rocm-10-sdk-provider).
That prebuilt SDK is separate from the experimental TheRock source build, whose
full dependency lock and packaging remain pending. Existing Intel support is retained
in `packages-intel.pacman` and the legacy Intel-only commands.

After installing the AI profile, run the host enumeration and deterministic
per-GPU HIP, FP32/FP16/BF16 and RCCL checks, then inspect the same boot's kernel
journal for `amdgpu`, firmware, PCIe and reset errors:

```sh
./bin/workstationctl --config config/workstation.conf rocm validate artifacts/rocm-validation-01
```

## CPU-specific builds

Generate a user makepkg override from the currently installed, root-owned Arch
baseline:

```sh
./bin/workstationctl --config config/workstation.conf makepkg configure
```

The command replaces only `-march=x86-64 -mtune=generic` with
`-march=native -mtune=native`. It retains Arch's current `-O2` optimisation,
hardening, frame-pointer, linker, assertion, and LTO policy. Rust receives
`target-cpu=native`. Official packages, rescue media, shared caches, and
portable artifacts stay generic.

Configure ccache before building. Recalculate the job count from available
memory; 24 ordinary and 16 heavy jobs are ceilings:

```sh
./bin/workstationctl --config config/workstation.conf ccache configure
./bin/workstationctl --config config/workstation.conf build environment normal
./bin/workstationctl --config config/workstation.conf build environment memory-heavy
```

Load the printed environment in the build shell. Review the memory reserves in
[ROCM.md](ROCM.md) before changing the ceilings. Do not set global `-O3`, `-Ofast`, `-ffast-math`, PGO, loop unrolling,
or an alternate linker. Many AI projects override makepkg flags; inspect their
build logs.

## Shell and development clients

Install the pinned Oh My Zsh checkout and reviewed configuration as the normal
user. Root remains on Bash for recovery:

```sh
./bin/workstationctl --config config/workstation.conf zsh setup
chsh -s /usr/bin/zsh
```

Oh My Zsh updates are disabled. Zsh uses packaged completions,
autosuggestions, and syntax highlighting; Bash completion is loaded lazily only
for the AWS completer. The prompt performs no network lookup or Git scan.

For a headless server, keep JetBrains and Codex on the client Mac and connect
through reviewed SSH access. Git, compilers, Zsh and AWS CLI work without a host
desktop. SSH remains opt-in during installation; authenticate as the named user,
not root, and review the network exposure before enabling it.

On a host with a graphical session, install Toolbox and Codex configuration separately:

```sh
./bin/workstationctl toolbox setup
./bin/workstationctl codex configure
```

Toolbox uses the checksum-pinned vendor archive and does not enable login
autostart. JetBrains and Codex authentication are interactive. Codex stores CLI
and MCP OAuth credentials in the desktop keyring. Do not run that configuration
step on a headless host without an available, unlocked Secret Service keyring.
This profile does not silently fall back to plaintext credential storage.
AWS administration begins
with `aws configure sso`; do not create static administrator keys. The Arch
packages supply Git, Git LFS, AWS CLI v2, Codex, kubectl, Kustomize, Helm, k9s,
kubectx, Stern, and eksctl.

The K3s workflow does not need Rancher Manager or Helm. If Rancher Manager is
added later, re-resolve its support matrix and use the Helm major version that
its installation documentation requires; do not assume Arch's current Helm 4
client is compatible with charts that require Helm 3.

For optional local-model agent clients in PyCharm or another ACP-capable IDE,
see [AGENT-HARNESSES.md](AGENT-HARNESSES.md). Qwen Code is the first client to
qualify; DSH and Hermes remain explicit alternatives. The generated client
bundle does not install a harness, expose SGLang, start a workload, or grant
tool permissions. Keep the client on the Mac or other development machine and
use an owner-operated loopback tunnel only after the selected SGLang profile
has passed its target-machine qualification.

## Reviewed AUR and git kernel

Paru is deliberately not trusted before the base and recovery paths work. Pin
and review its AUR checkout, then build it in an existing devtools clean chroot:

```sh
sudo mkarchroot /var/lib/archbuild/workstation/root base-devel
./bin/workstationctl aur checkout paru build/paru
./bin/workstationctl aur bootstrap-paru build/paru \
  /var/lib/archbuild/workstation artifacts/paru
```

The builder accepts only an initialized chroot below `/var/lib/archbuild`,
rejects tracked, staged, and untracked changes, archives only the locked AUR
commit, runs Namcap, and leaves a build-lock record. Review and sign the package,
add it to a pacman local repository, then install it explicitly. Paru never
receives blanket confirmation and is not allowed to rebuild kernels, firmware,
Mesa, or compute-runtime heads ad hoc.

The git-kernel path is similarly separate:

```sh
./bin/workstationctl aur checkout linux-git build/linux-git
./bin/workstationctl kernel build build/linux-git \
  /var/lib/archbuild/workstation artifacts/linux-git
# Review, sign, add, and install the resulting linux-git packages.
sudo ./bin/workstationctl kernel build-uki
sudo ./bin/workstationctl kernel promote linux-git \
  /var/lib/workstation/uki/arch-linux-git.efi /efi /efi2
```

The AUR packaging commit and the Torvalds-tree commit are independently locked.
The builder applies the repository's native-CPU Kconfig overlay through the
locked recipe's `config.user`, then verifies the headers package's effective
configuration. The build records config hashes/deltas, the headers package's
`.BUILDINFO` and toolchain summary, artifact hashes and Namcap output. Its measured
memory-heavy `MAKEFLAGS` limit is passed into the clean chroot; host
makepkg native flags and ccache configuration do not implicitly apply there.
Run this build on the actual Threadripper. It rejects existing output
directories and does not change the locked input checkout.
The UKI is built on encrypted root and verified against the configured Secure
Boot database before either ESP or BootOrder changes. Roll back without copying
or deleting files:

```sh
sudo ./bin/workstationctl kernel rollback lts
```

Keep stable as the initial default. The qualified git kernel can become the
daily experimental kernel after the promotion gate; retain stable/LTS recovery.

## Boot and TuneD

Use `boot-benchmark` before and after one change. The initramfs deliberately
relies on modalias/udev discovery instead of a blanket preload list. Static
preloading is a reliability exception for a proven boot-critical device, not a
general speed optimisation.

The generic installer uses `TUNED_PROFILE=auto`: headless selects `balanced`,
and desktop selects `desktop`. The home-lab example explicitly overrides this
with `accelerator-performance`. Installation writes the selection offline; it
does not call a TuneD daemon inside the chroot. On the installed host,
`workstationctl profile ai` selects that accelerator policy and `profile server`
restores `balanced`. The workstation config example selects `ai` when its
configured `profile` command is called without an argument; without a config,
the fallback remains `server`. All selections persist until changed.
Compare the profiles using the AI runbook: throughput-oriented settings can
force minimum performance and constrain idle states, increasing power and heat.
Keep `irqbalance`; do not add manual IRQ, NUMA, SMT, C-state, or mitigation
settings without repeatable workload evidence.

## KVM and K3s prerequisites

Enable IOMMU in firmware; the signed kernel command line retains `iommu=pt`
for passthrough mappings. This is not full DMA isolation for all host devices.
After installing the virtualization profile:

```sh
sudo ./bin/workstationctl virtualization configure developer
./bin/workstationctl virtualization validate
```

This enables only libvirt socket activation and group access. It does not
autostart a VM or virtual network. Log out after the group change. Validate KVM,
IOMMU groups, stable/LTS boots, and host networking before following the K3s
procedure in [operations](OPERATIONS.md).

Both R9700s stay on the Arch host for multi-GPU workloads. Any future passthrough
change requires a new device/topology decision and an isolated IOMMU group.
Never use `pcie_acs_override`.
