# Monitoring the workstation

Open Grafana's **Workstation / Overview**, then use the links to drill into GPUs,
Kubernetes, serving, host/storage or power. The default stack runs on bare-metal
K3s, separate from the optional VM lab. Retained target tests exercised real
collectors and dashboards. They do not establish today's health or negligible
benchmark overhead.

The [3 November component checks](VERIFICATION.md) passed Grafana dashboard and
Prometheus-query checks across restart, Loki/Tempo ingestion and retrieval,
Alloy privacy processing and Alertmanager routing. Those disposable fixtures
do not replace current target-health checks or a workload soak test.

For installation and collector internals, use the
[configuration reference](TELEMETRY-REFERENCE.md). All paths below refer to a
reviewed checkout unless they explicitly name an installed runtime.

The workstation sends telemetry to the MicroServer, which runs Grafana,
Prometheus, Loki and Tempo. Use the private NAS HTTPS address for Grafana. The
port-forward below applies only to installations with local backends. See the
NAS repository's `docs/WORKSTATION-TELEMETRY.md` for the receiver.

## Send telemetry to the NAS

Set `TELEMETRY_NAS_RECEIVER=microserver.lan.thespry.family` in the private
`workstation.conf`. The render then keeps only Alloy, kube-state-metrics and
node-exporter. Alloy sends metrics, logs and traces to the receiver on TCP 8080
and scrapes the NAS backends' self-metrics through it, so dashboards keep their
job labels. Collector configurations use the DNS name. The name is resolved
once at render time, because the collector's egress NetworkPolicy can only
match addresses; `evidence.json` records the resolved address. Re-render if
the NAS address changes.

The receiver admits one workstation source address
(`microserver_telemetry_client_address` in the NAS profile). Confirm that this
host reaches it from that address before applying:

```sh
curl -fsS -o /dev/null -w '%{http_code}\n' http://microserver.lan.thespry.family:8080/ready
```

The owner applies the render and starts the host collector:

```sh
workstationctl --config /private/workstation.conf telemetry render ./telemetry-nas
workstationctl telemetry verify ./telemetry-nas
kubectl apply -f ./telemetry-nas/stack.yaml
kubectl -n workstation-observability rollout status deployment/alloy --timeout=300s
```

The render also writes `host.alloy`, already pointed at the receiver. Replace
its one remaining placeholder, `192.0.2.4`, with the cluster Alloy Service
ClusterIP (`kubectl -n workstation-observability get svc alloy`); the host
resolver cannot resolve cluster Service names. Then install it as
`/etc/workstation-telemetry/host.alloy` and start the collectors as described in
[Host forwarding and Bridge access](TELEMETRY-REFERENCE.md#host-forwarding-and-bridge-access).
No Grafana credentials are created on the workstation in this mode.

## Host-wide eBPF collector

With the NAS receiver set, `TELEMETRY_EBPF=true` adds the `alloy-ebpf`
DaemonSet in `workstation-observability-host`. It runs privileged with the
host PID and network namespaces, by owner decision for this lab, and sends
directly to the NAS receiver:

- **Request metrics (Beyla).** Request rate, errors and duration for every
  HTTP, gRPC, SQL and Redis service in `ai-home-lab`, measured in the kernel
  without application changes. Routes are grouped heuristically; URL paths,
  client addresses, payloads and GenAI content are not collected, and no
  traces are exported.
- **Continuous CPU profiles (Pyroscope).** Every host process, including the
  AI-stack builds, labelled only by executable name (`host/<exe>`). Command
  lines and working directories are not exported.

Profiles need the NAS Pyroscope backend (the receiver's profiles component).
The collector's own health uses the `alloy-ebpf` job.

## Open Grafana

On a machine with the approved cluster access, confirm the context and start a
loopback-only forward:

```sh
kubectl config current-context
kubectl -n workstation-observability port-forward --address 127.0.0.1 svc/grafana 3000:3000
```

Open `http://127.0.0.1:3000/d/workstation-overview` on that machine. For remote
access, use the existing owner-operated SSH/VPN path. Credentials come from the
existing private setup; do not reset an existing Grafana database to gain access.
Services remain ClusterIP-only, with no anonymous Grafana login or public OTLP.

## Choose a dashboard

Start with 30-second scrapes, 10% Bridge traces and SGLang tracing disabled. Open
**Workstation / Overview** in Grafana's **Workstation** folder. Its existing
`/d/workstation-overview` bookmark remains valid. The links across the top open
these views and preserve the selected time range and matching filters:

| View | Question it answers | Details and filters |
| --- | --- | --- |
| Overview | Does the workstation have headroom? | CPU, available RAM, root filesystem, PSU DC power, serving availability, alerts and separate GPU bars |
| Kubernetes cluster | Is K3s ready to run work? | Node readiness/pressure, active Pod counts, deployment desired/available replicas, regular-container requests versus node allocatable, GPU requests and PVC state/capacity |
| Kubernetes Pods | Why is a Pod waiting, restarting or slow? | Namespace, Pod and phase filters; readiness, unschedulable/waiting reasons, last termination, restarts, actual CPU/memory, budgets and throttling |
| GPUs | What is happening on each card? | GPU filter; per-card package power, junction, memory and edge temperatures, compute activity, clocks, VRAM and identity from the AMD exporter (readable while a card sleeps); runtime power state and PCIe link width and speed from node-exporter's `pcidevice` collector |
| Host & storage | Is CPU, RAM or storage limiting work? | Continuous CPU flame graph of host processes (eBPF), CPU and memory trends, pressure stalls, filesystem capacity, block-device I/O filter, CPU/NVMe temperatures, reported CPU frequency and exposed ECC counters |
| Workloads | Are requests waiting, and what resources do containers use? | Sections for model serving (queue, KV pool, generation rate, TTFT/inter-token, speculative acceptance, prefix cache, p95 end-to-end and queue latency, retractions), gateway and eBPF service requests, containers (namespace filter, working set, CPU, budgets, throttling, restarts, GPU activity and VRAM by pod) and AI-stack builds (stage state, duration, compiler-cache hit ratio) |
| Power & energy | How do power, heat, energy and electricity costs change together? | PSU DC power/rail history, GPU power/temperature, cumulative energy and coverage; collapsible Electricity costs section with current cost/hour, projections, selected-window estimates, separate household standing charge and API/cache/manual status |
| Monitoring health | Can these observations be trusted? | Scrape targets, alerts, sample age, collector failures and filtered installation/kernel logs; Explore links for logs and traces |

Click an overview GPU bar to open that card's drilldown. Cards are named by the
AMD exporter's GPU id, which follows PCI order: GPU 0 is `0000:23:00.0` and GPU 1
is `0000:c3:00.0`. The PCI link and power-state panels are keyed by PCI address.
The dashboards refresh every 30 seconds. Use the same time range when comparing
views.

An empty panel means missing observations or a stopped workload, not measured
zero. The AMD exporter keeps reading a runtime-suspended (idle) card, so GPU
charts continue while a card sleeps. The **PCI runtime power state** panel shows
the kernel's PCI power state for each GPU display function: D0 is active, D3hot
is runtime-suspended. It is not a power reading. Missing measurements remain
gaps, rather than substituted zero activity.
An offline SGLang endpoint leaves its latency and throughput panels empty. The
generation-token counter measures server work and is not the benchmark's
delivered-output throughput. Container working set is separate from memory
requests and limits. PSU DC energy is cumulative over covered samples, not wall
energy or energy consumed within the selected dashboard range.

The `metrics` profile retains all eight dashboards. It replaces the log panel with
an explanation that logs and traces are disabled, and removes their Explore
instructions. The `full` profile retains the filtered log panel and Explore
instructions. Both profiles use the same dashboard sources and navigation.

The Kubernetes views include the explicitly configured namespaces: `default`,
`ai-home-lab`, `kube-system`, `kube-amd-gpu`, `kube-node-lease`, `kube-public`,
`workstation-observability` and `workstation-observability-host`. New namespaces
need an explicit collector update. Both object-state and optional cAdvisor
collection use this list. Metric families, sample/body limits, resource limits
and existing read-only RBAC remain bounded; Secret objects and arbitrary Pod
labels, logs or Kubernetes Events are not collected.

The Pod phase filter defaults to Pending, Running and Unknown. Retained
Succeeded/Failed diagnostic Pods appear in a separate history panel; select all
phases in the Pods dashboard to inspect them. Active readiness and allocation
views exclude those terminal Pods. Container requests exclude init containers,
Pod-level resource declarations and overhead, so they are not the scheduler's
effective reservation. GPU resource slots are not measured device ownership.
PVC requested capacity is not filesystem usage or a hard quota. API-server and
etcd latency and DaemonSet/StatefulSet desired-state metrics are not collected.
After a collector restart, removed Pod series can remain briefly visible within
Prometheus's lookback window. Confirm the settled state before interpreting a
short-lived extra Pod count as a new workload.

```promql
node_memory_MemAvailable_bytes{job="node"} / 1024^3
rate(node_pressure_memory_waiting_seconds_total{job="node"}[5m])
max by (gpu_id) (gpu_used_vram{job="amd-gpu"}) / max by (gpu_id) (gpu_total_vram{job="amd-gpu"})
time() - workstation_hardware_sample_timestamp_seconds
sum(sglang:num_queue_reqs{job="sglang",priority=""})
histogram_quantile(0.95, sum by (le) (rate(sglang:time_to_first_token_seconds_bucket{job="sglang"}[5m])))
```

The empty-priority selector uses the total queue gauge, not per-priority
breakdowns that would double-count. In Loki use `{job="workstation-install"}`
for stages and `{job="workstation-kernel"}` for classifications. Filter Tempo
by `spry-workstation-bridge`; SGLang requires trace qualification. Operation IDs
must not become metric labels. The collector strips unapproved attributes,
exception messages and raw OTel log bodies; filtering is not permission to send
secrets. Keep service identities and attribute values bounded.

Combine these observations with [performance validation](PERFORMANCE-VALIDATION.md).
NVMe firmware/SMART, RAID geometry, IPC/P2P correctness, ECC stability,
frame-time tails, distinct captured frames and wall power remain separate
measurements. No root SMART exporter, eBPF/profiler daemon or second controller
is installed. GPU board power is not wall electricity consumption.

Compare telemetry off/on with matching model, prompts, context, concurrency and
thermal conditions. Separate warmup and repeated steady-state samples; retain
TTFT/inter-token latency, throughput, peak memory, throttling, power and spread.
Change scrape rate or trace sampling independently. Retain changes only when
diagnostic value outweighs measured cost. Available RAM and fixtures cannot
establish negligible overhead.

## Host sampling, journal forwarding and Bridge

### Check local monitoring readiness

Run on the installed Arch workstation:

```sh
./bin/workstationctl telemetry status
systemctl status workstation-telemetry.timer workstation-alloy.service
```

`telemetry status` returns 0 for observed local prerequisites, active collector
units and a fresh producer timestamp; 10 for incomplete setup; 1 for unavailable
or failed discovery. The sampler service can be inactive between timer runs.
This check does not validate cluster ingestion or sensor accuracy. Start without
sudo to see the caller's actual read access. A sample older than 90 seconds or
more than 10 seconds in the future is not fresh.

For packages, sensors and activation, follow the
[collector setup reference](TELEMETRY-REFERENCE.md#sensor-drivers-and-unavailable-channels).
Bridge's policy and loopback forwarding are described in the
[Bridge section](TELEMETRY-REFERENCE.md#bridge-telemetry). Full telemetry includes
filtered logs/traces; `metrics` omits Loki, Tempo and journal collection. Neither
profile authorizes arbitrary diagnostics or changes hardware policy.

### Native SGLang metrics outside Kubernetes

The cluster collector scrapes `sglang.ai-home-lab.svc:30000`. That address does
not cover a native or host-network server bound to loopback. The host collector
can scrape one explicitly configured IPv4 loopback endpoint into the same
`sglang` job. The default has no native target. Metrics must already be enabled
on the server; a running process alone does not establish metric availability.

After reviewing the server's actual listener and metric endpoint, configure
the public target in `/etc/workstation-telemetry/sglang-metrics.env`:

```text
WORKSTATION_SGLANG_METRICS_ADDRESS=127.0.0.1:18088
```

Use the actual port, not the example by assumption. Keep this file root-owned,
mode 0640, group `workstation-alloy`. The canonical service reads it optionally.
For an older installed unit, use a drop-in containing `[Service]` and
`EnvironmentFile=-/etc/workstation-telemetry/sglang-metrics.env`, then reload
systemd and restart only `workstation-alloy.service`. Validate the candidate
host configuration with the same environment before activation. No model-server
restart, new listener or Kubernetes network-policy exception is required.

Both host profiles reject empty, non-loopback and invalid-port targets. Scrapes
run every 30 seconds with a five-second timeout and the existing sample, label
and body limits. Only SGLang metric families and scrape health are forwarded;
request/session/user/content labels are dropped. The existing bounded host
remote-write path and 512-MiB collector limit remain in use. This adds monitoring
HTTP traffic, so record it for benchmark comparisons; it is not generation work.
Remove the target setting when the temporary server retires or changes port.

SGLang's negotiated metrics response can use `sglang_` names even when a plain
`curl /metrics` shows `sglang:` names. Both collector profiles normalize the
underscore prefix to the dashboard's colon prefix only for `job="sglang"`.
The host performs this normalization before its metric-family allowlist;
privacy filters and scrape limits remain in force. Validate nonempty model
series as well as `up=1`: successful HTTP scraping alone can hide a downstream
filter that drops every model sample. Compare `scrape_samples_scraped`, the
collector's relabel counters and the final Prometheus series when diagnosing
this case. This change does not require a model-server restart.

Serving panels use either source. Kubernetes CPU/memory/Pod panels cover Pods;
a native model process will not appear there. Use Host and GPU views for its
system-level resources. A failed Kubernetes scrape can coexist with a healthy
native scrape; inspect `up{job="sglang"}` by instance before concluding the
model is stopped. No history can be recovered for periods before scraping began.

## Flexible Octopus prices and electricity estimates

The **Power & energy** dashboard's **Electricity costs** section values measured PSU DC output at the selected
electricity price. This is an estimate: the sensor excludes PSU conversion losses
and is not an AC wall meter. The household standing charge is shown separately;
it is not added per workstation, GPU, request or task. The selected-window value
uses covered counter differences and the **current** price. It does not reconstruct
historical billing after a tariff change. The counter endpoints are sampled near
the requested range boundaries, so the displayed covered hours can differ
slightly from the nominal range. Missing edge samples, counter resets,
expired tariff observations and measurement gaps remain visible as unavailable
or reduced coverage. No energy is extrapolated across unobserved intervals.

The initial public configuration in
`infrastructure/observability/host/tariff.json` retains the owner's supplied
28.11 p/kWh and 58.43 p/day, assumed VAT-inclusive. Both identity fields are null,
so this configuration makes no network requests. Do not infer a tariff from
rounded prices. Ask the owner for the exact single-rate Flexible electricity
code (`E-1R-VAR-…-<region>`) and whether payment is `DIRECT_DEBIT` or
`NON_DIRECT_DEBIT`. Do not request account credentials, meter identifiers or an
API key. This helper does not support Economy 7, Agile, Tracker or prepayment.

Octopus's [public product and price API](https://developer.oegb-kraken.energy/guides/rest/api-endpoints/)
provides `value_inc_vat` in pence, effective dates and payment methods. The
refresher selects one currently effective, unambiguous row per rate. It checks
both unit and standing prices together. It rejects redirects, foreign pagination
URLs and oversized responses; TLS verification remains enabled. The API check
uses at most four 128-KiB pages per rate with an eight-second request timeout.
The service deadline is 90 seconds. A successful refresh replaces the private
last-good cache atomically. During an API outage, a matching unexpired cached
rate can be used for up to 26 hours; otherwise the supplied manual rates apply.
The dashboard labels API, cached and manual sources. Publication expires after
two hours or at the API rate's effective end, whichever comes first.

The Bridge Runtime package includes the helper, templates, example configuration
and inactive units. After reviewing the additional memory allowance above, the
owner can enable the helper on the mapped test workstation:

```sh
sudo systemd-sysusers /usr/lib/sysusers.d/workstation-telemetry.conf
sudo install -d -o root -g root -m 0755 /etc/workstation-tariff
sudo install -o root -g root -m 0644 \
  /usr/lib/bridge/workstation-runtime/infrastructure/observability/host/tariff.json \
  /etc/workstation-tariff/config.json
# Set the exact tariff_code and payment_method in this nonsecret JSON to enable API refresh.
sudo systemctl enable --now workstation-tariff.timer
sudo systemctl start workstation-tariff.service
sudo systemctl show workstation-tariff.service -p Result -p ExecMainStatus -p MemoryPeak
```

The `workstation-tariff` user has its own `/var/lib/workstation-tariff` state
directory. It cannot write the hardware sampler's counters or state. Node
Exporter reads its public `tariff.prom` through a second textfile directory;
no inbound listener is added. The timer refreshes hourly with up to five minutes
of jitter. A configuration change needs a service restart, not an image rebuild.
Stop and disable only `workstation-tariff.timer` and `workstation-tariff.service`
to suspend price refresh. Preserve the configuration and cache for rollback.
Do not stop the hardware sampler. Without fresh tariff publication the cost
panels become unavailable while hardware dashboards continue to operate.

## Prepare and render without deploying

Use the existing config example with actual private API/node addresses. Set
`TELEMETRY_ENABLED=true`, select `TELEMETRY_PROFILE=full` or `metrics`, and keep
`TELEMETRY_GPU_EXPORTER=false` and `TELEMETRY_SGLANG_TRACE=false` until their
separate acceptance passes. Kubelet metrics require the exact node name, valid
TLS and the existing narrow RBAC. No `nodes/proxy` or TLS bypass is needed.

| Profile | Minimum allowance | With optional tariff helper |
| --- | ---: | ---: |
| `full` | 6656 MiB | 6720 MiB |
| `metrics` | 4864 MiB | 4928 MiB |

These allowances include the 768-MiB planning margin, not measured RSS or an
OS reservation. Set `TELEMETRY_RESERVE_MIB` accordingly and admit the complete
workload against fresh host resources. Include `--tariff true` in the render
when enabling the tariff helper. PVC requests and sampled output bounds are not
filesystem quotas. See [capacity details](TELEMETRY-REFERENCE.md#memory-cpu-and-storage).

```sh
./bin/workstationctl --config /private/workstation.conf \
  telemetry render /private/evidence/telemetry-01 apps/overlays/dual-gpu
./bin/workstationctl telemetry verify /private/evidence/telemetry-01
./bin/workstationctl --config /private/workstation.conf \
  hardware collect /private/evidence/hardware-01
./bin/workstationctl --config /private/workstation.conf \
  resources plan /private/evidence/hardware-01/hardware.json /private/evidence/resources-01
./bin/workstationctl telemetry plan \
  /private/evidence/resources-01/resource-plan.json \
  /private/evidence/telemetry-01 /private/evidence/capacity-01
```

Only hardware collection requires the target. Render/verify/plan do not connect
to a cluster. Fresh outputs contain `stack.yaml`, `workloads.yaml`, `evidence.json`
and hashes. A blocked plan is retained and does not authorize deployment.
`TELEMETRY_ENABLED=false` creates a disabled render; it does not uninstall a stack.

## Owner deployment after boot/recovery

Follow the [deployment procedure](TELEMETRY-REFERENCE.md#owner-deployment-after-bootrecovery)
for namespace admission, private credentials, reviewed manifests and host units.
Keep the previous render. Do not blindly apply `workloads.yaml` to an active
model: its checked-in zero-replica gates can stop that workload. A dashboard
repair should change only the reviewed dashboard mount/configuration.

The Bridge runtime carries this guide and its reference, but not every linked
test or installation guide. Use the matching full checkout for acceptance tests.
Package/image publication and installation are separate operations.

## Installation and interruptions

The installer can emit fixed local stage events before any monitoring stack
exists. Retain only the selected private events before reboot; the live journal
is volatile. An interrupted stage is unknown, and installation has no resume mode.
See [event collection](TELEMETRY-REFERENCE.md#installation-and-interruptions)
and the [installation procedure](INSTALLATION.md).

## Acceptance, failures and rollback

| Check | What it establishes |
| --- | --- |
| `bash tests/test_telemetry.sh` | Offline config, tariff, sensor and dashboard fixtures |
| `bash tests/test_telemetry_stack.sh` | Manifest pins, permissions and local rendering |
| `bash tests/test_telemetry_image_permissions.sh` | Synthetic fixture access and evidence privacy |
| `bash infrastructure/observability/validate-images.sh desktop-linux` | Actual pinned image parsers in an approved disposable local Docker environment; no automatic pulls |
| `make check-strict` | Required source gates; retain failures and explicit skips |
| Fresh `up`, hardware timestamps and nonempty workload series | Live delivery, not inference correctness |
| Matched telemetry off/on workload runs | Actual overhead; Grafana averages alone cannot qualify it |

Common failure checks:

| Symptom | Check |
| --- | --- |
| Grafana unavailable | Pod readiness, last termination/OOM status, memory working set versus limit, existing admin Secret, PVC and private tunnel |
| All host series stale | Sampler result, producer timestamp and host Alloy forwarding |
| VRAM visible but GPU clocks/activity missing | Channel readability and possible runtime suspension; preserve gaps instead of zeroes |
| Serving panels empty while `up=1` | Native target/instance, metric-name normalization and post-filter series |
| Pod budgets visible but actual CPU/RAM missing | Optional kubelet TLS/RBAC, namespace allowlist and cAdvisor target |
| Price remains manual | Exact supported tariff/payment settings, helper result and expiry |
| Energy/cost unavailable | Sensor/state freshness, coverage and counter discontinuities |

On 3 November 2026, a retained read-only investigation found Grafana had reached
its 512 MiB container memory limit and was OOM-killed. The host still had ample
available RAM. Kubernetes restarted Grafana, and its health/login routes then
responded. The investigation changed no limits or deployment settings. Check
container memory and restart evidence before treating another unresponsive
dashboard as host CPU exhaustion.

Rollback restores the previous reviewed render and host configuration through
owner maintenance. Stop only the named collectors/helper being retired. Preserve
PVCs, Secrets, private energy/tariff state and failure evidence; namespace deletion
or counter resets are not rollback. Check storage compatibility before a backend
downgrade and take a recoverable backup. See the
[image/collector reference](TELEMETRY-REFERENCE.md#image-validation-fixture-access).

Compiler-cache inventory and prune commands are
[plan-only](TELEMETRY-REFERENCE.md#inference-compiler-cache-inventory-and-prune-plans).
Shared-service notifications and independent host-outage checks have their
[own acceptance](TELEMETRY-REFERENCE.md#shared-service-notification-extension).
