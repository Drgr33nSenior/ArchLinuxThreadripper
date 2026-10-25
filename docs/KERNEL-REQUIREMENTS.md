# Native kernel requirements

The authoritative target policy is
`infrastructure/kernel/threadripper-9960x-dual-r9700.json`. It applies only to
the observed 9960X/TRX50 AI TOP/dual-R9700 workstation. Stable/LTS and recovery
kernels remain generic. A configuration/package pass is not a boot or workload
qualification.

## Audit scope, 7 October 2026

| Workload or device | Required coverage |
| --- | --- |
| ROCm and GPU communication | amdgpu/DC, HSA, AMD P2P plus generic PCI P2PDMA, ATS/PRI/PASID, SVM/HMM and device-memory migration |
| CPU scheduling and memory | Native CPU compilation, SMP/SMT, AMD P-State/CPPC, NUMA, dynamic preemption, existing hugepage capabilities |
| K3s/containerd | Namespaces, cgroup CPU/memory/PID/device controls, seccomp, BPF, overlayfs, bridge/veth/VXLAN and IPv4/IPv6 Netfilter/NAT/IP sets |
| Boot and encrypted storage | EFI/UKI, initramfs decompression, both NVMe devices, md RAID0, dm-crypt, AES/XTS/SHA256, XFS/FAT, TPM, USB and keyboard recovery |
| Networking and peripherals | Aquantia, ath12k/Wi-Fi 7, SATA, xHCI, Thunderbolt, USB storage/audio, WireGuard |
| Profiling and reliability | perf, AMD PMUs, tracepoints/ftrace, kprobes/uprobes, BPF/BTF, PSI, EDAC/MCE/RAS, AER/DPC and security controls |
| Monitoring | k10temp, Corsair PSU, RAPL MSR/common, SPD5118, NCT6775, Gigabyte WMI and SMBus; driver presence does not prove board/sensor access |
| Gaming/streaming | amdgpu display, simpledrm/VKMS, 32-bit compatibility, ntsync, evdev/uinput/UHID and USB audio |

The first audit found `CONFIG_HSA_AMD_P2P` disabled in the running kernel.
Its exact compiled amdgpu peer-admission function returned false. The new
overlay enables the option and package validation rejects its absence.
The other listed capabilities were already enabled in the observed kernel;
they are now explicit requirements rather than incidental Arch defaults.
Every loaded module is also required by the fresh profile's package closure.

AMD also lists `CONFIG_DMABUF_MOVE_NOTIFY`. That historical switch does not
exist in the pinned Linux 7.3-rc6 source. The
[DMA-buffer Kconfig](https://github.com/torvalds/linux/blob/a90ee4305c4a5df72c11b31dacfdc76e00fcf78a/drivers/dma-buf/Kconfig)
has no such option, and amdgpu registers
[`amdgpu_dma_buf_move_notify` as its `invalidate_mappings` callback](https://github.com/torvalds/linux/blob/a90ee4305c4a5df72c11b31dacfdc76e00fcf78a/drivers/gpu/drm/amd/amdgpu/amdgpu_dma_buf.c)
without that conditional. Require `CONFIG_DMA_SHARED_BUFFER=y`,
`CONFIG_PCI_P2PDMA=y` and `CONFIG_HSA_AMD_P2P=y` in the effective configuration.
The policy, build overlay and package gate enforce these available symbols.
Adding an unknown option would not enable additional functionality. This is
source evidence; the running kernel and GPU handover still need target checks.

The overlay disables only i915, Xe, Nouveau and legacy Radeon, which are not
drivers for the two installed R9700s. This reduces build/package scope; it is
not a demonstrated runtime speedup. It does not remove Intel-named RAPL or
Thunderbolt drivers required on this AMD platform. No localmodconfig pruning,
mitigation disablement, ACS override, power/clock override, forced SMBus access
or default IOMMU-mode change is included. BTF and diagnostic facilities remain
available for subsequent performance work.

## Build and verification

Use the existing [profile, clean-chroot build and UKI workflow](WORKSTATION.md#reviewed-aur-and-git-kernel).
Keep the same locked kernel commit while qualifying these configuration changes.
Kernel jobs use a separate bounded estimate; use all 48 logical CPUs only when
the available-memory calculation permits it. Parallel linking and large source
files can exceed a per-job estimate. Retain the observed peak and stop on
unexpected pressure; do not treat MAKEFLAGS as a memory limit.

Before installation, verify the effective packaged configuration, module
inventory, exact source/recipe identities, package audits and signatures. Before
reboot, verify both recovery ESP copies, the candidate UKI signature, embedded
kernel/release/command line and storage/unlock/input contents. Keep passphrase
recovery available: a new signed UKI can still invalidate the old TPM PCR11
binding. No TPM enrollment change follows automatically.

After boot, verify host devices, storage, networking and error logs, then test
GPU peer discovery in both directions, bounded copy correctness, supported
remote atomics, RCCL transport and the fixed serving workload. A newly visible
peer is not proof of correct atomics or a serving speedup. Do not force an
unsupported access or promote numerical/performance results from an old boot.

## Primary sources

- [AMD driver IOMMU/P2P requirements](https://instinct.docs.amd.com/projects/amdgpu-docs/en/docs-31.60.0/conceptual/iommu.html): includes AMD-specific P2P configuration. The obsolete `DMABUF_MOVE_NOTIFY` name is not added to the pinned kernel; its exact Kconfig is authoritative. The documented Radeon IOMMU-remapping recommendation remains a separate future test, not part of this change.
- [Linux kernel configuration and recovery](https://docs.kernel.org/admin-guide/README.html): localmodconfig follows loaded modules; preserve a backup kernel and module tree.
- [Kbuild inventory definitions](https://docs.kernel.org/kbuild/kbuild.html): use both packaged modules and `modules.builtin` when validating coverage.
- [K3s requirements](https://docs.k3s.io/installation/requirements): runtime configuration checks complement, but do not replace, the project's workload and network-policy tests.

The retained deployed Linux source at
`a90ee4305c4a5df72c11b31dacfdc76e00fcf78a` and the package's effective `.config`
resolve actual symbol names. Rolling documentation is not a reason to introduce
unknown options or unrelated upgrades.
