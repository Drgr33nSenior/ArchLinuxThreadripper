# Host upgrade plan

A plan for moving the workstation off its installation package snapshot. It
applies the general procedure in [Operations](OPERATIONS.md#host-updates) to
the host as observed on 12 November 2026. Nothing here has been executed. The
owner runs it in a maintenance window. Agents must not change mirrors, run
partial upgrades or reboot the host.

## Where the host is

| Item | Observed 2026-11-12 |
| --- | --- |
| Mirror | `https://archive.archlinux.org/repos/2026/09/04/$repo/os/$arch`, the custom ISO's dated mirrorlist |
| Configured package databases | core 3 Sep, extra 4 Sep 2026; `pacman -Qu` against them is empty, so the system matches the snapshot |
| Behind current Arch | 271 of 637 packages (private `checkupdates`-style comparison; see `workstation_update_pending_packages`) |
| Advisory scan | 16 tracker matches, all `no_fix_recorded` (below); no match has a recorded fix in either the snapshot or current Arch |
| Boot entries | git (default), stable, LTS and LTS recovery UKIs on both ESPs |
| Foreign packages | `linux-git`, `linux-git-headers`, `k3s-workstation-bin`, `arch-workstation-boot`, `arch-workstation-bridge-runtime`, `spry-ai-workstation-bridge` (and `rasdaemon-workstation` once installed) |
| Backups | Not configured (parked by the owner) |

### Is the snapshot still intentional?

Not as a lasting policy. The ISO pins its dated mirror only so that installation
transactions stay coherent. [Operations](OPERATIONS.md#move-from-the-installation-snapshot-to-rolling-arch)
and [ISO reference](ISO-REFERENCE.md) say to keep it through the initial
transactions, then make one reviewed full upgrade. The preconditions for that
move have been met: stable, LTS and recovery boot images exist on both ESPs, and
the host has run its workloads since 8 November. The snapshot is now a
transition that has not happened yet, and every week adds to the eventual
upgrade. Pulling individual packages forward on the snapshot is a partial
upgrade and is not supported.

### The 16 advisory matches

All 16 come from tracker groups that record no fixed version. Each group's
"affected" version is older than the installed one, often by several major
releases. For example, `linux-lts` (AVG-2701, AVG-2683, 2022 CVEs) has
affected 5.15.x, but 6.18.49 is installed. Upstream release notes confirm two of
the newest fixes: Linux-PAM 1.7.1 fixed CVE-2025-6020 (1.7.2 is installed), and
OpenSSL 3.5.1 fixed CVE-2025-4575 (3.6.4 is installed). The other matches are
2025 CVEs fixed in releases older than the installed version (libxml2 2.15.3,
systemd 261, coreutils 9.11, perl 5.42) or groups from 2020–2022. These look
like stale tracker records, not open exposure. Only the two checks above were
verified against upstream; the rest is an assessment that remains under
review. The daily scan keeps reporting them as `no_fix_recorded`, and the
dashboard says that they need review rather than presenting them as clean.

## Choose an update policy

| Option | Mirror | Cadence | Rollback |
| --- | --- | --- | --- |
| A. Rolling (documented default) | Official HTTPS mirrors from the mirrorlist generator | Monthly full upgrade, sooner for a `fix_needs_snapshot_move` advisory of high or critical severity | Previous UKI plus cached packages (`pacman -U` of a complete earlier set) |
| B. Dated snapshot steps | A newer `archive.archlinux.org/repos/YYYY/MM/DD` each cycle | Monthly; record the date like `versions.lock` pins | Point the mirrorlist back at the previous date and run `pacman -Syuu` (a complete, coherent downgrade) |

**Recommendation: option B.** Each upgrade is a reviewed, repeatable step, which
matches the AI-stack pins and the ISO lock. The advisory classes stay meaningful:
`fix_in_configured_repo` means one full `-Syu` fixes the package, and
`fix_needs_snapshot_move` means the next snapshot step is due. Rollback to a
known date is one transaction. Option A is simpler and follows the current
documentation. Choose either, but do not stay indefinitely on the installation
snapshot.

## What this upgrade changes

Notable changes among the 271 packages, with what to check:

| Area | Change | Check |
| --- | --- | --- |
| Kernels and firmware | `linux` 7.2.2 → 7.2.15, `linux-lts` 6.18.49 → 6.18.62, `linux-firmware` and `amd-ucode` 20260810 → 20261019 | Both UKIs re-signed and byte-identical on both ESPs; amdgpu probe of both R9700s; GPU validation |
| Boot tooling | `mkinitcpio` 41 → 42, `systemd`/`systemd-ukify` 261 → 262, `cryptsetup`, `xfsprogs` | `zzz-bootstrap-uki-sync.hook` says to recheck its trigger list when upgrading mkinitcpio; compare it with mkinitcpio 42's hooks before the upgrade |
| Crypto and runtimes | `openssl` 3.6 → 4.1 (major), `perl` 5.42 → 5.44, `python` 3.14.7 → 3.14.10, `glibc` snapshot bump | `rebuild-detector` output after the transaction; foreign packages are mostly scripts or static (`k3s`) |
| Toolchain | `clang`/`llvm` 22 → 23 | Affects the next `linux-git` build only; rebuild and qualify it separately |
| GPU userspace | `mesa`/`vulkan-radeon` 26.2.2 → 26.2.6; host `python-pytorch-opt-rocm` 2.13 → 2.15; host ROCm 7.2.4 unchanged | Host ROCm/PyTorch smoke tests; serving images are separate and unaffected |
| Containers | `podman` 6.1 → 6.2, `crun` 1.29 → 1.31 | `podman images` over the 400 GiB rootless store; one `build-stage.sh` dry stage |
| Telemetry | `grafana-alloy` 1.13 → 1.21 | `grafana-alloy validate /etc/workstation-telemetry/host.alloy` before restarting `workstation-alloy` |
| Tools | `nvme-cli` 2 → 3, `fakeroot` epoch change, `smartmontools` rebuild | Maintenance jobs use `smartctl`; rerun them once |

K3s is not affected: its pinned binary comes from `k3s-workstation-bin`.
Kubernetes workload images are not affected either. Kernel modules for
`linux-git` do not change, but its headers package depends on openssl, perl and
python. Rebuild `linux-git` only through its own reviewed procedure.

## Before the window

1. Record the current state outside the paths being edited:
   `pacman -Q`, `pacman -Qm`, `/etc/pacman.conf`, `/etc/pacman.d/mirrorlist`,
   `bootctl list`, the ESP UKI hashes, the hardware report and `systemctl --failed`.
2. Make sure the package cache holds the full installed set (`paccache` keeps 3
   versions and every boot/storage/GPU version). Keep the custom package build
   directories.
3. Backups are parked. Before upgrading, at least copy `/etc`, `/var/lib/sbctl`
   (offline, encrypted) and the K3s datastore with the documented
   [service recovery](SERVICE-RECOVERY.md) capture. Without them, a failed
   upgrade can only be rolled back, not restored.
4. Read current Arch news for the snapshot-to-target interval, especially
   openssl 4, mkinitcpio 42 and systemd 262.
5. Check free space for about 3–5 GiB of downloads.

## In the window

1. Close admission and drain GPU workloads: `sudo workstationctl mode maintenance --execute`.
2. Option B: replace the date in `/etc/pacman.d/mirrorlist` with the reviewed
   target date. Option A: install a reviewed list from the official generator.
   Check `/etc/pacman.conf` for archive URLs and `IgnorePkg`.
3. Run one complete `sudo pacman -Syu`. Do not use `--ignore` or `--overwrite`.
4. Review `.pacnew` files, the `rebuild-detector` output and the UKI hook
   result. Confirm both ESPs hold byte-identical signed UKIs.
5. Reboot into the stable kernel, then the default git kernel. Confirm that
   `workstation-hardware-evidence.service` wrote this boot's
   `/var/lib/workstation-evidence/boot-<boot id>/hardware.json`.
6. Run storage, remote-access, GPU and LLM smoke tests, then
   `sudo workstationctl mode family --execute`. Confirm on the maintenance
   dashboard that every maintenance job succeeds again. The advisory scan's
   pending-package count should drop to 0 for option A, or to the gap between
   the chosen date and today for option B.

## Roll back

- Boot failure: select the previous stable, LTS or recovery UKI from the boot
  menu. Both ESPs still hold them until the next transaction.
- Package regression, option B: restore the previous mirrorlist date and run
  `sudo pacman -Syuu`. Option A: reinstall the recorded package set from the
  cache with one `pacman -U` transaction.
- Do not downgrade single packages, and do not mix the snapshot and current
  repositories.

## After the upgrade

- Keep `workstation-advisories.timer` running. Treat
  `WorkstationAdvisoryFixAvailable` with `fix_needs_snapshot_move` and high or
  critical severity as a reason to bring the next cycle forward.
- Rebuild `rasdaemon-workstation` only if `rebuild-detector` lists it. It links
  libtraceevent, libpci and sqlite.
- Record the new snapshot date (option B) and the upgrade evidence in the
  owner's change log.
