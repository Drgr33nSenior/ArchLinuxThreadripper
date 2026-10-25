# Workstation telemetry and agent diagnostics

This runbook covers the local Prometheus, Grafana, Loki, Tempo and Grafana Alloy
stack. It runs on the workstation's bare-metal K3s cluster, not the NAS or the
optional KVM lab. The default `full` profile includes all listed components. The
optional `metrics` profile retains Prometheus, Grafana, host/GPU/Bridge metrics
and collector health only. It does not collect logs or traces. Status:
implemented and source/configuration-tested; not deployed or hardware qualified.
No telemetry result authorizes an agent to install, restart services, change
policy or tune hardware.

## Design and authoritative inputs

| Component | Role | Persistence and boundary |
| --- | --- | --- |
| Prometheus | Metrics, recording rules and alert evaluation | Local PVC; 14 days or 24 GB TSDB retention, whichever expires first |
| Grafana | Dashboard and metric/log/trace exploration | Local PVC; owner-created admin Secret; no anonymous login |
| Loki | Structured installation events and classified kernel warnings/errors | Local filesystem object storage; seven-day retention |
| Tempo | Sampled Bridge and optional SGLang spans | Single binary; local storage; 72-hour retention; no Kafka |
| Cluster Alloy | Static scrapes, OTLP filtering, batching and forwarding | Local cursor/WAL PVC; bounded queues; no cloud destination |
| Host Alloy | Filtered journald collection; loopback Bridge OTLP receiver | Private local state; unprivileged, with journal-read group access |
| node-exporter | CPU, RAM, pressure, filesystem, disk and textfile metrics | Read-only host paths, separate admission namespace; no GPU device mounts |
| kube-state-metrics | Pod/deployment state, requests and limits | Read-only Kubernetes RBAC; not actual resource utilisation |
| Optional kubelet scrape | Actual pod memory, CPU time and throttling | Verified TLS and node-specific `nodes/metrics` RBAC |
| Host sysfs sampler | GPU sensors, CPU power-policy state and supported Corsair PSU output power | Atomic textfile and private sampled-energy state; unprivileged timer; no ROCm library injection |

The main namespace is `workstation-observability`. Only
`workstation-observability-host` permits hostPath admission for node-exporter;
its pod still runs non-root, drops capabilities and is not privileged. Read-only
host-root access remains a meaningful permission. Do not grant arbitrary agent
execution as a collector user or expand admission policies to make a pod start.

Services are ClusterIP-only. No Ingress, public OTLP receiver or new public API
is created. NetworkPolicies restrict traffic, with explicit API and workstation
IPs. Node-origin traffic has special Kubernetes networking semantics:
NetworkPolicy is not a host firewall or authentication. Keep management private
on the workstation/LAN/VPN. Start with loopback port-forwards.

Keep one source for each setting:

- `config/workstation.conf.example`: operator choices and planner reserve.
- `infrastructure/observability/versions.json`: image digests, host package
  evidence, primary-source links and limitations.
- `infrastructure/observability/config/`: retention, scrapes, filtering and queues.
- `infrastructure/observability/dashboards/workstation.json`: provisioned dashboard.
- `templates/workstation/telemetry/`: inactive host units and system users.
- `lib/workstation/telemetry.py`: offline rendering, integrity and capacity checks.

Change canonical sources, render into a new private directory, review the diff
and retain the previous render. ConfigMap names include content hashes;
target-address changes produce new pod references. All Deployments use
`Recreate`: upgrades have downtime but do not need duplicate memory or
simultaneous RWO mounts. No new operator or monitoring framework manages them.

## Versions and support

Cluster versions, immutable digests and upstream configuration sources are
authoritative in [versions.json](../infrastructure/observability/versions.json).
Use its selected Tempo single-binary schema when reviewing configuration.

The host uses Arch's `grafana-alloy` package, executable
`/usr/bin/grafana-alloy`, pinned to 1.13.2-1 by the selected package sources.
Validate its config with the matching official 1.13.2 image parser and
log-filter tests before deployment. This intentionally differs from cluster
Alloy 1.19.2: they exchange OTLP, not shared libraries. The Arch package still
needs signature/installation and service qualification. Revalidate after rolling
updates. See [Arch metadata](https://archlinux.org/packages/extra/x86_64/grafana-alloy/)
and [Alloy Linux configuration](https://grafana.com/docs/alloy/latest/configure/linux/).

The two pinned collector versions expose different memory-limiter metric names.
Host Alloy 1.13.2 embeds collector 0.142 and emits the deprecated
`otelcol_processor_refused_*_total` family. Cluster Alloy 1.19.2 embeds collector
0.158 and emits `otelcol_processor_memory_limiter_refused_*_total`. The alert
matches both families and includes spans, log records and metric points. The
rule fixture is evaluated with the pinned Prometheus image; it is not evidence
that a live collector was placed under memory pressure.

The version linkage is from [Alloy 1.13.2's module file](https://github.com/grafana/alloy/blob/v1.13.2/go.mod)
and [Alloy 1.19.2's module file](https://github.com/grafana/alloy/blob/v1.19.2/go.mod).
The collector [0.142 metadata](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.142.0/processor/memorylimiterprocessor/metadata.yaml)
and [0.158 metadata](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.158.0/processor/memorylimiterprocessor/metadata.yaml)
define the two families. The pinned transform can remove scope attributes but
does not provide an individual span-link transform context. The collector
[0.158 transform documentation](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/v0.158.0/processor/transformprocessor/README.md)
therefore supports the fail-closed rule that drops spans with links. The local
fixture checks the selected pinned image behavior with synthetic data only.

Do not infer exporter support from similar R9700/R9700S names. The
`deferred_amd_exporter` entry in `versions.json` records the reviewed artifact,
its older AMD-SMI/profiler inputs and the chart's broad device privileges.
`TELEMETRY_GPU_EXPORTER=true` therefore fails explicitly. The sysfs sampler
does not need it and does not claim unsupported counters. A future candidate
needs an available immutable image, actual R9700 driver/API checks, minimal
device access and measured overhead.

The exact locked AMD image source supports
`--enable-metrics`, and optionally `--enable-trace --trace-modules request
--otlp-traces-endpoint HOST:4317` with `SGLANG_TRACE_LEVEL=1`. There is no
`--trace-level` flag. Source support does not establish that optional OTel
dependencies import or that this model traces correctly. Keep tracing off until
the exact image passes acceptance. See
[SGLang metrics](https://docs.sglang.io/docs/references/production_metrics) and
[request tracing](https://docs.sglang.io/docs/references/production_request_trace).

## Memory, CPU and storage

The full profile's cluster container limits total **4736 MiB**. Host Alloy is
limited to 512 MiB and the sampler to 128 MiB. Its calculated allowance is
6144 MiB: 4736 + 512 + 128 + the explicit 768 MiB planning margin. The metrics
profile removes Loki (1024 MiB) and Tempo (768 MiB), so its calculated allowance
is 4352 MiB. `TELEMETRY_RESERVE_MIB` remains the owner-approved capacity reserve;
it must be at least the calculated allowance and may be higher. Do not allocate
an apparent profile saving to model workloads without a separate reviewed plan.
Neither allowance is a kubelet reservation, an RSS measurement or proof of
consumption. Cluster CPU limits are summed. Host collector/sampler CPU quotas
are 50%/10%; observe throttling before increasing collection rates.

The offline SGLang memory planner can receive the reviewed nonsecret rendered
`evidence.json` through `--telemetry-evidence`. It verifies the selected
profile's schema and requires `--other-mib` to be at least `reserve_mib`; it
hashes that input into the unqualified candidate. This adds a conservative floor
only. Omitting the input remains compatible, but does not prove that telemetry
is absent or make the corresponding capacity available to a model.

Historical lower-memory example: on a nominal 64 GiB machine, existing 12 GiB host, 4 GiB K3s and 2 GiB eviction
reserves leave 46 GiB. A 38 GiB SGLang pod, 2 GiB WebUI and 6 GiB telemetry
allowance consume all of that. Firmware-reserved RAM can lower Linux MemTotal:
a real 64 GiB inventory can correctly fail this plan. Do not round it up or
remove safety reserves to force success. Review workloads and measured memory
before adjusting limits.

SGLang's 16 GiB `/dev/shm` ceiling is inside its 38 GiB pod limit, not extra RAM.
The 38 GiB value is not measured consumption. Use
[startup and serving evidence](PERFORMANCE-VALIDATION.md#right-size-sglang-host-ram)
to propose a smaller cap; Grafana averages alone cannot establish a safe limit.
The RAG overlay's larger WebUI envelope does not fit that historical combination.
VMs, builds, games and other deployments are additional demand. The planner
counts each selected workload at one intended replica even when gated at zero.
It checks limits, not requests; it does not silently lower model context,
quality, concurrency or memory. Rediscover and replan after adding DIMMs.

Five PVC requests total 64 GiB: Prometheus 30, Loki 20, Tempo 10, Grafana 2 and
Alloy 2. Local-path sizes are **not filesystem quotas**. WALs, indexes, compaction
and delayed deletion need space beyond retained payloads. Everything competes
with models/builds on the same encrypted XFS array. Time retention is not a byte
cap. No storage-layout change or NAS replication is introduced. See
[Loki retention](https://grafana.com/docs/loki/latest/operations/storage/retention/).

## Prepare and render without deploying

Run from the reviewed checkout. For the installed runtime, change to
`/usr/lib/bridge/workstation-runtime` and use absolute private config/output
paths. Python needs PyYAML; `kubectl` supplies local Kustomize rendering. The
three telemetry commands do not connect to Kubernetes.

The minimal runtime carries this runbook and the required render assets, not
the full documentation/test checkout. Linked installation, home-lab, ISO and
performance runbooks need the matching reviewed checkout (or its separately
installed documentation). The first four acceptance rows below are checkout
commands; do not assume their test scripts are installed with the runtime.

Copy the existing workstation example into private configuration. Set actual
values, not these example identities:

```sh
TELEMETRY_ENABLED=true
TELEMETRY_API_ADDRESS=192.168.50.10
TELEMETRY_WORKSTATION_ADDRESS=192.168.50.10
TELEMETRY_KUBELET=true
TELEMETRY_NODE_NAME=arch-workstation
TELEMETRY_RESERVE_MIB=6144
TELEMETRY_PROFILE=full
TELEMETRY_MARGIN_MIB=768
TELEMETRY_WORKLOADS=sglang,open-webui
TELEMETRY_GPU_EXPORTER=false
TELEMETRY_SGLANG_TRACE=false
```

The API address is the actual private API-server destination, not an assumed
ClusterIP. The workstation address is the node's private address for host
collector ingress and optional kubelet scraping. Numeric RFC1918/ULA addresses
are required; missing, public, loopback and documentation addresses fail closed.
Review this single-node design before using multiple nodes or API addresses.
`TELEMETRY_KUBELET=false` omits its extra RBAC/configuration.

Set `TELEMETRY_PROFILE=metrics` only after reviewing the generated render. It
uses a Prometheus-only Grafana datasource, a metrics-only cluster Alloy
pipeline, and a metrics-only host Alloy configuration. Loki/Tempo workloads,
their ConfigMaps, NetworkPolicies, datasource navigation, journal collection and
trace receivers are absent. The host self-scrape, bounded direct remote write,
GPU textfile metrics, Bridge metric receiver, collector refusal/export alerts
and missing-signal alerts remain. Existing full renders remain compatible.

## Inference compiler-cache inventory and prune plans

The SGLang cache PVC contains model-adjacent compiler caches. It is not the
model-weight store, retained benchmark evidence, or the build worker's ccache.
`inference_cache.py` inventories only these fixed managed paths below an
owner-supplied host PVC mount:

- `triton/workstation/<cache-identity>`
- `torchinductor/workstation/<cache-identity>`

Leave `INFERENCE_CACHE_ROOT` empty until the owner has discovered that mount.
The tool refuses noncanonical roots, symlinks in the root ancestry or managed
parents, mount crossings, special files, ownership drift, unsafe registry
fields, and inputs larger than its fixed inventory bounds. It records
unregistered namespace entries, but excludes them from every candidate list. It
does not use atime as last-use evidence.

Create the private owner registry separately. Every namespace must specify its
exact relative path, runtime and model SHA-256 identities, expected UID/GID,
active-reference list, last-known-good flag and managed-use timestamp. The
registry is the provenance/liveness input; directory names and hashes alone are
not deletion authority. Keep it owner-private because it identifies runtime
artifacts.

```sh
umask 077
python3 lib/workstation/inference_cache.py inventory \
  /discovered/host/pvc/cache /private/cache-registry.json /private/evidence/cache-inventory-01
python3 lib/workstation/inference_cache.py plan \
  /private/evidence/cache-inventory-01 /private/evidence/cache-prune-plan-01 \
  --reserve-mib 20480
```

The inventory records allocated filesystem bytes (`st_blocks * 512`), not
logical file lengths. It counts a hard-linked inode once across the managed
tree. Active, last-known-good, missing, unregistered and shared-content
namespaces are excluded. The prune plan gives exact disposable candidates,
estimated reclaim, the free-space shortfall and whether reviewed removal could
meet the configured reserve. It always has a plan-only status and performs no
deletion. If disposable space cannot meet the reserve, it returns a retained
`blocked-insufficient-disposable-space` plan. A future deletion executor must
independently revalidate the registry, paths, ownership, active references and
free space. Do not delete cache data during inventory, startup or source tests.

Owner read-only identity checks after K3s installation:

```sh
kubectl config current-context
kubectl get nodes -o wide
kubectl -n default get endpoints kubernetes -o wide
```

Do not print kubeconfig contents. Confirm the context before writes. Create a
private evidence parent outside Git, then use new child names:

```sh
./bin/workstationctl --config /path/to/workstation.conf \
  telemetry render /path/to/evidence/telemetry-01 apps/overlays/dual-gpu
./bin/workstationctl telemetry verify /path/to/evidence/telemetry-01

# Actual workstation only, not the Mac or CI runner:
./bin/workstationctl --config /path/to/workstation.conf \
  hardware collect /path/to/evidence/hardware-01
./bin/workstationctl --config /path/to/workstation.conf \
  resources plan /path/to/evidence/hardware-01/hardware.json /path/to/evidence/resources-01
./bin/workstationctl telemetry plan \
  /path/to/evidence/resources-01/resource-plan.json \
  /path/to/evidence/telemetry-01 /path/to/evidence/capacity-01
```

Inspect `capacity.json`. A non-fitting plan returns failure and retains a
`blocked-capacity` record. Unknown topology stays blocked; fixtures never
authorize deployment. Resource planning does not enable static CPU Manager.

Outputs: `stack.yaml`, `workloads.yaml`, `evidence.json` and `SHA256SUMS`. Evidence
includes source-file hashes, available Git/build identity, images, resolved
choices and `NOT RUN` hardware status. Hashes detect change relative to the
manifest; they are not signatures or a trust anchor. Dirty content hashes matter
more than HEAD alone. Secret resources are rejected and provisioned separately.
`TELEMETRY_ENABLED=false` produces an explicitly disabled empty render; it does
not uninstall an existing deployment.

## Owner deployment after boot/recovery

These are manual owner operations, not agent-run source validation. Complete
[Installation](INSTALLATION.md), recovery and then [Home lab](HOME-LAB.md).
Review context, storage, free space, capacity and generated YAML first.

1. Apply `infrastructure/observability/namespaces.yaml` for namespaces/quotas.
2. Create Grafana credentials locally; never in a command argument, prompt,
   transcript, repository or `--from-literal` option.
3. Apply reviewed `stack.yaml`; verify readiness and private access.
4. For a fresh unstarted application deployment, apply reviewed `workloads.yaml`.
   SGLang stays at zero replicas until its existing qualification/session gates pass.

For an already-running deployment, do not blindly apply `workloads.yaml`: source
replica gates can stop it. Reconcile telemetry changes with actual workload
state and the owner maintenance plan.

Example credential entry in **Bash**, with an existing private RAM-backed
`/run/user/UID` directory and confirmed Kubernetes context:

```bash
(
  set +x
  umask 077
  credential_dir=$(mktemp -d "/run/user/$(id -u)/grafana-credentials.XXXXXX") || exit
  trap 'unset grafana_password; rm -f -- "$credential_dir/admin-user" "$credential_dir/admin-password"; rmdir -- "$credential_dir"' EXIT
  printf 'admin' >"$credential_dir/admin-user"
  read -r -s -p 'New Grafana password: ' grafana_password
  printf '\n'
  [[ -n $grafana_password ]] || exit 1
  printf '%s' "$grafana_password" >"$credential_dir/admin-password"
  unset grafana_password
  kubectl -n workstation-observability create secret generic grafana-admin \
    --from-file=admin-user="$credential_dir/admin-user" \
    --from-file=admin-password="$credential_dir/admin-password"
)
```

This creates rather than overwrites the Secret. Use normal owner rotation for
existing credentials. Bootstrap environment values do not reset an existing
Grafana database password. Restrict Secret access and use the established
cluster encryption/backup procedure.

```sh
kubectl apply -f /path/to/evidence/telemetry-01/stack.yaml
kubectl -n workstation-observability get pods,pvc
kubectl -n workstation-observability-host get pods
kubectl -n workstation-observability rollout status deployment/grafana --timeout=300s
kubectl -n workstation-observability port-forward --address 127.0.0.1 svc/grafana 3000:3000
```

Open `http://127.0.0.1:3000` on that machine. Remote access needs an already
configured SSH/VPN path; the installer does not supply it automatically. Check
all seven infrastructure targets plus SGLang, and optional `kubelet-cadvisor`,
not only Grafana. A stopped SGLang is expected to be down. Alloy performs the scrapes, so
Prometheus's empty scrape configuration is intentional. Rules are visible in
Prometheus; no Alertmanager/external notification route is provisioned.

## Host sampling, journal forwarding and Bridge

### Check local monitoring readiness

Installation has separate package and activation stages:

| Path | Supplies | Still requires owner action |
| --- | --- | --- |
| Base installation, headless or desktop | `lm_sensors`, `dmidecode`, `nvme-cli`, `smartmontools` | Select Alloy and activate telemetry after boot/recovery qualification |
| Signed bootstrap package | Installer source and recovery commands | It does not install the target package list merely by being installed on the live ISO or host |
| Bridge runtime package | Monitoring helper, inactive host units and both profiles' render assets | Optional diagnostic/Alloy packages, reviewed configuration and activation |
| `dev setup monitoring` | The four diagnostic packages plus `grafana-alloy` in a full upgrade transaction | Host collector setup and cluster deployment |
| `telemetry render`, `full` or `metrics` | Local configuration artifacts | Applying a reviewed render and activating host collectors; rendering starts nothing |

An older base install that omitted the four diagnostic packages needs the
package correction. Missing Alloy, inactive units or an absent cluster stack
can instead mean the optional stage was never selected or applied. Check each
stage separately; a package list or successful render is not activation evidence.

On the installed Arch host, run this read-only check from the installed runtime
or a reviewed checkout:

```sh
./bin/workstationctl telemetry status
```

Exit `0` means the local diagnostic packages, active collector units and a fresh
hardware textfile were observed. Exit `10` means local setup is incomplete;
exit `1` means discovery is unavailable or failed. This is not a cluster scrape,
sensor-accuracy or complete hardware-coverage check. An inactive sampler service
is normal between timer invocations; its last result must still be successful.
The timestamp comes from the producer, not file modification time. Samples older
than 90 seconds or more than 10 seconds in the future are not fresh.

The report separates packages, unit states, textfile freshness and existing
hwmon channels. It also reads exposed CPU energy and ECC counters without
changing their permissions. Empty inventories and null values mean unavailable,
not zero. A runtime-suspended GPU can expose channel files that currently refuse
reads. Do not disable runtime power management to make a monitoring check pass.
Run without `sudo` first to see the current user's actual read access.

The installer explicitly includes `lm_sensors`, `dmidecode`, `nvme-cli` and
`smartmontools`. These tools do not automatically probe buses, run drive
self-tests or enable monitoring services. For an older installation, the
`monitoring` package profile selects those tools and `grafana-alloy` without
selecting the unrelated AI, desktop or virtualization packages:

```sh
# Installed runtime; use the root of the matching checkout if Bridge was omitted.
cd /usr/lib/bridge/workstation-runtime
# Read-only preview using the current cached package databases; no refresh or download.
pacman --sync --sysupgrade --print --needed --print-format '%n %v' -- \
  lm_sensors dmidecode nvme-cli smartmontools grafana-alloy
# Owner action after reviewing the full update and recovery prerequisites:
sudo ./bin/workstationctl dev setup monitoring templates/workstation/packages.pacman
```

The `sudo` command is an owner-run **full package transaction**. The cached
preview is not a future package lock; review the actual transaction too. Complete
the [update prerequisites](OPERATIONS.md#host-updates), retain the selected
repository snapshot and review the transaction first. Do not run it during a
model experiment. It does not enable the packaged telemetry units or deploy the
cluster stack. Continue with the host setup below and the reviewed stack
deployment; do not install a second host node-exporter service.

### Sensor drivers and unavailable channels

Read existing hwmon channels with `sensors`. Do not run `sensors-detect`, scan
I2C buses or force-load candidate drivers during workloads. A kernel module
being installed or loaded does not prove that it has bound to a device.

| Source | Driver/interface | Collection boundary |
| --- | --- | --- |
| CPU temperature | `k10temp` hwmon | Use the exposed label, such as `Tctl`; do not invent per-core temperatures |
| Radeon GPUs | `amdgpu` hwmon and BDF-keyed sysfs | Preserve unavailable readings and each GPU's identity |
| NVMe temperature/health | `nvme` hwmon; `nvme-cli` and `smartmontools` | Temperature alone is not a complete drive-health report |
| Corsair PSU | `corsair_psu` USB hwmon | DC output is not measured wall-input power; see below |
| ECC memory errors | `amd64_edac` sysfs | Both profiles enable node-exporter's `edac` collector; absent counters do not prove error-free memory |
| CPU package/core energy | Powercap, including `intel_rapl_msr` on supported AMD hosts | Energy counters can wrap; core and package domains overlap. The unprivileged sampler does not collect root-only counters |
| Board fan headers and DDR5 temperatures | Exact Super I/O chip driver and supported SPD hub, for example `spd5118` | Availability depends on chip support, firmware resource ownership and bus access |

The existing node-exporter `hwmon` collector carries readable temperatures,
fans, voltages, current and power. Benchmark snapshots retain those channels
and labels too. The ECC collector uses the existing read-only sysfs mount;
it does not install a driver, persist a RAS event history or require root.
Do not change sysfs permissions or run the exporter as root to obtain CPU energy.
That needs a separately reviewed narrow privileged reader.

If the SMBus device is unbound, inspect its PCI identity, module alias and boot
log. An ACPI `SystemIO`/`OpRegion` conflict means firmware owns an overlapping
resource. Do not use `acpi_enforce_resources=lax`, `force_addr`, `force_id` or a
forced bind as an installation workaround. Retain the exact board/BIOS/kernel
identity and conflict range for firmware or upstream-driver review. An iTE
controller family name alone does not justify an out-of-tree `it87` package.
The [kernel SPD5118 documentation](https://docs.kernel.org/hwmon/spd5118.html)
describes supported DDR5 temperature hubs; it does not establish that a
particular DIMM or inaccessible SMBus is supported.

### Configure the host collectors

The signed `arch-workstation-bridge-runtime` package owns telemetry helpers,
units, sysusers, source manifests and this runbook. Units remain inactive. The
server package profile includes `grafana-alloy`; a minimal runtime install
declares it optional. Use the documented full Arch package/upgrade transaction,
never a partial library upgrade.

```sh
pacman -Q arch-workstation-bridge-runtime grafana-alloy
pacman -Qo /usr/bin/grafana-alloy \
  /usr/lib/systemd/system/workstation-telemetry.service \
  /usr/lib/systemd/system/workstation-alloy.service
grafana-alloy --version
systemctl is-active workstation-alloy.service workstation-telemetry.timer
```

Inactive is the expected initial result. If package sysusers were not processed,
the owner can run `systemd-sysusers /usr/lib/sysusers.d/workstation-telemetry.conf`.

The sampler timer atomically writes
`/var/lib/workstation-telemetry/hardware.prom` every 30 seconds. It is readable
by node-exporter's textfile collector. The sampler cannot write device settings.
It exports PCI BDF identity, VRAM, busy percentage, negotiated link width, selected
DPM clocks, available hwmon temperatures/power, CPU driver/governor/EPP and boost.
Missing sensors are not zero: availability gauges identify missing core readings;
unsupported thermal/power series remain absent. Selected clocks are not sustained
clock measurements. Identical cards remain separate series; changed enumeration
requires reinspection. No GPU allocation or combined VRAM is inferred.

### Corsair PSU power and tracked energy

Connect the PSU's supported USB telemetry cable to the motherboard, following
the exact PSU revision's manual. Confirm the label: **HX1500i** is a supported
Corsair HXi model; a similar product name alone does not establish support.
The selected Linux source
[`73e3f0710014fe6d4ed98cfc02292f6121db7558`](https://github.com/torvalds/linux/blob/73e3f0710014fe6d4ed98cfc02292f6121db7558/drivers/hwmon/corsair-psu.c)
includes HX1500i USB identity `1b1c:1c1f`, registers the `corsairpsu` hwmon
name, and exposes read-only power sensors. The installed kernel must have
`CONFIG_SENSORS_CORSAIR_PSU` enabled and bind the actual device. A source pin
does not establish the installed kernel configuration or sensor accuracy.

The sampler automatically discovers one `corsairpsu` hwmon device; it does not
assume `hwmon0` or add a USB/hidraw permission. Multiple matching devices are
ambiguous and produce unavailable values. It reads only fixed sensor labels:
`power total`, `power +12v`, `power +5v` and `power +3.3v`. The hwmon suffix
`_input` means a sensor reading, **not AC input power**. Here the power readings
are DC output watts delivered to components. Rails are components of the total,
not additional consumption. GPU power remains a separate device observation.

No liquidctl/iCUE controller, fan control, rail-mode change, efficiency curve
or new daemon is added. Do not calculate AC watts from volts times amps: that
does not account for power factor. Use a separate measured wall-power source
for socket energy or electricity costs. See the
[Corsair cable instructions](https://assets.corsair.com/image/upload/v1701372411/documents/49_002658_AA_WW_HXi_Series_QSG_Web.pdf)
and the [input-power estimation limitation](https://github.com/liquidctl/liquidctl/blob/v1.15.0/docs/corsair-hxi-rmi-psu-guide.md).

Both telemetry profiles carry these bounded metrics through the existing
node-exporter textfile path:

| Metric | Meaning |
| --- | --- |
| `workstation_psu_output_power_watts` | Valid reported DC total, never a sum of total and rails |
| `workstation_psu_rail_power_watts` | Separate available DC rail readings; fixed `rail` labels `12v`, `5v`, `3v3` |
| `workstation_psu_available` | Whether one usable total-power sensor was read |
| `workstation_psu_sample_timestamp_seconds` | Actual valid sensor sample time, not the last Prometheus scrape |
| `workstation_psu_output_energy_joules_total` | Persisted estimate integrating covered DC power intervals |
| `workstation_psu_energy_covered_seconds_total` | Cumulative duration included in that estimate |
| `workstation_psu_energy_available` | Whether the private energy state is usable and persisted |
| `workstation_psu_energy_sample_timestamp_seconds` | Time of the corresponding energy-state observation |

The energy estimate uses trapezoidal integration between adjacent valid
samples. It requires the same boot and device, consistent elapsed clocks, and
an interval no longer than 90 seconds. Reboots, missing readings, suspend/clock
discontinuities, device replacement and long gaps do not contribute invented
energy. Coverage excludes those intervals. A first sample has no covered
interval; zero coverage is not evidence of zero consumption. Sampling can miss
fast power changes. This is neither a hardware energy meter nor a complete
electricity bill.

The sampler serializes collection and publication using its private lock. Its
versioned `.psu-energy.json` in `/var/lib/workstation-telemetry` is mode `0600`;
only `hardware.prom` is reader-visible. It durably saves the counters before
publishing them. Invalid state is retained, not reset: energy becomes
unavailable while other valid sensors remain usable. A stopped sampler's last
textfile becomes stale; PSU panels and Bridge use the embedded timestamps,
not scrape freshness, with a 90-second threshold.

Grafana includes DC total/rail history, cumulative estimated kWh and coverage.
One kWh is 3,600,000 joules. The cumulative stat is **not** energy consumed
within the dashboard's selected range. Prometheus retains time-series history
for at most 14 days or 24 GB; the private counter persists independently.
Back up that state with the existing host-state procedure. Do not reset it to
make a graph look healthy. No tariff, billing calculation or PSU alert is
enabled for machines without an explicitly expected PSU.

#### Owner qualification on the installed workstation

These are owner-run checks, not installation actions performed by source tests.
First confirm the exact PSU/cable and the reviewed kernel/runtime package.
The following reads do not change the driver or PSU:

```sh
uname -r
lsusb -d 1b1c:
for name in /sys/class/hwmon/hwmon*/name; do
  [ "$(cat "$name")" = corsairpsu ] || continue
  sensor_dir=${name%/name}
  printf '%s\n' "$sensor_dir"
  cat "$sensor_dir/power1_label" "$sensor_dir/power1_input"
done
systemctl show workstation-telemetry.service \
  -p User -p Group -p PrivateDevices -p NoNewPrivileges -p MemoryMax
```

After the existing reviewed telemetry activation, inspect two or more timer
samples at least 30 seconds apart:

```sh
systemctl status workstation-telemetry.timer workstation-telemetry.service
rg '^workstation_psu_' /var/lib/workstation-telemetry/hardware.prom
stat -c '%a %U %n' /var/lib/workstation-telemetry/.psu-energy.json
```

Require reader-visible watts, fresh timestamps, increasing covered duration,
and private state owned by `workstation-telemetry`. Verify these readings arrive
at the private Prometheus endpoint and both Grafana and Bridge. Compare idle
and an already approved workload; do not launch a GPU stress test solely for
this check. Verify missing/stale behavior during a separately approved
interruption, and that no energy is extrapolated across reboot or USB loss.
A wall meter may read higher because it includes conversion losses; it is not
an equality check against DC output. Inspect timer duration and CPU/memory
overhead; source fixtures do not qualify USB access or the packaged sandbox.

If energy state is corrupt, stop the timer through the normal owner maintenance
procedure and wait until the service is inactive. Retain the exact private
state and diagnose ownership/schema/storage first. Restore only a verified
backup while collection is stopped; never repair arbitrary fields or delete
the file to clear the error. A backup restore can decrease counters, so preserve
that discontinuity in monitoring interpretation. Do not change service
permissions, expose hidraw or run the sampler as root to obtain readings.

### Host forwarding and Bridge access

For host Alloy, obtain actual ClusterIPs. The full profile requires all three
services. The metrics profile requires only Alloy and Prometheus:

```sh
kubectl -n workstation-observability get svc alloy loki prometheus -o wide
```

The owner copies `infrastructure/observability/host/config.alloy` for `full`,
or `infrastructure/observability/host/config.metrics.alloy` for `metrics`, to
`/etc/workstation-telemetry/host.alloy`, root-owned and readable by
`workstation-alloy`. Replace the applicable example addresses with actual
ClusterIPs; IPv6 URLs need brackets. The host's Alloy listener self-scrapes only
its bounded health metric families under the distinct
`workstation-host-alloy` job, then remote-writes them directly to Prometheus.
The full profile also includes journal-drop and journal-write counters. This
path is intentionally separate from host OTLP forwarding. The heartbeat alert
uses a five-minute absence window plus a two-minute pending period; it also
detects loss of the self-monitoring forwarding path. A separate exporter alert
detects `up == 0` sustained for five minutes. Validate before starting:

```sh
sudo -u workstation-alloy /usr/bin/grafana-alloy validate /etc/workstation-telemetry/host.alloy
sudo systemctl enable --now workstation-telemetry.timer
sudo systemctl enable --now workstation-alloy.service
systemctl status workstation-telemetry.timer workstation-alloy.service
```

Only the host collector has supplementary `systemd-journal` access. It can read
more than its exported fields: this is a privileged data boundary despite being
non-root. It accepts only the fixed installer event schema and replaces selected
kernel warnings/errors with subsystem classifications. It does not export raw
boot/application logs. Local raw diagnosis remains an explicit owner action.

Bridge implementation is in the separate `Spry.ai-workstation-bridge` checkout;
its `docs/TELEMETRY.md` owns the policy/schema details. After building/installing
that reviewed candidate, merge this section into the existing `server.json`:

```json
"telemetry": {
  "enabled": true,
  "otlp_endpoint": "http://127.0.0.1:4318",
  "trace_sample_ratio": 0.1,
  "prometheus_url": "http://127.0.0.1:19090"
}
```

It is not a replacement server configuration. Enabled Bridge refuses nonempty
`OTEL_*` environment overrides before SDK parsing. No ambient collector/API
headers are imported. Policy changes need an owner restart of `bridged.service`;
there is no `ExecReload`. Existing credential, runtime-manifest and approved
capability prerequisites still apply.

Host Alloy supplies loopback OTLP 4318; do not also bind a port-forward there.
For the optional bounded diagnostic summary, initially keep a separate
owner-run local Prometheus connection:

```sh
kubectl -n workstation-observability port-forward --address 127.0.0.1 svc/prometheus 19090:9090
```

Without it, summary metrics report unavailable and Bridge operations continue.
For persistent queries choose an authenticated private HTTPS gateway and review
its networking separately. Do not expose unauthenticated Prometheus on the LAN.
Bridge accepts numeric loopback HTTP or reviewed private HTTPS, rejects
redirects/proxy-environment routing and bounds exporter queues.

Bridge emits registered HTTP-route and operation metrics/spans, not request
bodies, command output, credentials or prompts. Its owner-only
`GET /api/v1/telemetry/summary` accepts no custom PromQL, URL or selectors. It
returns bounded RAM, pressure, SGLang queue, TTFT and PSU observations with source
age, not a general database proxy or GPU qualification. Existing authorization,
approval and capability checks remain unchanged.

Bridge can use the optional, owner-authorized OpenAI Agents API adviser. It sends
only the fixed tool outputs defined by Bridge; it cannot approve or apply a
change. Keep telemetry local and send only the selected summary to the model.
Do not upload raw sessions, journal dumps, model inputs or kubeconfigs. The
adviser is not a replacement installer.

## Installation and interruptions

Installation works before K3s, Alloy or Bridge exists. Optional
`bootstrap-arch --events` writes fixed transitions to the local live journal;
network access and normal preflight remain independent.

Owner-console examples in the reviewed live environment:

```sh
bootstrap-arch --events --config /path/to/install.conf preflight
bootstrap-arch --events --config /path/to/install.conf --dry-run install
```

Add `--events` to the exact actual-install handoff in
[Installation](INSTALLATION.md). Never bypass confirmations or invent resume:
commands remain `preflight`, `install` and `verify`.

Only run ID, configuration hash, fixed stage, mode, outcome, exit code and time
are logged. The sink is checked before dispatch. Later logging failures warn
without replacing the primary outcome. An interrupted stage with only a start
event is **unknown**, not completed. Success in `dry-run` mode establishes only
the dry-run stage, not an installation.

The ISO journal is volatile. Before reboot, retain only the selected
`journalctl -t workstation-install -o cat` output in the existing private
controller/external journal, outside target disks. Never retain the whole
terminal transcript or Codex credential/session directory. Add build/repository
identity and artifact references. After interruption revalidate actual disks,
config, revision and state through the installation skill. Changed inputs need
a fresh plan; historical events do not establish safe destructive retries.

## Tinkering and useful queries

Start with 30-second scrapes, 10% Bridge traces and SGLang tracing disabled. The
dashboard covers host headroom/PSI, GPU VRAM/thermals/power/sample age, serving
queue/TTFT/inter-token latency and pod state. Use Explore for throughput and
optional kubelet throttling series. Empty panels mean
missing observations or stopped workloads, not measured zero.

```promql
node_memory_MemAvailable_bytes{job="node"} / 1024^3
rate(node_pressure_memory_waiting_seconds_total{job="node"}[5m])
workstation_gpu_vram_used_bytes / workstation_gpu_vram_total_bytes
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

## Acceptance, failures and rollback

Record passed, failed, blocked or not run for each gate:

| Gate | Command or observation | Boundary |
| --- | --- | --- |
| Offline regression | `HOME_LAB_PYTHON=/path/to/prepared/python bash tests/test_telemetry.sh` | Config, capacity, sensor and stage-event fixtures |
| Manifest contract | `bash tests/test_telemetry_stack.sh` | Pins, permissions, resources and local rendering |
| Image fixture permissions | `bash tests/test_telemetry_image_permissions.sh` | Actual generators under umask 077; intercepted launch-time mode/containment checks, not Docker execution |
| Actual image parsers | `bash infrastructure/observability/validate-images.sh desktop-linux` | Parsers and isolated dummy-secret pipelines; images must already exist |
| Full source suite | `HOME_LAB_PYTHON=/path/to/prepared/python make check-strict` | Explicit skips retained |
| Host package | `pacman -Qkk arch-workstation-bridge-runtime grafana-alloy` | Package files, not service/hardware health |
| Host services | `systemctl status workstation-alloy.service workstation-telemetry.timer` | Actual execution and local failure diagnosis |
| Metrics path | Private `up` query, fresh hardware timestamp and two distinct GPU identities | Ingestion, not GPU correctness |
| PSU path | Exact USB/kernel identity, unprivileged fresh DC watts, persistent energy/coverage, gap behavior and private dashboard | Actual PSU/sandbox qualification; not measured wall energy |
| Kubelet | `up{job="kubelet-cadvisor"}` and actual memory/throttling series | TLS/RBAC and usable observations; no `nodes/proxy` workaround |
| SGLang | Ready qualified model; nonempty serving metrics during test requests | Actual metrics contract with this image/model |
| Optional tracing | OTel import/help check in exact image, maintenance trace profile, sanitized spans | Dependencies and delivery, not numerical correctness |
| Bridge | Existing authenticated API client reads summary; HTTP/operation spans arrive | Go-to-Alloy integration, no paid API request needed |
| Persistence | Owner-controlled backend restart; old data remains queryable | PVC/WAL recovery, not backup |
| Overhead | Matched repeated serving/build/gaming runs | Measured cost and uncertainty |

### Image-validation fixture access

Run the image validator only in an explicitly authorized disposable environment
with a reviewed local Unix-socket Docker context and the pinned images cached.
Missing images or tools block it; it does not pull. Parser and host-log tests use
`--network none`; the synthetic OTLP pair uses an owned internal Docker network.
No external network, real journal, devices, host root or published ports are used.

The image validator makes only its nonsecret rule and OTLP input fixtures readable
by their nonroot container consumers. Input directories are mounted read-only and
separate from the private `0700` evidence parent; retained logs and IDs remain
owner-private. Read-only mounts do not grant read access across UIDs. Do not
replace these narrow modes with recursive permission changes or run consumers as
root.

The fixture and image-validator commands in the table check parsing, rule behavior,
sanitization, consumer access and retained-evidence privacy. They do not qualify
the target AMD64 service, live delivery, collector memory-pressure behavior or
workstation overhead.

For SGLang tracing use the existing owner-approved pod exec path to run
`python -c 'import opentelemetry.exporter.otlp.proto.grpc.trace_exporter'` and
`python -m sglang.launch_server --help` in the exact image. Do not install missing
wheels into a running baseline. Failed imports mean retain metrics and
`TELEMETRY_SGLANG_TRACE=false`; build a separate coherent candidate. Aggregated
Prometheus latency does not replace numerical/benchmark evidence.

Expected failure modes: missing Grafana Secret deliberately blocks startup;
wrong node/API IPs break scrapes; kubelet SAN mismatch needs a valid certificate,
not TLS bypass; stopped sampler or inaccessible textfile means stale/absent
data; memory limits can drop/refuse telemetry; full XFS affects the entire
machine. Inspect counters and timestamps before trusting dashboards. The local
stack cannot alert while the workstation is off. An external NAS probe is a
later optional availability check, not required infrastructure.

Rollback retains state:

1. Disable Bridge telemetry in its policy and restart it through owner maintenance.
   Restore prior SGLang telemetry settings through its existing lifecycle.
2. Stop/disable `workstation-alloy.service` and `workstation-telemetry.timer` to
   revert host collection. Do not delete users/state as a shortcut.
3. Restore the prior reviewed render, or scale each named telemetry Deployment
   to zero and remove only the reviewed node-exporter DaemonSet when retiring
   the stack. Confirm exact context/namespace. Keep PVCs, Secrets and evidence;
   deleting namespaces is not a rollback procedure.
4. Before a backend downgrade, check storage-format compatibility and take a
   recoverable copy through approved backup procedures. YAML rollback does not
   safely downgrade stored data; a retained PVC is not a backup.

Existing ISO archives, USB media and signed Bridge packages do not acquire
uncommitted changes. Build a reviewed installer/Bridge pair, test compatibility,
then use [ISO](ISO.md) for owner signing and assembly. No release pin, signing
key or runtime executable approval is silently changed. NAS backup/restore,
signed ISO/UEFI acceptance, gaming input and GPU handover remain separate.

## Shared-service notification extension

The owner now reports 128 GB RAM. Recollect actual memory/topology; a nominal
128 GiB less the existing 18 GiB reserves gives 110 GiB before firmware reserves.
`service plan` adds gateway, database, tools and Alertmanager and counts builds
and VMs explicitly. Existing limits and the telemetry reserve are retained.

`service notifications` composes the verified telemetry render with the pinned
Alertmanager, rules and exact receiver egress. The destination URL is a separate
Secret file, never a command-line value. The gateway exports only fixed phase,
result, active-request and oldest-request metrics. User/request IDs and content
are excluded. Existing Alloy redaction remains in place. Model-maintenance
alerts inhibit only model-outage/error notifications; backup failures still alert.

Local SSD capture and off-site replication export separate freshness/failure
metrics. Legacy backup success remains off-site success; a local snapshot never
clears an S3 failure. Local freshness alerts require the local-enabled signal,
so a remote-only configuration does not report a missing SSD. Both backup failure
routes remain active during model maintenance. Real Prometheus rule fixtures and
the local Alertmanager receiver test cover that distinction.

Run the HTTPS heartbeat on another device. Before intentionally stopping the
whole workstation, set its bounded `maintenance_until` (at most four hours);
a local Alertmanager cannot report total host failure. See
[rollout and notification acceptance](AI-SERVICE.md) and
[backup/resume maintenance](SERVICE-RECOVERY.md).
