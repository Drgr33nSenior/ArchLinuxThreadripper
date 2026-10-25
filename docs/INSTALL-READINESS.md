# Final installation acceptance

Use the reviewed source, package manifests and image digests. Record each gate's
command, exit status, candidate identity and result path. Keep credentials and
raw application data outside reports. The SGLang tuning campaign is separate;
do not interrupt its GPU owner.

## Before erasing the workstation

| Gate | Procedure | Acceptance |
| --- | --- | --- |
| Exact source | Run `make check-strict` with the [prepared toolchain](ISO-REFERENCE.md#verification-and-maintenance) | No failures or unexpected skips; record permitted image/Linux exceptions |
| Native gateway/database | Run `bash tests/service/native-smoke.sh` on an explicitly selected local disposable Docker engine, or inspect hosted CI for this revision | Real keys, expiry, isolation, limits, streaming, tools, cancellation and restart persistence pass; the inference backend is synthetic |
| Package/export closure | Follow [Bridge package acceptance](ISO-REFERENCE.md#offline-bridge-acceptance) | Signed packages contain the matching source/runtime; disconnected install and tamper refusal pass |
| Installer and recovery | Follow [two-disk UEFI acceptance](ISO-REFERENCE.md#4-disposable-uefi-installation-tests) and its recovery procedure | Install, stable/LTS/fallback boot, signed update and recovery pass on disposable file-backed disks |
| Backup destinations | Follow [service recovery](SERVICE-RECOVERY.md), including gaming homes | Restore the same explicit snapshot independently from SSD and S3; prove application usability and independent credentials |
| Tuned inference handoff | Compare the final serving selection with `versions.lock`, the selected overlay and built image manifest | Package and requalify selected patches, native dependencies, model revisions, draft/KV precision and runtime settings |

The native container CI job uses disposable hosted runners and generated fixture
credentials. It receives no workstation kubeconfig, GPU, NAS or production secret.
Its artifacts contain fixed phase/exit evidence and allowlisted container state;
raw startup/migration logs and fixture credentials are not published.

## Before enabling family use

1. Run `workstationctl hardware collect` and derive `resources plan` from that
   collection. Verify actual memory, trained DIMM speed, disk identities and GPUs.
   A planned 128 GB upgrade is not a measurement.
2. Complete installed-system verification and the owner-run Secure Boot,
   passphrase recovery, TPM+PIN and stable/LTS/recovery boot procedures in
   [installation](INSTALLATION.md). VM acceptance does not qualify physical paths.
3. Follow [service target acceptance](AI-SERVICE.md#notifications-and-target-acceptance).
   Test browser login and retained chats for two users, a revocable IDE key,
   actual HTTPS streaming/disconnects, WebSockets and denied lateral access.
4. Ingest a document and retrieve it through the browser and real IDE MCP client.
   Check citations, another user's denial, deletion and no-evidence behavior.
   Run the existing RAG evaluation against the deployed model.
5. Build and qualify the creative runtime. Prove an image survives restart and
   gives a bounded response when unavailable. Split mode currently fails CPU
   admission; qualify a measured profile adjustment before enabling it. Do not
   reduce limits solely to pass a plan.
6. Complete the Sunshine checks below and both directions of GPU handover.
7. Test owner-selected notification delivery and the independent observer from
   another device. Intentional maintenance must not report an inference outage.
   Monitor local backup freshness separately from off-site replication freshness.
8. Restore login, conversation, source/retrieval, image, gateway key policy,
   Moonlight pairing and a saved game into an isolated environment. Record real
   recovery as `NOT RUN` until exercised.

## Sunshine and Moonlight

Use [Sunshine](SUNSHINE.md) for the pinned image and measurement commands. The
disabled candidate uses a 1920x1080, 60 Hz headless KWin session. High refresh and
HDR require separate qualification.

- Build the image and run `tests/test_sunshine_image.sh` and
  `tests/test_wayland_processes.sh` with reviewed local image IDs.
- Prove rendering, capture and hardware encoding use the allocated GPU. Record
  the actual Moonlight codec, bitrate, frame cadence, encode/decode latency,
  frame-time tails, drops and audio.
- Demonstrate keyboard/mouse in XWayland and native Wayland windows, plus each
  controller type. The virtual KWin input route remains unresolved; device
  access does not prove interactive control. Preserve consent gates.
- Prepare private TCP/UDP streaming exposure and firewall policy for each owner.
  The checked-in ClusterIP Service does not provide complete LAN access. Test
  from the real client without exposing Sunshine administration publicly.
- Verify graceful game exit, disconnect/reconnect, restart, retained pairing and
  a saved game. Exercise AI to gaming to AI through the session CLI and confirm
  actual GPU release. Concurrent AI/gaming is not qualified.
- Restore each included gaming home from both destinations. Keep local saves
  and Proton prefixes; exclude only content proven safe to download again.

Keep gaming replicas zero until input, private access, GPU ownership and recovery
pass. Keep images disabled until their creative profile passes. Do not add broad
device/capability access to make a candidate start. Publishing source does not
enable either workload.

## Evidence and remaining inputs

Use `source-validated`, `integration-tested`, `hardware-validated`, `deployed` or
`NOT RUN`, with the exact scope. Preserve failures and meaningful skips.
Collect the actual backup SSD mountpoint/filesystem UUID, approved target/context,
private DNS/TLS/CIDRs, S3 region/bucket, protected credential file locations,
notification destination and observer device. Never put credential values in Git.

Keep independent backups and known-good signed recovery packages/media outside
the RAID array. Exclude the backup SSD from installer targets. Retain the existing
owner disk-identity/erase handoff; this checklist does not authorize destructive
installation or firmware changes.
