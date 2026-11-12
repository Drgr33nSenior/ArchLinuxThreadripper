# Operations runbook

## Host updates

For the first transition off installation media, complete the
[snapshot-to-rolling procedure](#move-from-the-installation-snapshot-to-rolling-arch). The host-specific
[upgrade plan](HOST-UPGRADE-PLAN.md) lists this host's pending changes and checks.
[Host maintenance](HOST-MAINTENANCE.md) covers the daily advisory report, SMART and
hardware-error monitoring and package-cache retention.
For subsequent full updates:

1. Run a Restic backup and record the installed package/kernel/GPU
   manifest.
2. Keep stable, LTS, and the last accepted git UKIs on both ESPs.
3. Perform one complete `pacman -Syu`; never partially update kernel, firmware,
   Mesa, or the selected GPU compute stack.
4. Confirm the signed-UKI ALPM hook copied byte-identical images to both ESPs.
5. Run storage, remote-access, GPU and LLM smoke tests; test desktop/Steam only
   where those optional layers are installed.
6. Roll back the selected UKI or cached package transaction on failure.

Capture a fresh reviewed hardware profile, then build `linux-git` in a clean
chroot from the reviewed AUR commit. Record the exact upstream kernel tag/commit,
hardware policy/profile, effective config, module inventory, compiler and package
hashes. Promote
it only after the stable/LTS paths have been tested on the current firmware.
The exact commands and clean-chroot boundary are in
[workstation setup](WORKSTATION.md).

## Move from the installation snapshot to rolling Arch

The custom ISO copies its dated mirrorlist into the installed system to keep
installation transactions coherent. Running `pacman -Syu` against that archive
does not move the host forward to today's packages.

After stable/LTS boot and recovery tests pass:

1. Back up the system and retain the known-good package archives and signed
   UKIs. Record the installed package list and the ISO release lock.
2. Inspect `/etc/pacman.conf` and `/etc/pacman.d/mirrorlist`. Save their current
   contents outside the paths being edited. Review current Arch news.
3. Select synchronized HTTPS mirrors using the
   [official mirrorlist generator](https://archlinux.org/mirrorlist/). Replace
   the dated archive selection deliberately. Check for explicit archive URLs
   and `IgnorePkg` entries in pacman configuration too.
4. Run one complete `sudo pacman -Syu`. Review `.pacnew` files and the UKI hook
   results, reboot, then collect new hardware and GPU validation reports.

Do not combine archived libraries with selectively updated ROCm, Mesa, firmware
or PyTorch. Arch supports full upgrades, not partial upgrades. See
[system maintenance](https://wiki.archlinux.org/title/System_maintenance).
Keep the ISO's build lock pinned; changing the installed host's update policy
does not require turning recovery media into a floating build.

For upstream development builds, review and advance the exact commits in
`versions.lock`, inspect recipe/config/build-option changes, and use a new output
directory. Existing pins are reproducible candidate inputs, not a claim that
they remain upstream HEAD. Refresh the kernel and its packaging recipe as a
reviewed pair. Refresh TheRock's dependency inventory with its entry-point pin.

## Performance profiles

The home-lab example selects TuneD's `accelerator-performance` policy. The
generic installer retains `auto`. Select the comparison/rollback profile with:

```sh
sudo ./bin/workstationctl profile server
```

`profile ai` selects accelerator performance again. TuneD selections persist;
there is no automatic restoration after a workload. Compare three or more paired
runs and retain the AI policy only when its workload gain justifies the power,
thermal, latency and acoustic cost. See the
[single/dual-GPU procedure](AI-PERFORMANCE.md#build-and-measure-llamacpp).

Do not add permanent governor, C-state, NUMA, hugepage, IRQ-affinity, RPS, core
isolation, or mitigation overrides.

Measure boot changes before retaining them:

```sh
sudo ./bin/workstationctl boot-benchmark
```

Run it before and after one change, then compare `time.txt`,
`critical-chain.txt`, and `blame.txt`; the SVGs are supporting evidence. Do not
mask a service merely because it appears in `blame`; confirm its dependency and
first-use cost.

## Local LLM (legacy Intel profile)

The local AI service now runs on Kubernetes: see [AI service](#ai-service) below. The
`llm` commands served the retired Intel Arc Pro B70 profile only and do nothing for the
R9700s:

```sh
./bin/workstationctl gpu validate 0000:BB:DD.F
./bin/workstationctl llm up 0000:BB:DD.F /srv/ai/vllm/models/selected
./bin/workstationctl llm status
./bin/workstationctl llm down
```

## K3s lab

Create the VM only after reviewing the generated definition and network plan:

```sh
sudo ./bin/k3s-lab --config config/k3s-lab.conf --dry-run network-create
sudo ./bin/k3s-lab --config config/k3s-lab.conf network-create
sudo ./bin/k3s-lab --config config/k3s-lab.conf --dry-run create
sudo ./bin/k3s-lab --config config/k3s-lab.conf create
sudo ./bin/k3s-lab --config config/k3s-lab.conf up
# Verify the guest SSH host key out of band and populate SSH_KNOWN_HOSTS_FILE.
# Then connect with SSH_PRIVATE_KEY_FILE and run: sudo passwd developer
sudo ./bin/k3s-lab --config config/k3s-lab.conf provision
sudo ./bin/k3s-lab --config config/k3s-lab.conf status
```

Do not provision on first-contact SSH trust. Compare the guest's
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` output through the libvirt
console with the candidate collected by `ssh-keyscan`, then install only the
matching line at `SSH_KNOWN_HOSTS_FILE`. The generated inventory forces that
dedicated file, strict host-key checking, and `SSH_PRIVATE_KEY_FILE`.

Before the first provision, connect to the management IP and run
`sudo passwd developer` interactively (replace the name from configuration).
Cloud-init grants passwordless sudo only for this bootstrap step. Ansible
requires the sudo password, verifies it exists, installs a password-required
policy, and removes the one-time grant. Snapshot, upgrade, and restore commands
therefore prompt for the guest sudo password.

`down` takes a fresh etcd snapshot before a clean shutdown. Sites are expected
to be unavailable while the VM is stopped.

Before a K3s update:

1. Review K3s release notes and workload compatibility for the locked version.
2. Take and export an etcd snapshot and token.
3. Take an application-consistent PV export.
4. Create a cold VM backup.
5. Apply one pinned K3s minor-version step.
6. Validate nodes, system Pods, Traefik, DNS-01, SELinux, NetworkPolicies, and
   the external port boundary.
7. Revert the overlay and restore etcd if any gate fails.

Repository tests render Kubernetes and Helm source locally only; they never
contact a cluster.

## Backups

The [local SSD and S3 workflow](SERVICE-RECOVERY.md#independent-backup-ssd-then-s3)
captures host policy paths and consistent application/gaming state locally before
separate off-site replication. Verify the mounted SSD identity; keep it outside
the RAID and installer targets. Test both restores. The older host-only timer
below does not capture gaming or other Kubernetes PVCs.

- Review the installed include/exclude policy, then create the encrypted
  repository, password, and least-privilege AWS credentials interactively.
- The host backup includes `/var/lib/sbctl`; retain a separately encrypted,
  offline copy of the owner signing keys and recovery procedure as well.
- Run `backup run check` before enabling either timer.
- Keep 7 daily, 5 weekly, and 12 monthly host snapshots.
- Retention selects only snapshots tagged `workstation`, so it cannot forget
  unrelated snapshots if a repository is shared accidentally.
- Take a weekly cold backup of the complete VM backing chain and boot metadata,
  not only the writable overlays. Follow [K3s operations](../ansible/K3S-OPERATIONS.md)
  for the detached restore procedure.
- While K3s is running, take compressed etcd snapshots every six hours and
  retain 14 locally.
- Export the K3s token and latest snapshot through protected credentials.
- Test a file restore and detached K3s restore clone quarterly.

VM snapshots on the same RAID0 array are rollback points, not backups.

First build, review and sign the optional `arch-workstation-backup` split package
using [the ISO source-package workflow](ISO.md), then install it through pacman
with the approved local signing trust. It owns the helper under
`/usr/lib/arch-workstation-backup`, the vendor units and the two editable
`/etc/restic/workstation.{include,exclude}` policy files. It has no activation
scriptlet. `backup install` verifies package ownership and reloads unit definitions;
it no longer copies unowned files into `/usr`.

If a previous setup installed `/usr/local/libexec/workstation-restic` or the
three Restic units under `/etc/systemd/system`, migration is manual. Stop the
old timers during a reviewed maintenance window, archive the old helper/units
and policy, and resolve the unit overrides before enabling the packaged version.
Preserve encrypted credentials and recovery material. Do not use pacman's
overwrite option to conceal a file-ownership conflict.

After the package is installed, setup remains explicit and disabled by default:

```sh
sudo ./bin/workstationctl backup install
sudo ./bin/workstationctl backup credential repository
sudo ./bin/workstationctl backup credential password
sudo ./bin/workstationctl backup credential aws
sudo ./bin/workstationctl backup run init
sudo ./bin/workstationctl backup run check
sudo ./bin/workstationctl backup enable
sudo ./bin/k3s-lab --config config/k3s-lab.conf backup-init
```

The credential commands send values to `systemd-creds` over standard input;
secret values do not enter unit files or process arguments. Use a dedicated S3
identity restricted to the selected bucket. AWS SSO remains the default for
interactive administrative work. Store the Restic password and recovery IAM
procedure separately offline: host-bound systemd credential blobs alone cannot
recover a failed RAID0 workstation.

## Future GPU

Do not change VFIO configuration until the exact card is installed. Record the
complete IOMMU group and pass every same-card function together. If the group
contains host storage, a host GPU, a host NIC, or another required controller,
move the card or abandon passthrough. Never use ACS override.

## AI service

Day-to-day tasks for the family assistant on the bare-metal K3s cluster (namespace
`ai-home-lab`). First-time setup (DNS, TLS, accounts and keys) is in
[AI service setup](AI-SERVICE.md); what the service does is in [the assistant guide](ASSISTANT.md).

### Check it is healthy

```sh
kubectl -n ai-home-lab get pods
sudo ./bin/workstationctl session status /var/lib/workstation-session
```

In shared mode every one of these should be `Running` and ready: `sglang`, `litellm` (the
gateway), `gateway-postgres`, `open-webui`, `service-tools`, `swarmui`, `comfyui-gpu0`,
and for memory `graphiti-memory`, `memory-embedding` and `neo4j`. The Grafana Workloads
dashboard shows serving speed, GPU memory and power ([monitoring](TELEMETRY.md)). The
gateway's own state (`phase`, `model`, `images`, `active` requests):

```sh
kubectl -n ai-home-lab exec deploy/litellm -c gateway -- python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:4000/_service/status').read().decode())"
```

### Common situations

| Situation | What to do |
| --- | --- |
| Switch between family (shared), coder (large), gaming and maintenance | `sudo workstationctl mode family` (or `coder`, `gaming`, `maintenance`); add `--execute` after checking the plan. See [service modes](MODES.md#switch-modes) |
| After a reboot, a GPU pod fails with `unresolvable CDI devices` | The DRA driver lost its per-claim device files. Scale the GPU consumers to zero and back: [post-reboot CDI fix](HOME-LAB.md#4-gpu-operator-and-allocation) |
| Pictures refused with `image_backend_needs_operator_check` | A cancelled or failed picture left a marker. Check both generators are idle, then remove it: [picture recovery](ASSISTANT.md#pinned-inputs-and-gates) |
| A video never arrives or chat says `video_busy` | Check `comfyui-gpu0`'s queue: [short videos](ASSISTANT.md#short-videos-shared-mode) |
| After a fresh render, the Think/Fast presets lack tools or vanish for the children | Re-apply the presets with **both** `--group Parents --group Family`: [presets](ASSISTANT.md#reasoning-choices-in-the-model-menu) |
| Someone new needs an account, pictures or a coding key | [AI service setup](AI-SERVICE.md) (`service provision`, `service browser-bind --images`) |

### GPU power cap

`workstation-gpu-power.service` holds both R9700s at 235 W with the `auto` performance level
at every boot, from [`infrastructure/gpu-power/policy.json`](../infrastructure/gpu-power/policy.json).
The policy records why: `auto` was fastest (manual and high levels pin the clock below its
boost), and 235 W costs about 3% per verify step against 300 W (29.4 against 28.6 ms) and
avoids the roughly 100 °C junction temperature measured at 300 W. Compare benchmarks only at the same cap.

```sh
systemctl status workstation-gpu-power.service
```

### Backups and restore

[Service recovery](SERVICE-RECOVERY.md) covers the quiesced capture of the cluster's
SQLite datastore and the service volumes, existing-PV Retain migration and isolated restore.
Model weights are restaged from their pinned catalogue entries, not backed up. The chat
model, draft and SDXL live on the NAS and are never captured. In the `creative-models`
volume the capture skips the weight trees `Models/` and `Video/` (Wan) and keeps SwarmUI's
`Data/` and `Output/`. The optional VM lab's recovery procedure
does not cover this cluster.
