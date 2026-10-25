# Bare-metal shared-service recovery

This procedure covers the single-node K3s **SQLite** cluster, not the optional
embedded-etcd AlmaLinux VM lab. K3s requires the matching server token to decrypt
bootstrap data; retaining a database without that token is incomplete recovery.
[K3s backup/restore](https://docs.k3s.io/datastore/backup-restore).

## Scope and consistency

`service-backup capture` takes the existing canonical session lock for the whole
operation, verifies target/boot/identity, and calls the existing maintenance path
with its inherited descriptor. It closes model admission and stops managed GPUs.
It records existing gaming deployment identities before maintenance, then scales
gaming, WebUI, tools and gateway to zero with resourceVersion guards and waits
for their Pods to exit. It captures PostgreSQL with `pg_dump -Fc` and protected
global-role SQL, then stops PostgreSQL. Both Kubernetes and CRI must report no
application containers before the PVC copy. It then stops K3s before capturing
server state, SQLite, matching token and configuration. **Stopping K3s alone does
not stop workload containers.** An active/unmanaged application Pod makes capture
fail rather than becoming an arbitrary live filesystem copy.

The protected capture includes:

- K3s server tree (TLS/bootstrap/encryption configuration and datastore), a
  standalone SQLite backup, matching token, `/etc/rancher/k3s`, K3s unit/environment
  and workstation policy configuration.
- WebUI accounts, chats, uploads and embedded retrieval state; Swarm Data/Output;
  unresolved image-job state; PostgreSQL database and role dumps. Gateway
  configuration/keys reside in matching K3s Secrets and PostgreSQL.
- Every present `gaming-home`, `parent-gaming-home` and `kids-gaming-home` claim,
  including Sunshine pairing/configuration, Steam user data, local saves and
  Proton prefixes. The complete home is retained initially. Unknown gaming-home
  claims or unmanaged writers are refused instead of silently omitted.
- Model revision lock, actual staged inference file inventories, the RAG
  `SHA256SUMS`, pinned creative asset checksum, and per-PVC UID/PV/source-path journal.
  Creative model weights, SGLang weights/caches and the RAG model PVC are excluded.
  Restage them at the pinned identities before usable retrieval/inference tests.

Before first service rollout, set `scope` to `baseline` in the protected backup
policy. This uses the same quiescence, SQLite/token and encrypted upload path for
existing bound WebUI/image state, before gateway/tools/database resources exist.
It refuses those new resources rather than omitting their data. Its manifest
explicitly records `scope: baseline` and the captured claims; this is not a full
service restore. Pending claims contain no bound application data. After service
initialization, change the policy to `scope: service` and use a fresh staging path.
The full capture requires all four valuable claims to be bound, including an
initialized image/output claim even when the GPU workload remains disabled.

Capture stays mode 0700 with mode-0600 files. The default remote-only mode uploads with existing systemd
Restic credentials and tag `workstation-service`, accepting remote SFTP, HTTPS S3
or HTTPS Restic REST storage. A local RAID0 path is refused. A NAS can supply one
of those remote protocols. Restic encrypts repository content; maintain an
independent encrypted copy of repository/password/provider credentials that does
not depend on the workstation's TPM or failed array. Host Restic excludes its
credential stores and does not replace this independent recovery copy.

## Independent backup SSD, then S3

The optional local-first mode commits an encrypted Restic snapshot to a separate
SSD. It includes the reviewed installed host include/exclude policy alongside the
consistent service capture. Arbitrary open user files do not become transactional
snapshots: save work and stop games before capture. A successful local backup does
not establish off-site success. S3 replication is a separate owner-started operation
after explicit service resume; it does not acquire the host session lock or stop
workloads again.

1. Prepare the SSD through the owner storage workflow. Keep it outside the RAID
   and the installer's target disks. Record its actual mountpoint, filesystem UUID
   and persistent disk identity; do not use example identities. Mount a supported
   local filesystem and use root-owned directories without group/other write
   access. Keep space for retention and the capture staging files. A 4 TB SSD is
   approximately 3.64 TiB before filesystem overhead; capacity is not qualification.
2. In the private backup policy, set `local_repository` to an object containing
   `mountpoint`, `filesystem_uuid` and `repository` (a directory below that mount).
   `null` or an absent key preserves remote-only behavior. The local-first unit
   refuses a missing policy instead of accidentally uploading while services wait.
   Checks before maintenance reject a missing/wrong mount, symlink, RAID/LVM
   backing, rotational disk, or any physical disk shared with root, K3s or staging.
3. Install and review `/etc/restic/workstation.include` and `.exclude` from the
   signed backup package. Local capture requires `/etc`, `/home` and
   `/var/lib/sbctl`. It refuses include paths that encompass live K3s, capture
   staging or the backup repository. Keep existing credential/cache exclusions.
   The gaming PVC archives preserve saves even though host Steam caches are excluded.
4. Initialize new local and S3 repositories using protected password files. Set
   `LOCAL_REPOSITORY` to the verified mounted SSD repository and use new protected
   file paths. Verify the drive before creating a repository; never initialize a
   directory on the root filesystem because the SSD is absent:

   ```sh
   sudo restic --repo "$LOCAL_REPOSITORY" --password-file /private/recovery/local-password init
   sudo env AWS_SHARED_CREDENTIALS_FILE=/private/recovery/aws AWS_EC2_METADATA_DISABLED=true \
     restic --repository-file /private/recovery/s3-repository --password-file /private/recovery/s3-password \
     init --from-repo "$LOCAL_REPOSITORY" --from-password-file /private/recovery/local-password --copy-chunker-params
   ```

   These commands target **new, empty repositories** only. Existing S3 repositories
   with different chunker parameters require a separately planned migration; the
   replication command refuses the mismatch. Restic encrypts both repositories.
   Keep independent encrypted recovery copies of both passwords and AWS access.
   [Restic repository-copy requirements](https://restic.readthedocs.io/en/stable/045_working_with_repos.html#copying-snapshots-between-repositories).
5. Protect the local unit credential through the existing owner credential workflow:

   ```sh
   sudo systemd-creds encrypt --name=restic_local_password /private/recovery/local-password /etc/credstore.encrypted/workstation-restic-local-password.cred
   ```

   Use the existing remote repository/password/AWS credentials for replication.
   Do not put values into shell arguments or reports. Prepare the host telemetry
   directory before capture. Reconcile unresolved image jobs and gracefully exit
   games; do not rely on a forced Pod exit to save game progress.
6. Set the policy's `staging` to a new safe basename below
   `/var/lib/workstation-service-backup`, then run:

   ```sh
   sudo systemctl start workstation-service-backup-local.service
   sudo systemctl show workstation-service-backup-local.service -p Result -p ExecMainStatus
   sudo ./bin/workstationctl service-backup verify "$CAPTURE_DIRECTORY"
   ```

   Success records `local-backup.json`, binding the exact completed snapshot ID
   to the capture manifest, host paths and SSD identity. It releases the session
   lock and leaves services stopped for the verified resume procedure below.
   A failure preserves evidence; inspect actual state and the journal before resume.
7. Resume K3s, database and applications using recorded UIDs and original replicas.
   Restore GPUs only through the qualified session path. Verify HTTPS and then
   set `CAPTURE_NAME` to that capture directory's safe basename:

   ```sh
   sudo systemctl start "workstation-service-backup-replicate@${CAPTURE_NAME}.service"
   sudo systemctl show "workstation-service-backup-replicate@${CAPTURE_NAME}.service" -p Result -p ExecMainStatus
   ```

   The unit invokes `workstationctl service-backup replicate <policy> <capture>
   --execute` through the installed Python implementation. It verifies the local
   snapshot's scope and embedded manifest, then copies that exact ID and verifies
   the destination snapshot's scope and manifest. `offsite-backup.json` records
   the actual destination ID separately from `source_snapshot_id`. Network
   failure keeps the valid local snapshot and staging record; retry the same
   instance. It neither recaptures live data nor initializes/prunes repositories.
8. Check local and off-site freshness independently. Metrics use
   `workstation_service_backup_local_*` and `workstation_service_backup_offsite_*`;
   legacy success/failure metrics remain off-site aliases. Local alerts apply
   only when `workstation_service_backup_local_enabled` is one. No disruptive
   capture or replication timer is enabled automatically.

   Freshness is the verified capture's recovery-point time, not upload completion.
   Repeating an old successful copy cannot refresh a stale backup or replace a
   newer recovery point. The systemd success hook does not advance freshness;
   its failure hook preserves that time and reports the failed operation.

Use Restic snapshot retention independently in each repository. Deleting a local
snapshot does not automatically delete its S3 copy. Never mirror directory deletions
or use object-age expiration to remove active repository objects. Verify bucket
versioning/deletion protection against repository maintenance before enabling it.
Keep capture staging until S3 copy and both restore drills pass; it can contain
large complete gaming homes. Record total repository growth, download cost and
recovery time. Local and S3 snapshot IDs may differ: select each destination's
explicit ID from its own snapshot listing and verify the recovered manifest.

## Retain existing state before rollout

For each valuable bound claim (`webui-rag-qwen3-data`, `creative-models`,
`service-tools-state`, `gateway-postgres` and all present gaming-home claims), inspect only PVC/PV metadata using the
explicit operator context. Confirm namespace, claim UID, node and backing path:

```sh
kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab get pvc "$CLAIM" -o json
kubectl --context "$SERVICE_CONTEXT" get pv "$PV" -o json
kubectl --context "$SERVICE_CONTEXT" patch pv "$PV" --type merge -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
kubectl --context "$SERVICE_CONTEXT" get pv "$PV" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}'
```

The expected final policy is Retain. Do not recreate a populated PVC to change
`storageClassName`. New claims use `workstation-retain`; existing PVs require the
explicit patch above. PVC requests are advisory with local-path and do not cap
disk usage. Preserve at least the existing filesystem reserve plus space for a
complete staged capture. Monitor XFS capacity and backup size before ingestion.

Retention proposal: 7 daily, 5 weekly and 12 monthly successful service captures,
plus a manually retained snapshot for each proven restore/major upgrade. Review
actual growth and recovery objectives. Use `restic forget --tag workstation-service
--group-by host,tags --keep-daily 7 --keep-weekly 5 --keep-monthly 12 --dry-run` with
protected repository/password file options first. Do not group by timestamped
capture paths. No new prune timer is enabled; pruning requires owner review after
a successful recovery drill and an independently retained recovery snapshot.

## Owner-run capture and resume

1. Install the reviewed source/runtime and optional backup unit. Confirm Python,
   kubectl, K3s/CRI, Restic and current hardware evidence. Prepare the existing
   telemetry textfile directory. Fill `config/service-backup.example.json` outside
   Git and install it as `/etc/workstation/service-backup.json`, root:root 0600.
   Its target/kubeconfig/state directory must exactly match the canonical session
   policy. Use a **new** path under `/var/lib/workstation-service-backup` for staging;
   create only the parent root:root 0700. Each capture has a new path.
   For each materialized inference model that is staged, collect its file hashes
   into a fresh protected path and list that path in `model_inventories`:

   ```sh
   sudo ./bin/workstationctl service-backup inventory-model Qwen3.8-27B-FP8 "$MATERIALIZED_LARGE_MODEL" /var/lib/workstation-evidence/large-model-inventory.json
   sudo ./bin/workstationctl service-backup inventory-model Qwen3.5-9B "$MATERIALIZED_SMALL_MODEL" /var/lib/workstation-evidence/small-model-inventory.json
   ```

   Run only the command for models actually staged. Use the model's materialized
   revision directory, not a symlinked Hugging Face cache. Inventory collection
   reads all weight bytes and can take time; repeat after changing staged files.
   The capture verifies pinned identities and preserves these hashes. It does not
   rehash excluded weights on every backup or claim their quality is qualified.
2. Ensure all required PVs are Retain and that PostgreSQL is Running. Resolve
   pending Jobs/other application Pods first. Run the independent observer's
   bounded planned-maintenance window before the whole cluster stops.
   For `baseline` scope, PostgreSQL/gateway/tools must not exist. Existing WebUI
   and image PVs alone are captured; retain and verify their bound PVs first.
3. Start the owner-run unit; there is deliberately no disruptive automatic timer:

   ```sh
   sudo systemctl start workstation-service-backup.service
   sudo systemctl show workstation-service-backup.service -p Result -p ExecMainStatus
   sudo ./bin/workstationctl service-backup verify "$CAPTURE_DIRECTORY"
   ```

   Expected: Result=success, consistent capture validated, encrypted upload
   completed. `workstation_service_backup_last_success_seconds` updates only after
   upload; failure sets a fixed-label metric. A successful hash/SQLite check is
   **not** a disaster-recovery qualification.
4. On success or failure, retain the protected journal. The command leaves K3s and
   application writers stopped; it does not automatically restore inference or
   resume an unresolved image. Start K3s, verify this node Ready, and inspect the
   journal's deployment UIDs against live objects. Resume the original PostgreSQL
   replica first; wait Ready; then the original gateway, WebUI and tools replicas.
   Never blindly scale a replacement UID. Use `session restore ... --execute` or
   an explicitly selected qualified mode for GPUs after database/app readiness.
   Recheck HTTPS and remove the external maintenance window. If capture failed
   before stopping K3s, inspect actual state rather than assuming that every step ran.

Gateway migration rollback uses a fresh database restored from the pre-migration
dump. PostgreSQL 18.6's `pg_dump`/`pg_restore` are supplied by the selected database
image; use that same major for recovery. [PostgreSQL dump guidance](https://www.postgresql.org/docs/current/backup-dump.html).

## Isolated restore drill

Use a new disposable VM/device with an isolated network and a **different** cluster
context, no route to production APIs/PVCs, and no public ingress. Do not run the
following on the active workstation. Prepare matching K3s version and the previous
reviewed manifests/images. A CPU-only drill can validate accounts, data and keys;
GPU image generation still requires separate target qualification.

1. On the isolated device, unlock the off-array repository with the independent
   recovery copy, not `/etc/credstore.encrypted` from the failed machine. Run Restic
   check and restore the **explicit snapshot ID** into a fresh protected directory:

   ```sh
   restic --repository-file /recovery/credentials/repository --password-file /recovery/credentials/password check
   restic --repository-file /recovery/credentials/repository --password-file /recovery/credentials/password restore "$SNAPSHOT_ID" --target /recovery/new-restic-stage
   ./bin/workstationctl service-backup restore-stage /recovery/new-restic-stage/var/lib/workstation-service-backup/<capture-name> /recovery/verified-capture
   ```

   For S3, supply the independent AWS credential file through
   `AWS_SHARED_CREDENTIALS_FILE`, not command arguments. The staging command refuses
   existing destinations and overlap with active K3s, kubelet, `/etc` or `/run`.
   It never extracts archives onto an active cluster or copies live PVCs.
2. Confirm this is the new isolated VM, with no running K3s service, no route to
   the workstation and no shared active storage. Inspect archive member names
   locally, then extract only into a **new** recovery directory:

   ```sh
   umask 077
   test ! -e /recovery/disposable-k3s
   mkdir -m 700 /recovery/disposable-k3s
   mkdir -m 700 /recovery/disposable-k3s/server
   tar -tf /recovery/verified-capture/server.tar > /recovery/server-members.txt
   tar -xf /recovery/verified-capture/server.tar -C /recovery/disposable-k3s/server
   install -m 600 /recovery/verified-capture/state.db /recovery/disposable-k3s/server/db/state.db
   rm -f -- /recovery/disposable-k3s/server/db/state.db-wal /recovery/disposable-k3s/server/db/state.db-shm
   printf '{}\n' > /recovery/empty-config.yaml
   ```

   The removed WAL/SHM files belong only to this disposable copy; the standalone
   database already includes its committed WAL data. Keep original archives and
   `config.tar` intact. Do not install the captured host unit or network config.
3. With the **captured K3s version**, run a foreground server in one terminal.
   Agentless mode prevents restored workloads from starting during inspection:

   ```sh
   sudo k3s server --config /recovery/empty-config.yaml --data-dir /recovery/disposable-k3s \
     --token-file /recovery/verified-capture/server-token --disable-agent \
     --bind-address 127.0.0.1 --https-listen-port 16443 --advertise-address 127.0.0.1 \
     --tls-san localhost --node-name restore-isolated --flannel-backend none \
     --disable coredns --disable servicelb --disable traefik --disable local-storage --disable metrics-server \
     --secrets-encryption --write-kubeconfig /recovery/disposable.kubeconfig --write-kubeconfig-mode 600
   ```

   In a second terminal, use **only** this kubeconfig:

   ```sh
   sudo kubectl --kubeconfig /recovery/disposable.kubeconfig config rename-context default service-restore-isolated
   sudo kubectl --kubeconfig /recovery/disposable.kubeconfig --context service-restore-isolated -n ai-home-lab get deployments -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas
   ```

   Expected: bootstrap decrypts and captured application replicas are zero. Stop
   the disposable foreground server if any application replica is nonzero. Repeat
   a **separate disposable copy** with a deliberately mismatched synthetic token;
   startup must fail. Do not change the successful copy or active token to test this.
   Agentless mode is an experimental K3s inspection mode, not the service's normal
   deployment. Its flags were checked against the selected
   [v1.35.8+k3s1 CLI source](https://github.com/k3s-io/k3s/blob/v1.35.8%2Bk3s1/pkg/cli/cmds/server.go).
4. After the agentless checks pass, stop that foreground server. On the isolated
   host only, prepare the reviewed local-path provisioner, Flannel and node agent
   using the same captured K3s version and isolated network inputs. Keep application
   replicas zero and the GPU device plugin disabled for the CPU restore drill.
   For every retained PV,
   bind only the disposable node/path to its captured claim. Extract the WebUI,
   Swarm, tools and each gaming-home archive into fresh corresponding storage paths. Preserve file
   ownership. Do not reuse the active node's storage path over a network mount.
5. Initialize a fresh PostgreSQL PVC with the recovered application credential.
   Review the protected role dump before applying it; do not recreate the existing
   bootstrap `postgres` role blindly. Restore the application dump with the selected
   PostgreSQL client, inside the disposable database Pod:

   ```sh
   kubectl --context "$RESTORE_CONTEXT" -n ai-home-lab exec -i "$RESTORE_DATABASE_POD" -- pg_restore -U postgres -d litellm --exit-on-error < /recovery/verified-capture/litellm.dump
   ```

   Restore matching gateway master/salt/browser mapping Secrets from the recovered
   K3s state. Check scoped role permissions and migrations before starting gateway.
6. Restage the exact CPU embedding assets from the retained RAG/model lock. Start
   WebUI and verify the test user's **existing** login, conversation, uploaded
   source file and an authorized cited retrieval result. Verify a second user's
   denial and a deleted document's absence. Confirm the saved image is still
   retrievable by its owner after a restart without regenerating it.
7. Verify an existing valid gateway key still works with a deterministic local
   backend, a revoked key still fails, and account/model/quota ownership persists
   after database/gateway restart. Keep real SGLang qualification separate. Clear
   no unresolved image marker until the abandoned job is explicitly reconciled.
8. Record snapshot ID, source/kernel/K3s/image identities, independent credential
   provenance (no values), restored artifact checksums, access outcomes, elapsed
   recovery time and failures. Keep this record and a recovery snapshot off-array.
   Remove only the disposable environment through its owner-controlled lifecycle.

For gaming, first inspect restored home ownership and app paths with replicas zero.
Retain optional portal consent state without logging it. In a separately qualified
GPU session, prove existing Moonlight pairing, account access and a saved game.
An extracted archive or restored Steam download does not prove save recovery.
Repeat recovery from both SSD and S3 without using credentials bound only to the
failed workstation. Never extract restored archives onto active PVCs.

## Restore record for this change

**NOT RUN:** real K3s bootstrap/token recovery, independent repository credentials,
PostgreSQL recovery, user login/conversation/document/retrieval/image restore,
Sunshine pairing/saved-game recovery, SSD/S3 snapshot replication and
hardware-backed service recovery. No live workstation or backup destination was
available. SQLite fixture copying, token/checksum mismatch refusal and isolated
path checks are software evidence only; they do not establish disaster recovery.
