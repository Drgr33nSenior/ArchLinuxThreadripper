# Security and trust boundaries

## Protected values

Never commit or print:

- LUKS passphrases, recovery keys, or header backups.
- Secure Boot private keys.
- SSH private keys or YubiKey credential material.
- AWS access-key secrets, SSO caches, or Restic passwords.
- K3s tokens, kubeconfigs, service-account tokens, or Kubernetes Secret data.
- ACME account keys or Route53 DNS credentials.

Configuration examples contain only non-secret identifiers. Runtime credentials
must enter through an interactive prompt, protected file descriptor, systemd
credential, or manually created Kubernetes Secret. Commands must not put secret
values in arguments because arguments may appear in process listings and logs.

## Supply chain

- Pacman packages come from fully synchronized signed Arch repositories.
- AUR sources are pinned to reviewed Git commits, built in clean chroots, and
  placed in a signed local repository. Paru never receives blanket confirmation.
- Cloud images require both an expected SHA-256 and a valid upstream detached
  signature.
- Standalone and repository-overridden OCI images require a digest. Upstream
  Kubernetes release-manifest bytes are checksum-locked at an exact release;
  record the resolved controller image IDs at promotion and reject drift.
- Kubernetes release manifests require a pinned version and checksum.
- JetBrains Toolbox uses one pinned vendor archive and checksum. Toolbox then
  remains the only updater for installed JetBrains IDEs.

## Secure Boot and encryption

Secure Boot enrolment is never automatic. The operator exports existing
firmware databases, retains required vendor certificates, and verifies each UKI
before promotion. Private keys stay on the encrypted root filesystem or
offline, never on an ESP.

The human Argon2id slot remains available after FIDO2 enrolment. FIDO2 and the
recovery key are additional paths, not replacements. Back up the LUKS header
after every token or keyslot change.

Discard is enabled by explicit policy. This leaks which encrypted blocks are
unused but does not reveal their plaintext.

## Network boundaries

The headless host does not enable a graphical login or automatically expose SSH.
When host SSH is selected, root login is prohibited. A separately prompted root
password exists for authenticated rescue/emergency-console access; disk unlock
and console login are distinct controls. Test both before removing recovery media.

The following network boundaries describe the retained KVM/EL9 lab and legacy
loopback LLM. The additive bare-metal workload profile has its own disabled
deployment and admission gates in [HOME-LAB.md](HOME-LAB.md).

- The local LLM API listens only on loopback.
- Libvirt accepts local Unix-socket clients only.
- The K3s API and SSH listen on the private management path.
- The DMZ admits public TCP 443 and required ICMPv6 only.
- etcd, kubelet, Flannel VXLAN, K3s management endpoints, and NodePorts are never public.
- Rancher Manager, if added, remains private.

The VM's explicit Pod Security defaults keep application namespaces restricted.
The only repository
PSA enforcement exception is the dedicated `local-path-storage` system
namespace, whose helper Pods require node-local hostPath access; its audit and
warn levels remain restricted.

The Route53 DNS-01 key is limited to the delegated challenge zone. The Restic
key is limited to one backup bucket. Administrative AWS work uses SSO. These
three identities are never shared.

Host Restic credentials are encrypted with `systemd-creds` and exposed only in
the oneshot service credential directory. The timers are installed disabled,
and enablement first runs `restic check`. Encrypted credential files and AWS
SSO token caches are excluded from the ordinary host backup policy.

The K3s guest uses a one-time, named passwordless sudo file only to let the
operator set the administrator's local password. Provisioning refuses to remove
that grant until a password exists, then replaces it with password-required
sudo. Ansible always uses the explicit private-key path and dedicated
known-hosts file from the non-secret lab configuration.

## Destructive boundaries

`bootstrap-arch --execute install` is the only command intended to erase disks. It must
run from an Arch live ISO, accept two immutable by-id paths, validate recorded
serials, reject active devices, and require confirmation of resolved paths and serials.
Without `--execute`, installation is a dry run. No storage, UKI, firmware or
LUKS-header operation is performed during repository tests.

The workstation has no swap, zram or hibernation. Argon2id calibration uses an
in-memory benchmark; it does not modify a keyslot. The actual installation
records the selected KDF costs without recording salts or passphrases.

K3s lifecycle commands address one configured development VM. They must never
derive a production target from a name or use broad libvirt cleanup commands.
Restore tests create detached clones and do not overwrite the active VM.
