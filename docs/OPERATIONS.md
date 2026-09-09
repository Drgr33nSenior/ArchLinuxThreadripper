# Operations runbook

## Host updates

For the first transition off installation media, complete the
[snapshot-to-rolling procedure](#move-from-the-installation-snapshot-to-rolling-arch).
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

Build `linux-git` in a clean chroot from the reviewed AUR commit and record the
exact upstream kernel commit, config hash, compiler, and package hashes. Promote
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

## Local LLM

For the current two-R9700 profile, use the `rocm validate` and `rocm inference`
procedures in [ROCM.md](ROCM.md). The following commands retain the legacy Intel
service and do not serve AMD GPUs.

Validate the B70 and discover its exact PCI address before startup:

```sh
./bin/workstationctl gpu validate 0000:BB:DD.F
./bin/workstationctl llm up 0000:BB:DD.F /srv/ai/vllm/models/selected
./bin/workstationctl llm status
./bin/workstationctl llm down
```

The service remains on demand and loopback-only. Promote a new image, model,
precision, or kernel only after deterministic smoke tests, three benchmark
runs, and an eight-hour soak with no GPU reset, device loss, numerical failure,
memory leak, or desktop regression.

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

Do not run Kubernetes or Helm validation against an unclassified remote
cluster. Repository tests render source locally only.

## Backups

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
