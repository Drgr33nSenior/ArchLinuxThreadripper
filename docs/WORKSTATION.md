# Workstation setup

Run this procedure only after you verify stable and LTS boots, Secure Boot
recovery, disk unlock, and the first offline LUKS-header copy.
For the AI-specific audit and measurement sequence, see
[AI-PERFORMANCE.md](AI-PERFORMANCE.md). If installed from the custom ISO, review
its mirrorlist and the explicit transition to rolling Arch first.

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
New configurations select `gpt-6.1-sol`, as does the
[separate installation-console profile](INSTALLATION.md#optional-codex-authentication).
The configure command refuses to overwrite an existing file. To migrate an
existing desktop configuration, have the owner set `model = "gpt-6.1-sol"` at
the top level of `~/.codex/config.toml`, before any table headers. Preserve its
credential-store settings and confirm the selected model at interactive startup.
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
./bin/workstationctl kernel profile artifacts/kernel-profile
./bin/workstationctl aur checkout linux-git build/linux-git
./bin/workstationctl kernel build build/linux-git \
  /var/lib/archbuild/workstation artifacts/kernel-profile artifacts/linux-git
# Review, sign, add, and install the resulting linux-git packages.
sudo ./bin/workstationctl kernel build-uki
sudo ./bin/workstationctl kernel promote linux-git \
  /var/lib/workstation/uki/arch-linux-git.efi /efi /efi2
```

Run `kernel profile` as the installed named user on the target workstation. It
collects only the reviewed board/CPU identity, PCI IDs and bound modules, loaded
module names, running kernel release and kernel configuration. It does not retain
DMI serials, GPU serials, network addresses, credentials or the kernel command
line. A profile is accepted only for the 9960X, TRX50 AI TOP F12h and exact dual
R9700/device inventory in
`infrastructure/kernel/threadripper-9960x-dual-r9700.json`. A BIOS, card, driver
or topology change requires a new reviewed policy; do not edit a captured result.
The current generic kernel does not need to contain the candidate-only native CPU
setting. The profile records every difference between its configuration and the
candidate contract. The clean-chroot build, not capture, must close all of those
differences. Device binding and the captured profile manifest still fail closed.

The AUR packaging commit and the Torvalds-tree commit are independently locked.
The builder applies the repository's native-CPU Kconfig overlay through the
locked recipe's `config.user`, then verifies the headers package's effective
configuration and every required module against the captured profile. The build
records policy/profile/module/config hashes and deltas, the headers package's
`.BUILDINFO` and toolchain summary, artifact hashes and Namcap output. Its measured
kernel-specific `MAKEFLAGS` and `NPROC` limit is passed into the clean chroot; host
makepkg native flags and ccache configuration do not implicitly apply there.
Run this build on the actual Threadripper. It rejects existing output
directories and does not change the locked input checkout.

The build applies `templates/workstation/linux-git-packaging.patch` to a
verified copy of the locked recipe. It pins the recipe's version to the locked
7.3-rc6 source so makepkg does not reset the package release after configuration.
Package release 2 added the headers package's required script interpreters,
cryptography module and tools, and removes its incorrect backup entries.
Release 3 enables AMD PCIe peer support and omits the four other GPU families
listed in the target overlay. The source commit remains unchanged.
It does not change kernel code or disable package audits. The build retains the
effective recipe and patch hashes. Audit logs and detached signatures are not
package archives and are excluded from artifact discovery.

The current experimental source records both the annotated `v7.3-rc6` tag object
and its exact peeled commit. It is not a released 7.3 kernel or a floating branch.
AMD documents the R9700/gfx1201, but does
not list Arch or Linux 7.3 in its supported operating-system kernel matrix. Treat
this as an experimental daily candidate. A passing profile/build closure proves
only that the required configuration and module files exist. It does not prove
that either GPU initializes, ROCm works, suspend/resume works or performance is
better. Keep stable and LTS as recovery kernels and do not make the git kernel
the default until the boot, GPU, storage, network and sustained-workload gates pass.

Do not generate the production configuration from one `lsmod` snapshot or run
`localmodconfig` automatically. Unused-at-capture recovery, USB, networking and
filesystem modules can still be required later. The reviewed policy keeps those
capabilities and fails the build if the effective configuration or package module
closure drops one. This is an exact required-capability contract, not a claim that
removing other Arch defaults produces a measurable gain. The four excluded
drivers are i915, Xe, Nouveau and legacy Radeon; the installed R9700s use
amdgpu. USB, storage, network and display recovery remain available. A different
GPU inventory needs a revised profile or a generic recovery kernel.

The policy now checks AMD peer support explicitly: `CONFIG_PCI_P2PDMA=y`
alone is insufficient without `CONFIG_HSA_AMD_P2P=y`. It also retains SVM/HMM,
container isolation and networking, profiling/BTF, TPM/keyboard recovery,
sensor modules, and Wine/Sunshine input support. Every currently loaded module
is added to the required package inventory, not used to prune other modules.
See [the kernel requirements audit](KERNEL-REQUIREMENTS.md) for scope and sources.

Kernel compilation has its own `KERNEL_BUILD_JOBS=48` ceiling and
`BUILD_KERNEL_JOB_MIB=512` planning estimate. The previous heavy-build estimate
of 4096 MiB/job unnecessarily restricted this kernel build to eight jobs.
The CPU/cgroup and available-memory checks still apply. With the default
16 GiB host and 8 GiB link reserves, 48 jobs require at least 48 GiB available:

```sh
./bin/workstationctl --config config/workstation.conf build environment kernel
```

These are planning values, not a hard memory quota or a claim that 48 jobs are
always faster. Run the first build in a maintenance window; retain memory,
compiler concurrency and elapsed-time observations. Stop a build if its memory
pressure threatens the host. Do not lower reserves or enable swap to force a
job count. Existing normal and memory-heavy C++/HIP build limits are unchanged.
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
