# Host maintenance

Low-overhead jobs that watch the host's drives, hardware errors, security
advisories, package cache and build storage. They report into the existing
node-exporter, Alloy and NAS Grafana stack. None of them upgrades packages,
changes mirrors, runs SMART self-tests, offlines memory or deletes build
outputs or images. The [upgrade plan](HOST-UPGRADE-PLAN.md) covers moving off
the package snapshot.

The `host_maintenance` Ansible role in
[`infrastructure/ansible/roles/host_maintenance`](../infrastructure/ansible/roles/host_maintenance)
installs everything below. The view is the **Maintenance & hardware health**
row of **Workstation / Host & storage** in Grafana.

## What runs

| Unit | When | Runs as | Reads | Writes |
| --- | --- | --- | --- | --- |
| `smartd.service` | Every 30 minutes (smartd default) | root | Both NVMe drives by `/dev/disk/by-id` | Journal warnings only |
| `rasdaemon.service` | Continuously | root | Kernel RAS trace events (MCE, memory controller, PCIe AER, memory failure, CPER) | `/var/lib/rasdaemon/ras-mc_event.db` |
| `workstation-hardware-health.timer` | Every 10 minutes | root, no network | SMART logs, the rasdaemon database, unit states | `maintenance-health.prom` |
| `workstation-advisories.timer` | Daily | dynamic user | Arch security tracker, installed packages, a private current-Arch database copy | `maintenance-advisories.prom`, `advisories.json` |
| `workstation-retention-report.timer` | Daily, 05:30 | the build user, read-only home | Podman store metadata, `~/ai-stack/builds`, live Kubernetes image references | `maintenance-retention.prom`, `retention.json` |
| `paccache.timer` | Weekly | root (packaged unit) | `/var/cache/pacman/pkg` | Removes package files beyond the retention below |
| `fstrim.timer`, `systemd-tmpfiles-clean.timer` | Unchanged | Unchanged | Unchanged | Result recorded only |

Every job ends with a root `ExecStopPost` hook (`workstation_maintenance.py
job-result`). It records the result and the run's CPU time, peak memory and
disk I/O from systemd accounting. It also validates the job's metrics file
(fixed name prefixes and well-formed lines only) before installing it in
`/var/lib/workstation-telemetry`. Reports stay in each job's state directory:

- `/var/lib/workstation-advisories/advisories.json`
- `/var/lib/workstation-retention/retention.json`
- `/var/lib/workstation-maintenance/jobs.json`

## Read the results

```sh
systemctl list-timers 'workstation-*' paccache.timer fstrim.timer
jq '.findings[] | {package, installed, class, severity, fixed, current_arch}' \
  /var/lib/workstation-advisories/advisories.json
sudo ras-mc-ctl --summary
sudo smartctl -a /dev/disk/by-id/nvme-<model>_<serial>
jq '.runs[] | select(.decision == "candidate")' /var/lib/workstation-retention/retention.json
```

The Monitoring dashboard's filtered log panel includes the
`job="workstation-maintenance"` streams: job results, classified smartd warnings
and rasdaemon events. Host Alloy replaces each message with a fixed template;
the full line stays in the local journal.

Missing data is not healthy data. An empty panel means a job or timer has not
reported. The NAS raises `WorkstationHardwareHealthSampleStale` and
`WorkstationAdvisoryScanFailing` for missing or stale reports. The rules live in
the NAS repository's `observability/config/workstation-alerts.yaml`. The NAS
Alertmanager's default receiver is local-only, so alerts are visible in its UI
but are not delivered anywhere until a receiver is configured.

## Security advisories

The advisory job downloads the Arch security tracker
(`security.archlinux.org/issues/all.json`) and runs `arch-audit --show-testing
--json` against that file. It does not use `-u`. It then repeats the match
independently: every group except "Not affected" whose recorded fix, if any, is
newer than the installed version. If the two disagree, the scan fails. A
failed, partial or empty scan is never reported as clean. The last good
findings stay visible with their own timestamp, and
`workstation_advisory_last_run_success` drops to 0.

Each vulnerable installed package gets one class:

| Class | Meaning |
| --- | --- |
| `fix_in_configured_repo` | The fixed version is in the configured (snapshot) repositories; a full `pacman -Syu` against them would install it |
| `fix_needs_snapshot_move` | The tracker records a fix that is newer than the snapshot; it needs the [upgrade plan](HOST-UPGRADE-PLAN.md) |
| `no_fix_recorded` | The tracker records no fixed version. Review applicability: many such groups are stale entries for versions far older than the installed one |

`newer_in_current_arch` shows whether current Arch ships a newer build. The job
reads current Arch from a private database copy (`checkupdates`-style, under its
own state directory, refreshed with `fakeroot pacman -Sy --dbpath`). It never
touches `/var/lib/pacman/sync` or the mirror list. The same copy gives
`workstation_update_pending_packages`.

The tracker cannot assess foreign packages. These are listed as uncovered:
`linux-git`, `k3s-workstation-bin`, the `arch-workstation-*` packages and
`spry-ai-workstation-bridge`. Container images are scanned separately by Trivy
when the AI-stack pipeline publishes them. Kernel and K3s security relies on
their pinned upstream releases (`versions.lock`).

## SSD health

smartd watches both NVMe namespaces by stable identity. The role discovers each
controller's model and serial and requires one `/dev/disk/by-id` link per drive
(`lab_maintenance_expected_nvme_count`, default 2). smartd checks overall
health and the critical-warning bits (`-H`), new error-log and self-test-log
entries, and temperature. It logs at 70 °C and raises a critical message at
80 °C. The drives report their own warning and critical thresholds as 84 and
88 °C.

No self-tests are scheduled, and the jobs never start one. To run a short
self-test, choose a quiet maintenance window:
`sudo smartctl -t short /dev/disk/by-id/...`.

The health job also exports each drive's lifetime counters. Unsafe shutdowns
count power losses without a clean NVMe shutdown notification.

## Hardware errors (rasdaemon)

`rasdaemon-workstation` is built from
[`infrastructure/packages/rasdaemon/PKGBUILD`](../infrastructure/packages/rasdaemon/PKGBUILD).
It uses the upstream 0.8.5 release, pinned by hash. It records MCE (AMD SMCA
decoding), EDAC memory-controller, PCIe AER, memory-failure and non-standard
CPER events into SQLite. Page offlining, CPU isolation, ABRT reporting and
disk-error tracing are not compiled in, so no automatic mitigation can be
switched on through `/etc/conf.d/rasdaemon`. The node-exporter EDAC counters
remain on the dashboard alongside it.

Build it as an unprivileged user, review it and install it:

```sh
cd infrastructure/packages/rasdaemon && makepkg -d --cleanbuild
sudo pacman -U rasdaemon-workstation-0.8.5-1-x86_64.pkg.tar.zst
```

`-d` skips the build-time check for `perl-dbd-sqlite`, which is needed only by
`ras-mc-ctl` at run time. `pacman -U` installs it from the configured repository.
Sign the package with the owner key (`makepkg --sign`) when the rest of the
local packages are signed. Events persist in
`/var/lib/rasdaemon/ras-mc_event.db` across daemon restarts and reboots.

## Package cache

`paccache.timer` runs weekly with the packaged unit's idle CPU and I/O priority
and sandbox. Only its arguments change:

```text
paccache -r -c /var/cache/pacman/pkg -k 3 -i <protected packages>
```

Three versions of every cached package are kept, installed or not. Every
version of the boot, storage and GPU packages in `lab_paccache_protected` is
kept: kernels, firmware, microcode, initramfs and UKI tooling, the
initramfs storage stack and Mesa. Uninstalled packages are not removed, and
orphans are not touched. The custom kernel and workstation package artifacts
live outside the pacman cache (the kernel build and install work directories
and the package build directories), so paccache never sees them.

## Build and image retention (report only)

The retention job never deletes. Deletion is not implemented. It writes an
explicit candidate list with estimated recoverable space, using these rules:

| Decision | Rule |
| --- | --- |
| keep | Images whose digest is used by a Deployment, ReplicaSet with replicas, Job, CronJob, StatefulSet, DaemonSet or Pod |
| keep | The previous distinct image of each Deployment's rollout history (rollback) |
| keep | Base and tool images pinned in the current `stack.lock` |
| keep | The newest published image of each repository |
| keep | Run directories whose `RECEIPT.json` inputs match the current `stack.lock` pins, and the newest completed run of each input stage (the newest two for `sglang`) |
| keep | Run directories of a stage that is building now (stage state file or an `ai-stack-*` container) |
| keep | Image-stage run directories with `out/image.digest` (scan, SBOM and provenance; small) |
| review | Images that were never published (`out/image.digest` absent): the local copy may be the only one |
| candidate | Published images (the registry holds them) that are not kept above |
| candidate | Superseded completed input runs, and incomplete runs idle for over a day |

If any input (Kubernetes, Podman metadata, build tree or `stack.lock`) cannot
be read, no candidates are listed. Image space is counted per layer, so shared
layers are counted once and never attributed to a candidate that shares them
with a kept or review image. `~/ai-stack/ccache`, `src`, `trivy-cache` and the
ROCm build tree are always preserved and reported separately. Inference model
caches are not part of this inventory.

To reclaim space, the owner removes reviewed candidates by hand. First confirm
each published digest is still in the registry. Then remove one tag at a time
with `podman rmi`, and run `podman image prune` for the now-unused build-cache
layers. Do not run `podman system prune -a`. Unused by Podman does not mean
disposable.

## Configuration

Set these in the private site file (`group_vars` defaults in brackets):

| Variable | Purpose |
| --- | --- |
| `lab_maintenance_build_user` (empty) | The owner of `~/ai-stack` and the rootless Podman store; empty skips the retention report |
| `lab_maintenance_expected_nvme_count` (2) | Required number of NVMe namespaces with by-id links |
| `lab_smart_temperature_info` / `_critical` (70/80) | smartd temperature limits, below the drives' 84 °C threshold |
| `lab_paccache_keep` (3) and `lab_paccache_protected` | Package cache retention |
| `lab_ras_enabled` (true) | Require and enable `rasdaemon-workstation` |

Run the playbook with `--check --diff` first. The role restarts only smartd
and rasdaemon, and only when their configuration changes. It never restarts
K3s.

## Roll back

```sh
sudo systemctl disable --now workstation-advisories.timer workstation-hardware-health.timer \
  workstation-retention-report.timer paccache.timer smartd.service rasdaemon.service
sudo rm /etc/systemd/system/workstation-{advisories,hardware-health,retention-report}.{service,timer}
sudo rm -r /etc/systemd/system/{paccache,fstrim,systemd-tmpfiles-clean,rasdaemon}.service.d
sudo systemctl daemon-reload
sudo rm /var/lib/workstation-telemetry/maintenance-*.prom
```

Restore `/etc/smartd.conf` from the timestamped backup Ansible left beside it.
Keep `/var/lib/rasdaemon` as hardware-error evidence. Use
`pacman -R rasdaemon-workstation` only if the package itself must go. Set
`lab_host_maintenance_enabled: false` so later plays do not reinstall the
role. Revert the host Alloy journal sources by reinstalling the previous
`host.alloy` and restarting `workstation-alloy.service`.
