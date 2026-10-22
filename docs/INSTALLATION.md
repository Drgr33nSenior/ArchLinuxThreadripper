# Installation runbook

Use [ISO.md](ISO.md) to build, sign and write the custom media. This guide owns
the target sequence: boot, network, optional Codex, configuration, installation,
manual key enrolment, recovery and Bridge handoff. Disk writes, passwords,
firmware trust and reboots are owner-run, not agent tool calls.

Optional `--events` records nonsecret stage transitions to the live local journal.
See [Telemetry: installation and interruptions](TELEMETRY.md#installation-and-interruptions)
for retention and limits. This needs neither K3s nor Bridge and adds no resume command.

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
7. Disable CSM. Enable Above 4G Decoding, Resizable BAR, SVM and IOMMU. Keep
   Secure Boot enforcement disabled while booting the unqualified live USB;
   do not clear its firmware key databases or reset the TPM for this step.
   Select the physical SPI TPM rather than AMD fTPM. Confirm
   which TPM Linux selected before enrolment; see
   [the TPM procedure](HOME-LAB.md#2-secure-boot-tpm2-and-pin).
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

<a id="connect-without-codex"></a>

### Connect on the live ISO (iwd)

These commands apply only to the live ISO. The installed system uses
NetworkManager, not `iwctl`. After reboot, follow
[First-boot connectivity](#first-boot-connectivity); no Wi-Fi password is copied.

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

The packaged console configuration selects OpenAI's `gpt-6.1-sol`.
This is a cloud Codex model, not a local SGLang workload. After login, confirm
the model in the interactive startup display. Account/workspace access is still
required; model-access failure does not select another model automatically.
See the [Sol 6.1 model](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [Codex model configuration](https://learn.chatgpt.com/docs/config-file/config-sample).

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

For the home-lab storage and host policy, use the packaged
`infrastructure/host/install.conf.example` instead. In a full checkout, an
ignored `config/install.conf` is also supported. Commands below use the custom
ISO launcher and `/run/install.conf`; from a checkout, use
`sudo ./bin/bootstrap-arch` with that checkout's config path.

Both new-install examples select `BOOT_UNLOCK=tpm2-pin`. This selects the boot
configuration; it does not enrol a TPM or prove that TPM unlock works. Keep the
PIN out of the configuration. Older configurations that omit `BOOT_UNLOCK`
retain the legacy `fido2` policy, as do those that explicitly select `fido2`.
There is no automatic conversion of an existing installation.

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

Immediately before the first destructive command, the installer displays both
persistent by-id paths, resolved whole disks and serials. Check them against
your recorded hardware. To approve erasing **both** disks, type exactly `ERASE`.
Any other response or end of input cancels installation. The installer rechecks
both paths and serials before writing; changed identities or failed probes stop
installation. Root access, a local terminal and `--execute` are still required.
Enter the LUKS passphrase only through cryptsetup's terminal prompt. The
repository does not capture it.

Set both the named user's password and the root recovery-console password at
the interactive prompts. Root must have an authenticated console recovery path;
its password is not a LUKS unlock credential. Root SSH login remains prohibited.
Do not record these prompts in a session transcript.

The initial install creates official stable and LTS UKIs. It does not build a
git kernel, enrol an unlock token, generate AWS keys, install AUR packages, or create
the K3s VM.

No swap, zram or resume configuration is generated; hibernation modes are disabled.

Before rebooting, copy `/mnt/etc/cryptsetup/luks-*.header` to protected offline
media, record a checksum, unmount that media, and remove it. A header copy left
only inside this RAID0-encrypted root cannot recover a damaged LUKS header or a
failed NVMe member.

### First-boot connectivity

After reboot, sign in at the **installed workstation's local console**. The
installer installs and enables NetworkManager, including `nmcli` and `nmtui`.
`iwctl` is a live-ISO tool and is not installed on the target. Wi-Fi credentials
are **not** migrated from iwd; reconnect before expecting remote access.

For the text menu, run:

```sh
sudo nmtui
```

Choose **Activate a connection**, select your Wi-Fi network and enter its
password locally. No desktop is required. Alternatively, inspect the interface
and available networks, then connect from the command line:

<a id="installed-wifi-connect"></a>

```sh
nmcli device status
nmcli device wifi list
sudo nmcli --ask device wifi connect "YOUR SSID" ifname wlan0
timedatectl status
```

Use the newly discovered interface, not necessarily the live ISO's name. Expect
that device to become `connected`. `--ask` keeps the password out of arguments
and shell history. Do not use `--show-secrets` or record password prompts.
NetworkManager stores its own protected connection profile; it is not a copy of
an iwd profile. See the upstream [console tools](https://networkmanager.dev/docs/api/latest/nmtui.html)
and [nmcli reference](https://networkmanager.dev/docs/api/latest/nmcli.html).

If NetworkManager is not running, check `systemctl is-active NetworkManager.service`
and `systemctl is-enabled NetworkManager.service`. On this installed host only,
start the configured service with `sudo systemctl start NetworkManager.service`.
If `nmcli device status` lists no Wi-Fi device or reports it as unavailable,
inspect `nmcli radio wifi` and `rfkill list` before diagnosing drivers or firmware.
Do not install or start a second network manager to conceal a failed service or
missing device. Captive portals, enterprise Wi-Fi and proxies still require the
owner's network-specific procedure.

If SSH was selected with `ENABLE_SSH=true`, verify networking,
`systemctl status sshd`, the named user's access and host-key fingerprint locally
before connecting remotely. Root SSH remains prohibited. Live SSH remains
masked regardless of that option.

## 5. Enrol Secure Boot keys

Run this section at the **installed workstation's local console**, not on the
Mac, live USB or an installation chroot. First boot stable, LTS and recovery
with Secure Boot disabled and unlock with your LUKS passphrase. Select the
installer's named entries in the firmware boot menu; `sudo efibootmgr -v` shows
their paths. Keep the recovery USB and protected offline LUKS header backup.

Secure Boot authenticates boot code. TPM+PIN unlock in section 6 is separate.
The ISO's GPG release signature is neither of these. The installer already ran
`sbctl create-keys` and signed the unified kernel images (UKIs). **Do not generate
replacement keys.** Key creation is not firmware enrolment.

### 5.1. Inspect and back up before changing firmware

Run these read-only checks:

```sh
sudo sbctl status
sudo sbctl list-files
findmnt --mountpoint /efi
findmnt --mountpoint /efi2
```

Expect an installed sbctl key set and the three primary UKIs under
`/efi/EFI/Linux/`. Secure Boot should still be disabled at this stage. Missing
keys, images or ESP mounts are a stop condition, not a reason to rerun installation.
Print only the public-certificate fingerprints:

```sh
for certificate in /var/lib/sbctl/keys/PK/PK.pem /var/lib/sbctl/keys/KEK/KEK.pem /var/lib/sbctl/keys/db/db.pem; do
  sudo openssl x509 -in "$certificate" -noout -subject -fingerprint -sha256
done
```

Choose mounted, separately encrypted offline backup storage **off the RAID0 and
not the installer USB**. Substitute its real path below. Do not overwrite an
earlier backup:

```sh
BACKUP='/path/to/mounted/encrypted-offline-backup'
sudo test ! -e "$BACKUP/sbctl-before-enrolment" &&
  sudo cp -a /var/lib/sbctl "$BACKUP/sbctl-before-enrolment"
```

This copy contains private keys. Verify the protected backup locally, then
unmount and disconnect that storage. Never copy these keys to an ESP, Git,
unencrypted media, Docker, Codex or a journal. Record only public fingerprints
and that the backup was verified.

Use the motherboard's documented firmware export procedure to archive its
existing **PK, KEK, db and dbx** databases and establish how to restore them.
These are the platform key, key-exchange keys, allowed signatures and revoked
signatures. A screenshot or `sbctl list-enrolled-keys` is not a restorable export.
If the firmware cannot export them, stop and establish a board-specific recovery
procedure before removing any key. Do not guess menu labels or clear all keys.

### 5.2. Verify both boot copies

The block below checks signatures against your owner's public db certificate
and compares the independent ESP copies. It stops on the first failure:

<a id="verify-owner-ukis"></a>

```sh
(
  set -e
  for esp in /efi /efi2; do
    for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
      sudo sbverify --cert /var/lib/sbctl/keys/db/db.pem "$esp/EFI/Linux/$uki"
    done
    sudo sbverify --cert /var/lib/sbctl/keys/db/db.pem "$esp/EFI/BOOT/BOOTX64.EFI"
    sudo cmp /efi/EFI/Linux/arch-recovery.efi "$esp/EFI/BOOT/BOOTX64.EFI"
  done
  for uki in arch-linux.efi arch-linux-lts.efi arch-recovery.efi; do
    sudo cmp "/efi/EFI/Linux/$uki" "/efi2/EFI/Linux/$uki"
  done
)
```

Expect every signature to verify and every `cmp` to exit 0 without output.
Do not enrol keys or enable enforcement if this block fails. For the broader
installed storage/boot checks, use [section 7](#boot-and-storage-verification).

### 5.3. Enrol owner keys, then verify enforcement

This is an **owner-run firmware trust change**, not an agent operation:

1. With the backups and boot checks complete, enter firmware setup manually.
   Use the board's documented procedure to enter **Setup Mode** for custom key
   enrolment. Disabling Secure Boot alone does not mean Setup Mode is active.
   Do not reset the TPM or use a generic key-clearing command.
2. Boot the installed stable UKI with your passphrase. Run `sudo sbctl status`.
   Expect **Setup Mode: Enabled**. Stop if that state is not present.
3. Review which Microsoft/OEM certificates the GPU option ROMs and other boot
   paths need. The following sbctl command includes Microsoft's certificates and
   the firmware's built-in db/KEK defaults; these defaults do **not** preserve
   every extra certificate currently installed. If the archived databases have
   additional required certificates, prepare a reviewed custom-certificate
   enrolment instead. Do not proceed until their preservation is resolved.

```sh
sudo sbctl enroll-keys --microsoft --firmware-builtin db,KEK
```

4. Stop on an error. Do not add `--yolo`, `--yes-this-might-brick-my-machine`,
   `--ignore-immutable` or disable sbctl's sandbox to bypass a refusal.
   Inspect the enrolled public certificates and state:

```sh
sudo sbctl list-enrolled-keys
sudo sbctl status
```

5. Confirm the intended owner and required vendor certificates are enrolled.
   Expect **Setup Mode: Disabled** after the owner PK is enrolled. Firmware may
   enable enforcement immediately; if it remains disabled, enable Secure Boot
   manually without restoring factory keys over the owner keys.
6. Boot the installed stable entry and run `sudo sbctl status` again. Require
   **Secure Boot: Enabled** and **Setup Mode: Disabled** in the running system.
   Repeat the signature checks and passphrase boot tests for stable, LTS and
   recovery, including the second ESP's entries. Do not add a git kernel yet.

If firmware refuses an image, stop. Use the established firmware recovery route
to return enforcement to its previous disabled state, then boot the tested
passphrase path and investigate. Do not reset the TPM, replace signing keys or
erase the installation. The live USB remains unqualified for Secure Boot even
after the installed host passes these checks.

Command semantics: [sbctl 0.18 manual](https://github.com/Foxboron/sbctl/blob/0.18/docs/sbctl.8.txt).
Check `pacman -Q sbctl` and the installed `sbctl enroll-keys --help` if versions differ.

<a id="6-enrol-the-yubikey"></a>

## 6. Enrol TPM2 with a PIN

Run this section on the **installed host**, after section 5 and passphrase-only
stable/LTS/recovery tests succeed. `BOOT_UNLOCK=tpm2-pin` configures the boot
policy; it does not enrol a TPM. Until enrolment, use your LUKS passphrase.
The helper adds an unlock slot; it does not replace your passphrase or enable
Secure Boot. Do not run it from the ISO or a chroot.

### 6.1. Confirm the TPM and the encrypted root device

```sh
sudo sbctl status
systemd-analyze has-tpm2
sudo systemd-cryptenroll --tpm2-device=list
findmnt -no SOURCE /
sudo cryptsetup status cryptroot
```

Use `crypt_root` instead of `cryptroot` for the home-lab example, or the mapper
name in your reviewed configuration. Require enabled Secure Boot, disabled
Setup Mode and complete TPM2 support. Confirm in firmware **and Linux device
discovery** that the selected chip is the physical SPI module, not AMD fTPM.
`auto` cannot establish this identity; stop if it is ambiguous or multiple TPMs
are available. Never clear/reset the TPM to make these checks pass.

In `cryptsetup status`, identify the underlying **device** for encrypted root.
The standard example uses `/dev/md/archroot`; home-lab uses `/dev/md0`. Do not
use an NVMe member, ESP or the decrypted `/dev/mapper/...` device. After matching
the device to your installed root and reviewed configuration, set:

```sh
LUKS_DEVICE='/dev/md/archroot'
LUKS_UUID=$(sudo cryptsetup luksUUID "$LUKS_DEVICE")
sudo cryptsetup luksDump "$LUKS_DEVICE"
```

Substitute your verified device. Require a nonempty, matching UUID and LUKS2
metadata. Inspect slots/tokens locally; do not paste full metadata into an agent.
If a `systemd-tpm2` token already exists, stop: the helper deliberately refuses
replacement. This is not a slot-wiping or automatic reenrolment procedure.

### 6.2. Establish recovery before adding a TPM slot

In your own unrecorded local terminal, add a separate recovery key if you do not
already have a tested one:

```sh
sudo systemd-cryptenroll --recovery-key "/dev/disk/by-uuid/$LUKS_UUID"
```

This command **prints a recovery secret**. Store it in your protected offline
recovery record, not shell variables, arguments, logs, Git, screenshots or Codex.
Test it by entering that recovery key at this command's local prompt:

```sh
sudo cryptsetup open --test-passphrase "/dev/disk/by-uuid/$LUKS_UUID"
```

Require exit 0. This test does not create another mapping; a later recovery boot
is still required. Keep the original passphrase slot. Reconnect your verified,
separately encrypted off-array backup storage and substitute its path:

```sh
BACKUP='/path/to/mounted/encrypted-offline-backup'
sudo test ! -e "$BACKUP/luks-before-tpm2.header" &&
  sudo cryptsetup luksHeaderBackup "/dev/disk/by-uuid/$LUKS_UUID" --header-backup-file "$BACKUP/luks-before-tpm2.header"
```

Verify that protected backup locally, then unmount and disconnect the storage.
LUKS headers are protected recovery material; do not send them to an agent.

### 6.3. Obtain the correct early-unlock PCR 11 prediction

The goal is to calculate a **boot fingerprint for the moment the encrypted disk
is unlocked**. It is not your PIN, passphrase, LUKS UUID or the checksum of the
whole UKI file. The result is one 64-character hexadecimal value, called
`EARLY_PCR11_SHA256` in the next section.

PCR 11 is a TPM register that changes as the boot image and later boot stages
are measured. Reading it after login normally gives a later value, not the value
needed for disk unlock. The policy still checks PCRs **0+7+11**: firmware,
Secure Boot policy, and the boot image/stage. Do not remove PCR 11 to skip this
step. Keep using your tested LUKS passphrase until TPM enrolment is qualified.

**Start with inspection, not enrolment.** At the installed host's local console,
run this read-only block. It selects the stable UKI; substitute the LTS path only
if that is the exact signed image you intend to cold-boot next:

<a id="tpm2-prediction-inspect"></a>

```sh
(
  set -e
  UKI=/efi/EFI/Linux/arch-linux.efi
  pacman -Q systemd systemd-ukify mkinitcpio
  sha256sum "$UKI"
  sudo journalctl -b -u systemd-pcrphase-initrd.service --no-pager -n 20
)
```

Expect three package versions, the selected UKI's checksum and up to 20 messages
from the current boot's initramfs PCR-phase service. This **does not calculate
the fingerprint**. A missing package, unreadable image, skipped service or
`No entries` needs investigation; none proves that this stage is absent from
the selected image. If you need help, share the version lines and these phase
messages only. Do not share PINs, passphrases, recovery keys or full boot logs.

The remaining calculation needs two verified inputs:

- The exact sections embedded in that signed UKI, not files from a newer kernel
  or a separately rebuilt initramfs.
- The boot stage that runs before root unlock. The upstream
  [initramfs phase service](https://github.com/systemd/systemd/blob/v261.2/units/systemd-pcrphase-initrd.service.in)
  records `enter-initrd` before cryptsetup, but its inclusion, activation and
  ordering must also be confirmed in **your selected image**. Its presence on
  the installed root filesystem or a journal success alone is insufficient.

Use [HOME-LAB's calculation procedure](HOME-LAB.md#predict-the-early-unlock-pcr-11)
to extract those sections, establish the phase and run `systemd-measure calculate`.
That reference includes a conditional six-section example; do not use it
unchanged if your UKI has additional measured sections. There is currently no
installer command that discovers and validates all these inputs for you.
The enrolment helper in 6.4 **accepts a fingerprint; it does not calculate one**.
If you cannot establish the image and phase, pause here for review. Secure Boot
can remain enabled and passphrase unlock remains available; do not reset the TPM
or enrol a guessed value.

After the image/phase checks and recovery-capable rehearsal pass, use the
calculation's `11:sha256=` result for that verified early phase. Copy **only the
64 hexadecimal characters after `=`**, not the prefix, UKI checksum or a
post-login PCR reading, into this variable in the same terminal used for 6.4:

```sh
EARLY_PCR11_SHA256='REPLACE_WITH_REVIEWED_64_HEX_DIGEST'
```

Replace the placeholder; it is deliberately not a usable digest. The digest is
not a secret, but it must belong to your reviewed image and phase. Setting this
variable makes no TPM or disk change. Continue to the **preview** in 6.4 only
when the prediction has been verified; accepting its syntax is not qualification.

### 6.4. Preview, then enrol in your local terminal

Use the installed bootstrap package's helper if present:

```sh
ENROLL_SCRIPT=/usr/lib/arch-workstation-bootstrap/infrastructure/host/tpm2-enroll.sh
test -r "$ENROLL_SCRIPT"
```

If it is absent, use `ENROLL_SCRIPT="$PWD/infrastructure/host/tpm2-enroll.sh"`
from the root of a full reviewed checkout. Do not download an unrelated script
or assume the narrower Bridge runtime contains it. Preview first:

<a id="tpm2-enrol-preview"></a>

```sh
bash "$ENROLL_SCRIPT" "$LUKS_UUID" "$EARLY_PCR11_SHA256"
```

Expect a printed `systemd-cryptenroll` command with `--tpm2-with-pin=yes`,
`--tpm2-pcrs=0:sha256+7:sha256+11:sha256=YOUR_DIGEST` and your exact LUKS UUID
path. **Preview performs no discovery or enrolment**; it checks argument syntax
and prints the plan. It does not validate the prediction, TPM, recovery or device.

Only after all preceding checks pass, run this yourself:

<a id="tpm2-enrol-execute"></a>

```sh
sudo bash "$ENROLL_SCRIPT" "$LUKS_UUID" "$EARLY_PCR11_SHA256" --execute
```

The helper checks root/Linux/local-terminal requirements, LUKS identity, existing
slots/tokens, TPM2 support and firmware Secure Boot/Setup Mode. It then asks for
`ENROLL RESOLVED_DEVICE LUKS_UUID`; type its exact displayed confirmation only
after checking the device. Your confirmation also attests the SPI identity,
tested recovery, header backup and reviewed PCR prediction: the helper cannot
prove those owner checks. Enter the existing unlock credential and new strong
alphanumeric PIN only at cryptenroll's local prompts. Do not add wipe flags.

On failure, stop and retain all recovery slots. On success, inspect the added
token locally and create a **new** protected header backup, using a distinct
filename such as `luks-after-tpm2.header`. Do not overwrite the earlier backup.

### 6.5. Cold-test and retain fallback access

Cold-test TPM+PIN on the exact enrolled UKI. Test passphrase and recovery-key
unlock on stable, LTS and recovery boot paths. Record which image satisfies the
TPM policy and which images require recovery; one static PCR 11 value does not
authorize every different UKI. Repeat the applicable tests after the git UKI is
accepted. Firmware, Secure Boot policy or UKI changes can invalidate the static
PCR binding; retain recovery access and plan manual reenrolment using the
canonical procedure. A signed UKI alone does not prove TPM unlock compatibility.

If TPM unlock fails, use the tested passphrase/recovery path. Do not repeatedly
guess the PIN: TPM dictionary-attack protection can lock it out. Do not wipe
slots, reset the TPM or rerun installation. Review changed firmware, Secure Boot
policy, UKI bytes and phase before planning a separate reenrolment.

A YubiKey is not required. Existing FIDO2 tokens remain optional legacy unlock
paths; this procedure neither enrols nor removes them. TPM PIN requirements do
not apply to the separate passphrase or recovery-key paths.

Also test authenticated rescue/emergency-console access from a disposable boot
or recovery drill. Successful LUKS unlock alone does not prove that `sulogin`
can authenticate the root recovery account.

## 7. Verify before optional layers

Complete [First-boot connectivity](#first-boot-connectivity) before checks that
need the network or remote access.

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

The installation media and first boot remain on generic signed stable/LTS
kernels. After those recovery paths pass, use the
[reviewed git-kernel procedure](WORKSTATION.md#reviewed-aur-and-git-kernel) to
capture the exact target hardware contract and build the separately pinned
experimental kernel. Profiling is an installed-host step; do not specialize the
ISO for the controller CPU or promote an unbooted candidate during installation.

## Bridge package and owner handoff

The base installation includes sensor and storage diagnostic tools, but does
not activate the telemetry stack. Complete the
[local monitoring readiness check](TELEMETRY.md#check-local-monitoring-readiness)
after the boot checks. Keep missing drivers, unreadable sensors and inactive
collectors distinct; package installation alone does not establish monitoring.

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
