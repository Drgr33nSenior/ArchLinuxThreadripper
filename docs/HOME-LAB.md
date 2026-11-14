# Bare-metal AI home lab

This is an additive profile for the existing Arch repository. It does not replace
the EL9/KVM lab, install a second operating system, or run a cluster during tests.
The target is the Threadripper 9960X with 64 GB installed system RAM,
two Samsung 9100 PRO NVMes, and two R9700s. Actual DIMM population, trained
speed and Linux MemTotal must be recollected; capacity alone does not establish them. `gfx1201` is an expected input that must match `rocminfo`.

The host profile defaults to headless. Neither AI nor remote gaming requires a
host-wide GNOME/GDM session. The gaming workload still needs its own qualified
display/encoding stack; removing the host desktop does not bypass the image,
GPU-allocation or admission gates below.

```text
infrastructure/
├── host/
│   ├── install.conf.example
│   ├── tpm2-enroll.sh
│   ├── check-network.sh
│   └── dkms-signing.conf.example
├── packages/k3s/PKGBUILD
├── ansible/
│   ├── site.yml
│   ├── inventory.example.ini
│   ├── group_vars/ai_lab_baremetal.yml
│   └── roles/{arch_host,k3s_baremetal}/
└── gpu-operator/{chart.lock,values.yaml,deviceconfig.yaml,render.sh}
apps/
├── base/{sglang,swarmui,comfyui-gpu0,open-webui,shared-api,steam-headless}/
├── components/{assistant,memory,rag-reranker}/
└── overlays/{assistant*,service*,single-gpu,dual-gpu,rdna4-compat,parent,kids,family,rag,rag-dense}/
```

The family AI service is the `assistant-memory` composition, rendered from the serving
contract and running in shared mode: Open WebUI, the gateway (LiteLLM and PostgreSQL),
private Traefik HTTPS, service-tools (MCP), memory, and SwarmUI plus `comfyui-gpu0` for
pictures and video. See [AI service setup](AI-SERVICE.md) and [service modes](MODES.md).

## Deployment status and boundaries

Host configuration, K3s, the GPU operator with DRA, and the AI service are installed and
running. The AI service is deployed from a render of the qualified serving profile (see
[service modes](MODES.md)); rendered manifests leave replicas at zero and the session
tooling starts the workloads. Gaming has a pinned-input build recipe but still needs its
built image promoted and display, input and security qualification.

The source-default `apps/` SGLang Deployment (FP8 model, `llm-models` volume) is legacy
and has not been applied since the November 2026 reinstall. A serving-contract render
replaces it with the image and profile from the
[AI serving stack pipeline](../infrastructure/ai-stack/README.md). See
[NAS-backed volumes](#nas-backed-volumes-nfs-csi) for its model storage.

K3s documents general Linux prerequisites. This Arch/ROCm/GPU Operator
combination still requires target-hardware qualification.
Single-node K3s, one motherboard and RAID0 have no high-availability guarantee.
Snapshots on the same array do not protect against an NVMe failure.

All application resources currently use `ai-home-lab`; infrastructure controllers
use their own namespaces. This is a trusted-family boundary, not hostile-tenant
isolation. Family members receive application accounts, not permission to create
pods, change priorities, read Secrets or attach arbitrary PVCs. NetworkPolicy
does not make a shared kernel or GPU equivalent to separate virtual machines.

Gaming is installed from the service render's `gaming.yaml` (see
[Sunshine](SUNSHINE.md#deploy-on-the-workstation)) and stays disabled until qualified. The Wayland candidate runs as UID/GID 1000
with RuntimeDefault seccomp and all Linux capabilities dropped, so it no longer
requires the former `SYS_ADMIN` or `SYS_NICE` exception. That source change is
not qualification: verify GPU device-node access, capture, input, audio, private
streaming ports and Steam egress before promotion. Do not relax the namespace
policy or add host access merely to start a game. The `family` overlay remains a
reviewable, disabled composition, not a runnable game-streaming release.

## 1. Storage and direct UEFI boot

Use the canonical installer, not an Ansible filesystem module against live root:

```sh
cp infrastructure/host/install.conf.example config/home-lab.conf
# Edit both persistent NVMe paths and serial numbers after inspecting lsblk.
sudo ./bin/bootstrap-arch --config config/home-lab.conf preflight
sudo ./bin/bootstrap-arch --config config/home-lab.conf install
```

`install` above is a dry run. Only an authorised operator may add `--execute`
after inspecting the resolved devices and the full erase confirmation. No script
reboots or updates firmware automatically. Do not rerun installation over a
partial or working installation.

The two disks each retain a 2 GiB FAT ESP. Their remaining partitions form
`/dev/md0`, metadata 1.2, RAID0, with a 512 KiB chunk. LUKS2 wraps that array;
`/dev/mapper/crypt_root` is XFS with `su=512k,sw=2`. UEFI cannot read mdadm RAID0,
LUKS or XFS directly. It starts a signed UKI on an independent ESP, and the UKI's
initramfs assembles and unlocks root. The second ESP is boot-copy redundancy,
not RAID0 data redundancy.

The profile requests Argon2id with `--pbkdf-memory 4194304`, four lanes and a
5-second calibration budget. Cryptsetup treats memory as a maximum and can use
less. Run `./bin/workstationctl encryption calibrate config/home-lab.conf artifacts/argon2-home-01`
on the target and inspect actual memory/time/parallelism before erasing anything. If 4 GiB is a strict minimum,
do not accept a calibration below it; review the unlock budget first. The actual
keyslot costs are recorded after formatting. Argon2id affects password unlocking,
not bulk dm-crypt throughput. TPM enrollment uses its own high-entropy keyslot;
the PIN is protected by TPM policy and dictionary-attack controls, not Argon2id.

The existing mkinitcpio/systemd-stub UKI path is retained. `sd-encrypt` includes
TPM2 and FIDO2 integration; `tpm2` and `fido2` are not extra mkinitcpio hooks to
append blindly. The existing UKIs include microcode and the requested kernel
command line. Validate the actual IOMMU groups: upstream does not document `on`
as an `amd_iommu=` value, and `iommu=pt` does not provide DMA isolation for every
host device. Do not use ACS overrides.

The home-lab example selects `TUNED_PROFILE=accelerator-performance` separately
from its headless package policy. Compare it with `workstationctl profile server`
(`balanced`) after installation; the AI profile can increase idle power and heat.
See [AI-PERFORMANCE.md](AI-PERFORMANCE.md) for native-kernel/HIP builds, the
dated-mirror transition and measured qualification. These are post-install
actions; the live ISO kernel remains generic.

Keep swap, zram and resume disabled. Follow [INSTALLATION.md](INSTALLATION.md)
for offline LUKS header backup. Copy a fresh header after every keyslot change to
separately encrypted, disconnected media; never commit it. Test the recovery
passphrase without deleting any slot. An old header can retain old keyslot access.

<a id="2-secure-boot-tpm2-and-pin"></a>

## 2. Secure Boot and hardware-key unlock

Direct UEFI boot uses firmware PK/KEK/db trust, not shim's MOK enrollment.
The existing installer creates sbctl keys and registers/signs the UKIs. Firmware
enrollment remains manual after exporting the original firmware keys and checking
GPU option-ROM requirements. Do not blindly replace firmware trust stores.
Use [INSTALLATION sections 5 and 6](INSTALLATION.md#5-enrol-secure-boot-keys)
for the owner-run sequence, backups, exact commands and expected states. Those
steps run on the installed workstation, not the controller or live ISO.

The sbctl package's transaction hook signs registered files. The existing
`zzz-bootstrap-uki-sync.hook` verifies signatures before synchronizing ESP copies.
Check that both hooks run in order and test stable/LTS signatures before any
manual reboot. New installations use the pacman-owned runtime helper described in
[ISO.md](ISO.md). Existing `/usr/local` helpers require an explicit migration;
they are not removed or overwritten automatically.

With upstream `amdgpu`, do not introduce an out-of-tree driver to satisfy a
signing requirement. If a reviewed DKMS package becomes necessary, first prove
the selected kernel trusts the module certificate and enforces module signatures.
UEFI db trust alone is not proof of module trust. Set `lab_enable_dkms_signing`
and `lab_module_trust_verified` only after that test. The optional Ansible block
checks protected local key metadata and configures DKMS's native signing hook;
it never transfers the private key or rebuilds modules. DKMS signs before module
compression. Verify signing failures stop package promotion and check each
kernel's modules before rebooting. Keep the upstream/LTS recovery path.

The home-lab install example selects `BOOT_UNLOCK=fido2`. Follow
[the owner-local YubiKey procedure](INSTALLATION.md#6-enrol-a-yubikey-for-touch-only-unlock)
for touch-only enrollment, protected header backups and key-absent recovery tests.
No key reset or automatic enrollment is part of installation. The remaining
TPM guidance applies only when explicitly selecting `BOOT_UNLOCK=tpm2-pin`.

For that optional static PCR policy, bind PCRs **0+7+11** and require a PIN.
PCR 0 changes on firmware updates; PCR 7 changes with Secure Boot state/policy;
PCR 11 changes with UKI content and boot phases. Enrollment after login using
literal `--tpm2-pcrs=0+7+11` normally captures the wrong PCR 11 phase for early
root unlock. The helper therefore requires the predicted early-unlock SHA256
value for PCR 11 while sampling current PCRs 0 and 7:

The [enrolment procedure](INSTALLATION.md#6-enrol-tpm2-with-a-pin) distinguishes
argument-only preview from the checked execution handoff. Its terminal
confirmation attests owner-verified hardware identity, recovery and prediction;
the helper cannot prove them. It requires a local terminal, enabled Secure Boot,
an existing keyslot and no TPM token, and does not wipe slots. A keyslot count
does not prove that the owner has a usable recovery credential.

Confirm firmware selects the physical SPI module, not AMD fTPM, before using
`--tpm2-device=auto`. A motherboard connector description cannot prove which TPM
Linux selected. No PIN or passphrase is accepted in configuration or arguments.

Static PCR 11 binding is deliberately update-sensitive. A correctly signed new
UKI can still fail TPM unlock. Retain the recovery passphrase, back up headers,
and plan reenrollment after firmware/UKI changes. Signed PCR policies are a more
maintainable alternative, but are not silently substituted for the requested
static policy. The helper refuses existing TPM tokens rather than wiping slots.
Passphrase and recovery-key fallback remain available. FIDO2 tokens remain
independent of this optional TPM path. PIN requirements apply to the TPM path,
not to every emergency recovery method.

### Predict the early-unlock PCR 11

This is a **target-specific qualification step**, not an automatic enrollment
feature. Start with [INSTALLATION 6A.3's read-only inspection and expected results](INSTALLATION.md#63-obtain-the-correct-early-unlock-pcr-11-prediction)
after Secure Boot and passphrase recovery tests. This reference covers the
subsequent image/phase review and calculation. Keep the image hash and tool
versions from that inspection. Use the same exact signed UKI selected for the
next cold boot; the stable example is:

```sh
UKI=/efi/EFI/Linux/arch-linux.efi
ukify inspect --all "$UKI"
sudo lsinitcpio -l "$UKI"
```

1. Inspect the embedded initramfs's units, enablement links, conditions and
   ordering. Determine which PCR 11 phase extensions run **before the root
   cryptsetup service unlocks**. A service installed on the root filesystem is
   not proof it was included or activated in that initramfs. In the reviewed
   upstream unit, `systemd-pcrphase-initrd.service` orders before cryptsetup and
   extends `enter-initrd`, but the actual image must include and activate it.
2. Review the TPM firmware event log and the boot's PCR-phase journal locally.
   The firmware log alone need not contain userspace phase extensions. Match the
   measured UKI sections and phase sequence; do not use the post-login PCR 11
   value or `systemd-measure --current` as an early-unlock prediction. If the
   unlock phase cannot be established, stop and keep passphrase-only unlock.
3. Extract the **embedded bytes**, not `/etc/os-release`, a current command line
   or a separately rebuilt initrd. Use a private temporary directory and the
   installed `ukify inspect` extraction syntax. No signing or header mutation
   is involved. The following example applies only if inspection confirms a
   single-profile image whose measured inputs are exactly `.linux`, `.osrel`,
   `.cmdline`, `.initrd`, `.uname` and `.sbat`:

```sh
umask 077
UKI_PARTS=$(mktemp -d)
ukify inspect "$UKI" \
  --section=".linux:binary@$UKI_PARTS/linux" \
  --section=".osrel:binary@$UKI_PARTS/osrel" \
  --section=".cmdline:binary@$UKI_PARTS/cmdline" \
  --section=".initrd:binary@$UKI_PARTS/initrd" \
  --section=".uname:binary@$UKI_PARTS/uname" \
  --section=".sbat:binary@$UKI_PARTS/sbat"
```

Check that every expected extraction succeeded and matches the inspected section
size/digest. Missing files or an extractor returning 0 are not sufficient
evidence. If additional measured sections such as `.ucode`, `.splash`, `.dtb`,
`.pcrpkey`, `.profile`, `.dtbauto` or `.hwids` are present, include their exact
bytes and corresponding `systemd-measure` options after review. Multi-profile
images, duplicate sections or unusual PE virtual/raw sizes require a separate
review; do not apply this six-input example unchanged.

4. Set `UNLOCK_PHASE` to the verified phase path; there is intentionally no
   default here. With systemd-measure matching the embedded stub's measurement
   semantics, calculate for **SHA256** and that one early phase:

```sh
(
  set -e
  : "${UNLOCK_PHASE:?Set the verified early-unlock phase path first}"
  for component in linux osrel cmdline initrd uname sbat; do
    test -s "$UKI_PARTS/$component"
  done
  /usr/lib/systemd/systemd-measure calculate --bank=sha256 --phase="$UNLOCK_PHASE" \
    --linux="$UKI_PARTS/linux" --osrel="$UKI_PARTS/osrel" \
    --cmdline="$UKI_PARTS/cmdline" --initrd="$UKI_PARTS/initrd" \
    --uname="$UKI_PARTS/uname" --sbat="$UKI_PARTS/sbat"
)
```

5. Require a successful calculation with the intended phase and one SHA256
   PCR 11 digest. Check the prediction against the measured image/phase evidence
   in a recovery-capable boot rehearsal. Retain the UKI hash, tool versions,
   measured section inventory, phase and predicted digest privately. Only then
   use that nonsecret digest as `EARLY_PCR11_SHA256` in
   [the enrollment preview](INSTALLATION.md#tpm2-enrol-preview). It must be 64
   hexadecimal characters; syntax acceptance alone does not validate it.

These example interfaces were checked against upstream
[systemd-measure](https://github.com/systemd/systemd/blob/v261.2/man/systemd-measure.xml),
[ukify](https://github.com/systemd/systemd/blob/v261.2/man/ukify.xml),
[systemd-stub](https://github.com/systemd/systemd/blob/v261.2/man/systemd-stub.xml)
and the [initrd phase unit](https://github.com/systemd/systemd/blob/v261.2/units/systemd-pcrphase-initrd.service.in).
Use the installed manuals and re-review if your tool/stub versions differ.
The [cryptenroll reference](https://github.com/systemd/systemd/blob/v261.2/man/systemd-cryptenroll.xml)
explains explicit PCR digests, PINs and update-sensitive static binding. This
procedure does not replace the existing static policy with a signed PCR policy.

## 3. Package and configure bare-metal K3s

K3s is the default for home-lab installations because its single-server SQLite
datastore and bundled Flannel, NetworkPolicy
controller, CoreDNS, Traefik and local-path storage fit this one-machine lab.
RKE2's compliance-oriented defaults are not a requirement here. K3s still runs
ordinary Kubernetes workloads; it does not remove the need for access controls,
tested backups or GPU qualification. See the [architecture](https://docs.k3s.io/architecture),
[networking services](https://docs.k3s.io/networking/networking-services) and
[RKE2 comparison](https://docs.rke2.io/#how-is-this-different-from-rke-or-k3s).

`infrastructure/packages/k3s/PKGBUILD` reads `K3S_VERSION` and
`K3S_BINARY_SHA256` from `versions.lock`. The pin is `v1.37.2+k3s1`.
Verify its binary hash against the upstream checksum file and release-asset
metadata. The package owns
`/usr/bin/k3s` and `k3s.service`, adapted from that release's service definition.
It omits uninstall/killall helpers and does not start services. Review, build
and sign it with makepkg as a non-root user, then install the signed package.

It includes containerd v2.3.7-k3s1 and Traefik chart 41.6.1 (K3s build 41.6.101).
Chart 41's schema rejects chart 40's top-level `logs` key (access logs moved to
`accessLog`); the service's Traefik values set neither and rely on the default,
access logs off, so the same values render identically on both charts. Existing
ingress-nginx migration settings must use `kubernetesIngressNGINX` instead of
`kubernetesIngressNginx`. Review the [release notes](https://github.com/k3s-io/k3s/releases/tag/v1.37.2%2Bk3s1)
and the Kubernetes 1.37 urgent upgrade notes before upgrading.

**Kubernetes 1.37 and the GPU Operator.** AMD documents GPU Operator v1.5.1, and
upstream `main` as of 2026-11-14, for Kubernetes 1.29–1.36 only. The owner chose
1.37 anyway (2026-11-14): the chart allows `>=1.29`, the operator parts in use
(controller, node labeller, metrics exporter; the device plugin is off) use
stable APIs, and the DRA driver is the git build against the 1.37 libraries. Its
health service needs a 1.37 kubelet, which stops watching a driver that answers
`Unimplemented`; a 1.36 kubelet retries every 5 s and logs an error each time.
Recheck this when AMD publishes a release that lists 1.37.

Kubernetes does not support minor-version downgrades: after an upgrade, the
rollback is restoring the pre-upgrade server state (stopped K3s, the SQLite
datastore and its matching token) with the previous package, not reinstalling an
older package over the upgraded datastore. Complete the existing backup and
restore procedure before a running-cluster upgrade.

Use a fresh K3s data directory. The role refuses existing `/var/lib/rancher/rke2`
state. An existing RKE2 installation needs a separate workload/PVC migration and
restore plan; changing a binary or reusing its datastore is not an upgrade.

Supply the non-secret variables in `infrastructure/ansible/group_vars/` through
a local override file and copy `inventory.example.ini` to `inventory.local.ini`.
No host is present in the example inventory. Use one explicitly mapped `dev`,
`tst` or `int` host. Configure private LAN, pod/service CIDRs, DNS, interface and
TLS SANs; the read-only shared network validator rejects overlap and public node
addresses. Supply a fresh hardware report from this same host and boot.

The play checks Arch prerequisites and both GPUs before generating:

- `/etc/rancher/k3s/config.yaml`: systemd cgroups, no swap, encrypted Secrets,
  no DRA feature gate (locked on since 1.35, removed in 1.38), explicit network
  binding and the default SQLite datastore.
- `config-v3.toml.tmpl`: extends K3s's base rather than copying generated output.
- `config-v3.toml.d/10-cdi.toml`: CDI directories and systemd runc settings, without
  duplicate TOML tables or globally privileged containers.

The pinned K3s uses containerd 2.x. Its v3 filename is preferred over the legacy
`config.toml.tmpl`. Device files for non-root pods use K3s's scoped
`nonroot-devices` setting. Protect `/etc/cdi` and `/var/run/cdi` from tenant writes.

K3s keeps a local copy of every image it pulls and, by default, removes none until the
disk is 85% full. The role's `80-workstation-images.conf` kubelet drop-in removes images
no container has used for `lab_image_maximum_gc_age` (default 336h, 14 days; `0s`
disables it). Builds stay in the registry, so an older one is pulled again when needed.
An image that exists only locally, such as an imported `localhost/` build, is removed
too once unused that long; push it to the registry, or rebuild it, before scaling its
Deployment to zero for longer.

Flannel VXLAN uses the selected LAN interface. K3s's NetworkPolicy controller
stays enabled. ServiceLB is disabled to avoid automatically binding host ports.
The shared AI service exposes Traefik through the reviewed
`infrastructure/service/traefik.yaml`. This is a ClusterIP Service whose only
externalIP is the node address, on TCP 443. Until it is applied, the bundled
LoadBalancer Service stays pending. Application services remain ClusterIP. The bundled local-path
provisioner uses `/var/lib/rancher/k3s/storage` on encrypted root; local volumes
are not replicated and their default reclaim policy is Delete. Keep off-array
backups and review PVC retention before deleting a claim. The separate VM lab
uses its existing pinned Retain provisioner and embedded-etcd recovery workflow.

`lab_manage_service` and `lab_restart_approved` default to false. Review/load the
generated sysctls and modules before enabling service management. Also review
NetworkManager's CNI handling.

The role installs the host guard `/etc/nftables.d/k3s-host-guard.nft` and its
`k3s-host-guard.service`, which K3s requires. The guard owns only the
`inet k3s_host_guard` table and never flushes the ruleset. The role validates
the file with `nft -c` before installing it. It loads the file in one
transaction only when `lab_manage_service` and `lab_network_reviewed` are both
true. Rules:

- TCP 443 is accepted only on `lab_network_interface`, to `lab_node_ip`, from
  `lab_https_client_cidrs`. These are the service site's `client_cidrs`. TCP
  443 to every other local address or interface is dropped. This check runs at
  prerouting priority -150, before kube-proxy's DNAT to Traefik.
- The Kubernetes API (6443) is accepted only from `lab_api_client_cidrs`
  (default: `lab_management_cidr`) arriving on `lab_api_interfaces` (default:
  `lab_network_interface`), or from the Pod/Service CIDRs. Other 6443 traffic is
  dropped. List only internal interfaces; a DMZ NIC is never listed.
- The kubelet (10250) and VXLAN (8472) rules are unchanged: Pods only, and no
  peers.

SSH and other host services are left to the existing host policy. No rule
accepts traffic on another NIC. The second NIC (`eno1` on gr33nstein) is
reserved for a future DMZ. It has no address and no bound service. Before it is
configured, it needs its own reviewed default-drop policy. Public exposure is
out of scope for the current service.
This play does not flush the host firewall or disable its security services.
No CIS certification is claimed for this Arch configuration.

```sh
ansible-playbook -i infrastructure/ansible/inventory.local.ini \
  --limit CHOSEN_LAB_ALIAS --ask-become-pass \
  -e @infrastructure/ansible/group_vars/site.local.yml \
  infrastructure/ansible/site.yml
```

The command is an operator-run remote mutation, not part of `make check`.
For a consistent SQLite backup, quiesce applications separately, stop `k3s`, and
back up `/var/lib/rancher/k3s/server/db/`, its sibling `token` file, configuration
and PVC data through an encrypted off-array backup process. Stopping K3s alone
does not stop its workload containers. Keep the token with the corresponding
database: it is required to decrypt the restored cluster state. Do not print it
or copy kubeconfigs into this repository. Restart the service after the backup.
`k3s etcd-snapshot` does not back up SQLite. The existing host Restic include list
does not include these database or PVC paths; configure their backup explicitly.
See [K3s backup and restore](https://docs.k3s.io/datastore/backup-restore).

Retain the previous runtime package and configuration with each backup. Rehearse
restoration into an isolated fresh target with the same version and token before
upgrading. Kubernetes downgrades are not equivalent to reverting a package.

## 4. GPU Operator and allocation

`chart.lock` pins AMD GPU Operator v1.5.1 and its chart SHA256. Download that exact
URL to a local cache, then render without a cluster or Helm repository mutation:

```sh
bash infrastructure/gpu-operator/render.sh /path/to/downloaded-chart.tgz
```

The renderer deliberately excludes Helm lifecycle hooks. The upstream deletion
hooks remove DeviceConfigs across namespaces and delete CRDs. Never apply raw
hook Jobs as ordinary Kubernetes manifests. No install, uninstall or upgrade is
automated here; review the pinned chart's lifecycle hooks before using Helm for
release management. Automatic CRD upgrade and NFD deletion cleanup are disabled.

The values disable KMM installation **and watching**, default DeviceConfig
creation and automatic remediation, and render the DRA `gpu.amd.com`
DeviceClass. Apply the separate DeviceConfig only after its CRD exists. It
disables driver management, blacklisting, driver upgrades and partition
management. It allocates GPUs through the DRA driver instead of the traditional
device plugin, and keeps the node labeller. The driver image is built from a pinned
upstream `main` commit by the AI-stack [`dra` stage](../infrastructure/ai-stack/README.md#stages)
and pinned by digest; upstream release v1.0.1 is the documented rollback in the
DeviceConfig. The DRA pilot is in
[gpu-operator/dra-pilot](../infrastructure/gpu-operator/dra-pilot/README.md). NFD is enabled. The operator/device-plugin images are
digest-pinned. [The values](../infrastructure/gpu-operator/values.yaml) also pin
the bundled NFD version to its Linux/amd64 manifest. This bare-metal profile is
not a multi-architecture deployment. Verify every rendered image before release
promotion; registry hashes establish identity, not runtime qualification.

Disabling operator features does not remove their upstream RBAC permissions.
The manager retains cluster-wide Secret read/write, node and workload mutation,
and CRD deletion permissions. Review and obtain owner approval for these grants
before installation. Keep NFD's read-only host mounts in its infrastructure
namespace; do not relax application namespace controls to admit them.

With the device plugin, the resource name is `amd.com/gpu`, not `://amd.com`.
Workloads that still request it stay Pending once DRA allocates the GPUs; they
must reference ResourceClaims instead. AMD prohibits running its DRA driver and
device plugin simultaneously. CDI is runtime support,
not GPU partitioning or a scheduling resource by itself.

After a reboot, a pod on the standalone claims can fail with `CDI device injection
failed: unresolvable CDI devices k8s.gpu.amd.com/gpu=<claim-uid>-gpu-N-NNN`. The
driver writes one CDI spec per prepared claim into `/var/run/cdi`, a tmpfs, but its
prepared-claim checkpoint survives the reboot, so it reports the claims as prepared
without rewriting the specs (seen 2026-11-11 with v1.0.1; the source of the pinned
`main` commit behaves the same). Release and re-prepare them:
scale the consumers to zero, check that both claims show no consumer, then scale
back up:

```sh
kubectl -n ai-home-lab scale deploy/sglang --replicas=0
kubectl -n ai-home-lab wait --for=delete pod -l app.kubernetes.io/name=sglang --timeout=180s
kubectl -n ai-home-lab get resourceclaims gpu-0 gpu-1 \
  -o 'custom-columns=NAME:.metadata.name,USERS:.status.reservedFor[*].name'
kubectl -n ai-home-lab scale deploy/sglang --replicas=1
```

Device names (`gpu-0-129`) and `/dev/dri/cardN` numbers can also change across a
reboot (the cards swapped on 2026-11-11); claims that select by `pciBusID` keep
the right physical card.

NFD discovers the AMD PCI vendor; it does not automatically supply a verified
`gfx1201` label. The host profile adds `ai-home-lab.local/gfx` only after the
ROCm hardware report matches. Verify the actual labels, and that the DRA driver
publishes exactly two GPUs (`kubectl get resourceslices`), before enabling any
workload. Operator v1.5 does not imply compatibility with
every ROCm 10 build; qualify the kernel/userspace/image combination separately.

Do not use `HIP_VISIBLE_DEVICES=1` to reserve the second physical card. Physical
cards are owned through DRA: the operator-applied standalone ResourceClaims
`gpu-0` (`0000:23:00.0`) and `gpu-1` (`0000:c3:00.0`) in
[claims.yaml](../infrastructure/gpu-operator/claims.yaml) select cards by PCI
address. Workloads reference them by name: SGLang TP2 uses both, the single-GPU
SGLang profile `gpu-0`, and SwarmUI and the gaming sessions `gpu-1`. Every pod
that references a claim gets the same card, so references *share* it; DRA does
not isolate VRAM or compute, and the session tooling still stops one workload
before starting a conflicting one. A container sees only its claimed card, as
ordinal zero. Kustomize replaces `resourceClaims` lists rather than merging
them, so each overlay states its full list. Bench Jobs get their own two-GPU
claim from a ResourceClaimTemplate; DRA never allocates a device to two claims,
so a bench waits while the serving Deployment or SwarmUI hold the cards.

Sharing a card with SGLang TP2 needs a plan for both cards. SGLang gives every rank
the same KV pool, sized from the smallest free memory across ranks, so the room left
on `gpu-1` for SDXL (about 6.6 GiB with the seed's fp8 flags) also stays empty on
`gpu-0`. That matching headroom fits a second ComfyUI, so two images can render at
once: run it as its own pod on the shared `gpu-0` claim, not with `--cuda-device`,
because a pod only sees its claimed card, as ordinal zero. Nothing enforces the
split, and SGLang keeps growing after startup outside its KV pool (activations and
allocator cache: about 0.6–0.8 GiB per card, from 1.81 GiB free after graph capture
to 1.0–1.2 GiB at the peak of two concurrent 83k- and 22k-token prompts). The
ComfyUI seed therefore starts with `--reserve-vram 1.5`, so ComfyUI never counts
that space as free.

## 5. Workload profiles and tenant data

Render an overlay with `kubectl kustomize apps/overlays/NAME`. The site ConfigMap
in `single-gpu` supplies the StorageClass, private DNS host and TLS Secret name.
Replace the `.invalid` host, preload a matching TLS Secret and create the WebUI
session Secret through an approved secret store. No secret values are generated.
The bare-metal `local-path` StorageClass can be reused, but its default `Delete`
policy removes volume data when a claim is deleted. Review retention first; the
separate VM profile's `Retain` policy does not apply here. Neither policy provides
backups, and local-path volume size requests do not enforce disk quotas.

### NAS-backed volumes (nfs-csi)

[`infrastructure/nfs-csi/`](../infrastructure/nfs-csi) adds the NFS CSI driver
and the `nas-nfs` StorageClass for ReadWriteMany volumes on the NAS. The
AI-stack model store (`ai-models`) uses it. `local-path` stays the default
class for databases, indexes and other latency-sensitive state.

`chart.lock` pins csi-driver-nfs v4.13.6 and its chart SHA256. Download that
exact URL, then render and apply:

```sh
bash infrastructure/nfs-csi/render.sh /path/to/csi-driver-nfs-4.13.6.tgz > nfs-csi.yaml
kubectl --context <reviewed-context> apply -f nfs-csi.yaml
kubectl --context <reviewed-context> apply -f infrastructure/nfs-csi/storageclass.yaml
```

The renderer verifies the chart digest, refuses a K3s minor other than 1.36,
renders into `kube-system` without Helm hooks and does not create the
StorageClass. All five images are pinned by digest. Snapshots are disabled: no
snapshot controller, sidecar or CRDs. `bash tests/test_nfs_csi.sh` checks the
pins offline; set `HOME_LAB_NFS_CHART` to the archive to check the render too.

The StorageClass provisions each claim as `<namespace>/<claim>` under
`192.168.30.10:/srv/microserver/k8s`. It uses `reclaimPolicy: Retain` and
`onDelete: retain`, so deleting a claim or volume leaves the directory and its
data on the NAS. Remove released volumes and directories by hand after review.
Mounts are NFS 4.2 over TCP with `hard`: I/O waits while the NAS is
unreachable instead of failing.

For rollback, first delete or move every workload and claim that uses
`nas-nfs`, then delete the StorageClass and the rendered driver objects. The
NAS data stays in place. Do not remove the driver while volumes are mounted;
their Pods could not then unmount them.

Security: NAS volumes are not on the workstation's encrypted NVMe root, and
the mount options set no Kerberos or TLS security, so NFS traffic crosses the
LAN unencrypted. Export access is controlled on the NAS, not here. Place only
data that may cross the LAN in clear text, such as public model weights, on
`nas-nfs`.

### Sandbox homes (per-user NAS volumes)

Each person's coding workspace keeps its files in a ZFS dataset of their own on
the NAS, with a size limit and scheduled snapshots. The NAS repository's
`ansible/owner-sandbox-homes.yml` creates and exports the datasets (see its
`docs/KUBERNETES-STORAGE.md`). [`lib/workstation/sandbox_homes.py`](../lib/workstation/sandbox_homes.py)
renders the matching Kubernetes volumes. Both read one private list, so no
cluster component holds NAS credentials and nothing provisions datasets on demand.

**User mapping.** Keep `sandbox-users.json` with the other private site files
(next to `site.json` and `sandbox.json`); never commit the real file:

```json
{ "users": [
  {"webui_user_id": "00000000-0000-4000-8000-000000000001", "name": "example-parent", "quota": "20G", "group": "Parents"},
  {"webui_user_id": "00000000-0000-4000-8000-000000000002", "name": "example-child", "group": "Family"}
] }
```

| Field | Rule |
| --- | --- |
| `webui_user_id` | The exact Open WebUI user id; unique |
| `name` | Lowercase DNS label, at most 32 characters; unique. It names the dataset, export and claim, so do not change it once the home holds files |
| `quota` | Optional; whole gibibytes from `1G` to `500G`, default `20G` |
| `group` | `Parents`, `Family` or `Admin` |

`sandbox_homes.read_users(path)` validates the file and returns the users with
the quota filled in. At most 32 users are accepted.

**Interface for the sandbox.** Namespace `assistant-sandbox`; one claim
`sandbox-home-<name>` per user (`sandbox_homes.claim_name(name)`), bound to a
retained volume of the same name; every file belongs to UID/GID 1000 whichever
identity wrote it; the mount path is the workspace's choice. A pod needs no root
step or init container for ownership. Set `fsGroupChangePolicy: OnRootMismatch`
beside `fsGroup: 1000` so the kubelet skips its recursive ownership pass. The
namespace's ResourceQuota must allow one claim per user.

**Volumes.** For each user the render writes a PersistentVolume (NFS CSI driver,
the same server and mount options as `nas-nfs`, `Retain`, `ReadWriteOnce`,
empty storage class, `claimRef` to its claim) and the claim, which names the
volume. The volume's capacity shows the quota; the NAS dataset enforces it, and
`df` in the workspace reports it. Every claim requests a fixed 1Gi because
Kubernetes cannot resize a statically bound claim; a later quota change then
alters only the volume and the dataset.

**Order.** The exports must exist before a workspace mounts its claim:

```sh
# 1. Print the NAS variables; copy the file to where the NAS plays are run.
python3 lib/workstation/sandbox_homes.py nas-vars /private/sandbox-users.json > sandbox-homes.json
# 2. Run the NAS play there, first with --check --diff (NAS docs/KUBERNETES-STORAGE.md).
# 3. Render the volumes and claims. The namespace comes from the sandbox bundle.
python3 lib/workstation/sandbox_homes.py render /private/sandbox-users.json > sandbox-homes.yaml
kubectl --context <reviewed-context> diff -f sandbox-homes.yaml
kubectl --context <reviewed-context> apply -f sandbox-homes.yaml
kubectl --context <reviewed-context> -n assistant-sandbox get pvc
```

Neither output contains a user id. To add a person, add the entry and repeat
all three steps. To change a quota, repeat them; the claim is unchanged.

Removing a person from the list removes nothing by itself: delete their claim
and volume by hand after review. `Retain` and the NAS play both leave the
dataset and its snapshots in place until the owner destroys them on the NAS.
Restores are NAS-side operations, described in the NAS guide.

Security: as with `nas-nfs`, NFS traffic crosses the LAN unencrypted and export
access is controlled on the NAS. A home is exported only to this node, and each
workspace mounts only its own claim.

### ROCm 10 SGLang candidate

Status, 9 November 2026: this section describes the legacy `apps/` overlays
only. The current serving image, model and profile are in
[`infrastructure/ai-stack/`](../infrastructure/ai-stack/README.md).

`versions.lock` records AMD's SGLang 0.5.15.post1 / Python 3.14 / ROCm 10.0.0
image, its registry index and configuration digest. The Deployment selects the
Linux/amd64 manifest, not a floating tag. Registry metadata and manifest hashes
identify the candidate; they do not establish successful image execution.
AMD documents this image for Radeon in its
[SGLang guide](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html).

The existing `sglang-profile` ConfigMap selects `ATTENTION_BACKEND=triton`,
`SGLANG_USE_AITER=false`, `SGLANG_USE_AITER_AR=false` and
`SGLANG_ROCM_FUSED_DECODE_MLA=false`. These explicit values override enabled
AITER/MLA defaults, including the separate AITER collective used with TP=2.
They address AMD's
[R9700 AITER known issue](https://rocm.docs.amd.com/en/latest/about/release-notes.html#sglang-inference-might-fail-with-the-default-aiter-attention-backend-on-some-radeon-gpus);
they are not measured performance improvements. Do not import unrelated
architecture overrides, SDMA disabling or permissive Docker security settings.

The pod retains UID/GID 1000, default seccomp, dropped capabilities, exclusive
device-plugin requests and its existing CPU/RAM/shared-memory budgets. Its
working directory is `/tmp`; Triton, TorchInductor and XDG caches use the existing
16 GiB-request cache PVC. That request is advisory, not a disk quota: monitor
combined HF/JIT cache usage. No host driver, Docker daemon or additional serving
framework is installed by this manifest change.

Render and inspect locally; these commands do not contact a cluster:

```sh
kubectl kustomize apps/overlays/single-gpu
kubectl kustomize apps/overlays/dual-gpu
bash tests/test_home_lab.sh
```

Before promotion on the workstation, verify the pinned image starts as UID 1000,
imports its packaged HIP/PyTorch stack and accepts the configured server flags.
Then load the exact reviewed Qwen files; test generation, GPU allocation, memory
use, shutdown and repeated starts with each profile. Record actual kernel,
firmware, image, model and library versions. The base remains at `replicas: 0`
with pending qualification; an image digest alone must not bypass session gates.
Local IDE tool calling and an authenticated external API path remain separate
work. Hosted OpenAI IDE agents do not require this local service.

For rollback, restore the previous reviewed manifest/image/model combination
through the existing maintenance process. If no combination has passed target
validation, keep replicas at zero and the qualification marker pending. Do not
attempt recovery by applying the destructive OS installer.

### Workload choices

These are the legacy `apps/` overlays. Their SGLang profiles, including the
FP8 default, are not the current serving path; see the status note in
[deployment status](#deployment-status-and-boundaries).

| Profile | GPU use | Notes |
| --- | --- | --- |
| `default`, `dual-gpu` | SGLang 2 | Qwen3.8-27B-FP8, TP=2, 32K context, `heavy-ai-priority` |
| `single-gpu` | SGLang 1, SwarmUI 1 | Qwen3.5-9B BF16, 4K context; WebUI uses SGLang's internal `/v1` API |
| `rag`, `rag-dense` | SGLang 2; embeddings on CPU | Default Qwen3.8 chat plus Qwen3-Embedding-0.6B and separate retrieval data |
| `rdna4-compat` | Same as single | Explicit diagnostic HSA override and SDMA-off experiment |
| `parent`, `kids` | 1 each | Separate homes/credentials/selectors; kids use `-tenfoot` |
| `family` | At most 2 can allocate | All definitions present but disabled; not four simultaneous GPU allocations |

The [model review and staging guide](MODELS.md) records the complete inventory,
official checkpoint revisions, memory trade-offs and qualification procedure.
The default uses the official serialized FP8 checkpoint, not AWQ. The one-card
option uses BF16. SGLang reads quantization from the staged checkpoint config;
verify every transferred file before promotion. AMD's selected ROCm 10 image
is a vendor candidate, not a local model-capacity or correctness qualification.

The 16 GiB `/dev/shm` is a tmpfs limit, not an up-front allocation, and its pages
count against container memory. Single-GPU SGLang requests and limits 32 GiB;
the two-GPU profile requests and limits 38 GiB. These are initial test budgets,
not proof of model loading or room for RAG plus full telemetry. Use the
[host-memory workflow](PERFORMANCE-VALIDATION.md#right-size-sglang-host-ram) before
reducing them. Account
for host/K3s reserves, CPU staging, KV cache and other applications. With no swap,
stop source builds before large inference tests.

Kubernetes preemption can reclaim lower-priority GPUs and honors shutdown grace,
but cannot save a game or checkpoint an image/video generation automatically.
Queued lower-priority pods can remain Pending indefinitely. The host desktop is
not scheduled by Kubernetes; validate display/VRAM contention or use a headless
host before treating both cards as fully allocatable.

SwarmUI expects a reviewed image containing SwarmUI and its embedded ROCm ComfyUI
venv at `/SwarmUI/dlbackend/ComfyUI`. No CUDA image or auto-installer is substituted.
The initial backend seed selects `comfyui_selfstart`, disables automatic updates,
starts ComfyUI with fp8 UNet and text encoders and `--reserve-vram 1.5` (see the
sharing note in section 4), and leaves existing PVC configuration untouched: a
`Backends.fds` already on the state volume keeps its own arguments. The selected checkpoints
are Qwen-Image-2512 for pictures and Wan 2.2 T2V A14B for video ([MODELS.md](MODELS.md)); stage
them before starting, because the network policy prevents runtime downloads.
Its Service remains internal until application authentication and a private
ingress rule are reviewed. Limit children's accounts and extension installation
through application permissions; PVC separation is not parental content policy.

The unverified `games-on-whales/steam-headless` reference has been replaced by a
build-required placeholder. The recipe in `infrastructure/gaming` uses the
digest-pinned maintained Steam-Headless base, a hash-verified Sunshine release,
matched amd64/i386 Mesa backports and a locked compatible KWin package. Its
default image replaces the inherited rootful Xorg/XFCE/noVNC lifecycle with a
non-root KWin virtual Wayland session, PipeWire/WirePlumber and XWayland only for
Steam or older games. It uses native KWin capture with Vulkan Video by default.
Portal capture is an explicit owner-consent option, not an X11 fallback. The
`x11` image target remains an explicit rootful rollback candidate requiring its
own review. The home PVC mounts at `/home/default`; [SUNSHINE.md](SUNSHINE.md)
documents configuration, persistent-state migration, image promotion and target
tests.
The AMD compute plugin alone does not solve `/dev/uinput`/controller injection.
No blanket `/dev`, Docker socket, host IPC or host network mount is added. Gaming
services remain ClusterIP; private L4 exposure and precise input-device delegation
are separate acceptance gates. Steam authentication and downloads also require
an explicit egress policy; the default policy denies them. Do not expand host
access merely to make it start.

Open WebUI keeps authentication on and signup off. Bootstrap its first administrator
through an isolated, reviewed process before exposing the TLS ingress; tenant
accounts must have restricted model/tool permissions. SGLang is internal and its
network policy permits only WebUI clients. Do not grant end users namespace-level
pod creation: that would bypass this intended application boundary.

The optional `rag` overlay adds a CPU-only hybrid-retrieval pilot with a pinned
Open WebUI image, staged/hash-verified embeddings and separate pilot data/model
PVCs. It retains zero replicas and all security/network qualification gates.
See [RAG.md](RAG.md) for preparation, configuration precedence, memory budgets,
source provenance, target tests and rollback. No graph database, memory writer,
IDE retrieval endpoint or additional inference framework is installed.

## 6. Validation and promotion

`make check` includes offline storage-profile, TPM-plan, CIDR and rendered-overlay
tests. Set `HOME_LAB_PYTHON` to the configured interpreter with Jinja2/PyYAML to
include Jinja and TOML checks. Helm rendering is separate and needs the verified
chart archive; set `HOME_LAB_GPU_CHART` to include operator lifecycle checks.
Source/manifest checks do not prove that containers run.

Before changing any application replica count, record image/model digests and pass
the dual-GPU HIP/PyTorch/RCCL/reboot tests in [ROCM.md](ROCM.md). Then verify the
rendered K3s containerd configuration, both allocated devices, tmpfs accounting,
SGLang quantization, SwarmUI authentication, gaming input/encoding, private ingress,
network-policy denials, preemption, cold YubiKey touch-only unlock and key-absent
recovery. Test TPM+PIN instead only when that alternative was explicitly selected.

Keep last-known-good kernels, UKIs, source-built ROCm sets and runtime packages.
Test off-array backups of PVC data, encrypted header backups and SQLite recovery.
No commit, package publication, cluster deployment or hardware mutation is part
of repository validation.

## Primary references inspected

- [K3s advanced configuration and containerd v3](https://docs.k3s.io/advanced)
- [Pinned K3s release](https://github.com/k3s-io/k3s/releases/tag/v1.37.2%2Bk3s1)
- [AMD GPU Operator v1.5.1 source](https://github.com/ROCm/gpu-operator/tree/v1.5.1)
- [AMD device-plugin/DRA exclusivity](https://instinct.docs.amd.com/projects/gpu-operator/en/main/device_plugin/device-plugin.html)
- [systemd cryptenroll PCR syntax](https://github.com/systemd/systemd/blob/main/man/systemd-cryptenroll.xml)
- [systemd PCR measurements and boot phases](https://github.com/systemd/systemd/blob/main/docs/TPM2_PCR_MEASUREMENTS.md)
- [Arch sd-encrypt hook](https://github.com/archlinux/mkinitcpio/blob/master/install/sd-encrypt)
- [SGLang AMD support and quantization](https://docs.sglang.io/docs/hardware-platforms/amd_gpu)
- [SwarmUI container documentation](https://github.com/mcmonkeyprojects/SwarmUI/blob/master/docs/Docker.md)
- [Steam-Headless project](https://github.com/Steam-Headless/docker-steam-headless)
