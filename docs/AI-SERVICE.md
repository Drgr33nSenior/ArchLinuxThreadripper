# AI service setup

How the family AI service is set up: its components and endpoints, credentials, accounts
and keys, the first rollout, and the checks a new deployment must pass. It is running today
in shared mode (see the [README](../README.md#running-now)); day-to-day tasks are in
[operations](OPERATIONS.md#ai-service) and what it does for the family is in
[the assistant guide](ASSISTANT.md).

The live composition is the `assistant-memory` experience rendered from the
[serving contract](ASSISTANT.md#serving-contract), which replaces the source defaults this
page mentions for the older FP8/9B containers. The optional
[web-search adapter](WEB-SEARCH.md) adds Exa search and public-page reading for Open WebUI
and Qwen Code.

## Components, endpoints and state

| Component | Request path / owner | Credentials | Durable state and recovery |
| --- | --- | --- | --- |
| Traefik | Node management address only, TCP 443 (Service externalIP); browser and API DNS names | cert-manager-issued TLS Secret; private client CIDRs | K3s configuration and SQLite backup; certificate reissued by cert-manager |
| Open WebUI 0.13.3 (`service` and `assistant`) | Browser HTTPS → WebUI → internal `/webui/v1` gateway route | WebUI login; server-held application credential and user-ID mapping | Accounts, conversations, uploads, embedded Chroma on `webui-rag-qwen3-data` |
| LiteLLM OSS 1.109.1 | API HTTPS `/v1/models`, `/v1/chat/completions` → internal SGLang | Per-client virtual keys; separate master, salt and host-control credentials | Scoped `litellm` PostgreSQL role/database; dump plus matching K3s Secrets |
| SGLang | ClusterIP only; unchanged qualified model/runtime inputs | No ordinary user access to backend | Weights and caches rebuildable; model revisions retained |
| service-tools (MCP) | HTTPS `/mcp` → retrieval, memory, web search, handoffs and video | Caller's own WebUI session or restricted API key; the `admin` role is refused | Reuses WebUI document ACLs; handoffs and picture markers on `service-tools-state`; memory in Neo4j |
| Image adapter | WebUI native OpenAI image connector → internal adapter → ComfyUI API on each GPU | Trusted browser application identity plus per-user image permission | Unresolved job marker on `service-tools-state`; WebUI stores returned image under caller |
| ComfyUI | `comfyui-gpu1` on GPU 1, plus `comfyui-gpu0` on GPU 0 in shared mode; both internal on 7821, reachable only from service-tools | Operator owns workflows; no public ComfyUI administration | `creative-models` `Data/` and `Output/` captured; SDXL is read from the NAS; `Models/` and the Wan `Video/` weights are restaged from the catalogue (see [service recovery](SERVICE-RECOVERY.md)) |
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

The ASGI boundary caps request bodies at 1 MiB, except chat completions, which may be up to
64 MiB because Open WebUI resends earlier video clips (the boundary replaces each clip with a
short note before the model). It admits four active model requests. Its source defaults
(180-second requests; models `Qwen3.8-27B-FP8` at 32,768 tokens and `Qwen3.5-9B` at 4,096)
apply only to renders without a serving profile. A serving-contract render replaces the
FP8 route with `Qwen3.8-27B-MXFP4` at the mode's context (12,288-token output cap) and sets
streaming budgets ([time limits](ASSISTANT.md#time-limits)): 600 seconds to first content,
300 seconds of silence between chunks, a 3,600-second overall cap; the 9B split route
stays. Native per-key limits are lower by default. Context
metadata is not a tokenizer:
SGLang's actual context gate and the existing client/model qualification remain
required. A different active model returns 503; it never silently replaces one.

The gateway model list also publishes `context_window` for each configured chat model.
This is the full per-request window from the serving policy. Existing `max_input_tokens`
and `max_output_tokens` remain unchanged: the input budget reserves both output capacity
and scheduler headroom, so adding the two fields does not recover the full window.
Open WebUI preserves the provider field in `/api/models`, including in its nested `openai`
metadata. ACP clients can use it for their context usage display after a catalog refresh.
This metadata does not raise any inference limit or bypass provider enforcement.

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
  [selected retrieval ACL implementation](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/routers/retrieval.py).
- [K3s networking](https://docs.k3s.io/networking/networking-services),
  [Traefik chart 40.1.0 settings](https://github.com/traefik/traefik-helm-chart/blob/v40.1.0/traefik/values.yaml).
- [Swarm API](https://github.com/mcmonkeyprojects/SwarmUI/blob/ec823c3f750ce9b5103e623f3ad9918d02b8ddde/docs/API.md),
  [self-start backend](https://github.com/mcmonkeyprojects/SwarmUI/blob/ec823c3f750ce9b5103e623f3ad9918d02b8ddde/src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs),
  [WebUI image connector](https://github.com/open-webui/open-webui/blob/8bd8b4fac5e059578ac0c74b3c18d11139f88b7d/backend/open_webui/routers/images.py).
- [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/).

## Recollect the 64 GB baseline

The workstation has 64 GB installed RAM. Fresh DIMM count, configured speed
and MemTotal collection remain
**NOT RUN** on the target. On the Arch workstation, use new private paths:

```sh
sudo ./bin/workstationctl hardware collect /var/lib/workstation-evidence/hardware-64gb
sudo ./bin/workstationctl resources plan /var/lib/workstation-evidence/hardware-64gb/hardware.json /var/lib/workstation-evidence/resource-plan-64gb
```

Review `.memory`, `.cpu_topology` and the retained DMI evidence. Do not round
MemTotal up to installed capacity or infer channels from DIMM count. The planner
keeps the 12 GiB host, 4 GiB K3s and 2 GiB eviction reservations: nominal 64 GiB
leaves at most 46 GiB before firmware reservations. Keep current application limits.

`service plan` conservatively sums current limits for inference, WebUI/RAG,
gateway, PostgreSQL, tools, Alertmanager and the verified telemetry composition.
Pass separate `--build-mib/--build-cpus` and `--vm-mib/--vm-cpus` budgets when those
will run. Defaults of zero mean those workloads are excluded, not unlimited.
With the current limits, split mode exceeds even the metrics profile's 42-CPU
allocatable budget (43.25 logical CPU limits). **Do not activate it on the basis
of nominal capacity.** Measure and qualify a deliberate limit/profile adjustment first.
Revisit ingestion concurrency and build parallelism independently under pressure.

Current planning limits, before any build/VM allowance:

| Component | Large model mode | Split mode | CPU limit |
| --- | --- | --- | --- |
| SGLang | 38 GiB, two GPUs | 32 GiB, one GPU | 24 / 20 |
| WebUI/RAG | 6 GiB | 6 GiB | 2 |
| Gateway + PostgreSQL | 4 GiB | 4 GiB | 2 |
| Tools + Alertmanager | 0.75 GiB | 0.75 GiB | 0.75 |
| Telemetry reserve | 6.5 GiB | 6.5 GiB | 6.5 metrics / 10.5 full |
| Swarm/Comfy | Stopped | 12 GiB, one GPU | 0 / 12 |
| Total | 55.25 GiB | 61.25 GiB | Large: 35.25 / 39.25; split: 43.25 / 47.25 |

These are admission limits, not measurements. At nominal 64 GiB, the 46 GiB
planning allowance is exceeded by 9.25 GiB in large mode and 15.25 GiB in split
mode before builds or VMs. Actual firmware reservations reduce available RAM
further. Both full shared-service configurations therefore fail memory admission
with these limits. Keep them disabled until a separately measured and qualified
composition fits. Builds and VMs must share the same CPU/RAM allowance.

## Prepare and roll out

Run source preparation from the reviewed checkout. Commands that contact a cluster
below are owner-run rollout steps, **NOT RUN** by this change. Use the actual
context, node and new output paths; placeholders are not site values.

1. Complete `config/service-site.example.json` outside Git, for example as
   `/private/site.json`. Supply the actual private node address,
   management/client CIDRs, distinct browser/API DNS names, TLS Secret name,
   node and context. For gr33nstein, the owner's 2026-11-10 decisions give:

   | Key | Value |
   | --- | --- |
   | `context` | `default` |
   | `node` | `gr33nstein` |
   | `node_address` | `192.168.30.11` (VLAN 30, `enp103s0.30`; also the management address) |
   | `management_cidr` | `192.168.30.0/24` |
   | `client_cidrs` | `["192.168.0.0/16"]` (LAN clients; later WireGuard clients in the same range) |
   | `webui_host` | `chat.lan.thespry.family` (Open WebUI) |
   | `api_host` | `api.lan.thespry.family` (OpenAI-compatible `/v1` and `/mcp`) |
   | `tls_secret` | `ai-home-lab-tls` |
   | `webui_default_group` | Optional. The UUID of the Open WebUI group every new account joins (`DEFAULT_GROUP_ID`); empty or absent leaves it unset. |

   **Model visibility.** Open WebUI 0.12 shows a gateway model without an Open
   WebUI model entry to admins only. As admin, create a group (for example
   "Family") under *Admin Panel → Users → Groups*, add the existing accounts, then
   open the model under *Admin Panel → Settings → Models* and grant that group
   read access. Put the group's UUID in `webui_default_group` so new accounts
   join it. The group, members and grant live in Open WebUI's database, which
   is backed up. Each account still needs its own gateway browser key; the group
   only controls what the browser lists.

   Do not commit this site file. The renderer writes Traefik with
   `node_address` as its only Service externalIP on TCP 443. kube-proxy
   forwards exactly that address and port, so other host addresses and NICs are
   not bound. `externalTrafficPolicy: Local` keeps client source addresses for
   the `client_cidrs` allowlist. No NodePort is allocated. Port 80 stays closed
   rather than redirecting, so an API client that mistypes `http://` fails before
   it sends a bearer key in plaintext. The client URLs are
   `https://chat.lan.thespry.family` and `https://api.lan.thespry.family/v1`.

   **DNS (owner, UniFi).** The lab depends on these internal A records on the
   UniFi gateway. No public record is needed: certificates use DNS-01.

   | Record | Address | Used by |
   | --- | --- | --- |
   | `chat.lan.thespry.family` | `192.168.30.11` | Open WebUI in browsers and the JetBrains ACP agent |
   | `api.lan.thespry.family` | `192.168.30.11` | the OpenAI-compatible API (`/v1`, `/mcp`) for coding clients |
   | `threadripper-ai.lan.thespry.family` | `192.168.30.11` | the K3s API (`:6443`); `kubectl` on the workstation uses it, and the API certificate names it |
   | `registry.lan.thespry.family` | `192.168.30.10` | the image registry on the NAS; K3s pulls every local image from it |
   | `microserver.lan.thespry.family` | `192.168.30.10` | the NAS: NFS storage and monitoring |

   These are hand-made records, so restoring an older gateway configuration
   can remove them. On 2026-11-13 a rollback removed the first four. After
   any gateway restore, check that each name answers with its address, from
   a client and from the workstation:

   ```sh
   for n in chat api threadripper-ai registry microserver; do
     h="$n.lan.thespry.family"
     printf '%-16s %s\n' "$n" "$({ getent hosts "$h" || dig +short "$h"; } 2>/dev/null | awk '{print $1; exit}')"
   done
   ```

   The workstation has `getent` but not `dig`; macOS has `dig` but not
   `getent`.

   While a record is missing, upstream DNS answers "no such name", and clients
   cache that answer for up to 30 minutes after the record returns. On a Mac,
   clear the cache with `sudo dscacheutil -flushcache; sudo killall -HUP
   mDNSResponder`; Chrome keeps its own copy (*chrome://net-internals/#dns* →
   *Clear host cache*). Other devices recover when they rejoin the network.
   The JetBrains ACP agent reports a cached failure as "Connection failed:
   check URL, TLS/private CA and network".

   **Host firewall.** Set `lab_https_client_cidrs: ["192.168.0.0/16"]`,
   `lab_management_cidr: 192.168.30.0/24`, `lab_node_ip: 192.168.30.11`,
   `lab_network_interface: enp103s0.30`, `lab_api_client_cidrs:
   ["192.168.2.0/24", "192.168.4.0/24", "192.168.30.0/24"]` (the live API
   clients, kept) and `lab_api_interfaces: ["enp103s0", "enp103s0.30"]` in the
   private Ansible overrides. Never list the DMZ NIC (`eno1`). Run
   the bare-metal play ([HOME-LAB.md](HOME-LAB.md)) with `--check --diff`
   first and confirm that it changes only the guard files. Then run it with
   `lab_manage_service=true` and `lab_network_reviewed=true`. This replaces the
   live `k3s_host_guard` table in one nft transaction. Leave
   `lab_restart_approved` false; the guard needs no K3s restart. The table
   admits TCP 443 only on `enp103s0.30` to `192.168.30.11` from
   `192.168.0.0/16`, and 6443 only from `192.168.2.0/24`, `192.168.4.0/24` and
   `192.168.30.0/24` arriving on `enp103s0` or `enp103s0.30` (the live guard's
   API clients, kept), or from Pods/Services. It drops 443 to every other local
   address. Pods cannot reach the service through its public name.

   **DMZ NIC.** The second NIC, `eno1`, is reserved for a future DMZ. It has
   no address and no bound service, and no guard rule accepts traffic on it.
   Public exposure is out of scope for this service. A DMZ needs its own
   reviewed default-drop policy before `eno1` is configured.
2. Render the service and existing telemetry, then plan admission:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab get pvc -o json > /private/existing-pvc.json
   ./bin/workstationctl service render /private/site.json ./service-large --mode large --existing-claims /private/existing-pvc.json
   ./bin/workstationctl --config /private/workstation.conf telemetry render ./service-telemetry apps/overlays/service
   sudo ./bin/workstationctl service plan /var/lib/workstation-evidence/resource-plan-64gb/resource-plan.json ./service-large ./service-telemetry ./service-budget
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
   | `cloudflare-dns01-api-token` / `cert-manager` | `api-token` (scoped Cloudflare token, step 5) |

   Use protected file paths, never inline values or shell tracing. Secret creation
   is a separate owner action; the renderer never emits Secret values. Preserve
   the existing WebUI auth secret and account database. cert-manager creates and
   renews the TLS Secret; do not create it by hand. The NAS's own
   `*.lan.thespry.family` certificate and private key stay on the NAS.
5. Issue the certificate, then apply the service. Install the checksum-locked
   cert-manager release only (it is not yet installed):

   ```sh
   ./kubernetes/install-pinned-addons.sh check --component cert-manager
   ./kubernetes/install-pinned-addons.sh apply --component cert-manager \
     --kubeconfig /private/kubeconfig --context "$SERVICE_CONTEXT" --confirm-context "$SERVICE_CONTEXT"
   kubectl --context "$SERVICE_CONTEXT" -n cert-manager rollout status deployment/cert-manager-webhook --timeout=300s
   ```

   Create the Cloudflare token Secret, then render and apply both ClusterIssuers.
   The procedure is in [kubernetes/cert-manager/README.md](../kubernetes/cert-manager/README.md#cloudflare-workstation-ai-service).
   The token has *Zone → DNS → Edit* and *Zone → Zone → Read*, limited to the
   `thespry.family` zone. cert-manager documents Zone Read as required to look
   up the zone. Issue staging first. The staging certificate goes into its own
   Secret, `ai-home-lab-tls-staging`:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" apply -f service-large/certificate-staging.yaml
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab wait --for=condition=Ready certificate/ai-home-lab-tls-staging --timeout=600s
   kubectl --context "$SERVICE_CONTEXT" apply -f service-large/certificate.yaml
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab wait --for=condition=Ready certificate/ai-home-lab-tls --timeout=600s
   kubectl --context "$SERVICE_CONTEXT" -n ai-home-lab delete certificate/ai-home-lab-tls-staging secret/ai-home-lab-tls-staging
   ```

   That README also covers renewal (automatic, at about two-thirds of the
   lifetime) and failures (the current certificate stays in place while retries
   back off).

   The Traefik change converts the existing LoadBalancer Service to ClusterIP
   and releases its NodePorts. Validate the Service that it produces on the
   server first. A dry run of the HelmChartConfig alone does not validate that
   Service. Copy the bundled chart, which is
   `/var/lib/rancher/k3s/server/static/charts/traefik-40.1.7+up40.1.0.tgz` on
   the node:

   ```sh
   kubectl --context "$SERVICE_CONTEXT" -n kube-system get helmchart traefik -o jsonpath='{.spec.valuesContent}' > /private/k3s-traefik-values.yaml
   yq '.spec.valuesContent' service-large/traefik.yaml > /private/site-traefik-values.yaml
   helm template traefik /private/traefik-40.1.7+up40.1.0.tgz -n kube-system \
     -f /private/k3s-traefik-values.yaml -f /private/site-traefik-values.yaml --show-only templates/service.yaml \
     | kubectl --context "$SERVICE_CONTEXT" apply --dry-run=server -f -
   ```

   Apply reviewed `service-large/workloads.yaml` and `traefik.yaml` using the
   explicit context. K3s's helm-controller upgrades the bundled chart. Check
   `kubectl -n kube-system get svc traefik`: expect `ClusterIP` with external IP
   `192.168.30.11` and port `443/TCP` only. Do not turn on all scaffolded
   workloads. Start only PostgreSQL:

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

   Change an existing key's limits in place, found by its alias (the key value is
   never read or printed):

   ```sh
   ./bin/workstationctl service limits --base-url http://127.0.0.1:14000 --master-file /private/new-service-credentials/master-key --alias browser-zarren --tpm 2000000
   ```

   Bounds are 60 requests a minute, 2,000,000 tokens a minute and 2 requests at
   once. Concurrency is what shares the GPU between people. Tokens a minute count
   the whole re-sent conversation, cached or not, so a coding agent at 40,000-80,000
   tokens of context reaches 131,072 in two or three steps and gets `429` with a
   60-second retry. Give agent and adult keys 2,000,000; a key whose user never
   runs agents can stay lower.
7. Start WebUI and the tools adapter only after source, database and persistence
   checks. Disable open signup; create the first administrator and family users
   through the existing WebUI account workflow. Keep the RAG model/fixtures staged
   offline. Confirm each family user maps to a distinct gateway key/usage owner.
   Initialize the image/output claim without starting an unqualified GPU image,
   using an owner-reviewed non-GPU Pod that mounts only that claim, then remove
   that Pod. Verify all four valuable PVs are Retain. Take a full `service` capture
   and complete the isolated restore drill before admitting family traffic.
8. Install the gateway control file root:root 0600. Set
   `SESSION_SERVICE_ENABLED=true`, `SESSION_IMAGE_DEPLOYMENT=comfyui-gpu1` and
   `SESSION_SERVICE_CONTROL_FILE` in the reviewed host config. For shared mode
   (docs/ASSISTANT.md), also set `SESSION_IMAGE2_DEPLOYMENT=comfyui-gpu0`. Keep the existing
   canonical session policy, Kubernetes identity and lock. Requalify the selected
   model before using the current CLI:

   ```sh
   sudo ./bin/workstationctl --config /etc/workstation/workstation.conf session switch ai /var/lib/workstation-evidence/hardware-64gb/hardware.json /var/lib/workstation-session --execute
   ```

   The service moves through draining/starting/ready. Drain is at most 180 seconds
   followed by cancellation; physical release and rollout retain existing bounded
   waits. Failure preserves the journal and closes admission. After any process
   restart, explicitly re-run the qualified session path; never restore a ready flag.
   To change model profiles, first enter maintenance, apply the separately reviewed
   overlay, then request `split`. Never apply a new Pod template to a running model
   as an implicit image-triggered switch. Bridge v1 catalog support is separate.
9. Confirm that the step 1 DNS records resolve to `192.168.30.11` from client
   networks. Verify the certificate chain and SANs from both a browser and an
   IDE host. The certificate is publicly trusted (Let's Encrypt), so no private
   CA is needed. Do not use insecure TLS flags. Check that a client outside
   `192.168.0.0/16` cannot connect, and that `http://` (port 80) is refused.
   Generate the existing agent bundle with the HTTPS values from `client.conf`;
   `AGENT_RETRIEVAL_ENABLED=true` adds the read-only MCP tool to Qwen. Provide
   `WORKSTATION_AGENT_API_KEY` and the separate user-owned
   `WORKSTATION_RETRIEVAL_API_KEY` through the client credential store. Neither is
   written into the bundle. Keep Qwen tool approval enabled and ACP beside project
   files. DSH/Hermes retain their existing inference path; their MCP UI integration
   has not been qualified here.

## Image package and lifecycle

The narrow adapter uses WebUI's native OpenAI image connector and sends one fixed workflow
straight to ComfyUI's API: slot 0 to `comfyui-gpu1` (GPU 1), slot 1 to `comfyui-gpu0` (GPU 0)
in shared mode, so two pictures can run at once. It reads the result back through ComfyUI's
`/view`. SwarmUI is not in this path: its API runs its own model-load job before a raw
workflow and, for Qwen-Image, tried to download files the network policy blocks (found
2026-11-14). Public consumers cannot upload workflows or reach ComfyUI's administration API.

The deployed picture image is the AI stack's `creative-gfx1201` build (ComfyUI 0.45 and
SwarmUI on the same ROCm and PyTorch as SGLang; see
[image generation stages](../infrastructure/ai-stack/README.md#image-generation-stages)).
The rest of this section describes the earlier standalone creative package:
`infrastructure/creative/runtime.json` pins Swarm, Comfy, ROCm/.NET build images,
the qualification-time SDXL identity/checksum and bundled custom-node source. Pictures
now use Qwen-Image-2512 (`infrastructure/ai-stack/models/qwen-image-2512.json`): the
checked-in `qwen-image-workflow.json` fixes node types and parameters; only prompt, bounded
size and seed are substituted by the adapter. It uses stock nodes only (the picture ComfyUI
on GPU 1 runs with `--disable-all-custom-nodes`) and saves with `SaveImage`. Each ComfyUI
reads the picture files from `Image/` through its `comfy-model-paths.yaml`. Stage the checkpoint at the exact path
in the contract. Keep `Models` read-only after staging where the packaged backend
permits it; `Data` and `Output` must remain persistent and writable.
The packaged launcher creates Comfy's user/input/output directories there and
uses `/tmp/comfy-temp` for temporary files. Its managed-node directory is created
at build time; application source need not become writable. Existing persisted
`Data/Backends.fds` is never overwritten by the init container: review its
`ExtraArgs` against the checked-in backend before qualifying an upgrade.

Prepare clean pinned source trees, a reviewed NuGet `packages.lock.json`/package
closure, and a ROCm-image-compatible wheelhouse with a complete hash-pinned
`requirements.lock`. The runtime uses a standalone Python 3.14 environment with
the pinned ROCm 10.2/Torch 2.14/torchvision closure. The parent image is ROCm
10.1; inheriting its Python packages or library-path overrides is not supported.
Include the matching Ubuntu shared-library packages for `libxcb1`, `libgl1` and
`libglib2.0-0t64` under `wheels/runtime-debs`, with their complete dependency
closure and `SHA256SUMS`. Resolve these through the base distribution's signed
APT metadata. The image extracts these files; it does not install host packages.
Then run on a reviewed Linux amd64 builder:

```sh
python infrastructure/creative/prepare.py /staging/SwarmUI /staging/ComfyUI /staging/nuget /staging/wheels /staging/requirements.lock /staging/new-context
docker build --network=none --platform linux/amd64 -t workstation-creative-candidate /staging/new-context
```

The context records staged file hashes; the image checks the loaded HIP/RCCL
library hashes and a native torchvision CPU operator, then records its Python
inventory. Swarm uses its upstream build command with a self-contained runtime;
publishing flattens LLamaSharp's CPU variants into conflicting file paths.
The launcher creates fresh private .NET mutex storage on every start, including
when the container's numeric UID has no user database entry.
`AutoUpdate=false` does not disable upstream dependency repair, so the
candidate fails attempted runtime pip repair explicitly. No third-party custom
node download is part of this profile.

The 1 November 2026 smoke built the application offline and assembled a temporary
OCI candidate on the workstation through the retained `regctl` path. One R9700
generated a 512² SDXL PNG in a 5.16-second cold generation HTTP request. The test
client then failed on an unescaped filename space; the existing artifact was
recovered without another generation. Native generation worked, but that client
run remains failed. A fresh native-1024 test of the same image/model completed
one generation and the HTTP PNG fetch: 6.62 seconds to backend readiness and
10.74 seconds for the first generation request. The cabin/lake image was visibly
more photographic. This is one prompt, not general model-quality qualification
or a performance comparison across resolutions. The adapter already defaults
to 1024²; its accepted sizes and deployment defaults have not changed.
The complete Docker recipe, WebUI delivery, cancellation/recovery and persistence
tests remain open.
See `test-results/comfy-build.mnFNFExJ/HANDOFF.md` for identities and evidence.
The follow-up is sealed in `test-results/comfy-native1024.MuD59y10/HANDOFF.md`.
The checked-in deployment remains zero replicas with an `UNQUALIFIED` image.

The pinned Swarm dependency ImageSharp 3.1.12 has known vulnerability warnings.
A 3.2.0 candidate restored, then refused to build without a Six Labors licence
key. That attempt is preserved; no licence or audit check was disabled. The smoke
used only a fixed prompt and generated PNG. Resolve this dependency before
deployment. Do not promote the smoke digest or lower security to boot it.

The adapter permits one image per request at a Qwen-Image size (1328×1328 or the nearest of
1664×928, 1472×1104 and 1584×1056, and their portrait forms), a 2,000-character
prompt, no workflow macros, and an 8 MiB PNG result. It runs one picture at a time, or two
in shared mode. Generation is bounded to 240 seconds (measured 72–88 s, two at once included)
and the whole request to 270, inside Open WebUI's 300-second client timeout. Cancellation
removes only the adapter's own prompt from that ComfyUI's queue, or interrupts it by
`prompt_id` if it is running, so a video on the same card is untouched. Any submitted
generation without a successful terminal result retains a durable unresolved marker: an
interrupt acknowledgement does not prove GPU completion. Restart
cannot fabricate success or launch a duplicate. Recovery: enter maintenance;
verify the ComfyUI Pods and DRM holders
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
file. `API_URL` is the actual HTTPS origin on the standard port, for example
`https://api.lan.thespry.family`. System CA trust must already trust the
selected certificate; no `-k` override is permitted:

```sh
umask 077
curl --config "$CLIENT_CURL_CONFIG" --fail --silent --show-error --max-time 15 "$API_URL/v1/models" > /private/model-list.json
curl --silent --show-error --max-time 15 --output /private/unauthorized.json --write-out '%{http_code}\n' "$API_URL/v1/models"
curl --config "$CLIENT_CURL_CONFIG" --fail --silent --show-error --no-buffer --max-time 190 \
  --header 'Content-Type: application/json' \
  --data '{"model":"Qwen3.8-27B-MXFP4","messages":[{"role":"user","content":"Reply with the word ready."}],"max_tokens":16,"stream":true}' \
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
| Images and video | In shared (or split) mode: generate one image and one short clip, restart WebUI/Swarm, retain the artifacts; a user without picture permission is denied; large mode returns bounded unavailable |
| Handover | Concurrent mode changes cannot acquire the same lock; verify actual devices released before allocation; failure keeps recoverable state and no fabricated completed image |
| Recovery | Complete independent SSD and S3 drills in SERVICE-RECOVERY.md, including login/conversation/document/retrieval/image, gateway keys and gaming-home state |
| Alerts | Inject an authorized test alert, confirm owner receipt/resolution; maintenance inhibits model outage only; stop the observer's HTTPS target to verify independent failure detection |

## Namespace, model storage and serving contract

**Namespace.** [`apps/base/namespace.yaml`](../apps/base/namespace.yaml) is
the only definition of the `ai-home-lab` Namespace. It enforces the
commissioned Pod Security level, `restricted` (audit and warn `restricted`),
which the live namespace already enforces from the earlier AI-stack file.
[`infrastructure/ai-stack/k8s/models.yaml`](../infrastructure/ai-stack/k8s/models.yaml)
now holds only the `ai-models` claim and references this Namespace; apply
`apps/base/namespace.yaml` first. Every Pod template rendered from `apps/`
overlays and the AI-stack fetch, qualification and bench Jobs set the fields
the restricted level checks (non-root, no privilege escalation, all
capabilities dropped, `RuntimeDefault` seccomp, allowed volume types); this was
checked offline only, so confirm admission with `kubectl apply --dry-run=server`
before the first apply. No workload needs `baseline`. A workload that does
must use its own namespace, not a weaker label here.

**Model storage (for the serving owner).** The AI stack mounts its weights
from the NAS-backed `ai-models` claim (`nas-nfs`, ReadWriteMany). The NAS
storage guide (HP-ProliantGen10-DebianNAS `docs/KUBERNETES-STORAGE.md`)
recommends a workstation SSD/NVMe cache, populated from the NAS, for model
files used during inference, to avoid repeated model-load traffic and NAS
latency. The serving profile mitigates load cost (serial loading, no
multithreaded NFS reads, page-cache release) but does not cache locally. This
difference is recorded here and in the serving contract's `assets.storage_note`;
it is not changed by the consumer work.

**Serving contract.** `service render --serving-profile` renders the
assistant composition from the AI-stack serving profile (see
[ASSISTANT.md](ASSISTANT.md#serving-contract)). Operator steps, all owner-run:

1. Obtain the runtime image digest for the profile's default arm from the
   serving owner, and confirm the profile and catalogue status.
2. Render with `--serving-profile` and `--runtime-image`; read `status` and
   `missing_inputs` in `evidence.json`. Do not apply an `incomplete` render.
3. Issue virtual keys for `Qwen3.8-27B-MXFP4` with `service provision`; keys
   allowlisted only for the FP8 alias cannot call it.
4. Copy the `client.conf` values into the client configuration and regenerate
   the agent bundle.
5. Apply only after the profile is qualified, `service plan` admits the
   render, and `service_session.py`/`session.sh` accept the new alias.

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
