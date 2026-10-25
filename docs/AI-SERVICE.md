# Shared workstation AI service

This is an opt-in source implementation. It is not a deployed or hardware-qualified
service. All new application replicas remain zero. See the
[reconciliation and evidence matrix](AI-SERVICE-RECONCILIATION.md).

## Components, endpoints and state

| Component | Request path / owner | Credentials | Durable state and recovery |
| --- | --- | --- | --- |
| Traefik | Actual private node IP, TCP 32443; browser and API DNS names | Owner TLS Secret; private client CIDRs | K3s configuration and SQLite backup |
| Open WebUI 0.11.3 | Browser HTTPS → WebUI → internal `/webui/v1` gateway route | WebUI login; server-held application credential and user-ID mapping | Accounts, conversations, uploads, embedded Chroma on `webui-rag-qwen3-data` |
| LiteLLM OSS 1.106.2 | API HTTPS `/v1/models`, `/v1/chat/completions` → internal SGLang | Per-client virtual keys; separate master, salt and host-control credentials | Scoped `litellm` PostgreSQL role/database; dump plus matching K3s Secrets |
| SGLang | ClusterIP only; unchanged qualified model/runtime inputs | No ordinary user access to backend | Weights and caches rebuildable; model revisions retained |
| Read-only MCP | HTTPS `/mcp` → supported WebUI retrieval/file APIs | Caller's own restricted WebUI API key | Reuses WebUI document ACLs and retrieval state; no second vector store |
| Image adapter | WebUI native OpenAI image connector → internal adapter → SwarmUI API | Trusted browser application identity plus per-user image permission | Unresolved job marker on `service-tools-state`; WebUI stores returned image under caller |
| SwarmUI / ComfyUI | One self-started ComfyUI owned by Swarm; internal 7801 only | Operator owns workflows; no public Swarm administration | `creative-models/Data` and `Output` retained; `Models` downloadable |
| Session / Bridge | Root host CLI, canonical flock, existing identity/qualification gates | Dedicated Kubernetes identity and root-owned control-key file | Durable transition journal; no inference key grants administrative access |
| Alertmanager | Prometheus → internal Alertmanager → chosen webhook | URL in Secret file; exact IP/port egress | Retained notification state; reconstruct configuration from source/Secrets |
| Independent observer | Another device checks HTTPS status | Separate receiver credential and CA trust | Detects whole-workstation failure; bounded owner maintenance window |

Browser history, local IDE sessions, approved documents, gateway accounting and
inference caches remain separate. The gateway neither synchronizes histories nor
provides durable agent execution. Ordinary users receive no kubeconfig or proxy
process to operate. An IDE agent runs beside its project files and uses HTTPS.

## Version and feature decisions

Image references in `apps/base/shared-api/workloads.yaml` and
`infrastructure/service/alertmanager.yaml` pin Linux amd64 manifests. Native
LiteLLM authentication remains authoritative for virtual-key ownership, expiry,
revocation, model allowlists, RPM/TPM and parallel-request policy. This deployment
uses one process and one replica: it does not claim distributed quotas, enterprise
SSO, enterprise audit features or Responses API parity. No fallback model or cloud
provider is configured. Retries are zero in the gateway and generated Qwen bundle.

The ASGI boundary caps bodies at 1 MiB, global active model requests at four and
request duration at 180 seconds. Native per-key limits are lower by default.
The two model IDs remain `Qwen3.8-27B-FP8` (32,768 context, 4,096 output cap) and
`Qwen3.5-9B` (4,096 context, 1,024 output cap). Context metadata is not a tokenizer:
SGLang's actual context gate and the existing client/model qualification remain
required. A different active model returns 503; it never silently replaces one.

The browser application credential alone grants no model usage. The private
`/webui` route requires that credential **and** a mapped, unexpired WebUI user ID,
then translates to that person's native virtual key. Public ingress exposes only
exact consumer routes and removes identity headers. Direct API requests use their
own key. Native admin/control routes have no Ingress. Trusted WebUI and operators
are inside this boundary; a compromised trusted application is not tenant isolation.

Prompts, code, documents, cookies, arbitrary provider headers and caller accounting
tags are not default telemetry. Spend records retain native key/user ownership and
token/cost metadata with prompt storage disabled. Native gateway stdout/stderr
are discarded because dependency exception text can contain request data or
credentials. Fixed-label status/request metrics, readiness and Kubernetes events
remain available. Diagnose native internals in the disposable synthetic smoke
environment; do not enable raw gateway logs for family traffic.

Primary sources checked against selected source versions:

- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys),
  [limits](https://docs.litellm.ai/docs/proxy/users),
  [selected native authentication](https://github.com/BerriAI/litellm/blob/49943cd52ea46aacf8db2e5cb2b890d580fe9bb4/litellm/proxy/auth/user_api_key_auth.py),
  [migration CLI](https://github.com/BerriAI/litellm/blob/49943cd52ea46aacf8db2e5cb2b890d580fe9bb4/litellm/proxy/proxy_cli.py).
- [WebUI/LiteLLM integration](https://docs.litellm.ai/docs/tutorials/openweb_ui),
  [WebUI environment configuration](https://docs.openwebui.com/reference/env-configuration/),
  [selected retrieval ACL implementation](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/routers/retrieval.py).
- [K3s networking](https://docs.k3s.io/networking/networking-services),
  [Traefik chart 40.1.0 settings](https://github.com/traefik/traefik-helm-chart/blob/v40.1.0/traefik/values.yaml).
- [Swarm API](https://github.com/mcmonkeyprojects/SwarmUI/blob/ec823c3f750ce9b5103e623f3ad9918d02b8ddde/docs/API.md),
  [self-start backend](https://github.com/mcmonkeyprojects/SwarmUI/blob/ec823c3f750ce9b5103e623f3ad9918d02b8ddde/src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs),
  [WebUI image connector](https://github.com/open-webui/open-webui/blob/2a960a59fe1dbbd35282f0556b3666d81102e781/backend/open_webui/routers/images.py).
- [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/).

## Recollect the 128 GB baseline

The capacity upgrade is owner-reported. DIMM count, configured speed and MemTotal
remain **NOT RUN** on the target. On the Arch workstation, use new private paths:

```sh
sudo ./bin/workstationctl hardware collect /var/lib/workstation-evidence/hardware-128gb
sudo ./bin/workstationctl resources plan /var/lib/workstation-evidence/hardware-128gb/hardware.json /var/lib/workstation-evidence/resource-plan-128gb
```

Review `.memory`, `.cpu_topology` and the retained DMI evidence. Do not round
MemTotal up to 128 GiB or infer channels from DIMM count. The planner keeps the
12 GiB host, 4 GiB K3s and 2 GiB eviction reservations: nominal 128 GiB leaves
110 GiB before firmware reservations. Keep current application limits.

`service plan` conservatively sums current limits for inference, WebUI/RAG,
gateway, PostgreSQL, tools, Alertmanager and the verified telemetry composition.
Pass separate `--build-mib/--build-cpus` and `--vm-mib/--vm-cpus` budgets when those
will run. Defaults of zero mean those workloads are excluded, not unlimited.
With the current limits, split mode exceeds even the metrics profile's 42-CPU
allocatable budget (43.25 logical CPU limits). **Do not activate it on the basis
of added RAM.** Measure and qualify a deliberate limit/profile adjustment first.
Revisit ingestion concurrency and build parallelism independently under pressure.

Current planning limits, before any build/VM allowance:

| Component | Large model mode | Split mode | CPU limit |
| --- | --- | --- | --- |
| SGLang | 38 GiB, two GPUs | 32 GiB, one GPU | 24 / 20 |
| WebUI/RAG | 6 GiB | 6 GiB | 2 |
| Gateway + PostgreSQL | 4 GiB | 4 GiB | 2 |
| Tools + Alertmanager | 0.75 GiB | 0.75 GiB | 0.75 |
| Telemetry reserve | 6 GiB | 6 GiB | 6.5 metrics / 10.5 full |
| Swarm/Comfy | Stopped | 12 GiB, one GPU | 0 / 12 |
| Total | 54.75 GiB | 60.75 GiB | Large: 35.25 / 39.25; split: 43.25 / 47.25 |

These are admission limits, not measurements. At nominal 128 GiB, the 110 GiB
planning allowance would leave 55.25 GiB in large mode and 49.25 GiB in split mode
before additional work. Actual firmware reservations reduce those values. With
full telemetry, large mode leaves only 2.75 of the 42 allocatable logical CPUs
under this conservative policy. Builds and VMs must share that CPU allowance;
do not treat the remaining RAM as authorization for 48 parallel compiler jobs.

## Prepare and roll out

Run source preparation from the reviewed checkout. Commands that contact a cluster
below are owner-run rollout steps, **NOT RUN** by this change. Use the actual
classified context, node and new output paths; placeholders are not site values.

1. Complete `config/service-site.example.json` outside Git. Supply actual private
   IPv4 address, management/client CIDRs, distinct browser/API DNS names, TLS
   Secret name, node, context and `dev`/`tst`/`int` classification. Confirm that the
   K3s NodePort address range includes the chosen address. Review the host firewall
   to allow TCP 32443 only from approved clients; keep 6443 management-only.
2. Render the service and existing telemetry, then plan admission:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab get pvc -o json > /private/existing-pvc.json
   ./bin/workstationctl service render /private/site.json ./service-large --mode large --existing-claims /private/existing-pvc.json
   ./bin/workstationctl --config /private/workstation.conf telemetry render ./service-telemetry apps/overlays/service
   sudo ./bin/workstationctl service plan /var/lib/workstation-evidence/resource-plan-128gb/resource-plan.json ./service-large ./service-telemetry ./service-budget
   ```

   A blocked budget is a stop condition. All rendered replicas remain zero.
   The PVC metadata preserves existing immutable storage classes. Before apply,
   compare their UIDs with `evidence.json`; re-render if an object was replaced.
   Omit `--existing-claims` only for a new cluster with no existing claims.
   Render `--mode split` into a separate directory only after reviewing its gates.
3. Complete the [baseline backup and existing-PV Retain migration](SERVICE-RECOVERY.md)
   before creating the new service claims/deployments. Use backup scope `baseline`
   for the pre-service cluster, then `service` after initialization. The full
   capture requires the four valuable claims to be bound; it cannot back up a
   gateway database that does not yet exist. If no application has ever run and
   no data claim is bound, record that absence and initialize the service first.
   New `workstation-retain` claims do not modify existing bound PVs or enforce
   filesystem quotas. Do not delete/recreate a populated PVC to change its class.
4. Create private credentials outside Git:

   ```sh
   ./bin/workstationctl service credentials /private/new-service-credentials
   ```

   This produces mode-0600 random credentials, including a PostgreSQL admin
   password, scoped application password, database URL, gateway master/salt/control
   keys and empty browser mapping. Keep an independent encrypted recovery copy.
   Through the approved operator context, create these Secrets from files:

   | Secret / namespace | `kubectl create secret generic --from-file` input keys |
   | --- | --- |
   | `gateway-postgres` / `ai-home-lab` | `admin-password`, `litellm-password` |
   | `gateway-credentials` / `ai-home-lab` | `database-url`, `master-key`, `salt-key`, `control-key` |
   | `gateway-browser-keys` / `ai-home-lab` | `mapping.json` |
   | `gateway-image-permissions` / `ai-home-lab` | `image-users.json` (no inference keys) |
   | `webui-gateway-connection` / `ai-home-lab` | `service-key` |
   | chosen TLS Secret / `ai-home-lab` | `kubectl create secret tls ... --cert=<file> --key=<file>` |

   Use protected file paths, never inline values or shell tracing. Secret creation
   is a separate owner action; the renderer never emits Secret values. Preserve
   the existing WebUI auth secret and account database.
5. Apply reviewed `service-large/workloads.yaml` and `traefik.yaml` using the
   explicit context. Do not turn on all scaffolded workloads. Start only PostgreSQL:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab scale deployment/gateway-postgres --replicas=1
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab rollout status deployment/gateway-postgres --timeout=300s
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab create -f service-large/migration.yaml
   ```

   Record the generated Job name and wait for its Complete condition, timeout 660s.
   Backoff is zero. Native Prisma migrate uses the selected v2 resolver; no db push
   or schema reset. Only after completion, qualify and start one gateway replica.
   Readiness checks database access; a gateway restart leaves model admission closed.
6. Provision users/keys through an operator-only loopback port-forward:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab port-forward --address 127.0.0.1 service/litellm 14000:4000
   ./bin/workstationctl service provision --base-url http://127.0.0.1:14000 --master-file /private/new-service-credentials/master-key --request /private/client-policy.json --output /private/new-client-credential
   ```

   Start with `config/service-client-policy.example.json`: choose an owner ID,
   client name, exact model allowlist, RPM/TPM/concurrency and expiry up to 90 days.
   Use `--existing-user` for another client belonging to an existing owner. The
   ordinary client receives only its virtual key, not the master/salt/control files.
   For a browser key, use the **exact WebUI user UUID** as `user_id`, then:

   ```sh
   ./bin/workstationctl service browser-bind --mapping /private/new-service-credentials/mapping.json --key-file /private/new-client-credential/virtual-key.json --user-id "$WEBUI_USER_ID" --images
   ```

   Publish both protected mapping Secrets through the owner workflow. The image
   adapter receives only per-user image permission/expiry, not inference keys. Remove
   `--images` for users without image permission. Revoke inference with
   `service revoke --base-url ... --master-file ... --key-file ...`; include `--mapping /private/new-service-credentials/mapping.json`
   for a browser key and publish both updated mapping Secrets to revoke image
   permission. These are separate permissions. Verify denial before deleting the
   local credential record. WebUI retrieval keys have their own WebUI revocation.
7. Start WebUI and the tools adapter only after source, database and persistence
   checks. Disable open signup; create the first administrator and family users
   through the existing WebUI account workflow. Keep the RAG model/fixtures staged
   offline. Confirm each family user maps to a distinct gateway key/usage owner.
   Initialize the image/output claim without starting an unqualified GPU image,
   using an owner-reviewed non-GPU Pod that mounts only that claim, then remove
   that Pod. Verify all four valuable PVs are Retain. Take a full `service` capture
   and complete the isolated restore drill before admitting family traffic.
8. Install the gateway control file root:root 0600. Set
   `SESSION_SERVICE_ENABLED=true`, `SESSION_IMAGE_DEPLOYMENT=swarmui` and
   `SESSION_SERVICE_CONTROL_FILE` in the reviewed host config. Keep the existing
   canonical session policy, Kubernetes identity and lock. Requalify the selected
   model before using the current CLI:

   ```sh
   sudo ./bin/workstationctl --config /etc/workstation/workstation.conf session switch ai /var/lib/workstation-evidence/hardware-128gb/hardware.json /var/lib/workstation-session --execute
   ```

   The service moves through draining/starting/ready. Drain is at most 180 seconds
   followed by cancellation; physical release and rollout retain existing bounded
   waits. Failure preserves the journal and closes admission. After any process
   restart, explicitly re-run the qualified session path; never restore a ready flag.
   To change model profiles, first enter maintenance, apply the separately reviewed
   overlay, then request `split`. Never apply a new Pod template to a running model
   as an implicit image-triggered switch. Bridge v1 catalog support is separate.
9. Point family DNS at the actual private IP. Verify certificate chain/SANs from
   both a browser and an IDE host, including your private CA if used. Do not use
   insecure TLS flags. Generate the existing agent bundle with the HTTPS values
   from `client.conf`; `AGENT_RETRIEVAL_ENABLED=true` adds the read-only MCP tool
   to Qwen. Provide `WORKSTATION_AGENT_API_KEY` and the separate user-owned
   `WORKSTATION_RETRIEVAL_API_KEY` through the client credential store. Neither is
   written into the bundle. Keep Qwen tool approval enabled and ACP beside project
   files. DSH/Hermes retain their existing inference path; their MCP UI integration
   has not been qualified here.

## Image package and lifecycle

The narrow adapter uses WebUI's native OpenAI image connector because SwarmUI owns
the existing ComfyUI lifecycle and outputs. Swarm's API is not a ComfyUI endpoint.
There is no second backend/frontend starting another GPU process. Public consumers
cannot upload workflows or reach Swarm's administration API.

`infrastructure/creative/runtime.json` pins Swarm, Comfy, ROCm/.NET build images,
SDXL model identity/checksum and bundled custom-node source. The checked-in
`sdxl-workflow.json` fixes node types and parameters; only prompt, bounded size and
seed are substituted by the adapter. The sole custom output node is
`SwarmSaveImageWS` from the same Swarm source revision. Swarm supplies model paths
through its `Data/comfy-auto-model.yaml`. Stage the checkpoint at the exact path
in the contract. Keep `Models` read-only after staging where the packaged backend
permits it; `Data` and `Output` must remain persistent and writable.
The packaged launcher creates Comfy's user/input/output directories there and
uses `/tmp/comfy-temp` for temporary files. Its managed-node directory is created
at build time; application source need not become writable. Existing persisted
`Data/Backends.fds` is never overwritten by the init container: review its
`ExtraArgs` against the checked-in backend before qualifying an upgrade.

Prepare clean pinned source trees, a reviewed NuGet `packages.lock.json`/package
closure, and a ROCm-image-compatible wheelhouse with a complete hash-pinned
`requirements.lock`. Then run on a reviewed Linux amd64 builder:

```sh
python infrastructure/creative/prepare.py /staging/SwarmUI /staging/ComfyUI /staging/nuget /staging/wheels /staging/requirements.lock /staging/new-context
docker build --network=none --platform linux/amd64 -t workstation-creative-candidate /staging/new-context
```

The context records staged file hashes; the image records its resolved Python
inventory. `AutoUpdate=false` does not disable upstream dependency repair, so the
candidate fails attempted runtime pip repair explicitly. No third-party custom
node download is part of this profile. A complete dependency closure and offline
image build are **NOT RUN** on this Mac; the manifest remains `UNQUALIFIED` until
that build and the physical resource/quality tests pass. Do not replace its tag
with a guessed digest or lower security to boot it.

The adapter permits one image, 512² or 1024², a 2,000-character prompt, no workflow
macros, and an 8 MiB PNG result. Generation is bounded to 120 seconds. Cancellation
interrupts only the adapter's Swarm session. Any submitted generation without a
successful terminal result retains a durable unresolved marker: interruption
acknowledgement and zero Swarm counters do not prove GPU completion. Restart
cannot fabricate success or launch a duplicate. Recovery: enter maintenance;
verify Swarm/Comfy Pods and DRM holders
are gone; preserve the marker as evidence; clear it only after the owner has
resolved the abandoned job; then restart the qualified split profile.

## Notifications and target acceptance

Render the extension from the existing verified telemetry output:

```sh
./bin/workstationctl service notifications ./service-telemetry ./service-notifications --receiver-cidr "$RECEIVER_IP_CIDR" --receiver-port "$RECEIVER_PORT"
```

Create `service-alert-receiver` in `workstation-observability` from a protected
`url` file containing the chosen HTTPS webhook URL. Apply the composed
`service-notifications/stack.yaml` in place of the base telemetry render; enable
Alertmanager only after an authorized delivery test. Install the independent
heartbeat service/timer on another device with its own config, receiver file and
CA trust. No recipient has been invented. Local receiver tests prove routing and
inhibition, not delivery to the owner's destination.

On that independent Linux device, install only the observer script and its two
units from this checkout. No workstation installer is needed:

```sh
sudo install -Dm0644 lib/workstation/service_heartbeat.py /usr/lib/arch-workstation-bootstrap/lib/workstation/service_heartbeat.py
sudo install -Dm0644 templates/workstation/telemetry/workstation-external-heartbeat.service /etc/systemd/system/workstation-external-heartbeat.service
sudo install -Dm0644 templates/workstation/telemetry/workstation-external-heartbeat.timer /etc/systemd/system/workstation-external-heartbeat.timer
```

Create `/etc/workstation-observer/config.json` root:root 0600 with these fields:
`status_url` = the actual API HTTPS URL ending `/_service/status`, `ca_file` =
the public CA certificate path (or null for system trust), `maintenance_until` =
0, and `webhook_file` =
`/run/credentials/workstation-external-heartbeat.service/observer-webhook`.
Store the selected receiver URL separately in `/etc/workstation-observer/webhook`,
root:root 0600. The unit requires `/usr/bin/python` with Python 3.11 or newer.
After the destination is authorized, run `sudo systemctl daemon-reload`,
`sudo systemctl start workstation-external-heartbeat.service`, then
`sudo systemctl enable --now workstation-external-heartbeat.timer`.
Expected: the first healthy check exits zero. A deliberately unreachable test
endpoint produces the fixed heartbeat notification and exits one; a receiver
failure exits two. Restore the actual endpoint after that test. Use an epoch
timestamp no more than four hours ahead for planned whole-host maintenance;
model-only maintenance requires no external suppression.

For repeatable HTTPS checks from an ordinary client, prepare a mode-0600 curl
config in the client's credential store containing its Authorization header.
Use its path as `CLIENT_CURL_CONFIG`; never pass a key with `-H` or print that
file. `API_URL` is the actual HTTPS origin including port 32443. System CA trust
must already trust the selected certificate; no `-k` override is permitted:

```sh
umask 077
curl --config "$CLIENT_CURL_CONFIG" --fail --silent --show-error --max-time 15 "$API_URL/v1/models" > /private/model-list.json
curl --silent --show-error --max-time 15 --output /private/unauthorized.json --write-out '%{http_code}\n' "$API_URL/v1/models"
curl --config "$CLIENT_CURL_CONFIG" --fail --silent --show-error --no-buffer --max-time 190 \
  --header 'Content-Type: application/json' \
  --data '{"model":"Qwen3.8-27B-FP8","messages":[{"role":"user","content":"Reply with the word ready."}],"max_tokens":16,"stream":true}' \
  "$API_URL/v1/chat/completions" > /private/stream.txt
```

Expected: the permitted model is listed, the unauthenticated request returns 401,
and the stream delivers incremental events followed by its normal completion.
Repeat with an expired/revoked client's protected curl config: 401/403 is expected.
Repeat the selected model request in maintenance: 503 with `Retry-After` is expected.
For split qualification, use its exact `Qwen3.5-9B` ID. Never substitute that ID
silently after a large-model failure. The native disposable test script exercises
the tool round trip, quotas, cancellation and restart cases without real users:
`bash tests/service/native-smoke.sh`. It still needs a functioning local Docker
daemon and is **NOT RUN** in this checkout's local evidence. The hosted CI job
runs on pull requests, main pushes and manual dispatch; inspect its exact revision
before citing a result. Its artifacts exclude raw logs and generated credentials.

Target checks, all **NOT RUN** until an owner records evidence:

| Check | Procedure and expected result |
| --- | --- |
| Browser persistence | Family login → conversation → restart WebUI → same login/conversation; separate user cannot read it |
| Browser attribution | Two users send the same fixture prompt; native usage belongs to their two mapped keys; unmapped user denied |
| API access | From IDE HTTPS list models, stream a fixture answer, complete a tool-call/result round trip and inspect usage; revoked/expired/wrong-model key denied |
| Limits/cancellation | Exceed a key's configured RPM/TPM/concurrency; bounded 429; disconnect a stream and confirm backend activity drops; no replayed tool action |
| Unavailable backend | Enter maintenance; new requests return 503 with Retry-After; restart gateway leaves admission unavailable until explicit session revalidation |
| Network isolation | From an unrelated unprivileged test Pod, database, SGLang, WebUI and gateway connections fail; only the documented pairs succeed; remove only that test Pod afterward |
| Proxy chain | Browser WebSocket remains usable through idle intervals; streaming delivers incrementally; client disconnect cancels; TLS SAN/chain and private CIDRs are enforced |
| Retrieval | User A queries an allowed collection through MCP and opens citation; B's private collection denied; deleted document gives no stale snippet; rerun RAG quality fixtures |
| Images | Only after split admission/build qualification: generate one image, restart WebUI/Swarm, retain the artifact; another user denied; large mode returns bounded unavailable |
| Handover | Concurrent mode changes cannot acquire the same lock; verify actual devices released before allocation; failure keeps recoverable state and no fabricated completed image |
| Recovery | Complete independent SSD and S3 drills in SERVICE-RECOVERY.md, including login/conversation/document/retrieval/image, gateway keys and gaming-home state |
| Alerts | Inject an authorized test alert, confirm owner receipt/resolution; maintenance inhibits model outage only; stop the observer's HTTPS target to verify independent failure detection |

## Rollback and remaining inputs

Before each rollout retain the previous reviewed render, exact image IDs, current
UID/replica journal and a successful application-consistent backup. To roll back,
close admission with `session switch maintenance`, stop gateway/tools/WebUI writers,
and apply the previous reviewed source. Keep retained PVs and recovery credentials.
Do not downgrade a migrated database in place: restore the previous dump into a
fresh isolated database/PVC and validate it before switching. Remove only the
new ingress routes/HelmChartConfig through the approved owner workflow if exposure
must be reverted; never delete a populated PVC or namespace as rollback.

Owner inputs are grouped here: actual current-boot hardware evidence; classified
context/node and canonical session identity; private IP/CIDRs/DNS and TLS chain/key
files; existing PV identities and restore staging capacity; off-array repository
and independent recovery credentials; WebUI user UUIDs/permissions and key policies;
reviewed creative build dependency closure; measured split-mode CPU admission;
notification destination/IP/port and independent observer device. None prevents
the repository's independent source and fixture work.

Use [final installation acceptance](INSTALL-READINESS.md) for the combined package,
boot, service, Sunshine and recovery gates. Supply the actual backup SSD
mountpoint/filesystem UUID and protected local/S3 credential paths before enabling
local-first capture. Publishing the source does not qualify the target services.
