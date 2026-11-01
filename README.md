# Arch Linux Threadripper AI Workstation

Installation and operations tools for a Threadripper 9960X / Gigabyte TRX50 AI TOP
workstation with two Radeon AI PRO R9700 GPUs. Arch owns the hardware; bare-metal
K3s allocates GPUs to AI and optional gaming workloads. The optional AlmaLinux
KVM lab is separate. New hosts are headless by default.

**Start with the [documentation guide](docs/README.md)** for current runtime
boundaries, tested features and the right procedure for each task.

## Common tasks

| Task | Guide |
| --- | --- |
| Prepare installation media | [ISO build and assembly](docs/ISO.md) |
| Install or recover the workstation | [Installation](docs/INSTALLATION.md) |
| Update, back up or restore | [Operations](docs/OPERATIONS.md), [service recovery](docs/SERVICE-RECOVERY.md) |
| Use Grafana or diagnose missing data | [Monitoring](docs/TELEMETRY.md) |
| Configure models and private chat/API access | [Models](docs/MODELS.md), [AI service](docs/AI-SERVICE.md) |
| Build the Sunshine/Moonlight candidate | [Sunshine](docs/SUNSHINE.md) |
| Measure or optimize inference | [Performance validation](docs/PERFORMANCE-VALIDATION.md), [Hyperloom](docs/HYPERLOOM.md) |

## Commands

Run from the reviewed checkout unless a guide specifies an installed launcher.

| Entry point | Purpose |
| --- | --- |
| `bin/bootstrap-arch` | Live-ISO preflight, installation and installed-system verification |
| `bin/workstationctl` | Hardware/resource plans, builds, workload sessions, telemetry and development tools |
| `bin/k3s-lab` | Optional KVM/AlmaLinux/K3s lifecycle and recovery |
| `bash infrastructure/iso/release.sh` | Fresh package preparation and explicitly authorized ISO assembly |
| `make check-strict` | Required source checks; reports failures and unexpected skips |

Keep nonsecret choices in the existing config examples and exact versions in
locks. Model weights and credentials are staged separately, not in the ISO.
The default install includes the signed Bridge package and runtime/reference
payload; it does not activate management services or authorize host operations.
`INSTALL_BRIDGE=false` opts out.

## Before changing the host

`bootstrap-arch --execute install` **erases both configured disks**. The default
dry run still checks explicit by-id paths, serials, UEFI, unmounted whole disks
and equal RAID-member sizes. An interrupted installation is not resumable.

RAID0 has no redundancy. Preserve tested off-array backups, independent unlock
credentials and stable/LTS recovery boots before experimental changes. No swap,
zram or hibernation is configured. Each GPU has its own 32-GB-class VRAM pool.
The owner reports 128 GB host RAM; collect current topology and Linux-visible
memory before planning capacity.

Source publication, a successful target test and production qualification are
separate. Checked-in workload replicas remain zero until their acceptance gates
pass. See [installation readiness](docs/INSTALL-READINESS.md) and
[security boundaries](docs/SECURITY.md).
