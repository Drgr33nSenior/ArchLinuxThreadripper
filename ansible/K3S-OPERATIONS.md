# K3s lab operations

Run all lifecycle commands from the Arch host, as root, with an explicit copy
of `config/k3s-lab.conf.example`. The configuration and `versions.lock` must
not contain credentials.

## Provisioning

1. Create the tagged DMZ bridge on the host and replace every `REPLACE_*`
   value in a private configuration file.
2. Create the powered-off guest: `bin/k3s-lab --config CONFIG create`.
3. Start it explicitly: `bin/k3s-lab --config CONFIG up`.
4. From the libvirt console, record the guest Ed25519 SSH host-key fingerprint.
   Compare it with a host-side `ssh-keyscan` candidate, then put only the
   verified key in `SSH_KNOWN_HOSTS_FILE`.
5. Connect with `SSH_PRIVATE_KEY_FILE` and run `sudo passwd ADMIN_USER`.
   The one-time passwordless bootstrap grant exists only for this step.
6. Provision with `bin/k3s-lab --config CONFIG provision`. The generated
   inventory enforces the explicit identity and known-hosts file. Ansible asks
   for the sudo password, refreshes lock-derived variables, and removes the
   bootstrap grant after installing password-required sudo.
7. Confirm the K3s node is healthy before applying the source tree and
   pinned add-ons.

The VM uses no graphical display and exposes no network console. Use the
host-authenticated libvirt PTY with `virsh -c qemu:///system console LAB_NAME`;
exit with `Ctrl+]`. Confirm the pinned AlmaLinux cloud image provides its serial
login console during the first-boot test. If the console is unavailable, stop
qualification and inspect the image's serial/getty configuration before relying
on it for recovery. Guest login authentication is not disabled by this setting.

The VM uses K3s with embedded etcd to retain its lifecycle snapshot and restore
commands. The bare-metal AI profile uses SQLite instead. Neither profile needs
RKE2's `cis` switch, which K3s does not implement. The VM configures restricted
Pod Security defaults explicitly, keeps SELinux enforcing with the pinned
`k3s-selinux` policy, and retains the management/DMZ firewall. Namespace-specific
baseline and local-path exceptions remain in the reviewed Kubernetes manifests.

K3s uses Flannel VXLAN on the management interface and its embedded NetworkPolicy
controller. Bundled Traefik and ServiceLB provide ingress; the host firewall
continues to admit only TCP 443 through the DMZ. The bundled local-path component
is disabled because this VM installs the existing pinned provisioner separately.

These commands create a new K3s lab. Provisioning refuses existing RKE2 state.
Keep an existing RKE2 VM and its backups intact and migrate applications/PVCs
through a separate restore-tested process. Old RKE2 snapshots and configuration
are not inputs to these K3s lifecycle commands.

Cluster DNS is the Service CIDR's network address plus ten. Generated Ansible
variables carry that address; the role also validates an explicitly supplied
address against the Service CIDR and excludes the Kubernetes API service address.

Provisioning verifies the pinned RPM key before changing the firewall. It loads
and verifies the `k3s_lab_guard` nftables table before it stops firewalld. The
table replacement is atomic and does not flush other tables. The owned
`k3s-lab-firewall.service` persists that policy; K3s requires it before starting.
NetworkManager and sshd also require the firewall unit, so a boot-time policy
failure prevents interface configuration and SSH startup. Recover through the
authenticated local serial console, correct the policy, and start the firewall
successfully before starting networking. The firewall unit waits for local
filesystems, not for network services; this avoids a dependency cycle.
Stopping this unit leaves the filter in place but stops its dependent services;
use the serial console for that operation. Global nftables configuration and
its service are not changed or restarted. Keep console access
available for first provisioning, and test management access and DMZ denial in a
disposable guest before exposing the lab. The role does not restart the global
nftables service during later policy updates. For an existing installation,
review any old include of this table in `/etc/sysconfig/nftables.conf` manually;
the role does not remove user-managed includes.

## Backup and restore test

The guest creates compressed etcd snapshots every six hours and retains 14.
`bin/k3s-lab --config CONFIG down` first requests one additional tagged
snapshot through Ansible and then waits for a clean guest shutdown. `backup`
only accepts a shut-off guest. It validates the system disk's two-image qcow2
chain against `create.lock`, hashes the original backing image, and verifies
that the data disk has no backing file. Unexpected disks, external qcow2 data
files, host passthrough, and TPM state stop the backup.

The encrypted Restic snapshot includes both qcow2 disks, the original backing
image, current inactive domain XML, management network XML, UEFI code/template
files, the selected VM's NVRAM, creation configuration and lock, generated
Ansible inputs, and cloud-init source. An attached seed ISO is included; a
previously detached seed is not required. A new `backup-metadata.*` directory
records the input paths and tool versions for each attempt. The command streams
the original files to Restic; it does not flatten or copy large disks into RAM.
Keep the guest off throughout the backup and use `restic check` and a restore
drill to qualify the resulting snapshot. Neither check runs automatically here.

This validation requires `qemu-img`, `jq`, and `xmllint` (Arch `libxml2`) on the
host. Legacy guests without `create.conf` and `create.lock` fail closed. Establish
their original base-image hash and reviewed creation configuration manually;
do not substitute the current release lock for unknown creation provenance.

An upgrade accepts only the exact `K3S_VERSION` from `versions.lock`. It
requires a running VM, creates the tagged etcd snapshot, performs the clean
shutdown and cold Restic overlay backup, then restarts the VM before applying
the checksum-locked K3s binary. The separate SELinux policy uses an exact RPM
NEVRA, a locked SHA256 and package signature verification. A failure at any gate
stops the upgrade. The upstream installation script is never downloaded or run.
The SELinux RPM is fetched directly because its upstream repository does not
publish a `repomd.xml.asc` signature; no repository verification setting is relaxed.

Export the etcd snapshot, K3s server token, and necessary server configuration
to the protected backup location separately. A VM snapshot on the RAID0 host is
not a backup. Quarterly, restore an exported snapshot only into a separate
clone inventory through `k3s-lab --config CONFIG restore-test SNAPSHOT`. Before that
command, copy the snapshot to an absolute path on the clone, set
`K3S_RESTORE_TEST_INVENTORY` to the clone-only inventory and
`K3S_RESTORE_TEST_NAME` to the clone hostname. The command requires a name
different from the primary node, limits Ansible to it, verifies the clone
hostname, runs the bounded `k3s server --cluster-reset
--cluster-reset-restore-path=...` command while the service is stopped, then
starts K3s normally. It cannot target the primary lab.

For a host-loss drill, restore the complete Restic snapshot to a new staging
directory while the original VM pool is unavailable. Verify the base-image
SHA-256 against the saved `create.lock`. The saved XML and qcow2 backing link
contain original absolute paths: either reconstruct those paths on the detached
test host or explicitly relocate every XML disk, loader and NVRAM reference.
For a relocated overlay, an operator can update its backing link with
`qemu-img rebase -u -f qcow2 -F qcow2 -b VERIFIED_BASE RESTORED_SYSTEM_DISK`
only after verifying that base's exact checksum. Never point it at another
image, even if its filename matches. Check the restored backing chain again.

Before defining the restored guest, assign a different name/UUID and MAC
addresses, disable autostart, disconnect the DMZ, and use an isolated management
network. Restore the saved NVRAM and firmware files as well as the disks. Boot
and validate the clone without the original pool before declaring the backup
recoverable. An etcd-only restore does not establish whole-VM recoverability.

## Route53 credentials

Create the least-privilege Route53 DNS-01 credential interactively after the
cluster exists. Store it only in the approved secret mechanism. Do not add it
to cloud-init, an inventory, Ansible variables, Kubernetes YAML, shell history,
or this repository.

The normal lifecycle uses the generated, non-secret inventory below
`VM_POOL_DIR`. Set `K3S_ANSIBLE_INVENTORY` only for a reviewed alternate
inventory; restore tests require a separate clone-only inventory.

`create` renders a non-secret issuer candidate at
`VM_POOL_DIR/generated/route53-cluster-issuer.yaml`. Review it after installing
cert-manager. Create `route53-dns01-credentials` through an interactive secret
workflow in the `cert-manager` namespace, then apply the issuer with an explicit
kubeconfig and context. The generated file references secret keys; it never
contains their values.

After creating the host's encrypted Restic password and AWS credentials, run
`bin/k3s-lab --config CONFIG backup-init` once. It requires the exact
bucket/prefix confirmation, decrypts credentials only into a mode-0700 `/run`
directory, and initializes the configured `bucket/k3s/LAB_NAME` repository.
`backup` uses that exact target rather than an ambient repository variable.
