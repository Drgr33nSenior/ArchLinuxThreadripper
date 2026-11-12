# AI performance and experimental builds

Use this guide for resource budgets, CPU Manager and AI/gaming session policy.
Hardware reports must distinguish expected inventory from discovery; no local
workstation speedup has been established by source tests.

| Task | Authoritative guide |
| --- | --- |
| Native SDK, CPU builds, ccache and source packaging | [ROCm](ROCM.md) and [workstation setup](WORKSTATION.md) |
| Serving, paired HIP/Vulkan, CPU/storage measurement and numerical qualification | [Performance validation](PERFORMANCE-VALIDATION.md) |
| Optional model compilation, cache reuse and kernel dispatch | [Model kernels](MODEL-KERNELS.md) |
| Continuous local telemetry and combined AI/observability capacity | [Telemetry](TELEMETRY.md) |
| Models, retrieval and IDE clients | [Models](MODELS.md), [RAG](RAG.md), [agent harnesses](AGENT-HARNESSES.md) |
| Snapshot-to-rolling transition and maintenance | [Operations](OPERATIONS.md#move-from-the-installation-snapshot-to-rolling-arch) |

Arch supplies signed binary packages; it does not rebuild the distribution for
this CPU. Keep the coherent distribution/vendor baseline and measure selected
source builds before promotion.

## Design boundaries

| Layer | Configuration in this repository | Qualification still required |
| --- | --- | --- |
| Installer ISO | Generic signed Arch packages from a dated snapshot | UEFI boot and separate Secure Boot qualification |
| Installed host | Home-lab example selects TuneD `accelerator-performance`; headless remains independent | Compare power, clocks, temperatures and latency against `balanced` |
| Git kernel | Pinned AUR and kernel commits, native-CPU Kconfig overlay, memory-heavy job limit, effective config and package records | Compile on the actual CPU, sign, boot and test both GPUs |
| Other source builds | Native makepkg CPU flags, content-checked ccache, memory-derived concurrency, CMake/Ninja compile and link pools | Actual cache hits, effective compiler flags and build peak memory |
| llama.cpp | Same locked source for HIP and Vulkan, native CPU backend, retained CLI/benchmark binaries | Numerical checks, one/two-GPU measurements and soak |
| ROCm from source | TheRock plan limits build, distribution and test GPU targets | Full dependency seal, source build and pacman packaging |
| Kubernetes AI | Disabled SGLang with a digest-pinned AMD ROCm 10 image and explicit Radeon settings | Target/image validation, pinned model artifacts and serving tests |

The ISO is a portable installation/recovery environment. Native kernel and
userspace builds run after installation on the Threadripper, not inside the
Mac's emulated ISO builder. The bootstrap package does not include
`workstationctl`; use the reviewed full repository checkout on the installed
host. Rebuild, sign and assemble a fresh ISO package run to include installer
changes; an existing ISO is unchanged.

## Host and kernel policy

Follow [workstation setup](WORKSTATION.md#boot-and-tuned) for persistent TuneD
selection and [kernel builds](WORKSTATION.md#reviewed-aur-and-git-kernel) for the
native Kconfig/clean-chroot path. CPU-specific packages are not generic recovery
artifacts. Host CFLAGS do not replace kernel Kconfig.

The separate `performance tuned` experiment restores the previous verified
profile after completion or failure; ordinary `profile` selections persist.
Compare latency, heat, power and memory as well as throughput. Keep the existing
mitigations, SMT, THP, NUMA and IRQ policy unless a reversible, measured change
passes correctness and recovery gates. `iommu=pt` is not full host-device DMA
isolation.

## Build and measure llama.cpp

Use a clean checkout at `ROCM_LLAMA_CPP_COMMIT`, a coherent SDK and the same
boot's full two-GPU inventory. See [ROCm prerequisites](ROCM.md#native-compilation-and-ccache).
These commands build applications, not ROCm, and do not install them globally:

```sh
./bin/workstationctl --config config/workstation.conf ccache configure
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/perf-hardware
./bin/workstationctl --config config/workstation.conf rocm build-llama /path/to/locked/llama.cpp artifacts/perf-hardware/hardware.json artifacts/hip
./bin/workstationctl --config config/workstation.conf rocm build-llama-vulkan /path/to/locked/llama.cpp artifacts/perf-hardware/hardware.json artifacts/vulkan
```

Use fresh output directories. Both builds remain `built-not-qualified`.
Follow the [sealed paired benchmark and quality procedure](PERFORMANCE-VALIDATION.md#llama-and-multi-gpu-commands);
do not use executable-only hashes or raw ad hoc benchmark runs as equivalent
provenance. Match the model, quantization, corpus and inference settings.

For one/two-card scaling, use the same model that fits one card including KV
and workspace. A model that only fits two cards tests capacity, not speedup.
The benchmark excludes tokenization/sampling; measure serving latency separately.
Stop competing builds/VMs when needed to preserve the current measured no-swap budget.
Keep failures, and promote only after numerical, memory, thermal and recovery checks.

## Configuration and qualification boundaries

Resource plans use a fresh target inventory, not nominal RAM or an earlier boot.
The workstation currently has 64 GB RAM; DIMM population, trained speed and MemTotal
must be recollected. Two R9700s remain separate allocation/memory domains.
Use the [runtime map](README.md#keep-the-runtime-layers-distinct) to distinguish
host SDKs, checked-in overlays and the owner-selected ROCm 10.2 campaign.

| Responsibility | Canonical implementation | Qualification boundary |
| --- | --- | --- |
| CPU/NUMA/RAM/GPU discovery | `lib/workstation/hardware.sh` | Same-boot observed topology; optional probes can remain unavailable |
| Native builds and cache budget | `lib/workstation/build.sh`, config example | Effective flags, memory and cache evidence for each artifact; no global fast-math/PGO |
| Kernel build and recovery | `templates/workstation/linux-git.config`, kernel policy/locks | Retained physical tests are scoped to their exact build/boot; preserve stable/LTS |
| Host ROCm and native llama.cpp | `lib/workstation/rocm.sh`, `versions.lock` | Generic TheRock package release remains separate from source-built serving experiments |
| K3s and static CPU allocation | Bare-metal Ansible role, GPU-operator lock | Opt-in full-core CPU Manager migration; explicit admission and rollback |
| Workload envelopes and caches | `apps/`, resource planner | Measured host RAM and each card's VRAM; requests, limits and PVC sizes are not usage |
| GPU handover | `lib/workstation/session.sh` | Exact UID/lock/snapshot checks, allocations and device holders; stop managed AI before gaming |
| Sunshine candidate | [Sunshine guide](SUNSHINE.md) | Capture/basic input passed on the pinned Arch trial; audio, games, persistence and deployment remain open |
| Peer transfer diagnostics | `tests/hardware/hip-peer-copy.cpp` | Match UUID/BDF and retained readback evidence; BAR metadata is not transfer proof |

Version pins remain in `versions.lock` and component locks. This source map does
not reopen previously measured kernel/P2P work or turn a source fixture into a
physical result. Use fresh evidence for a changed artifact, boot or workload;
do not repeat a GPU diagnostic solely because this guide was edited.

### Discover and budget the target

Run collection on the installed host. Optional `dmidecode`, `nvme` and SMART
permissions affect available fields, not the existing required GPU gates.
Keep reports private: they include device serials and platform identifiers.
An unknown board revision or trained speed requires physical/manual follow-up.

```sh
./bin/workstationctl --config config/workstation.conf \
  hardware collect artifacts/target-boot-02
./bin/workstationctl --config config/workstation.conf \
  resources plan artifacts/target-boot-02/hardware.json artifacts/resources-02
jq '{cpu_topology,memory,platform,pci_gpus,nvme_devices,trim_evidence}' \
  artifacts/target-boot-02/hardware.json
./bin/workstationctl --config config/workstation.conf build environment memory-heavy
./bin/workstationctl --config config/workstation.conf ccache stats
```

The resource planner reserves three *discovered physical cores*, not CPU IDs
0–5. With uniform SMT2 that is six logical CPUs, matching system/kube reserves
4+2. It reserves 12 GiB host RAM, 4 GiB K3s and 2 GiB eviction headroom. An exactly
64 GiB report therefore yields 46 GiB for scheduling; an exactly 128 GiB report
yields 110 GiB. Actual firmware-visible RAM can be lower. Unknown or asymmetric
topology refuses static planning; keep the existing `none` policy and recollect.
Use `RESOURCE_*` overrides in the same literal workstation config, not separate
environment-only policy. The output `ansible-vars.json` is an offline proposal,
not a cluster change. Recollect and regenerate after any DIMM/SMT/firmware change.

Compilation uses current `MemAvailable` and cgroup limits, not DIMM count. For
example, 60 GiB available minus the existing 16 GiB workload reserve and 8 GiB
link reserve permits nine 4 GiB heavy compile jobs, capped by available CPUs and
`MEMORY_HEAVY_JOBS`. Explicit job caps are honoured but cannot bypass RAM checks.
Increase `BUILD_RESERVE_MIB` or reduce the job caps for other active workloads.
Do not put a large build tree in tmpfs. Ccache's default 100 GiB is a cleanup
budget; verify the selected user-owned cache path is on the intended NVMe mount.

### CPU Manager migration and rollback

Session switches, and so `workstationctl mode` ([service modes](MODES.md#before-the-first-switch)),
refuse to run until the static policy is in place.

The existing default is still `lab_cpu_manager_policy: none`. For static mode,
review the generated CPU list and reserves, then pass the generated variables
through the existing Ansible extra-vars mechanism along with the existing site
network variables and current-boot `lab_hardware_report`. For an offline syntax
check (no target contact):

```sh
ansible-playbook -i infrastructure/ansible/inventory.example.ini --syntax-check \
  infrastructure/ansible/site.yml -e @artifacts/resources-02/ansible-vars.json
```

The controlled drop-in is
`/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/90-workstation-cpu.conf`.
Changing policy, reservations or cache options on an existing node requires the
owner's maintenance procedure: drain approved workloads, stop K3s, back up its
configuration and `/var/lib/kubelet/cpu_manager_state`, resolve the old checkpoint
according to the upstream migration procedure, and apply the reviewed config
before restarting. This code never deletes checkpoints, drains a cluster, or
stops K3s automatically. An unchanged managed file is idempotent. Setting `none`
while the managed static file still exists fails rather than silently retaining
static policy. Rollback uses the same maintenance procedure, restoring the
previous kubelet configuration and matching state policy; do not copy a stale
checkpoint into a running kubelet. Review other kubelet drop-ins/CLI overrides
for conflicts as part of qualification.

Cache alignment remains off; `lab_cpu_manager_cache_alignment: true` opts into
the uncore-cache preference, stable since 1.36 (no feature gate). A cache group is not a NUMA node or an
inferred CCD. Static CPU Manager does not pin host daemons, interrupts or builds.
No host affinity or IRQ isolation claim is made by this tranche.

### Workload and session selection

All application replicas remain zero in source until qualification. Limits are
unchanged: SGLang single GPU 20 CPU/32 GiB; dual GPU 24 CPU/38 GiB; SwarmUI
12 CPU/12 GiB; gaming 12 CPU/12 GiB. Requests now equal these limits. The Swarm
seed init container requests/limits 2 CPU/64 MiB. SGLang's 16 GiB memory-backed
`/dev/shm` counts within its memory limit, not as additional free memory.
The default dual-GPU model is Qwen3.8-27B-FP8 at 32768 context tokens; the
explicit single-GPU option is Qwen3.5-9B in BF16 at 4096. Neither forces AWQ.
The AMD SGLang 0.5.15.post1/ROCm 10 image is digest-pinned, but target
qualification is still pending. CPU offload stays unchanged. These different
models are not a controlled one-versus-two-GPU scaling comparison.

Set these optional keys in `config/workstation.conf` to your actual reviewed
target; the example values below are not discovered node/context identities:

```ini
SESSION_CONTEXT=reviewed-kube-context
SESSION_NODE=actual-node-name
SESSION_NAMESPACE=ai-home-lab
SESSION_AI_DEPLOYMENT=sglang
SESSION_GAME_DEPLOYMENT=parent-steam-headless
SESSION_TIMEOUT_SECONDS=300
SESSION_AI_STARTUP_TIMEOUT_SECONDS=2100
SESSION_STREAMING_PATH=sunshine
```

Use `steam-headless` instead of the parent-prefixed deployment for the base
overlay, or the exact kids deployment when selected. These commands manage only
the named AI/game Deployments. AI rollout now has a separate 2100-second allowance for the
1800-second startup probe window; termination/release waits remain 300 seconds.
An unrelated SwarmUI or pending GPU Pod blocks
handover; it is not silently stopped. The target must be the local Arch host,
have the current boot identity, a qualified static CPU setup and exactly one
accelerator node with two advertised GPUs. Root can inspect all host DRM
descriptors; non-root or incomplete `/proc` visibility cannot prove release.

```sh
# Offline intent only, no kubectl access:
./bin/workstationctl --config config/workstation.conf session plan gaming
# Owner-only target commands, after qualification; NOT executed by coding tests:
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  maintenance artifacts/target-boot-02/hardware.json /var/lib/workstation-session --execute
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  ai artifacts/target-boot-02/hardware.json /var/lib/workstation-session --execute
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  gaming artifacts/target-boot-02/hardware.json /var/lib/workstation-session --execute
sudo ./bin/workstationctl session status /var/lib/workstation-session
sudo ./bin/workstationctl --config config/workstation.conf session restore \
  artifacts/target-boot-02/hardware.json /var/lib/workstation-session --execute
```

`restore` restores the replica snapshot taken on the first successful transition
attempt, not a guessed AI default. State is root-owned mode 0700/0600; `flock`
serializes cooperating commands. Repeated ready requests do not rescale Pods.
Failure or cancellation retains a failed state and blocks new managed builds;
fix the cause, then explicitly retry or restore. There is no automatic restart
over a possibly occupied GPU. Disconnects do not trigger a controller: end the
game and run `restore`. Stop or pause already-running builds through their own
terminal/job manager before gaming; the cooperative build marker only blocks
new managed compiler commands. No temporary TuneD/power changes need restoration.

Gaming currently fails **before** stopping AI: its built image is not promoted,
and complete gaming/network/audio/recovery acceptance remains open. Basic capture,
encoder startup and keyboard/mouse passed the bounded Arch trial; see
[Sunshine](SUNSHINE.md#current-result-1-november-2026). The Wayland candidate drops
all capabilities and runs non-root; do not set
`workstation.ai/qualification: qualified` merely to bypass this gate. It is the
final reviewed marker after immutable image provenance and target tests; the
command also checks digest pins and the conservative security profile. For Steam
Remote Play select
`SESSION_STREAMING_PATH=steam` and a qualified manifest with Sunshine disabled;
for Sunshine keep rendering/capture/encoding on its plugin-allocated GPU and
verify the chosen codec/backend with the actual Moonlight client.
The [Sunshine implementation and qualification runbook](SUNSHINE.md) records the
KWin/PipeWire session, explicit Vulkan/VA-API profiles, persistent shader-cache
settings and allocated-device checks. These do not remove the promotion or
handover gates.

This tranche stops the selected AI workload before gaming, including models
spanning both cards. A paused request queue does not unload VRAM. Simultaneous
one-GPU AI + one-GPU gaming is deferred: 32+12 GiB consumes 44 of approximately
46 GiB allocatable before other Pods. Physical cards are selected by the DRA
claims `gpu-0`/`gpu-1` (docs/HOME-LAB.md); sharing a claim does not isolate
VRAM. BDFs and render ordinals are
re-resolved at runtime, not used as an unsupported scheduler selector. The DRM
check is point-in-time evidence only; a host process outside these controls can
subsequently acquire a GPU. `/dev/kfd` is global and is not proof of card identity.

### Paired benchmarks and physical diagnostics

Build the two alternatives from the same clean pinned checkout. Vulkan needs
the installed Arch Vulkan loader/Mesa RADV and shader compiler in addition to
the common build tools; it does not receive HIP `gfx1201` compiler flags.

```sh
./bin/workstationctl --config config/workstation.conf rocm build-llama-vulkan \
  /path/to/locked/llama.cpp artifacts/target-boot-02/hardware.json artifacts/llama-vulkan-02
artifacts/llama-01/build/bin/llama-bench --list-devices
artifacts/llama-vulkan-02/build/bin/llama-bench --list-devices
./bin/workstationctl --config config/workstation.conf rocm benchmark-llama \
  artifacts/target-boot-02/hardware.json artifacts/llama-01/build/bin/llama-bench \
  artifacts/llama-vulkan-02/build/bin/llama-bench /path/to/reviewed-model.gguf \
  artifacts/paired-02 ROCm0/ROCm1 Vulkan0/Vulkan1
```

Use the actual names printed by each binary. Select one card per backend for a
one-card run, or both for a two-card run. The complete physical inventory must
still contain both cards. Their ordinals alone do not prove PCI identity. The command checks
retained binary hashes and the same locked source revision; it records identical
model hash, batch/ubatch/FA and prompt/generation inputs. Defaults are batch 2048,
microbatch 512, Flash Attention `auto`, 512 prompt and 128 generation tokens,
three repetitions with upstream warm-up in each of two alternating backend
pairs. Explicit defaults are 12 CPU threads, `f16` K/V caches and automatic
split selection (`none` for one device, `layer` for two). CLI inference context remains 2048;
`llama-bench` has no context-size option and explicitly records that limitation.
This is warm compute throughput, not a cold model-load or end-to-end serving
latency result. Keep raw JSON averages, spread and per-repetition samples. Repeat
with the same workload and stabilized power/temperature; do not add gains from
different models or engines.

Functional peer-copy diagnostic, explicitly on the workstation:

```sh
peer_run=$(mktemp -d artifacts/peer-copy.XXXXXX)
/opt/rocm/bin/hipcc -O2 --offload-arch=gfx1201 \
  tests/hardware/hip-peer-copy.cpp -o "$peer_run/hip-peer-copy"
timeout 180 "$peer_run/hip-peer-copy" > "$peer_run/result.txt"
```

Each ordered pair must report correct readback. Exit 77 means no capable pair,
not a pass. A successful transfer API/readback does not independently prove the
physical transport avoided host staging; use profiling before evaluating a
direct-reduction patch. PCI/BAR flags alone never establish functional transfer.

For each new build/boot, review the applicable checks below against its retained
handoff. This checklist is not a claim that all historical physical work is unrun:

- Inspect board revision/BIOS and DIMM labels against the matching Gigabyte manual.
  Recheck `lspci -Dvv -s <observed-BDF>` during load for expected width/speed; do
  not interpret a power-saving idle speed as sustained link throughput.
- Compare `nvme smart-log /dev/disk/by-id/<observed-controller-identity>` only
  when the tool accepts that resolved controller path; otherwise resolve its
  serial to `/dev/nvmeN` first. Read temperatures, critical warnings and wear.
  Use `findmnt -T <cache-or-model-directory>`, `lsblk -D`, and
  `systemctl status fstrim.timer` to verify the existing NVMe/RAID/LUKS/XFS path.
  Do not reformat or enable a new discard policy as a diagnostic.
- Record cold model-load time separately from warm repetitions, without global
  page-cache dropping. A cold run requires a documented cache-state procedure;
  uncontrolled page-cache state must be labelled unknown. If using `fio`, select
  a new named disposable file in an explicitly selected directory, set a size
  below measured free space and a timeout, and never pass an installed raw device.
- With a qualified SGLang image, capture model/quantisation revision, prompt/decode
  throughput, TTFT and inter-token p50/p95/p99 using its version-matched serving
  benchmark. The llama microbenchmark cannot supply these serving metrics.
- Log host/GPU RAM, clocks, temperatures and power alongside runs with the
  existing telemetry. For gaming record frame-time p95/p99, Sunshine encode
  latency/drops and client network latency at a fixed resolution/codec/bitrate.
  Keep the game, capture and encoder on the allocated device. Test shutdown,
  disconnect, failed launch and manual restore before unattended use.
- Run the existing HIP/PyTorch/RCCL checks, reboot and soak tests. Confirm ECC
  reporting/stability and measured host bandwidth before/after the four-DIMM
  upgrade; recompute worker and pinned-buffer budgets rather than doubling them.

Local-path PVC requests are not filesystem quotas. Monitor the 16 GiB HF-cache
budget and the existing model/game paths; remove only reviewed cache contents
while the workload is stopped. Keep shader caches under persistent gaming home,
not writable container layers. Existing RAID0 risk, encrypted layout, periodic
TRIM and Restic backup mechanisms are retained. There is no new replicated
storage stack or automatic cache/data deletion.

### Ranked follow-up experiments

| Priority | Experiment and evidence | Keep only if |
| --- | --- | --- |
| 1 | Same-model HIP versus Vulkan, then batch/ubatch/FA sweeps using the pinned upstream interfaces above | Repeatable prompt/decode improvement beyond run spread, unchanged outputs/quality and acceptable VRAM/latency |
| 2 | Two-DIMM versus four-DIMM host bandwidth and preprocessing/build worker sweep; CPU channel capability from AMD, actual population from discovery | Measured throughput rises without host-memory pressure, OOMs or worse game frame-time tails |
| 3 | Static whole-core baseline versus opt-in uncore-cache alignment on K3s 1.36 | Better p95/p99 latency under the same mixed load with no admission failures or host-service starvation |
| 4 | Stock versus existing native kernel, then supported TuneD/EPP or GPU power-cap experiments with explicit prior-value restoration | Improved work/joule or latency with stable clocks/temperatures and no correctness/boot regression |
| 5 | Qualified Sunshine Vulkan versus VAAPI on the real clients, using upstream-supported capture paths | Lower encode latency/drops and good frame-time tails at identical codec/quality settings |

[Native SGLang experiment plans](PERFORMANCE-PROFILES.md#native-sglang-experiment-plans)
cover speculative depth and hybrid-state precision without enabling either.
Their runtime support and performance remain owner-qualified experiments.

ASPM changes, direct HIP P2P reductions, Q6_K patches, AITER/FP8 workarounds,
development compiler patches, sched_ext, forced NUMA/NPS,
global hugepages, memory overclocking, PBO/Curve Optimizer, undervolting and
external AMF forks remain deferred. Their exact revisions and upstream merge
status must be reverified before a separate experiment; none was silently
enabled from an unreviewed research percentage. Security mitigations, IOMMU
configuration and filesystem durability are retained.

### Validation and RAG

Use [RAG.md](RAG.md) for the current composition, migration, preparation,
evaluation and rollback procedure. RAG source configuration and fixture checks
do not qualify a model, image or target hardware.
