# Architecture

For the bare-metal AI/gaming stack and the separate optional VM lab, see the
[visual stack overview](STACK.md). It distinguishes repository configuration
from workloads and hardware that still need qualification.

## Headless host

The default host runs a multi-user target, upstream GPU drivers and server tools.
GNOME/GDM and local Steam are optional, not dependencies of AI, KVM or K3s.
A qualified gaming workload must supply its own graphics session and encoder
access. Removing the host desktop does not resolve its admission or GPU-sharing
requirements. No existing host is automatically converted.

## Host storage and boot

```text
UEFI firmware
  ├─ ESP 1: signed stable, LTS, git, and recovery UKIs
  └─ ESP 2: identical signed UKIs and fallback path
          ↓
mdadm RAID0 (two equal NVMe member partitions)
          ↓
LUKS2 (Argon2id passphrase + optional FIDO2 + recovery key)
          ↓
XFS root
```

The ESPs are independent. They protect against an ESP or firmware-entry
failure, but they cannot make RAID0 survive an NVMe failure.

The first installed boot paths are official Arch stable and LTS kernels. A
reviewed, commit-pinned `linux-git` package is built later in a clean chroot.
Its UKI is first generated and signed on encrypted root, then copied to both
ESPs. Kernel promotion changes BootOrder but never deletes a fallback.

The packaged ALPM hook covers mkinitcpio's kernel and initramfs regeneration
triggers. It explicitly signs and verifies the regenerated UKIs before mirroring
stable, LTS, recovery and fallback images. Manual synchronization verifies
existing signatures unless signing is explicitly requested. Before copying,
the shared validator requires exact `/efi` and `/efi2` mounts matching fstab,
distinct GPT ESP identities and different parent disks. Firmware entry selection
also checks the expected ESP PARTUUID and loader, not just its display label.
Git-kernel promotion stays manual so an untested tree cannot become
preferred merely because a package was updated.

## GPU layers

The current dual-R9700 profile keeps both GPUs on the Arch host using upstream
`amdgpu`, official firmware and RADV. Native builds and ccache are user-scoped;
hardware detection supplies the single ROCm code-generation target. See
[ROCM.md](ROCM.md) for implemented validation and the remaining reproducible
TheRock build/packaging milestone. Official and experimental generations have
separate environments and signed package snapshots.

### Retained Intel design

The following design applies only to the legacy B70 package profile and commands.

The Intel Arc Pro B70 remains bound to the in-kernel `xe` driver and serves
GNOME, Steam, and the local LLM container. The host uses official Arch firmware,
Mesa, Vulkan, Intel compute runtime, and Level Zero packages.

Intel's published B70 validation targets its Ubuntu/OMIX combinations, not an
Arch host on AMD Threadripper. The Arch stack is current and technically
appropriate, but it remains an explicitly unvalidated workstation combination;
smoke, soak, version capture, and rollback are part of every promotion.

Experimental layers are separated:

- A git kernel has its own signed UKI.
- Mesa main starts in a private prefix and must move as a matched 64/32-bit set.
- Compute-runtime development components stay in a pinned rootless container.
- The LLM server is on demand and binds only to `127.0.0.1:8000`.

A future passthrough GPU is not pre-bound. Its complete IOMMU group is validated
after installation before exact PCI functions are assigned to VFIO. ACS
override is prohibited.

## K3s lab

```text
Trusted host ── libvirt NAT management NIC ── K3s API and SSH

Public IPv4/IPv6 ── router firewall ── tagged DMZ bridge ── VM DMZ NIC
                                                     └─ TCP 443 → Traefik
```

The host has no Layer-3 address on the DMZ bridge. The VM receives an access
NIC, not a VLAN trunk. Kubernetes Pods and Services remain IPv4-only. Public
IPv6 terminates at the VM boundary, and the router remains stateful.

The VM is one AlmaLinux K3s server with 8 vCPU, 16 GiB fixed memory, an 80 GiB
system overlay, and a separate 120 GiB data overlay. Flannel uses the management
NIC. Traefik is the only ingress controller. K3s, Rancher Manager, and the
future GPU worker are separate lifecycle decisions. The current two R9700s are
reserved for host AI workloads, not VM passthrough.

The VM keeps embedded etcd for its snapshot/restore workflow and SELinux
enforcement through the pinned K3s policy RPM. The bare-metal AI home lab uses
K3s's default SQLite datastore. Both use the same Kubernetes version lock;
neither single-server topology provides high availability.

## Configuration and provenance

The three `config/*.conf` files contain only literal, non-secret deployment
values. Parsers use an explicit key allowlist and never evaluate configuration
as shell code.

`versions.lock` is the authoritative external-input lock for cloud images,
manifests, OCI images, vendor archives, and AUR source commits. Refresh it only
through a reviewed change that verifies upstream signatures or checksums.

Rootless AI containers, libvirt guests, and K3s containers share the host
kernel where applicable; none can repair a bad host kernel, firmware, IOMMU, or
PCIe configuration. That is why stable/LTS boot paths precede all container and
VM work.
