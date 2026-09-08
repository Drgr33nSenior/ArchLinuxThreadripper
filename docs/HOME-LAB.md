# Bare-metal AI home lab

This is an additive profile for the existing Arch repository. It does not replace
the EL9/KVM lab, install a second operating system, or run a cluster during tests.
The target remains the Threadripper 9960X, 64 GiB ECC RDIMM, two Samsung 9100 PRO
NVMes, and two R9700s. `gfx1201` is an expected input that must match `rocminfo`.

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
├── base/{sglang,swarmui,steam-headless,open-webui}/
└── overlays/{single-gpu,dual-gpu,rdna4-compat,parent,kids,family,rag,rag-dense}/
```

## Deployment status and boundaries

Host configuration, TPM enrollment planning, K3s packaging/configuration,
operator rendering, and workload scaffolding are implemented. They are not a
qualified installation. Application replicas default to zero. Replace every
remaining `UNQUALIFIED` image with an audited digest before enabling it. SGLang
now selects a digest-pinned AMD ROCm 10 candidate; it still requires target and
model validation. Gaming has a pinned-input build recipe but still requires
promotion of the built image and display/input/security qualification.

K3s documents general Linux prerequisites. This Arch/ROCm/GPU Operator
combination still requires target-hardware qualification.
Single-node K3s, one motherboard and RAID0 have no high-availability guarantee.
Snapshots on the same array do not protect against an NVMe failure.

All application resources currently use `ai-home-lab`; infrastructure controllers
use their own namespaces. This is a trusted-family boundary, not hostile-tenant
isolation. Family members receive application accounts, not permission to create
pods, change priorities, read Secrets or attach arbitrary PVCs. NetworkPolicy
does not make a shared kernel or GPU equivalent to separate virtual machines.

Gaming is intentionally not enabled. The Wayland candidate runs as UID/GID 1000
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

## 2. Secure Boot, TPM2 and PIN

Direct UEFI boot uses firmware PK/KEK/db trust, not shim's MOK enrollment.
The existing installer creates sbctl keys and registers/signs the UKIs. Firmware
enrollment remains manual after exporting the original firmware keys and checking
GPU option-ROM requirements. Do not blindly replace firmware trust stores.

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

For the requested static PCR policy, bind PCRs **0+7+11** and require a PIN.
PCR 0 changes on firmware updates; PCR 7 changes with Secure Boot state/policy;
PCR 11 changes with UKI content and boot phases. Enrollment after login using
literal `--tpm2-pcrs=0+7+11` normally captures the wrong PCR 11 phase for early
root unlock. The helper therefore requires the predicted early-unlock SHA256
value for PCR 11 while sampling current PCRs 0 and 7:

```sh
bash infrastructure/host/tpm2-enroll.sh LUKS_UUID EARLY_PCR11_SHA256
```

This only prints the plan. Derive the digest with `systemd-measure calculate`
from the exact UKI sections and the installed initramfs's unlock phase. Include
every measured section and check the event log; do not copy the post-login PCR
value or assume `enter-initrd` without inspecting the boot sequence. Test against
recovery media first. The execution form adds `--execute` and requires a local
terminal, enabled Secure Boot, an existing recovery slot, no existing TPM token,
and an exact resolved-device confirmation. It passes `--tpm2-with-pin=yes` to
cryptenroll. Enter a strong alphanumeric PIN only in cryptenroll's prompt.

Confirm firmware selects the physical SPI module, not AMD fTPM, before using
`--tpm2-device=auto`. A motherboard connector description cannot prove which TPM
Linux selected. No PIN or passphrase is accepted in configuration or arguments.

Static PCR 11 binding is deliberately update-sensitive. A correctly signed new
UKI can still fail TPM unlock. Retain the recovery passphrase, back up headers,
and plan reenrollment after firmware/UKI changes. Signed PCR policies are a more
maintainable alternative, but are not silently substituted for the requested
static policy. The helper refuses existing TPM tokens rather than wiping slots.
FIDO2/recovery fallback remains available; PIN requirements apply to the TPM path,
not to every emergency recovery method.

## 3. Package and configure bare-metal K3s

K3s replaces RKE2 for new home-lab installations. The 2026-09-06 review selected
K3s because its single-server SQLite datastore and bundled Flannel, NetworkPolicy
controller, CoreDNS, Traefik and local-path storage fit this one-machine lab.
RKE2's compliance-oriented defaults are not a requirement here. K3s still runs
ordinary Kubernetes workloads; it does not remove the need for access controls,
tested backups or GPU qualification. See the [architecture](https://docs.k3s.io/architecture),
[networking services](https://docs.k3s.io/networking/networking-services) and
[RKE2 comparison](https://docs.rke2.io/#how-is-this-different-from-rke-or-k3s).

`infrastructure/packages/k3s/PKGBUILD` reads `K3S_VERSION` and
`K3S_BINARY_SHA256` from `versions.lock`. The pin is `v1.35.7+k3s1`, which keeps
the previous Kubernetes minor and patch. Its binary hash matches both the
upstream checksum file and release-asset metadata. The package owns
`/usr/bin/k3s` and `k3s.service`, adapted from that release's service definition.
It omits uninstall/killall helpers and does not start services. Review, build
and sign it with makepkg as a non-root user, then install the signed package.

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
  DRA feature gates, explicit network binding and the default SQLite datastore.
- `config-v3.toml.tmpl`: extends K3s's base rather than copying generated output.
- `config-v3.toml.d/10-cdi.toml`: CDI directories and systemd runc settings, without
  duplicate TOML tables or globally privileged containers.

The pinned K3s uses containerd 2.x. Its v3 filename is preferred over the legacy
`config.toml.tmpl`. Device files for non-root pods use K3s's scoped
`nonroot-devices` setting. Protect `/etc/cdi` and `/var/run/cdi` from tenant writes.

Flannel VXLAN uses the selected LAN interface. K3s's NetworkPolicy controller
stays enabled. ServiceLB is disabled to avoid automatically binding host ports;
Traefik's LoadBalancer service remains pending until a reviewed exposure method
is supplied. Application services remain ClusterIP. The bundled local-path
provisioner uses `/var/lib/rancher/k3s/storage` on encrypted root; local volumes
are not replicated and their default reclaim policy is Delete. Keep off-array
backups and review PVC retention before deleting a claim. The separate VM lab
uses its existing pinned Retain provisioner and embedded-etcd recovery workflow.

`lab_manage_service` and `lab_restart_approved` default to false. Review/load the
generated sysctls and modules before enabling service management. Also review
NetworkManager's CNI handling and the host firewall: allow the API only from the
management LAN; do not expose kubelet, etcd, supervisor or VXLAN ports to WAN.
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
creation, DRA DeviceClass creation and automatic remediation. Apply the separate
DeviceConfig only after its CRD exists. It disables driver management, blacklisting,
driver upgrades, partition management and DRA; it enables the traditional device
plugin and node labeller. NFD is enabled. The operator/device-plugin images are
digest-pinned; audit rendered chart helper/NFD images before release promotion.

The resource name is `amd.com/gpu`, not `://amd.com`. DRA infrastructure being
enabled does not make these device-plugin workloads DRA consumers. AMD prohibits
running its DRA driver and device plugin simultaneously. CDI is runtime support,
not GPU partitioning or a scheduling resource by itself.

NFD discovers the AMD PCI vendor; it does not automatically supply a verified
`gfx1201` label. The host profile adds `ai-home-lab.local/gfx` only after the
ROCm hardware report matches. Verify actual labels and allocatable GPU count are
two before enabling any workload. Operator v1.5 does not imply compatibility with
every ROCm 10 build; qualify the kernel/userspace/image combination separately.

Do not use `HIP_VISIBLE_DEVICES=1` to reserve the second physical card. The device
plugin chooses a GPU and container ordinals can change. SwarmUI requests one
exclusive GPU and its embedded backend uses visible ordinal zero. Deterministic
physical-card ownership requires a separate DRA/device-selection design using
discovered identities, not an environment-variable workaround.

## 5. Workload profiles and tenant data

Render an overlay with `kubectl kustomize apps/overlays/NAME`. The site ConfigMap
in `single-gpu` supplies the StorageClass, private DNS host and TLS Secret name.
Replace the `.invalid` host, preload a matching TLS Secret and create the WebUI
session Secret through an approved secret store. No secret values are generated.
The bare-metal `local-path` StorageClass can be reused, but its default `Delete`
policy removes volume data when a claim is deleted. Review retention first; the
separate VM profile's `Retain` policy does not apply here. Neither policy provides
backups, and local-path volume size requests do not enforce disk quotas.

### ROCm 10 SGLang candidate

`versions.lock` records AMD's SGLang 0.5.15.post1 / Python 3.14 / ROCm 10.0.0
image, its registry index and configuration digest. The Deployment selects the
Linux/amd64 manifest, not a floating tag. Public registry metadata and manifest
hashes were checked on 2026-09-07; image layers were not downloaded or executed.
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
the two-GPU profile requests and limits 38 GiB, leaving room for the Qwen
embedding pilot and other pods after host reservations. These are initial
test budgets, not proof that a particular model will load without OOM. Account
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
and leaves existing PVC configuration untouched. No image/video checkpoint is
selected yet. Review the current candidates in [MODELS.md](MODELS.md) and stage
the chosen model and dependencies; the network policy prevents runtime downloads.
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
network-policy denials, preemption, cold TPM+PIN unlock and recovery unlock.

Keep last-known-good kernels, UKIs, source-built ROCm sets and runtime packages.
Test off-array backups of PVC data, encrypted header backups and SQLite recovery.
No commit, package publication, cluster deployment or hardware mutation is part
of repository validation.

## Primary references inspected

- [K3s advanced configuration and containerd v3](https://docs.k3s.io/advanced)
- [Pinned K3s release](https://github.com/k3s-io/k3s/releases/tag/v1.35.7%2Bk3s1)
- [AMD GPU Operator v1.5.1 source](https://github.com/ROCm/gpu-operator/tree/v1.5.1)
- [AMD device-plugin/DRA exclusivity](https://instinct.docs.amd.com/projects/gpu-operator/en/main/device_plugin/device-plugin.html)
- [systemd cryptenroll PCR syntax](https://github.com/systemd/systemd/blob/main/man/systemd-cryptenroll.xml)
- [systemd PCR measurements and boot phases](https://github.com/systemd/systemd/blob/main/docs/TPM2_PCR_MEASUREMENTS.md)
- [Arch sd-encrypt hook](https://github.com/archlinux/mkinitcpio/blob/master/install/sd-encrypt)
- [SGLang AMD support and quantization](https://docs.sglang.io/docs/hardware-platforms/amd_gpu)
- [SwarmUI container documentation](https://github.com/mcmonkeyprojects/SwarmUI/blob/master/docs/Docker.md)
- [Steam-Headless project](https://github.com/Steam-Headless/docker-steam-headless)
