# Installation runbook

## Codex installation guidance

For Codex directly at the custom ISO console, follow [CODEX-INSTALL.md](CODEX-INSTALL.md):
connect with iwctl first, then choose API-key or device-code authentication through
the packaged launcher. It also covers first-boot NetworkManager reconnection and
optional installed-host continuation. Credentials are never carried into the target.

The repository-scoped [workstation-install skill](../.agents/skills/workstation-install/SKILL.md)
uses this runbook and the existing installer. With this checkout open in Codex:

```text
$workstation-install prepare an ISO from this checkout
$workstation-install plan my rebuild using config/install.conf
$workstation-install investigate my interrupted installation using my private controller journal
$workstation-install verify the installed workstation using config/install.conf
```

The skill keeps a small private journal on the controller or separate external
storage, outside Git and the target disks. It supplies no SSH access or reboot
continuity by itself. Disk writes, installation, key enrolment and reboots remain
owner-run handoffs. It does not resume a partial installation automatically or
treat a dry run as boot/recovery qualification.

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

1. Download a current Arch ISO from an official mirror.
2. Verify its checksum and signature from a separate trusted machine.
3. Copy this repository to separate media or retrieve a reviewed commit.
4. Boot the ISO in native UEFI mode.
5. Establish time synchronization and a trusted network path.

Alternatively, build and qualify the [project ISO](ISO.md). New installations
require the signed `arch-workstation-boot` package and a trusted signing key in
the live keyring before erasure. The project ISO bundles that package. On an
official ISO, pass its exact path with `--boot-package FILE`; do not disable
signature checks. The package owns the UKI sync helper and pacman hook.

Do not enrol custom Secure Boot keys or erase disks until the preflight report
matches the recorded hardware.

## 3. Configure and inspect

Copy `config/install.conf.example` to an ignored `config/install.conf`. Replace
the disk paths with complete `/dev/disk/by-id/...` values and copy the exact
serials reported by `udevadm` or `lsblk`. Do not use `/dev/nvme0n1` names.

Keep `HOST_PROFILE=headless` for an AI/remote-gaming server. This omits GNOME,
GDM and host gaming packages, keeps the multi-user target, and selects TuneD's
balanced policy. `HOST_PROFILE=desktop` is an explicit new-install alternative.
An older configuration without this key also defaults to headless. `ENABLE_SSH`
remains a separate opt-in; establish named-user access before relying on it.

Run:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf preflight
sudo ./bin/bootstrap-arch --config config/install.conf --dry-run install
```

Confirm that the plan creates two 2 GiB ESPs and uses only the remaining two
partitions as RAID0 members. Confirm this storage order:

```text
mdadm RAID0 → LUKS2 → XFS
```

Stop if either disk is mounted, active in another storage stack, has an
unexpected size or serial, or resolves to the same device.

## 4. Install

The install action defaults to a dry run. Run the real installation only from
the live ISO with the explicit execution flag:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf --execute install
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

Use `workstationctl encryption calibrate config/install.conf artifacts/argon2-01`
on the target before installation to measure the Argon2id unlock budget. The
benchmark opens no device. Installation recalibrates with the selected limits
and records actual time/memory/CPU costs in `/etc/cryptsetup/argon2id-parameters.json`.
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

Run the boot-artifact verification and record its output without secrets:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf verify
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
