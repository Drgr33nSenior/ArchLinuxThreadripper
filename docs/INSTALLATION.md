# Installation runbook

Use [ISO.md](ISO.md) to build, sign and write the custom media. This guide owns
the target sequence: boot, network, optional Codex, configuration, installation,
manual key enrolment, recovery and Bridge handoff. Disk writes, passwords,
firmware trust and reboots are owner-run, not agent tool calls.

With the reviewed checkout open on a controller, the
[installation skill](../.agents/skills/workstation-install/SKILL.md) also supports:

```text
$workstation-install plan my rebuild using config/install.conf
$workstation-install investigate my interrupted installation using my private controller journal
$workstation-install verify the installed workstation using config/install.conf
```

## 1. Prepare the hardware and firmware

1. Confirm the exact RDIMM count. A two-DIMM 2x32 GiB kit belongs in the
   printed `DDR5_A1` and `DDR5_E1` slots, but it activates only two of the four
   memory channels. For maximum compilation and CPU-AI bandwidth, use one
   matched, QVL-listed four-DIMM kit in `A1`, `C1`, `E1`, and `G1`. Do not
   assume that two separately binned kits will train together.
2. Install the RAID NVMes in two of `M2A_CPU`, `M2B_CPU`, and `M2C_CPU`.
   `M2D_CPU` is not available with the non-PRO Threadripper 9960X.
3. Put the first R9700 in `PCIEX16_1` and the second in an available full-width
   CPU slot with adequate physical clearance. Record actual PCIe links after
   boot. Connect all required CPU, board auxiliary and GPU power using PSU-native cables.
4. Use a full-IHS sTR5 cooler. Size the PSU at no less than the board vendor's
   1200 W single-GPU guidance; reassess around a 1600 W ATX 3.1-class supply
   for the dual-GPU configuration. Verify both R9700 cards' cooling and
   connector requirements rather than relying only on the reference design.
5. Confirm that firmware is at least the Gigabyte F10 release required for the
   9960X. F11 was the current suffix-free choice when this lock was prepared;
   reassess later releases manually. Do not flash firmware from this repository.
6. Load firmware defaults. Keep memory at JEDEC/Auto for initial testing.
7. Disable CSM. Enable Above 4G Decoding, Resizable BAR, SVM, IOMMU, Secure Boot
   capability, and firmware TPM.
8. Leave NUMA/NPS, CPPC, preferred cores, SMT, C-states, and PCIe ASPM at their
   defaults. Do not enable ACS override.

Record firmware version, memory part numbers, disk model/firmware/serials, GPU
PCI ID, NIC MAC addresses, and the physical slot map.

The board exposes two Marvell AQC113C 10 GbE ports. The K3s design uses one
for the trusted host/AI/NAS path and reserves the other as the unnumbered parent
of the tagged DMZ bridge. Do not assign a host address to that DMZ parent or
bridge.

## 2. Prepare trusted installation media

Build and qualify the [custom ISO](ISO.md), verify its release signature and
checksum on a trusted controller, then boot it in native UEFI mode. Package
signatures alone do not qualify Secure Boot for the live ISO.

Alternatively, verify an official Arch ISO and bring a reviewed full checkout
on separate media. Supply the signed boot package with `--boot-package FILE`
and establish its signer's trust in the live keyring before erasure. Supply the
matching Bridge bundle too, or explicitly set `INSTALL_BRIDGE=false`.
Never disable signature checks. Do not enrol keys or erase disks until the
preflight report matches the recorded hardware.

### Connect without Codex

Boot the custom ISO in UEFI mode. At its local console, inspect interfaces and
connect with iwd. Substitute the discovered station and your SSID:

```sh
iwctl
device list
station wlan0 scan
station wlan0 get-networks
station wlan0 connect "YOUR SSID"
exit
timedatectl set-ntp true
timedatectl status
arch-workstation-network mirrors
```

`wlan0` is an example, not a selected hardware identity. Enter the password only
at iwctl's local password prompt. Do not use `--passphrase`, shell tracing,
terminal recording or paste credentials into Codex. If the radio is blocked,
inspect `rfkill list` and resolve the appropriate radio locally. Wired networking
is also usable. Captive portals, enterprise Wi-Fi and proxy setup require the
owner's network-specific procedure; the assistant does not bypass them.

The mirror check reads the effective `pacman-conf` repositories and servers.
It requires synchronized time, DNS, a route to the resolved host and an actual
readable repository database over certificate-verified HTTPS (or a configured
local repository). It tries alternative configured servers per repository.
HTTP-only/credential-bearing URLs are refused; no URL or password is logged.
It does not update pacman's database. It does not prove every package download
will finish or replace signature verification. The same check is now part of
installer preflight, including dry-run and execute mode before erase confirmation.
Failures stop installation; there is no skip-network flag.

### Optional Codex authentication

Choose **one** launcher command at the local console, outside an agent tool call:

```sh
arch-workstation-codex api-key
# Alternative, not an automatic fallback:
arch-workstation-codex device-code
```

API-key mode prompts with input hidden and pipes the unexported key to the pinned
CLI's `login --with-api-key`. API usage is billed through OpenAI Platform,
separately from a ChatGPT subscription. Device-code mode runs `login --device-auth`;
complete the displayed URL/code in a browser on another device. Account/workspace
settings may disable that flow. Failure does not switch billing methods.
See [OpenAI authentication](https://learn.chatgpt.com/docs/auth).

The launcher probes API or authentication-server connectivity without credentials
or inference. An expected API HTTP 401 establishes reachability, not valid login.
Transport/DNS/TLS errors are connectivity failures. Login failure is separate.
`login status` checks reuse by another process, but an API key can be stored
without proving model entitlement. The first successful owner-requested response
establishes API/model access and may incur usage charges. Report 401/403, quota,
rate-limit and model-access errors separately; never change the account/provider
to conceal one. No automated smoke test makes a paid request.

The same fresh `CODEX_HOME` and config serve login and the interactive client.
File credentials, logs, sessions and caches stay in a private 0700 directory on
`/run` (root) or `/run/user/UID` (named user), checked for tmpfs; configuration is
0600 and child files inherit umask 077. The runtime directory must exist and be
writable; no home-directory fallback is used. Active
swap is refused and core dumps are disabled. This is deliberate file persistence
between processes, not `ephemeral` authentication, which lasts only within one
process. Existing user credentials and the desktop configuration are untouched.
See [credential configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

The client uses read-only sandboxing and `on-request` approval, including
when the live console is root. No bypass flag, automatic approval or new root
service is added. The pinned CLI rejects the older `untrusted` policy; it is not
used as a presumed safety setting. Do not approve destructive tool calls: execute the installer
yourself in another local console. If sandbox startup fails, stop; do not disable
it to proceed. Root still has broad read access: never ask the agent to inspect
the private session directory, iwd profiles, password prompts or recovery data.

Normal exit and INT/TERM/HUP clean the owned RAM session after stopping the
owned client. Reboot requires fresh authentication. SIGKILL/power loss cannot
run cleanup; RAM state is not a durable journal. After an abnormal launcher kill,
close remaining Codex processes and have the owner remove only that verified
session directory, or reboot when otherwise safe. Never copy a user's home,
`auth.json`, raw Codex sessions or login logs into the target or a journal.

The launcher starts in `/usr/lib/arch-workstation-bootstrap`, a package tree
with discoverable skills and no required Git checkout. Create the configuration
in the next step, then open `/skills`, confirm `workstation-install` appears,
and enter:

```text
$workstation-install plan my rebuild using /run/install.conf
```

## 3. Configure and inspect

`INSTALL_BRIDGE` defaults to `true`, including older configurations that omit it.
The selected ISO must carry the matching signed Bridge/runtime bundle and
official dependencies. Preflight and dry-run verify and report these identities
before erasure. Set `INSTALL_BRIDGE=false` explicitly to omit this payload.
Follow [the Bridge handoff](#bridge-package-and-owner-handoff) after boot verification.
Installation does not activate services or create management credentials.

On the custom ISO, create a nonsecret local configuration:

```sh
cp /usr/lib/arch-workstation-bootstrap/config/install.conf.example /run/install.conf
nano /run/install.conf
```

For the home-lab TPM/host policy, use the packaged
`infrastructure/host/install.conf.example` instead. In a full checkout, an
ignored `config/install.conf` is also supported. Commands below use the custom
ISO launcher and `/run/install.conf`; from a checkout, use
`sudo ./bin/bootstrap-arch` with that checkout's config path.

Replace the disk paths with complete `/dev/disk/by-id/...` values and copy the exact
serials reported by `udevadm` or `lsblk`. Do not use `/dev/nvme0n1` names.

Keep `HOST_PROFILE=headless` for an AI/remote-gaming server. This omits GNOME,
GDM and host gaming packages, keeps the multi-user target, and selects TuneD's
balanced policy. `HOST_PROFILE=desktop` is an explicit new-install alternative.
An older configuration without this key also defaults to headless. `ENABLE_SSH`
remains a separate opt-in; establish named-user access before relying on it.

Run:

```sh
bootstrap-arch --config /run/install.conf preflight
bootstrap-arch --config /run/install.conf --dry-run install
```

Confirm that the plan creates two 2 GiB ESPs and uses only the remaining two
partitions as RAID0 members. Confirm this storage order:

```text
mdadm RAID0 → LUKS2 → XFS
```

Stop if either disk is mounted, active in another storage stack, has an
unexpected size or serial, or resolves to the same device.

To measure the unlock budget before installation, use the reviewed checkout's
`./bin/workstationctl encryption calibrate /run/install.conf artifacts/argon2-01`
on the target. This in-memory benchmark opens no device. Installation recalibrates
with the selected limits and records actual time/memory/CPU costs in
`/etc/cryptsetup/argon2id-parameters.json`.

## 4. Install

The install action defaults to a dry run. Run the real installation only from
the live ISO with the explicit execution flag:

```sh
bootstrap-arch --config /run/install.conf --execute install
```

The command requires both fully resolved device paths and serials in its confirmation immediately before
the first destructive command. Enter the LUKS passphrase only through
cryptsetup's terminal prompt. The repository does not capture it.

Set both the named user's password and the root recovery-console password at
the interactive prompts. Root must have an authenticated console recovery path;
its password is not a LUKS unlock credential. Root SSH login remains prohibited.
Do not record these prompts in a session transcript.

The initial install creates official stable and LTS UKIs. It does not build a
git kernel, enrol a YubiKey, generate AWS keys, install AUR packages, or create
the K3s VM.

No swap, zram or resume configuration is generated; hibernation modes are disabled.

Before rebooting, copy `/mnt/etc/cryptsetup/luks-*.header` to protected offline
media, record a checksum, unmount that media, and remove it. A header copy left
only inside this RAID0-encrypted root cannot recover a damaged LUKS header or a
failed NVMe member.

## 5. Enrol Secure Boot keys

Treat key enrolment as a manual recovery boundary:

1. Export and archive the current firmware key databases.
2. Confirm the installer-created owner keys are present only on encrypted
   root, create a separately encrypted offline backup of `/var/lib/sbctl`, and
   record the public-certificate fingerprints.
3. Verify those owner keys and each stable, LTS, and
   recovery UKI signature against `db.pem`.
4. Enter firmware Setup Mode manually.
5. Enrol owner keys while preserving required OEM and Microsoft certificates.
6. Verify signatures and firmware Secure Boot state.
7. Boot stable and LTS before adding any git kernel.

Never store Secure Boot private keys in this repository or on the ESPs.

## 6. Enrol the YubiKey

Complete passphrase-only boot tests first. Verify that the key supports the
FIDO2 `hmac-secret` extension, then add a native systemd FIDO2 LUKS token with
PIN and touch. Do not require global user verification. Generate a separate
recovery key, store it offline, and create a new offline LUKS header backup.

Cold-test passphrase, YubiKey, and recovery-key unlock on stable and LTS. Repeat
the tests after the git UKI is accepted.

Also test authenticated rescue/emergency-console access from a disposable boot
or recovery drill. Successful LUKS unlock alone does not prove that `sulogin`
can authenticate the root recovery account.

## 7. Verify before optional layers

### First-boot connectivity

Wi-Fi credentials are **not** migrated from iwd. The installed system enables
NetworkManager; reconnect locally before expecting remote access:

```sh
nmcli device status
nmcli device wifi list
sudo nmcli --ask device wifi connect "YOUR SSID" ifname wlan0
timedatectl status
```

Use the newly discovered interface, not necessarily the live ISO's name. `--ask`
keeps the password out of arguments/history. NetworkManager stores its own
protected connection profile. It is not a copy of an iwd profile. If SSH was
selected with `ENABLE_SSH=true`, verify networking, `systemctl status sshd`, the
named user's access and host-key fingerprint locally before connecting remotely.
Root SSH remains prohibited. Live SSH remains masked regardless of that option.

### Boot and storage verification

On the installed host, run the commands below from a reviewed checkout. Neither
`/run/install.conf` nor the live source tree survives reboot; restore only the
nonsecret config from external storage. The optional signed bootstrap package
described below provides an alternative `bootstrap-arch` launcher. Record
verification results without secrets:

```sh
sudo ./bin/bootstrap-arch --config /path/to/install.conf verify
./bin/workstationctl status
```

`bootstrap-arch verify` checks exact ESP mounts and identities, both copies, UKI signatures, fallback identity,
the installed initramfs encryption-policy source, direct-UEFI labels, and
BootOrder. Inspect the embedded UKI contents and live runtime separately
(substitute configured mapper/array names if changed):

```sh
sudo lsinitcpio -l /efi/EFI/Linux/arch-linux.efi | grep -E '(^|/)etc/crypttab$'
sudo mdadm --detail /dev/md/archroot
sudo cryptsetup luksDump /dev/md/archroot
sudo xfs_info /
findmnt -no SOURCE,FSTYPE,OPTIONS /
systemctl is-enabled fstrim.timer
cat /sys/devices/system/cpu/amd_pstate/status
sudo sbctl status
lspci -nnk -d 1002:
lspci -vv -d 1002:
./bin/workstationctl swap validate
journalctl -k -b | grep -Ei 'md|nvme|xfs|edac|mce|aer|pcie|amdgpu|firmware|reset'
```

Confirm RAID0 level/chunk/members, an Argon2id LUKS slot with the intended
memory and parallelism, the XFS `su=512k,sw=2` geometry, weekly trim, AMD
P-State status, Secure Boot, both R9700s bound to `amdgpu`, their ReBAR apertures,
firmware loading, and no storage, EDAC, MCE, PCIe, or GPU reset errors.

Keep the recovery USB until a restore from S3 has also been tested.

Installation persists TuneD's profile and manual selection mode directly in the
target filesystem. Inspect `tuned-adm active` after boot; enabling the service
offline is not evidence that its runtime policy was applied successfully.

The installed mkinitcpio policy deliberately leaves `MODULES=()` and uses
modalias/udev discovery. Blanket module preloading usually adds decompression
and probe work instead of shortening boot. Add a boot-critical module only
after `systemd-analyze` and kernel-log evidence shows that discovery is late or
unreliable; keep the change in both stable and recovery initramfs tests.

## Bridge package and owner handoff

After the boot/recovery baseline passes, inspect the default Bridge installation.
Package-installed, management-configured and workload-qualified are separate
statuses. No unit should have been enabled or started by package installation.

```sh
pacman -Q spry-ai-workstation-bridge arch-workstation-bridge-runtime
pacman -Qkk spry-ai-workstation-bridge arch-workstation-bridge-runtime
pacman -Qo /usr/lib/bridge/bridge-hostd /usr/lib/bridge/workstation-runtime/bin/workstationctl
cd /usr/lib/bridge/workstation-runtime
sha256sum --check --strict SOURCE-MANIFEST.sha256
cd /usr/lib/bridge/workstation-reference
sha256sum --check --strict SOURCE-MANIFEST.sha256
/usr/lib/bridge/bridge-hostd --check-reference /usr/lib/bridge/workstation-reference
systemctl is-enabled bridged.service bridge-hostd.socket bridge-worker.socket
systemctl is-active bridged.service bridge-hostd.service bridge-worker.service bridge-hostd.socket bridge-worker.socket
```

Expect disabled/inactive, with corresponding nonzero systemctl status. Missing
units are not success. Package-installed is not management-configured or
workload-qualified. Do not change the example UIDs into assumed target identities.

Follow the **bundled Bridge** `/usr/share/doc/spry-ai-workstation-bridge/docs/OPERATIONS.md`
and `HOST-EXECUTOR.md`: resolve `id bridge`, `id bridge-worker` and workload
reader GIDs; prepare bounded storage and read-only runtime/system manifests;
review workstation/session/helper/worker policies and restricted cluster identity;
select loopback or private HTTPS management address and TLS trust where required;
import the initial managed source while stopped; create the owner token locally
in an owner-only output file. Finally activate services through the owner's host
management process. Never put tokens, kubeconfigs or private keys in Codex or a
journal. Empty authorization maps and missing capabilities must remain refusals.

For upgrades/rollback, follow the bundled Bridge OPERATIONS guide: stop admission,
settle operations and stop writers, then take a protected backup before changing
packages. Retain the matching signed Bridge/runtime pair. Do not overwrite active
policies or replay journals blindly. `INSTALL_BRIDGE=false` skips a new install;
it does not uninstall an existing controller.

## Optional installed-host Codex

You can continue using the controller's skill and sanitized local results without
installing Codex on the target. For optional target-console Codex, preserve the
same signed bootstrap package and signature from the ISO build on separate media.
Use the retained snapshot for `sudo pacman -Syu openai-codex`. Verify the exact
locked version with `pacman -Q openai-codex`. Have the owner inspect the bootstrap
signer's public fingerprint and establish its pacman trust explicitly, then run
`sudo pacman -U /path/to/arch-workstation-bootstrap-VERSION-any.pkg.tar.zst` with
its adjacent signature. Do not bypass `LocalFileSigLevel`. This reuses the signed
package; it does not run an installer scriptlet or copy live credentials.

Run `arch-workstation-codex api-key` or `device-code` as the named user, with fresh
authentication. Reintroduce only your nonsecret config/journal. Use:

```text
$workstation-install verify this installation using my external config and journal
```

The owner runs `sudo bootstrap-arch --config /path/to/install.conf verify` on the
installed root. The default Bridge payload now provides the separately owned
post-install runtime at `/usr/lib/bridge/workstation-runtime`; see
[Bridge handoff](#bridge-package-and-owner-handoff). Bootstrap source availability is not permission
to run post-install actions in the live ISO. Keep stable/LTS/recovery
qualification separate from a verify exit code.
Before moving to rolling mirrors, review the optional bootstrap package's exact
Codex dependency: update/rebuild it coherently, or remove that optional bootstrap
package. Never remove `arch-workstation-boot`, which owns the UKI recovery hooks,
merely to upgrade Codex. Follow the existing full-upgrade/recovery policy.

## Continuity and interrupted installation

Use the skill's small nonsecret journal on the controller or separate storage,
outside both target disks and Git. Save the config there separately; `/run` is
lost on reboot. Record source/build identities, config fingerprint, artifact
references, checks and exit status, manual handoffs and the next safe action.
No credential/session copying or journal export automation is supplied.
After reconnecting, compare those records with the actual target and configuration.
Changed inputs invalidate affected checks. Unknown outcomes stay unknown. Do not
rebuild/resign an old run to regain conversational context.

The skill supplies no SSH connection or persistent session. After interruption,
inspect actual disks, mounts, revision and configuration before selecting the
next safe action. Unknown outcomes remain unknown. There is no resume command;
never repeat a destructive install over partial state. Use
[recovery inspection](ISO-REFERENCE.md#5-recovery-test), and stop for owner
intervention where the installer has no safe continuation.
