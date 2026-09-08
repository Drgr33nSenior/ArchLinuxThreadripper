# AI performance and experimental builds

This is the 2026-09-07 source audit for the Threadripper 9960X, dual R9700 and
64 GiB host. The changes below provide targeted build controls and a measurement
path. They are not evidence of a measured speedup on that hardware.

The [local RAG pilot and context notes](RAG.md) were added on 2026-09-08. They
cover pinned CPU embeddings, hybrid retrieval, source provenance, the current
64 GiB/context budgets and a paired workstation test procedure. Graph retrieval,
shared agent memory and host KV offload remain separate follow-ups.

The [2026-09-08 model review](MODELS.md) selects Qwen3.8-27B-FP8 across both
GPUs as the default, Qwen3.5-9B as the explicit one-card option, and CPU
Qwen3-Embedding-0.6B for RAG. It records exact revisions, the remaining runtime
checks and model/index migration. These selections are not measured speedups.

The [local agent harness guide](AGENT-HARNESSES.md) defines the optional Qwen
Code, DSH and Hermes client path. It keeps IDE agents on the client machine,
uses a temporary owner-operated loopback tunnel after SGLang qualification, and
does not add an Ingress, automatic tool execution, or automatic RAG access.

Arch supplies signed binary packages and supports custom source packages; it
does not rebuild the whole system locally. Keep the official rolling stack as
the comparison baseline, then rebuild components that affect the workload.
See [Arch's distribution model](https://archlinux.org/about/).

## Audit decisions

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

## Move from the installation snapshot to rolling Arch

The custom ISO copies its dated mirrorlist into the installed system to keep
installation transactions coherent. Running `pacman -Syu` against that archive
does not move the host forward to today's packages.

After stable/LTS boot and recovery tests pass:

1. Back up the system and retain the known-good package archives and signed
   UKIs. Record the installed package list and the ISO release lock.
2. Inspect `/etc/pacman.conf` and `/etc/pacman.d/mirrorlist`. Save their current
   contents outside the paths being edited. Review current Arch news.
3. Select synchronized HTTPS mirrors using the
   [official mirrorlist generator](https://archlinux.org/mirrorlist/). Replace
   the dated archive selection deliberately. Check for explicit archive URLs
   and `IgnorePkg` entries in pacman configuration too.
4. Run one complete `sudo pacman -Syu`. Review `.pacnew` files and the UKI hook
   results, reboot, then collect new hardware and GPU validation reports.

Do not combine archived libraries with selectively updated ROCm, Mesa, firmware
or PyTorch. Arch supports full upgrades, not partial upgrades. See
[system maintenance](https://wiki.archlinux.org/title/System_maintenance).
Keep the ISO's build lock pinned; changing the installed host's update policy
does not require turning recovery media into a floating build.

For upstream development builds, review and advance the exact commits in
`versions.lock`, inspect recipe/config/build-option changes, and use a new output
directory. Existing pins are reproducible candidate inputs, not a claim that
they remain upstream HEAD. Refresh the kernel and its packaging recipe as a
reviewed pair. Refresh TheRock's dependency inventory with its entry-point pin.

## Host and kernel policy

The home-lab installer example explicitly sets
`TUNED_PROFILE=accelerator-performance`. Older configuration files that omit
this optional key retain their existing headless `balanced` or desktop policy.
To select or compare profiles on an installed host:

```sh
sudo ./bin/workstationctl profile ai
tuned-adm active
tuned-adm verify
# Comparison and rollback:
sudo ./bin/workstationctl profile server
```

These selections persist until changed; there is no automatic reset after a
benchmark. The workstation config example defaults the explicit configured
`profile` command to `ai`, but merely loading that file does not tune the host.
The upstream [accelerator profile](https://raw.githubusercontent.com/redhat-performance/tuned/master/profiles/accelerator-performance/tuned.conf)
requests performance-oriented CPU and latency settings. Expect higher idle
power and heat; inspect the installed profile and actual driver behaviour.

The git-kernel builder applies `CONFIG_X86_NATIVE_CPU=y` through the pinned
recipe's `config.user` path. It verifies the packaged effective configuration,
including native CPU, AMD GPU/HSA, IOMMU, SMP, module signing and CPU mitigation
support. It retains config, compiler, package and Namcap evidence. Follow the
[kernel build and promotion procedure](WORKSTATION.md#reviewed-aur-and-git-kernel).
These packages are CPU-specific; do not distribute them as generic recovery
artifacts. Host `CFLAGS` are not a substitute for kernel Kconfig.

The existing upstream NUMA, THP, preemption and AMD P-state policy is retained.
No blanket `mitigations=off`, SMT disabling, fixed hugepage reservation, manual
IRQ affinity, forced P-state mode or GPU overclock is added. Firmware must
enable IOMMU; `iommu=pt` selects passthrough mappings and is not full host-device
DMA isolation. The undocumented `amd_iommu=on` token has been removed. See the
[kernel parameter reference](https://cdn.kernel.org/doc/html/latest/admin-guide/kernel-parameters.html).

## Build and measure llama.cpp

Use a clean checkout of `ROCM_LLAMA_CPP_REPOSITORY` at exactly
`ROCM_LLAMA_CPP_COMMIT` from the reviewed lock. The build command does not fetch,
install system packages, install binaries globally or package the ROCm stack.
Install a coherent HIP SDK plus CMake, Ninja and ccache first. The default uses
official Arch ROCm packages; the explicit
[ROCm 10 AUR provider](ROCM.md#reviewed-rocm-10-sdk-provider) accepts the reviewed
RDNA4 binary package. It does not build ROCm from source or migrate packages.
Unset architecture overrides and GPU visibility filters before collection.

```sh
./bin/workstationctl --config config/workstation.conf ccache configure
./bin/workstationctl --config config/workstation.conf \
  hardware collect artifacts/ai-boot-01
./bin/workstationctl --config config/workstation.conf \
  rocm build-llama /path/to/locked/llama.cpp \
  artifacts/ai-boot-01/hardware.json artifacts/llama-01
./bin/workstationctl --config config/workstation.conf \
  rocm validate artifacts/ai-validation-01 /usr/bin/python
./bin/workstationctl --config config/workstation.conf \
  rocm inference artifacts/llama-01/build/bin/llama-cli \
  /path/to/reviewed-model.gguf artifacts/ai-inference-01
```

The `/usr/bin/python` example is for the coherent Arch baseline. After selecting
the AUR SDK, pass a separately qualified matching ROCm Python environment; do
not assume an Arch PyTorch binary matches a replaced SDK. SGLang uses its own
container userspace, documented in the [home-lab guide](HOME-LAB.md#rocm-10-sglang-candidate).

The build requires a fresh, matching two-GPU observation from the same boot.
It records source identity, CMake options/cache, toolchain/package metadata and
binary hashes. A successful build is `built-not-qualified`. Ccache is enabled
for host C/C++ only, not the HIP compiler. This standalone upstream Release
build is not a makepkg rebuild and does not inherit Arch's full compiler policy.

For a scaling comparison, choose a local GGUF that fits completely on one card,
including KV cache and workspace. Use the same binary, model hash, CPU thread
count and TuneD profile for both runs. Confirm the device names first. The
following commands run on the installed Arch host, not this Mac:

```sh
bench=artifacts/llama-01/build/bin/llama-bench
model=/path/to/reviewed-model.gguf
bench_run=$(mktemp -d artifacts/llama-bench.XXXXXX)
"$bench" --list-devices
sha256sum "$bench" "$model" > "$bench_run/SHA256SUMS"
"$bench" --model "$model" --device ROCm0 --split-mode none \
  --n-gpu-layers 999 --threads 12 --n-prompt 512 --n-gen 128 \
  --repetitions 3 --output json --verbose \
  > "$bench_run/one-gpu.json" 2> "$bench_run/one-gpu.log"
"$bench" --model "$model" --device ROCm0/ROCm1 --split-mode layer \
  --tensor-split 1/1 --n-gpu-layers 999 --threads 12 --n-prompt 512 --n-gen 128 \
  --repetitions 3 --output json --verbose \
  > "$bench_run/two-gpu.json" 2> "$bench_run/two-gpu.log"
```

Run each command only if the previous one succeeds. Review JSON and logs; file
creation alone is not a pass. This pinned `llama-bench` uses slashes for devices
and tensor shares; commas request multiple benchmark cases. JSON includes
per-repetition measurements. The benchmark excludes tokenization and sampling;
it is not an end-to-end serving latency test. See the
[pinned benchmark interface](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/tools/llama-bench/README.md).

Confirm actual GPU layer placement and nonzero model buffers on both devices in
the dual run. Repeat in alternating order after thermal stabilization. Record
prompt and generation throughput separately, peak VRAM/RAM, clocks, power,
PCIe negotiation, kernel/ROCm versions and any GPU resets. Compare stock versus
native kernel only after keeping the userspace build fixed. Then compare CPU
thread counts, batching and attention options one at a time.

Two GPUs can increase model capacity without improving small-model throughput.
Measure before selecting row/tensor splitting or NUMA binding. A model that only
fits across both cards is a capacity test, not the same one-card comparison.
With no swap and 64 GiB RAM, stop source builds and unneeded VMs before large
inference tests. Do not reserve hugepages or additional Kubernetes memory without
measuring the remaining host headroom.

Promote only after correctness checks, repeatable workload gains and the
[soak/recovery gates](OPERATIONS.md) pass. Keep a result that is slower or fails
numerical tests as evidence; do not label a compiler flag an optimization solely
because the build succeeded.

## First-tranche implementation record

Reviewed on 2026-09-07. The repository is `ArchLinuxThreadripperAI`. The research
workbook was not available in this checkout; no Include/Reject decisions were
invented. All inventory values in the brief remain expectations until collected
on the workstation. The current two modules do not establish their trained speed
or channel configuration. Two 32 GiB GPUs have separate memory pools.

The existing flow is retained: `config/install*.conf` → `bin/bootstrap-arch` →
`lib/bootstrap/{config,preflight,install,verify}.sh` and `templates/arch/` →
installed encrypted RAID/XFS host. The full checkout's `bin/workstationctl` loads
literal allowlisted `config/workstation.conf` values into
`lib/workstation/runtime.sh`; component modules handle native builds and reports.
`infrastructure/ansible/site.yml` and its bare-metal roles configure the packaged
K3s runtime. `infrastructure/gpu-operator/` owns the device-plugin configuration;
`apps/` Kustomize overlays select disabled workload manifests and persistent
local-path PVCs. The existing KVM/AlmaLinux lab is a separate retained workflow.

| Candidate | Existing files/behaviour | Evidence and applicable versions | Decision | Implementation | Validation |
| --- | --- | --- | --- | --- | --- |
| Target topology | `lib/workstation/hardware.sh`: observed/pending GPU report | [Linux topology ABI](https://www.kernel.org/doc/html/latest/admin-guide/cputopology.html), [9960X](https://www.amd.com/en/products/processors/ryzen-threadripper/9000-series/amd-ryzen-threadripper-9960x.html) | Extend | CPU/SMT/cache/NUMA, RAM/DIMMs, board/BIOS, BDF/render/link/BAR, NVMe/SMART and TRIM evidence; optional probes stay nullable | `tests/test_hardware_topology.sh`; physical NOT RUN |
| Slot placement | Expected TRX50 AI TOP, no observed revision | [Gigabyte specifications](https://www.gigabyte.com/Motherboard/TRX50-AI-TOP/sp#sp): CPU-specific M2A/B/C Gen5 support; M2D unavailable for non-PRO | Discover, do not relocate automatically | Record revision and negotiated links, then compare the matching manual and physical slot labels | Owner inspection under load; NOT RUN |
| Native kernel and CPU builds | `lib/workstation/{runtime,build}.sh`, `templates/workstation/linux-git.config`: native CPU gate and generic recovery path | Existing kernel/AUR pins in `versions.lock`; [GCC `znver5`/native semantics](https://gcc.gnu.org/onlinedocs/gcc/x86-Options.html) | Retain | No cross-host native specialization; kernel Kconfig, host C/C++/Rust mechanisms kept separate; no new LTO/PGO without a representative profile | Existing build/kernel mock tests; actual compile/boot NOT RUN |
| Bounded builds/cache | `build.sh`: RAM/cgroup/CPU cap, link reserve, ccache content identity and budget | [ccache manual](https://ccache.dev/manual/latest.html), installed tool versions retained in build records | Retain and extend | Gaming/failed transitions inhibit new managed compiler commands; running/unrelated builds are not killed | `tests/test_resource_plan.sh` and existing build tests |
| Coherent GPU stack | `rocm.sh`, `versions.lock`, `infrastructure/gpu-operator/` | [AMD ROCm 10.0 matrix](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html): RDNA4/gfx1201; Arch is not listed as a supported host combination | Retain qualification gates | Host owns amdgpu; no competing driver installer. TheRock remains a source plan, not a qualified package release | Package/provenance tests; actual Arch kernel/ROCm/framework compatibility NOT RUN |
| Comparable inference builds | `rocm.sh`: pinned HIP llama.cpp already present | [Pinned Vulkan build](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/ggml/src/ggml-vulkan/CMakeLists.txt), pinned benchmark interface above | Implement | Vulkan alternative, shared explicit batch/ubatch/FA knobs, same model/source hashes, repeated JSON throughput/spread | `tests/test_rocm_vulkan_build.sh`, `tests/test_rocm_benchmark.sh`; physical NOT RUN |
| K3s exclusive CPUs | `infrastructure/ansible/roles/k3s_baremetal/`: previously kubelet defaults | K3s `v1.35.7+k3s1`; [drop-ins](https://docs.k3s.io/installation/configuration#kubelet-configuration-files), [v1.35 CPU policies](https://v1-35.docs.kubernetes.io/docs/tasks/administer-cluster/cpu-management-policies/) | Opt-in implementation | Offline SMT-based resource plan; static/full-core/strict reservation; restricted/pod topology; explicit checkpoint/running-service migration refusal | Resource fixtures and exact rendered task predicates; real kubelet admission NOT RUN |
| AI CPU/RAM/GPU envelopes | `apps/base/{sglang,swarmui}/`, dual overlay: Burstable before this tranche | [Guaranteed QoS](https://kubernetes.io/docs/concepts/workloads/pods/pod-qos/); [SGLang arguments](https://github.com/sgl-project/sglang/blob/main/docs/advanced_features/server_arguments.md), image still unqualified | Implement resource corrections; retain models | Requests now match existing limits; init container also whole-core/Guaranteed. Existing `.80` memory fraction and two-request concurrency exposed through the ConfigMap; bounded `/dev/shm` retained | `tests/home-lab/check-manifests.rb`; serving correctness NOT RUN |
| Exclusive GPUs and handover | Traditional plugin; no existing session controller | [AMD allocation semantics](https://instinct.docs.amd.com/projects/k8s-device-plugin/en/latest/user-guide/resource-allocation.html) | Implement conservative explicit command | `lib/workstation/session.sh`: exact UIDs, lock, snapshot, timeout, global pending/active GPU checks and live DRM holder check; stop all managed AI before gaming | `tests/test_session.sh` mocks; no real cluster or GPU execution |
| Gaming activation | `apps/base/steam-headless/`: floating image, pending qualification, capabilities conflict with baseline PSA | [Sunshine container requirements](https://github.com/LizardByte/Sunshine/blob/master/DOCKER_README.md), [configuration](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2configuration.html) | Keep gated | No security relaxation. Session command refuses before stopping AI. Steam and Sunshine are alternative paths; Moonlight is the Sunshine client | Manifest/security gate tests; capture/input/audio/codecs NOT RUN |
| Persistent storage | Model/creative/game PVCs, ccache; HF cache previously ephemeral | [K3s local storage](https://docs.k3s.io/storage) | Extend, retain layout | Separate 16 GiB HF cache PVC; read-only model mount retained. Existing gaming home holds shader caches; existing TRIM timer/encryption discard and backup design unchanged | Render/PVC tests and read-only discovery; actual cooling/TRIM/retention NOT RUN |
| Functional peer transfers | BAR/PCI metadata cannot prove a transfer | [HIP peer API](https://rocm.docs.amd.com/projects/HIP/en/latest/doxygen/html/group___peer_to_peer.html) | Explicit diagnostic only | `tests/hardware/hip-peer-copy.cpp`: ordered pairs, 16 MiB transfer, UUID/PCI identity and readback verification | Compiled mock-HIP API tests; actual HIP compile and physical transfer NOT RUN |

No dependency pins were advanced. `versions.lock` remains authoritative:
K3s `v1.35.7+k3s1`, llama.cpp
`427291b5b34cd914a31b3fd3b61a68f6184f4b9f`, TheRock
`b927c1865f37fa7bbecf5c7e35dee41b02afbb4f`, and the existing kernel/AUR pair.
The operator chart remains `v1.5.1`, with archive and image digests in
`infrastructure/gpu-operator/chart.lock` and its values. The plugin is
`1.31.0.10`; driver management and AMD DRA remain disabled. TheRock's pinned
[target selection](https://github.com/ROCm/TheRock/blob/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f/README.md)
does not prove a complete source dependency seal or an Arch-compatible release.
Official Arch package versions, compiler identity, CMake options and binary
hashes are recorded at actual build time. No Mesa/ROCm release number or image
digest is fabricated for builds that have not happened.

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
the v1.35 beta uncore-cache preference. A cache group is not a NUMA node or an
inferred CCD. Static CPU Manager does not pin host daemons, interrupts or builds.
No host affinity or IRQ isolation claim is made by this tranche.

### Workload and session selection

All application replicas remain zero in source until qualification. Limits are
unchanged: SGLang single GPU 20 CPU/32 GiB; dual GPU 24 CPU/42 GiB; SwarmUI
12 CPU/12 GiB; gaming 12 CPU/12 GiB. Requests now equal these limits. The Swarm
seed init container requests/limits 2 CPU/64 MiB. SGLang's 16 GiB memory-backed
`/dev/shm` counts within its memory limit, not as additional free memory.
The existing models, AWQ choice and 4096 serving context remain unchanged.
Explicit CPU-offload changes await an immutable, image-local verified serving
engine; the placeholder image does not establish support for a new flag.

Set these optional keys in `config/workstation.conf` to your actual reviewed
target; the example values below are not discovered node/context identities:

```ini
SESSION_CONTEXT=reviewed-kube-context
SESSION_NODE=actual-node-name
SESSION_ENVIRONMENT=dev
SESSION_NAMESPACE=ai-home-lab
SESSION_AI_DEPLOYMENT=sglang
SESSION_GAME_DEPLOYMENT=parent-steam-headless
SESSION_TIMEOUT_SECONDS=300
SESSION_STREAMING_PATH=sunshine
```

Use `steam-headless` instead of the parent-prefixed deployment for the base
overlay, or the exact kids deployment when selected. These commands manage only
the named AI/game Deployments. An unrelated SwarmUI or pending GPU Pod blocks
handover; it is not silently stopped. The target must be the local Arch host,
have the current boot identity, a qualified static CPU setup and exactly one
accelerator node with two advertised GPUs. Root can inspect all host DRM
descriptors; non-root or incomplete `/proc` visibility cannot prove release.

```sh
# Offline intent only, no kubectl access:
./bin/workstationctl --config config/workstation.conf session plan gaming
# Owner-only target commands, after qualification; NOT executed by coding tests:
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  maintenance artifacts/target-boot-02/hardware.json /var/lib/workstation/session --execute
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  ai artifacts/target-boot-02/hardware.json /var/lib/workstation/session --execute
sudo ./bin/workstationctl --config config/workstation.conf session switch \
  gaming artifacts/target-boot-02/hardware.json /var/lib/workstation/session --execute
sudo ./bin/workstationctl session status /var/lib/workstation/session
sudo ./bin/workstationctl --config config/workstation.conf session restore \
  artifacts/target-boot-02/hardware.json /var/lib/workstation/session --execute
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

Gaming currently fails **before** stopping AI: its built image is not promoted, its
requested capabilities conflict with baseline PSA, and capture/uinput/audio,
display access, network policy and encoder behaviour remain unqualified. Do not
set `workstation.ai/qualification: qualified` merely to bypass this gate. It is
the final reviewed marker after immutable image provenance and target tests;
the command also checks digest pins and the conservative security profile.
No namespace security exception was introduced. For Steam Remote Play select
`SESSION_STREAMING_PATH=steam` and a qualified manifest with Sunshine disabled;
for Sunshine keep rendering/capture/encoding on its plugin-allocated GPU and
verify the chosen codec/backend with the actual Moonlight client.
The [Sunshine implementation and qualification runbook](SUNSHINE.md) adds
explicit VA-API/Vulkan Video profiles, persistent shader-cache settings and
allocated-device checks. These do not remove the admission or handover gates.

This tranche stops the selected AI workload before gaming, including models
spanning both cards. A paused request queue does not unload VRAM. Simultaneous
one-GPU AI + one-GPU gaming is deferred: 32+12 GiB consumes 44 of approximately
46 GiB allocatable before other Pods, and physical-card selection is not provided
by the traditional `amd.com/gpu` count resource. BDFs and render ordinals are
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

Use the actual names printed by each binary and verify both refer to the complete
two-card set. Their ordinals alone do not prove PCI identity. The command checks
retained binary hashes and the same locked source revision; it records identical
model hash, batch/ubatch/FA and prompt/generation inputs. Defaults are batch 2048,
microbatch 512, Flash Attention `auto`, 512 prompt and 128 generation tokens,
three repetitions with upstream warm-up. CLI inference context remains 2048;
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

Physical checks still required — **NOT RUN: target hardware unavailable**:

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
| 3 | Static whole-core baseline versus opt-in uncore-cache alignment on K3s 1.35 | Better p95/p99 latency under the same mixed load with no admission failures or host-service starvation |
| 4 | Stock versus existing native kernel, then supported TuneD/EPP or GPU power-cap experiments with explicit prior-value restoration | Improved work/joule or latency with stable clocks/temperatures and no correctness/boot regression |
| 5 | Qualified Sunshine Vulkan versus VAAPI on the real clients, using upstream-supported capture paths | Lower encode latency/drops and good frame-time tails at identical codec/quality settings |

ASPM changes, direct HIP P2P reductions, Q6_K patches, AITER/FP8 workarounds,
speculative decoding, development compiler patches, sched_ext, forced NUMA/NPS,
global hugepages, memory overclocking, PBO/Curve Optimizer, undervolting and
external AMF forks remain deferred. Their exact revisions and upstream merge
status must be reverified before a separate experiment; none was silently
enabled from an unreviewed research percentage. Security mitigations, IOMMU
configuration and filesystem durability are retained.

### Local validation record

`HOME_LAB_PYTHON=/usr/local/bin/python3.11 make check` passed with 32 test
scripts on the macOS development host. This includes shell syntax/ShellCheck,
YAML and Kustomize rendering, Jinja-generated K3s configuration, exact offline
CPU-reservation predicates, installer/source-generation dry-runs, hardware and
build fixtures, and session failure/recovery tests. The peer diagnostic was
compiled against a deliberately simulated HIP header, not a ROCm installation.

Unavailable checks were reported, not treated as executed: shfmt, Bats,
Ansible syntax-check (the Homebrew launcher references a missing Python 3.8),
Linux `systemd-analyze`, a local pinned GPU-operator chart archive, real ccache
repeat compilation and real CMake/Ninja fixture generation. The selected
PyCharm Python 3.11 SDK did run the Jinja/YAML configuration checks. No installer,
cluster mutation, firmware action, real ROCm build, ISO assembly, disk benchmark
or performance workload ran on this development host.

## RAG and context tranche — 2026-09-08

The opt-in `apps/overlays/rag` composition extends `single-gpu` without changing
the other profiles. It pins Open WebUI 0.11.3 and a small CPU embedding model,
uses embedded Chroma, bounds threads/uploads/chunks, and keeps zero replicas
with pending qualification. It does not request a GPU or alter SGLang's context,
host packages, gaming allocation or no-swap policy.

`bin/workstationctl rag stage-models` prepares hash-verified offline assets;
`rag verify-models` checks them after transfer. `rag corpus` snapshots explicitly
selected Markdown documents with source hashes and nullable Git provenance.
The new `webui-rag-data` PVC isolates pilot settings/data from `webui-data`.
Git-managed settings override the Admin UI in this profile; read the migration
and rollback notes before activating it.

Tests are `tests/test_rag.sh` and `tests/home-lab/check-rag.rb`. The seed questions
in `tests/fixtures/rag/questions.json` include unknown-hardware and invented-gain
cases. They are test expectations, not measured answers. See [RAG.md](RAG.md) for
the evidence/decision matrix, exact preparation commands, acceptance metrics,
privacy boundaries, storage retention and deferred pgvector/reranking/graph work.

Local validation passed on 2026-09-08: `make check` with the configured Python
SDK (37 test scripts), Kubernetes 1.35 schema validation (28 objects per RAG
overlay) and actual staging/verification of all 11 model files. Optional tooling
skips and the unrun application/hardware tests are listed in the
[RAG verification record](RAG.md#local-verification-record--2026-09-08).
