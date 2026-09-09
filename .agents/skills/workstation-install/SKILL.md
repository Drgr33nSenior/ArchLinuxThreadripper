---
name: workstation-install
description: Prepare and verify ArchLinuxThreadripper installation media, plan a workstation rebuild, guide installation and first boot, diagnose interrupted installation stages, or verify an installed workstation. Use the existing installer and owner-run handoffs; not for general workload deployment or performance tuning.
---

# Workstation installation

Guide the owner through the existing workflow. Do not create another installer
or resume engine. Skill invocation does not authorize destructive operations.

## Read the current sources

Resolve the repository root as `../../..` from this directory; run the commands
below from that root. Read applicable agent instructions, including the
[guardrails](../../../.aiassistant/rules/workstation-guardrails.md), then:

On the custom ISO, that root is the signed package tree, not a Git checkout.
Read [console setup](../../../docs/INSTALLATION.md#optional-codex-authentication) for owner-local Wi-Fi/login
and first-boot reconnection. Use `BUILD-IDENTITY` and `SOURCE-MANIFEST.sha256`
when Git metadata is absent; never manufacture a revision. Login is an owner
handoff outside agent tools. Never read or export its private tmpfs session.
Controller-side media building needs the full checkout and its build tools;
do not attempt those stages from the bootstrap-only live package tree.

Read [Bridge ISO handoff](../../../docs/ISO.md#1a-bundle-bridge-unless-explicitly-opting-out) for the default-on target
payload. Review `INSTALL_BRIDGE` explicitly: omission means true, not disabled.
Before signing, seal the selected Bridge candidate against this run's frozen
installer source; retain package/source/dependency and repository identities.
Preflight/dry-run must pass the Bridge signature and offline-closure gates before
the owner installation handoff. Never install Bridge into the live root merely
to transport its archive. Missing/stale bundles block enabled installation.

- Always: [installation](../../../docs/INSTALLATION.md),
  [security](../../../docs/SECURITY.md), [operations/recovery](../../../docs/OPERATIONS.md),
  [installer CLI](../../../bin/bootstrap-arch) and its [implementation](../../../lib/bootstrap).
- Media or interrupted builds: [ISO runbook](../../../docs/ISO.md),
  [ISO reference and disposable recovery tests](../../../docs/ISO-REFERENCE.md),
  [release adapter](../../../infrastructure/iso/release.sh) and
  [USB writer](../../../infrastructure/iso/usb.sh).
- Configuration: [install example](../../../config/install.conf.example), or the
  explicit [home-lab example](../../../infrastructure/host/install.conf.example)
  with [home-lab unlock/recovery guidance](../../../docs/HOME-LAB.md).
  [Workstation configuration](../../../config/workstation.conf.example) belongs
  to the separate [post-install CLI workflow](../../../docs/WORKSTATION.md), not the installer.
- Version policy: [root lock](../../../versions.lock) and
  [ISO lock](../../../infrastructure/iso/versions.lock). Do not copy pins or
  hardware identities into this skill or silently update them during recovery.

Read the relevant runbook sections completely. Recheck commands against the
current source before use; stop and explain any source/runbook disagreement.

## Establish context and continuity

1. Identify Mac/controller, live Arch ISO, or installed workstation. Record the
   checkout path, revision and dirty state; HEAD alone does not identify local
   changes. Select the owner's literal install configuration; do not source it.
   Keep credentials out of configuration. Review only supported nonsecret keys.
2. Establish the actual machine/board identity, disk identities, boot mode,
   connectivity, time and tools. Expected inventory is not discovery. The Mac
   builds/prepares media; installer preflight and dry-run require root on an
   Arch live ISO with UEFI. Installed verification requires root and UEFI.
   Missing tools or inaccessible target state are blockers, not inferred passes.
   Preflight now checks configured mirrors, DNS/routing and time/TLS. Codex
   reachability, stored login and successful model access are separate checks.
3. Agree on an owner-controlled private journal on the controller or separate
   external storage, outside Git, the target disks and any USB being overwritten.
   Use a private directory/file (0700/0600 on POSIX), not temporary live-ISO RAM.
   Reuse existing build run records; do not create another build-state database.

Keep a small Markdown journal with these fields:

```text
Run ID; controller/journal location; target identity; last checked time
Repository revision; dirty-source manifest/reference; lock references
Reviewed nonsecret config path and SHA-256
ISO_RUN; package/source manifests; signing public fingerprint; ISO attempt/hash
Stage | verified/failed/blocked/untested | command and exit | scope/evidence
Failure summary; pending owner handoff; next safe action
```

Hash only reviewed nonsecret configuration. Keep artifact references and
sanitized check summaries, not raw transcripts. Never collect passwords, PINs,
private keys, tokens, recovery codes or LUKS headers in prompts or journals.
Follow local terminal entry and encrypted offline backup procedures in SECURITY.
Record only that a protected backup was completed. If output might contain
secrets (including malformed configuration echoed by a parser), have the owner
inspect it locally and supply a sanitized result; do not ingest it first and
then redact it. If exposure occurs, stop collection and report without repeating it.

The skill supplies neither SSH access nor persistent sessions. Use local console
access until the owner has established approved named-user remote access and
verified host identity. Live media do not automatically enable SSH. No Kubernetes
or Bridge is required. After disconnect, an operation's outcome is unknown until
checked; do not assume it stopped.

## Prepare and verify media

Follow ISO.md's package-build, owner-signing and assembly stages in order.
`bash infrastructure/iso/release.sh packages` is a preview. Use its documented
execution form only within existing build authorization and the verified Docker
context. Record the actual successful `ISO_RUN`; never select a run just because
its timestamp is newest. Retain its source snapshot, matching source locks,
packages, checksums and logs. Do not specialize media for the controller CPU.

Hand signing to the owner through local GPG/pinentry using ISO.md's exact commands.
Verify all required package and repository signatures and the expected public
fingerprint. A checksum alone is not signer trust. Partial signing is a blocked
stage: inspect existing signatures; do not delete or overwrite them to retry.

Use `bash infrastructure/iso/release.sh iso "$ISO_RUN" "$PUBLIC_KEY" "$FINGERPRINT"`
to preview the selected recorded run. These variables must refer to reviewed
artifacts, not private key material. Privileged ISO assembly is an owner handoff
using the documented `--execute --allow-iso-mounts` form. Respect retry boundaries:
reuse compatible signed packages for a new assembly attempt; source/package
changes require a newly reviewed build/signing cycle, not silent reuse. Never
rebuild or resign merely to continue a conversation. Preserve failed evidence.

Check final ISO checksums, manifests and owner-reviewed signature/trust evidence;
retain disposable UEFI test results separately. Signed packages do not establish
Secure Boot support for the live ISO. For official Arch media, follow INSTALLATION
signature checks and separately prepare the trusted signed boot package.

On the Mac, `bash infrastructure/iso/usb.sh list` lists candidates. Preview with
`bash infrastructure/iso/usb.sh write "$ISO_FILE" "$USB_DISK"`. Present the exact
whole-disk identity, capacity and ISO checksum. The owner alone runs the documented
`sudo bash infrastructure/iso/usb.sh --execute write "$ISO_FILE" "$USB_DISK"`
and types its exact confirmation. Keep the ISO/journal off that disk. Require
successful write/readback verification; report eject failure separately. On
failure, preserve the unverified status and follow ISO.md's retry/lock guidance;
never force-unmount, clear a lock blindly or substitute an unguarded writer.

## Plan and hand off installation

On the target live Arch ISO, ask the owner to run these with the reviewed config
path substituted consistently (the example is `config/install.conf`):

```sh
sudo ./bin/bootstrap-arch --config config/install.conf preflight
sudo ./bin/bootstrap-arch --config config/install.conf --dry-run install
```

Before the installation handoff, present both persistent by-id paths, resolved
whole devices, exact serials, capacities and active/mounted state. Present the
two independent ESPs and mdadm RAID0 → LUKS2 → XFS layout from the current plan,
with no swap. List selected host/GPU packages, host/TuneD profile, unlock policy,
optional services and all unresolved prerequisites. Confirm off-array backups
and a usable recovery route for existing data before erasure. Unknown/mismatched
identity, failed preflight or an incomplete plan blocks the handoff.

Dry-run does **not** verify the boot package signature. Check artifact availability
and owner trust evidence separately. Execute mode verifies the signed package
against the live keyring before its erase confirmation. Never use execute mode
just as a signature probe. On official media include `--boot-package FILE` with
the exact signed package path and adjacent signature; no trust bypass is allowed.

After prerequisites are satisfied, give the owner the documented command:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf --execute install
```

Do not run it as the agent. Preserve the exact-device confirmation and interactive
LUKS/user/root-recovery prompts. Do not add automatic answers or record that
terminal session. Before reboot, complete the runbook's protected offline LUKS
header backup and separate owner-key backup. Follow INSTALLATION's manual
firmware-key export, UKI signature checks, key enrolment and first-boot order.
Keep passphrase recovery; qualify stable/LTS/recovery boots before optional
FIDO2 enrolment. For `BOOT_UNLOCK=tpm2-pin`, use HOME-LAB's explicit owner-run
TPM/PCR procedure; do not infer enrolment from configuration or enrol from the ISO.
Firmware, key changes and reboots are owner handoffs, never automatic steps.

## Reconnect, diagnose and verify

After any interruption or reboot, reread the journal and check actual target,
revision/local changes, config fingerprint and artifact identity. If any changed,
refresh the plan and affected evidence. Repeat applicable preflight only in its
supported live environment with an unused target; do not dismantle an existing
array or mapping to make preflight pass.

There is no installer resume command. An interrupted install may already have
erased disks, created keys, installed packages or added firmware entries. Do not
rerun `install`, including execute mode, based on a journal's last stage. Use the
runbooks' recovery inspection, with owner-run read-only assembly/unlock/mount
where required. Dirty XFS may need a disposable copy for recovery testing.
Stop at a clear owner intervention or installer-support boundary when safe
continuation is not established. Do not invent repair flags or apply VM identity
exceptions to physical hardware.

On the installed target, use the documented owner-run check:

```sh
sudo ./bin/bootstrap-arch --config config/install.conf verify
```

The optional signed bootstrap package supports the same skill after first boot,
with local NetworkManager reconnection and fresh launcher authentication. The
post-install `workstationctl status` command uses the installed Bridge runtime
or a full reviewed checkout; do not run post-install actions in the live ISO.
`verify` checks the running installed root, not a live
ISO's mounted `/mnt`. Follow INSTALLATION's remaining storage, no-swap, signatures,
unlock and cold-boot checks. Preserve known-good stable/LTS/recovery media before
updates, tuning or optional workloads; follow OPERATIONS for later maintenance.

Report every stage as verified, failed, blocked or untested with evidence scope.
After first boot, follow INSTALLATION's Bridge ownership/version, runtime/reference hash,
catalog and inactive-unit checks. Hand actual UID/GID discovery, policy review,
private TLS/address selection, local owner-token creation and service activation
to the owner. Package hashes do not authorize system executables or qualify GPU
operations. Keep Codex credentials and journals separate from Bridge state.
A zero exit verifies only that command's checks. Fixture success, package
signatures and filesystem verification do not prove physical boot/recovery,
firmware trust, performance or optional workloads. Failed/skipped/missing checks
cannot become successful stages. Finish with pending checks and the next safe
owner action; do not claim a working recovery baseline until it was tested.
