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

## Start here

Run commands from the reviewed checkout unless the guide specifies the packaged
live-console launcher. Keep nonsecret settings in the existing config examples
and versions in the locks; do not infer target identity from a diagram or example.

| Task | Guide |
| --- | --- |
| Build, bundle Bridge, sign, assemble and write the installer ISO | [ISO](docs/ISO.md) |
| Boot, Wi-Fi, optional Codex, install, recover and hand off to Bridge | [Installation](docs/INSTALLATION.md) |
| Native media builds, troubleshooting and disposable UEFI acceptance | [ISO reference](docs/ISO-REFERENCE.md) |
| Packages, shell, native compiler policy and experimental kernels | [Workstation setup](docs/WORKSTATION.md) |
| Updates, backups and retained VM lifecycle | [Operations](docs/OPERATIONS.md) |
| Bare-metal K3s, networking, GPU allocation and application profiles | [Home lab](docs/HOME-LAB.md) |
| Native ROCm SDK, llama builds, ccache and source-package status | [ROCm](docs/ROCM.md) |
| CPU/RAM budgets and AI/gaming session policy | [AI performance](docs/AI-PERFORMANCE.md) |
| Serving, numerical quality and comparative measurement | [Performance validation](docs/PERFORMANCE-VALIDATION.md) |
| Opt-in compilation, cache reuse and kernel dispatch | [Model kernels](docs/MODEL-KERNELS.md) |
| Local metrics/logs/traces, Bridge telemetry and installation events | [Telemetry](docs/TELEMETRY.md) |
| Model selection/staging, retrieval and client agents | [Models](docs/MODELS.md), [RAG](docs/RAG.md), [agent harnesses](docs/AGENT-HARNESSES.md) |
| Gaming image, capture, encoding and input acceptance | [Sunshine](docs/SUNSHINE.md) |
| Design, dated diagram, secret boundaries and source register | [Architecture](docs/ARCHITECTURE.md), [stack view](docs/STACK.md), [security](docs/SECURITY.md), [sources](docs/SOURCES.md) |
| Optional management integration contract | [Bridge contract](docs/BRIDGE-CONTRACT.md) |
| Historical checks, failures and source-specific acceptance gaps | [Validation records](docs/validation) |

The default install includes the signed Bridge package and reviewed runtime/reference
payload; it does not activate management services, create credentials or authorize
host operations. `INSTALL_BRIDGE=false` explicitly opts out. Codex guidance is
optional and never substitutes for owner disk confirmation or local secret entry.

Bare-metal AI uses K3s with SQLite. The optional AlmaLinux/KVM lab uses embedded
etcd and its own restore workflow; its backups do not cover bare-metal application
volumes. Neither is high availability. Workloads remain disabled until their
image/device/access qualification passes. Model weights are staged separately,
not bundled in the ISO. The retained `llm` commands are Intel-only.

## Supported posture

This is a personal development workstation and lab, not a vendor-certified
production platform. Source tests, package builds, signed media, successful boots
and measured workload results are separate evidence. Preserve stable/LTS recovery
before experimental software; no hardware performance improvement is implied.

RAID0 has no redundancy: either member's failure loses root data. Keep tested,
off-array backups and independent recovery credentials. The intended NAS/bare-metal
K3s recovery acceptance remains separate from the optional VM backup workflow.
