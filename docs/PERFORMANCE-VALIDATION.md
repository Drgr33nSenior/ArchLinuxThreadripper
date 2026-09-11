# Performance validation: September 2026 audit

The [opt-in model-kernel workflow](MODEL-KERNELS.md) adds independent compilation,
warmup, restart-reuse and numerical/dispatch evidence without promoting defaults.

See [the 9 September implementation record](validation/AUDIT-FOLLOWUP-2026-09-09.md) for
runtime-library provenance, matching benchmark/quality configuration, installed
kernel verification, provider-aware monitoring and strict-check results.

See [validation of e5281734](validation/VALIDATION-e5281734.md) for the dependency-prepared
follow-up checks, retained logs, corrections and remaining qualification blockers.

Reviewed 2026-09-09 against checkout `bd4077f58afb6f81f3bfdd33986d001d46170dcc`,
the same revision reviewed externally on 8 September. Existing uncommitted
template annotations were preserved. No installer, GPU workload, live cluster,
firmware change or storage benchmark was run on the development Mac.

The [measurement-contract corrections](#measurement-contract-corrections) below
address the subsequent review of `46641c1f0d684654548547816ebdab4c02119289`.

**No performance default was promoted and no workstation speedup is claimed.**
Use the commands below on the installed target after its existing qualification
and access gates pass. Reports contain hardware identifiers and model/corpus
hashes: keep them private. Use new output directories for every run.

## Coverage and remaining limits

“Implemented” describes repository tooling, not successful hardware execution.
Every physical qualification and measured improvement in this table is **NOT
RUN — target hardware unavailable**.

| Audit item | Implementation and evidence | Local validation | Remaining qualification or blocker |
| --- | --- | --- | --- |
| Paired metrics | `lib/workstation/rocm.sh`: validate each document, both exact workload cases, finite positive rates and complete repetition samples before final success | `tests/test_rocm_benchmark.sh`: invalid first/second, empty/malformed/missing/duplicate cases, one/two selections | Actual HIP/Vulkan outputs and stable repeated results |
| SGLang serving | `performance.sh`, `serving.py`, `serving_sweep.py`, `tests/hardware/{sglang-evidence,serving-workload}.py`: private exact-Pod tunnel, native streaming, independent patches, model rehash | Local HTTP success/truncation/malformed fixtures, bounds, immutable inputs and patch resource preservation | Real TTFT/ITL/throughput/queueing; 4K/8K/32K only where the unchanged model/context fits |
| Startup/host RAM | `serving-startup --memory`, `serving_memory.py`: pod cgroup sampling, repeated cold/warm evidence, offline memory-only patch and rollback | `test_performance.sh`, `test_serving_memory.sh`: identity, pressure, interruption, phase coverage, provenance and patch fixtures | Cold load/JIT and steady-state peaks; smaller-limit stability, latency and quality remain unqualified |
| llama comparison/quality | Same locked revision; one/two selected devices with full two-card inventory; explicit threads, split, K/V, batch/ubatch/FA; alternating order; telemetry; retained numerical/perplexity binaries | Build/benchmark mocks and numerical-output parser fixtures | Actual gfx1201 kernels, per-backend numerical results, same-corpus PPL and coding correctness; backend ordinals still require physical identity review |
| Multi-GPU | `tests/hardware/hip-ipc.cpp` and expanded `torch-rocm.py`; existing smoke/peer copy retained; explicit host and Pod commands | Fork/exec/handle-lifetime/error/corruption simulation; this is **not HIP emulation or GPU qualification** | Actual IPC, RCCL readback and 4-byte–64-MiB scaling; debug logs establish chosen transport only after inspection/profiling |
| CPU/builds | `hardware.sh`, `measurement.py`, reversible TuneD command, `memory-bandwidth.cpp`; existing RAM-aware jobs, ccache and link pools retained | Unknown/sysfs fixtures, subprocess timeout, profile restoration and failure tests | Sustained effective clocks, bandwidth/worker scaling, build cache benefit, static/cache-aligned cpusets and throttling |
| Encrypted storage | `storage_benchmark.py`: new bounded scratch file, XFS/crypt/RAID0 ancestry checks, QD1/parallel/buffered read cases, geometry and thermal samples; `fio` added to target package list | Scratch ownership/symlink/space/read-only flags and invalid-result fixtures | Actual fio, sustained temperatures and real model loading. Buffered eviction is advisory, not certified cold media |
| Freshness/source builds | `stack_inventory.py` classifies effective archive URLs and records host packages separately from Pod runtime. Existing source/image locks retained | Mirror classification and credential redaction fixtures | Full TheRock dependency seal/build/pacman packaging remains incomplete; see below |
| Gaming | `wayland-session.sh` verifies dimensions and observed Hz; `frame_metrics.py` checks distinct decoded content | Wayland output/refresh mismatch and duplicate-frame fixtures | Pinned KWin 6.3.6 virtual backend is 60 Hz only. Higher refresh is rejected, not silently claimed. Simultaneous AI/gaming remains separately gated |

## Baseline and evidence

Keep the digest-pinned AMD SGLang `0.5.15.post1`, ROCm `10.0.0` container and
existing Radeon Triton/AITER settings. AMD documents this image family for
Radeon; it does not qualify an arbitrary Arch host kernel or a mixed native
SDK/container library process. Record `torch.version.hip`, installed packages,
allocated devices, model hashes and actual allowlisted launch arguments with
`serving-evidence`. [AMD SGLang guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html).

The `.80` static fraction covers weights/KV, not every activation or graph
allocation. Two running requests is a baseline, not an optimum. Do not raise
memory fractions or context to hide an out-of-memory result. The dual Pod is
24 CPU/38 GiB; its 16 GiB `/dev/shm` ceiling counts inside that limit when used. The
single Pod is 20 CPU/32 GiB. Preserve host/K3s reserves and all other Pod requests.
[SGLang tuning semantics](https://docs.sglang.io/docs/advanced_features/hyperparameter_tuning).

At the locked llama commit, HIP graphs default **ON** and HIP RCCL defaults
**OFF**. The builder now specifies those same values, checks the retained cache
and graph compile definition in Ninja, and records the build command. This
does not establish graph correctness on the target. No development kernel,
direct-P2P patch, architecture spoofing or new reduction path is enabled.
[Pinned options](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/ggml/CMakeLists.txt),
[HIP implementation](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/ggml/src/ggml-hip/CMakeLists.txt).

`HSA_OVERRIDE_CPU_AFFINITY_DEBUG=0` is a comparison candidate: ROCr helper
threads inherit parent affinity at zero; default one ignores it. The sweep
changes only this variable for that candidate. Check actual cpusets and helper
threads; do not infer CCDs from NUMA nodes.
[ROCr environment semantics](https://rocm.docs.amd.com/projects/ROCR-Runtime/en/latest/api-reference/environment_variables.html).

## Prepare target evidence

Configure `SESSION_CONTEXT`, `SESSION_NODE`, `SESSION_NAMESPACE` and
`SESSION_ENVIRONMENT=dev` through the existing literal workstation config.
Names and paths below are owner-selected examples, not discovery results.
Replace `sglang-EXACT-POD` with the current named Pod. The serving benchmark
requires the local host boot to match that Pod's node and verifies both physical
GPUs. It does not require static CPU Manager, so default scheduling remains a
valid baseline.

```sh
umask 077
mkdir -p artifacts
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/perf-hardware
./bin/workstationctl --config config/workstation.conf resources plan artifacts/perf-hardware/hardware.json artifacts/perf-resources
./bin/workstationctl performance stack > artifacts/host-stack.json
./bin/workstationctl --config config/workstation.conf rocm serving-evidence sglang-EXACT-POD artifacts/serving-before
```

Use the existing model-staging verification before this step. Full before/after
hashing reads the model mount and warms filesystem caches; serving throughput
tests therefore do not measure cold disk loading. Model/runtime/POD drift
prevents a successful final measurement record. A record marked
`incomplete` or `measured-awaiting-provenance` is not a successful measurement.

### Interactive and batch serving

Prepare a sufficiently long, non-secret coding corpus. Run this tokenizer
helper in the **same coherent image's Python environment** with its staged
tokenizer and exact revision; it never loads weights or downloads files:

```sh
python3 tests/hardware/serving-workload.py /path/to/staged/model EXACT_40_HEX_REVISION /path/to/coding-corpus.txt artifacts/interactive.json interactive --contexts 4096,8192,32768
python3 tests/hardware/serving-workload.py /path/to/staged/model EXACT_40_HEX_REVISION /path/to/coding-corpus.txt artifacts/batch.json batch --contexts 4096,8192,32768
./bin/workstationctl --config config/workstation.conf rocm benchmark-serving artifacts/interactive.json artifacts/serving-before artifacts/interactive-run
./bin/workstationctl --config config/workstation.conf rocm benchmark-serving artifacts/batch.json artifacts/serving-before artifacts/batch-run
```

Use `--contexts 4096` for the existing one-card 9B profile. Do not silently reduce
context, change quantization or substitute 9B for 27B when comparing devices.
The raw-completion workload is not a chat/tool-call quality evaluation. Keep a
separate fixed coding task suite and review correctness before promotion.

Interactive requests generate 128 tokens at concurrency 1/2; batch requests
generate 512 at 1/2/4. Each uses three repetitions and two requests per worker.
Concurrency is capped at eight, repetitions at twenty. Warm-up is excluded;
concurrency execution order alternates each repetition. Results retain request
failures, percentiles, repeat spread and aggregate output throughput. Server
chunk coalescing makes ITL chunk-normalized, not exact per-token arrival timing.

For cold-prefix comparison, use `prefix_state: new-prefix` and supply each
case's `cold_variants`: one distinct tokenizer-produced input-ID array per
request across all repetitions/concurrencies, each the same length as that
case's `input_ids`. Retain the corpus and workload hashes. Every response must
report zero cached tokens; otherwise the run fails its cold-prefix assertion.
Warm-prefix runs require observed cached tokens. Neither path flushes a shared
server cache. Do not call hash verification or a warmed prefix “cold model load.”

The command creates and closes its own exact-Pod loopback tunnel. It does not
create a Service/Ingress or bypass configured API authentication. If the server
requires a key, supply the existing `WORKSTATION_AGENT_API_KEY` via the established
secret mechanism, not an argument or checked-in file. Avoid dumping
`/server_info`: that upstream response can contain complete server arguments.

Generate comparison patches from a **single rendered Deployment JSON**, not a
List or a raw overlay. Use the existing render process, select the `sglang`
Deployment and convert it to JSON locally:

```sh
./bin/workstationctl rocm serving-sweep artifacts/sglang-deployment.json artifacts/serving-sweep
```

Each patch starts from that same baseline: request limit 1/2/4, memory fraction
.70/.75/.80, prefill 512/1024/2048/4096, CPU 8/12/20, or helper affinity 0/1.
The planner never applies patches. Review image `--help`, whole-core SMT
divisibility, resources and qualification annotations through the existing
manifest/maintenance process. Apply **one variable at a time**, collect new
evidence, then run the unchanged workload. Restore the baseline manifest between
candidates. No automatic Pod replacement or API-key handling is introduced.

To observe startup, prepare cold versus retained dedicated JIT/model caches in
an owner-approved maintenance session, then start the existing workload. While
the exact container is Running but not Ready, execute:

```sh
./bin/workstationctl --config config/workstation.conf rocm serving-startup sglang-EXACT-POD cold artifacts/startup-cold
# Repeat after a reviewed restart with retained caches and a new Pod name:
./bin/workstationctl --config config/workstation.conf rocm serving-startup sglang-EXACT-POD warm artifacts/startup-warm
```

Already-ready or restarted containers are rejected. Cache labels are
operator-declared. Elapsed time includes model load, JIT and warm-up together,
excludes image pull, and has polling/wall-clock uncertainty. Pure load/JIT phase
breakdown still requires reviewed engine instrumentation; no numbers are invented.

### Right-size SGLang host RAM

The 38 GiB request/limit is an initial budget, not a measured RAM requirement.
The `.80` setting controls GPU memory. It does not allocate 80% of host RAM.
Weights and active KV state normally reside on the GPUs in the existing
non-offloaded configuration. Host RAM still covers loading, page cache, process
state, shared/pinned buffers and compiler workers. Do not assume a full permanent
host copy of all GPU allocations, or size from steady RSS alone.

Run this workflow as the existing non-root workstation user, on the selected
two-GPU node. It requires readable cgroup v2 counters and the existing restricted
Kubernetes identity. It never starts/restarts a Pod, clears caches, resets memory
peaks or applies resources. A controller-only or missing-counter run cannot
produce sizing evidence. Plain `serving-startup` remains available without memory
collection.

1. Retain the rendered baseline `sglang` Deployment JSON, a tokenizer-produced
   workload and the current `resource-plan.json` from the commands above. Include
   the contexts/concurrency you intend to support. If serving takes less than
   60 seconds, increase repetitions/requests within the existing workload bounds;
   do not pad telemetry with idle time. Use the same workload for each restart.
2. In an owner-controlled maintenance window, prepare two cold and two warm
   starts, each with a **fresh Pod UID**. Cache labels remain owner declarations:
   keep cache preparation evidence. Do not delete shared caches to manufacture
   coldness. Within each cold/warm pair retain the same dedicated persistent
   cache paths and launch settings. Observe before Ready, then collect serving
   evidence and run the unchanged workload. For the first cold start:

   ```sh
   ./bin/workstationctl --config config/workstation.conf rocm serving-startup sglang-EXACT-POD cold artifacts/ram-cold1-start --memory
   ./bin/workstationctl --config config/workstation.conf rocm serving-evidence sglang-EXACT-POD artifacts/ram-cold1-evidence
   ./bin/workstationctl --config config/workstation.conf rocm benchmark-serving artifacts/interactive.json artifacts/ram-cold1-evidence artifacts/ram-cold1-run
   ```

   Repeat for `warm1`, `cold2` and `warm2`, changing the Pod and output paths, and
   using `warm` for warm starts. Hashing after readiness warms the file cache;
   it does not turn the subsequent inference run into cold-loading evidence.
3. Generate a candidate. `--other-mib 8192` below is only an example for a reviewed
   non-SGLang budget; count WebUI/RAG, telemetry, other Pods, builds and VMs. Do
   not count the same host collector twice inside host and telemetry allowances.

   ```sh
   ./bin/workstationctl rocm serving-memory-plan artifacts/sglang-deployment.json artifacts/interactive.json artifacts/perf-resources/resource-plan.json artifacts/ram-plan \
     --observation artifacts/ram-cold1-start artifacts/ram-cold1-run \
     --observation artifacts/ram-warm1-start artifacts/ram-warm1-run \
     --observation artifacts/ram-cold2-start artifacts/ram-cold2-run \
     --observation artifacts/ram-warm2-start artifacts/ram-warm2-run \
     --other-mib 8192
   ```

The planner reports sampled current/anon/file/shmem maxima separately from the
pod's lifetime peak. `shmem` is included in `file` and total memory; these values
must not be summed. `shmem` also includes SysV/shared anonymous mappings and is
not a measurement of `/dev/shm` alone. No file/shared memory is subtracted. The
candidate envelope adds the full configured shm ceiling as a growth reserve
to the lifetime peak. This deliberately conservative reserve can overlap prior
shm usage; it is **not** a claim of additional measured consumption.
Default additional headroom is the greater of 2 GiB and 25%, rounded up to
256 MiB. These are conservative **candidate-generation rules**, not a measured
optimum. Explicit controls are `--margin-mib` (at least 512), `--margin-percent`
(10–100), `--minimum-seconds` (30–21600) and `--memory-mib` (at least the computed
candidate, below the old limit). Insufficient room means refusal, not clipping.

Missing/failed phases, incomplete workload cases, changed Pod/container/launch
or model identity, unknown memory, swap use, nonzero limit/OOM/high events,
increased/reset memory PSI and gaps above 30 seconds reject a reduction plan.
No swap must be available on the host; a parent cgroup's `memory.swap.max=max`
alone does not imply that a child can swap. Hardware memory totals must still
match the resource plan; regenerate it after a DIMM change.
Older records without these identity and measurement-window fields are not
sizing evidence. Recollect them; do not add synthetic fields to retained runs.

Outputs are private `plan.json`, `patch.json` and `rollback.json`. Both patches
first test the entire expected Pod spec. They change only equal memory
requests/limits; CPU/GPU resources, shm, model, context, image and caches remain
unchanged. Evidence/tool/artifact hashes detect drift, but are not signatures,
target qualification or permission to apply. Keep these files outside Git;
patches contain the supplied Pod spec and can include sensitive configuration.

Apply a reviewed candidate only through the existing owner maintenance process.
Recheck live capacity and preserve a copy of the baseline. Repeat cold/warm and
sustained memory tests **at the smaller limit**, then compare numerical outputs:

```sh
./bin/workstationctl rocm kernel-quality artifacts/baseline-quality/result.json artifacts/candidate-quality/result.json artifacts/ram-quality.json --atol 0.001 --rtol 0.001 --memory-only
```

Create those quality runs with `kernel-evidence` and `kernel-warmup` from
[MODEL-KERNELS.md](MODEL-KERNELS.md). Keep image/model/GPU/compiler/launch identity
unchanged; `--memory-only` permits only a smaller equal request/limit. It does
not relax token/log-probability checks or qualify coding quality. Compare TTFT,
request latency, throughput, reclaim/pressure and startup time across repeated
matched runs. Treat changes within noise as inconclusive. Regenerate compilation
worker plans for the new cap. If stability, memory or latency regresses, restore
the baseline through the retained rollback patch; its spec test must still pass.
Do not bypass a failed rollback test after unrelated configuration changes.

The default remains 38 GiB until target evidence supports promotion. This work
does not change session handover, create a second boot path or implement automatic
resizing. Agent/Bridge integration is a separate client of the same deterministic
planner; see [the Go-agent handoff](BRIDGE-MEMORY-AGENT-PROMPT.md).

Mechanism references: [Linux cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html),
[Kubernetes Guaranteed QoS](https://kubernetes.io/docs/tasks/configure-pod-container/quality-service-pod/),
[SGLang loading](https://docs.sglang.io/docs/advanced_features/model_loading).
No MI300 constants, loader change or experimental ROCm flag is introduced.

Local validation on 11 September 2026 used installer HEAD `843a52b` plus the
uncommitted memory increment, preserving prior telemetry/ISO changes. With the
prepared validation tools, configured Python 3.11 (`HOME_LAB_PYTHON`) and pinned
operator chart (`HOME_LAB_GPU_CHART`), `make check-strict` passed. It ran 55 shell
test scripts, including 14 memory tests, six Bats cases and three Ansible syntax
checks. ShellCheck, shfmt, shell syntax, YAML and local manifest checks passed.
Ansible reported the expected empty/example-inventory warnings; no playbook ran
against a host. Local Markdown references and `git diff --check` also passed.

Private evidence is in `test-results/strict-check.FDAWLU/`. The preceding attempt
in `test-results/strict-check.NXy30i/` correctly failed because the pinned chart
was not selected; the chart was then supplied without changing skip policy.
Three permitted checks remain **SKIPPED**: built gaming-image smoke tests
(`HOME_LAB_GAME_IMAGE` unset), Linux Wayland process-group tests
(`HOME_LAB_WAYLAND_RUNTIME_IMAGE` unset), and `systemd-analyze` verification
(development host is macOS). All startup/serving memory evidence in these tests
is synthetic. Actual target collection, smaller-limit measurements, numerical
qualification and any RAM saving remain **NOT RUN**.

### Telemetry interpretation

Reports include monotonic/wall timestamps, BDF-keyed VRAM and clocks, hwmon
power/temperatures, CPU P-state/EPP/governor/boost/preferred-core observations,
and host memory. Missing/inaccessible sensors remain null. On a local readable
cgroup v2 hierarchy the Pod UID resolves to run-aligned memory/current,
CPU throttling and effective cpuset samples; unsupported layouts say unavailable.
Before/after container snapshots supplement them. `memory.peak` is a lifetime
high-water mark, not a reset per-run peak. Sampled maxima may miss spikes.

Server load samples record waiting requests; optional metrics are numeric-only
and omit labels. Queue occupancy and client TTFT are not measured per-request
queue latency. Prometheus histogram buckets without labels cannot establish
queue-time percentiles. Mark unavailable metrics explicitly rather than zero.
Sysfs frequency samples are not APERF/MPERF sustained effective clocks; use
existing target telemetry for that comparison.
[AMD P-state semantics](https://www.kernel.org/doc/html/latest/admin-guide/pm/amd-pstate.html).

## llama and multi-GPU commands

Build new HIP/Vulkan candidates with the existing commands in
[AI-PERFORMANCE.md](AI-PERFORMANCE.md#build-and-measure-llamacpp). Older builds
without retained quality executables must be rebuilt in new directories.
Use one local GGUF, fixed quantization and the same corpus throughout:

```sh
./bin/workstationctl --config config/workstation.conf rocm benchmark-llama artifacts/perf-hardware/hardware.json artifacts/hip/build/bin/llama-bench artifacts/vulkan/build/bin/llama-bench /path/to/model.gguf artifacts/llama-one ROCm0 Vulkan0
./bin/workstationctl --config config/workstation.conf rocm benchmark-llama artifacts/perf-hardware/hardware.json artifacts/hip/build/bin/llama-bench artifacts/vulkan/build/bin/llama-bench /path/to/model.gguf artifacts/llama-two ROCm0/ROCm1 Vulkan0/Vulkan1
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/perf-hardware/hardware.json artifacts/hip /path/to/model.gguf /path/to/quality-corpus.txt artifacts/quality-hip ROCm0/ROCm1
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/perf-hardware/hardware.json artifacts/vulkan /path/to/model.gguf /path/to/quality-corpus.txt artifacts/quality-vulkan Vulkan0/Vulkan1
./bin/workstationctl --config config/workstation.conf rocm validate artifacts/host-ipc /path/to/coherent-rocm/python
./bin/workstationctl --config config/workstation.conf rocm validate-pod sglang-EXACT-POD artifacts/pod-ipc
```

Replace device names with observed names and verify their PCI mapping. Positive
selected model allocations are required; unselected ROCm/Vulkan allocations
are rejected. Quality runs execute supported CPU-reference operations and
retain perplexity; compare both backends and a CPU reference with the same
model/corpus/context. A passing subset is not comprehensive model quality.

The host diagnostic retains smoke, peer copy, IPC and RCCL results. The Pod
diagnostic compiles only the small HIP checks in its private temporary directory;
it installs nothing. Missing `hipcc` or permissions is a diagnostic failure,
not permission to replace drivers or relax the Pod security policy. RCCL runs
ten repetitions after warm-up per size/rank, verifies every reduction and
records algorithm bandwidth, latency and transport debug output. Inspect P2P,
SHM/network selection and profile traffic: peer capability alone proves neither
IPC nor efficient TP.

For serving scaling, keep the **same fitting model** for TP=1, TP=2 and two
independent one-GPU workers. The last topology requires an owner-reviewed
second Deployment and fitting CPU/RAM budgets; it is not created by this tool.
Request one exclusive `amd.com/gpu` each, validate both assigned physical
devices, and benchmark the two private endpoints with matching total work.
The traditional plugin cannot select a particular BDF. Pipeline parallelism is
deferred until the pinned Radeon engine/model supports and passes that path.

## CPU, build and encrypted storage comparisons

Use the existing CPU Manager maintenance procedure in
[AI-PERFORMANCE.md](AI-PERFORMANCE.md#cpu-manager-migration-and-rollback) to compare
`none`, static full-core allocation, then the supported cache-alignment option.
Never delete an active kubelet checkpoint. Compare effective cpusets and
`cpu.stat`, not just requests. Host services and IRQs remain outside CPU Manager.

TuneD experiments serialize through a root-owned lock and restore the previous
verified profile on completion, failure or handled cancellation:

```sh
sudo ./bin/workstationctl performance tuned balanced artifacts/tuned-balanced -- /usr/bin/sleep 10
sudo ./bin/workstationctl performance tuned accelerator-performance artifacts/tuned-ai -- /usr/bin/sleep 10
```

These examples collect **idle** comparison evidence only. For load, substitute
the same bounded, reviewed benchmark executable; it runs as root, so do not
pass untrusted scripts or serving clients that should run as a user. Alternatively
keep this observation window open around a separately started user benchmark.
Prior manual drift is rejected. If restoration fails or the machine loses power,
use the recorded `previous-tuned-profile.txt` with `sudo tuned-adm profile NAME`
and verify it. SIGKILL/power loss cannot execute a shell trap.

```sh
./bin/workstationctl --config config/workstation.conf performance memory 4 256 artifacts/memory-4
./bin/workstationctl --config config/workstation.conf performance memory 12 256 artifacts/memory-12
./bin/workstationctl --config config/workstation.conf build environment memory-heavy
./bin/workstationctl --config config/workstation.conf ccache stats
```

The triad uses three arrays of the selected MiB size (768 MiB here), five timed
repetitions and readback correctness. It is a small standard-library benchmark,
not a claim of STREAM-equivalent bandwidth. Test 1/2/4/8/12/24/48 workers only
where discovered CPUs and current RAM reserves permit. Recollect hardware and
regenerate resource plans after DIMM changes; do not assume RAM bandwidth doubles.

For clean/warm builds, run the same locked builder into new output trees under
`performance run OUT 86400 -- COMMAND...`. First set `CCACHE_DISABLE=1` for an
uncached compile; then unset it and run two new-tree builds to populate and reuse
the cache. Retain `build environment memory-heavy` and `ccache stats` before/after
each, plus source, compiler, flags, RAM, compile/link pools and elapsed time.
Use separate config copies to vary `MAKE_JOBS`/`MEMORY_HEAVY_JOBS`; existing RAM
bounds remain authoritative. Never clear the shared cache to simulate a cold run.

For storage, select a new caller-owned directory on the installed encrypted XFS
filesystem. `fio` is the only added target package; it runs no service or automatic
install-time workload. No extra Python dependency is required.

```sh
install -d -m 700 /absolute/owned/nvme/scratch-perf
./bin/workstationctl performance storage /absolute/owned/nvme/scratch-perf artifacts/fio-baseline 4
```

This creates only `scratch-perf/workstation-fio.bin`, refuses existing files,
caps size at 16 GiB and 25% of free space, and retains the file for inspection.
After review, the owner can remove that **exact** scratch file with
`rm -- /absolute/owned/nvme/scratch-perf/workstation-fio.bin`. Do not remove model
files or pass raw devices. Direct-I/O QD1 and two-job reads, plus advisory-evicted
and warm buffered reads, have three repetitions and bounded timeouts. Compare
raw fio latency distributions and repetition spread, sustained NVMe temperature
and real `serving-startup`; a sequential file read is not model deserialization.
[fio semantics](https://fio.readthedocs.io/en/latest/fio_doc.html).

dm-crypt `same_cpu_crypt`, `submit_from_crypt_cpus`, `no_read_workqueue` and
`no_write_workqueue` are **not applied**. Evaluate one at a time only through an
owner-reviewed offline boot/recovery maintenance workflow: back up existing boot
configuration, retain a known-good UKI, change the selected mapping's policy,
boot, verify effective settings, rerun the same scratch workload, then restore
and reboot to baseline. Do not reload the active root mapping or expose
`dmsetup table --showkeys`. Argon2id unlock calibration is a separate boot-time
task. [Kernel dm-crypt options](https://docs.kernel.org/admin-guide/device-mapper/dm-crypt.html).

## Gaming refresh and rollback

The pinned KWin virtual backend creates a 60000 mHz mode and has no supported
launch refresh argument. `GAMING_REFRESH_HZ=60` is checked against `wayland-info`,
which reports **60.000 Hz**, and against requested dimensions. Values above 60
fail before session launch. A coherent newer KWin image/custom-mode path and
capture qualification are prerequisites to high-refresh promotion.
[Pinned virtual output](https://github.com/KDE/kwin/blob/v6.3.6/src/backends/virtual/virtual_output.cpp),
[Wayland output formatter](https://gitlab.freedesktop.org/wayland/wayland-utils/-/blob/main/wayland-info/wayland-info.c).

Record a changing frame counter through the actual Sunshine/Moonlight session,
without frame-rate conversion. Decode its recording preserving timestamps:

```sh
ffmpeg -i /path/to/captured-counter.mkv -map 0:v:0 -fps_mode passthrough -f framemd5 artifacts/counter.framemd5
./bin/workstationctl sunshine qualify-frames artifacts/counter.framemd5 60 artifacts/frame-result.json
```

At least five seconds and 95% of requested distinct frames are required for this
cadence check. Retain actual display, capture, encode/drop and client logs too;
lossy noise or unrelated recordings are not proof of high-refresh capture.
Ordinary static game scenes are not valid counter tests. Existing device
ownership, image promotion and AI unload gates stay intact. Simultaneous
one-GPU AI/gaming is not enabled automatically.

## Unresolved source-build work and promotion

TheRock remains a plan/inventory, not a source-built pacman stack. Its locked
`requirements.txt` has open version ranges without a full transitive hash seal;
the build requires patched source/submodule preparation and patched `patchelf`.
This checkout has no reviewed dependency closure, prepared Arch build root or
completed ABI/package ownership tests. Those are genuine missing inputs, not a
GPU benchmark result. Do not generate fictitious wheel hashes or call the
existing binary AUR provider a source build. The complete dependency-lock,
build/package/install-rollback workflow is **not delivered by this tranche**.
[Pinned dependencies](https://github.com/ROCm/TheRock/blob/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f/requirements.txt),
[source prerequisites](https://github.com/ROCm/TheRock/blob/b927c1865f37fa7bbecf5c7e35dee41b02afbb4f/README.md).

No coherent replacement stack was qualified, so locks remain unchanged. The
inventory command distinguishes dated Arch URLs from other candidate mirrors;
it does not certify remote mirror freshness. Follow the existing full-upgrade
procedure, retain stable/LTS recovery kernels, and recollect host/container
evidence separately. No partial upgrades or mixed library prefixes.

Rank the next target experiments: (1) serving concurrency/prefill at fixed model
and context, keeping lower p95 TTFT/ITL without errors; (2) TP=1/TP=2/two workers
for one fitting model, keeping useful total throughput and interactive latency;
(3) CPU allocation/helper affinity/TuneD, keeping latency gains at acceptable
power; (4) build workers and cache reuse, keeping elapsed-time gains without RAM
pressure; (5) encrypted QD1/model-load and sustained thermal comparisons.

Use at least three comparable repetitions, alternate baseline/candidate order,
report median, tails and spread, and retain failed runs. Differences within
normal run-to-run variation are inconclusive. Promote only after numerical,
coding-quality, memory, stability and recovery gates pass. Rollback is the
retained config/manifest/image baseline; no benchmark changes disks, encryption,
kernel mitigations or cluster defaults automatically.

## Local verification record

Executed on the development Mac, not the workstation:

- `make -j2 check`: syntax, ShellCheck and YAML checks completed. Its test target
  initially rejected the newly added display key against the old exact baseline.
  The assertion was updated to require **60 Hz**, with separate 2100/1800/120
  startup/probe/termination checks; no resource or security checks were removed.
  Earlier ShellCheck findings and a TuneD failure-status regression were fixed.
- `HOME_LAB_PYTHON=/usr/local/bin/python3.11 PYTHONDONTWRITEBYTECODE=1 make test bats ansible kubernetes systemd`:
  exit 0 after those fixes; **43 test files passed**, including 11 standard-library
  Python fixtures, HIP IPC control-flow simulation, all selected Kustomize
  overlays, configuration templates, model locks and existing safety tests.
- Focused `shellcheck -x tests/test_performance.sh`, Ruby manifest-checker syntax,
  and AST parsing of all 11 changed/new Python files passed.
- `c++ -std=c++17 -Wall -Wextra -Werror -fsyntax-only tests/hardware/memory-bandwidth.cpp`
  passed. This is compiler syntax validation, not a memory bandwidth result.
- Both documentation advisory audits and `git diff --check` passed.

Unavailable/skipped: shfmt; Bats; a working Ansible executable; Linux systemd
verification; real ccache and CMake/Ninja repeat-build fixtures; the pinned local
GPU Operator chart render; prebuilt gaming/Wayland image smoke tests; the USB
Bash-4 signal fixture. No dependencies were installed to hide these gaps.
Physical tests, GPU compiler execution and complete ROCm packaging remain pending.

## Measurement-contract corrections

The follow-up checkout was clean and exactly at
`46641c1f0d684654548547816ebdab4c02119289`. Software pins, model settings,
resource budgets and pending hardware qualification are unchanged.

- Numerical qualification now requires the pinned CSV header, supported required
  operations, empty error fields on supported rows, and a successful executable
  **and telemetry** record. The pinned CSV printer omits `passed`; executable
  status is necessary because CSV alone cannot certify success. Unsupported
  cases are counted, not treated as tests passed. Missing required operations,
  malformed rows and nonzero exits prevent `quality.json` success.
  [Pinned upstream output and exit contract](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/tests/test-backend-ops.cpp).
- `run.json` becomes successful only after the sampler completes. Failures
  retain the workload return code, error category/class and, for cancellation,
  signal number. A workload exit of zero does not override failed telemetry.
- The shared runner handles SIGTERM/SIGINT even when imported by storage. It
  terminates only its owned workload group, escalates to KILL after five seconds,
  and preserves the original failure/cancellation. Storage retains a failed
  aggregate record and its scratch file. The CLI uses `exec` so its PID reaches
  this handler. SIGKILL or power loss cannot perform cleanup or guarantee records.
- TPOT excludes tokens already present in the first chunk. A single arrival
  reports `tpot_seconds: null`; multiple arrivals report a labelled observed-chunk
  estimate. `first_chunk_tokens` and `tpot_scope` describe its limits. TTFT,
  complete-request latency and aggregate token throughput retain their meanings.
  Native abort/error finish metadata is rejected, even after the expected token
  count and a `[DONE]` marker. Private error messages are not retained.
  [Pinned SGLang abort metadata](https://github.com/sgl-project/sglang/blob/v0.5.15/python/sglang/srt/managers/schedule_batch.py).

Historical records must not be silently relabelled: repeat numerical
qualification and affected streaming measurements with this corrected runner.
No model rebuild or software upgrade is needed solely for these parser fixes
if the existing locked candidates retain their quality executables and hashes.

### Remaining real-integration commands

**NOT RUN — target hardware unavailable.** Run on the installed Arch host as
the target user. First release conflicting GPU workloads through the existing
maintenance process. Keep one GGUF and corpus for both backends. Replace the
model/corpus paths and candidate directories below with reviewed local inputs;
use the device names printed by each retained binary.

```sh
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/review-hardware
artifacts/hip/build/bin/llama-bench --list-devices
artifacts/vulkan/build/bin/llama-bench --list-devices
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/review-hardware/hardware.json artifacts/hip /path/to/model.gguf /path/to/quality-corpus.txt artifacts/review-quality-hip ROCm0/ROCm1
./bin/workstationctl --config config/workstation.conf rocm qualify-llama artifacts/review-hardware/hardware.json artifacts/vulkan /path/to/model.gguf /path/to/quality-corpus.txt artifacts/review-quality-vulkan Vulkan0/Vulkan1
```

Both runs must produce supported-operation counts and retained perplexity;
compare model/corpus hashes and numerical quality. Neither result promotes the
stack automatically. After restoring the qualified AI workload, use its exact
Ready Pod and the existing fixed coding workload:

```sh
./bin/workstationctl --config config/workstation.conf rocm serving-evidence sglang-EXACT-POD artifacts/review-serving-before
./bin/workstationctl --config config/workstation.conf rocm benchmark-serving artifacts/interactive.json artifacts/review-serving-before artifacts/review-serving-run
```

Inspect per-request `first_chunk_tokens`, `tpot_seconds` and `tpot_scope` in
`result.json`. Single-chunk responses must have null TPOT, not zero. Failures
must not produce a successful aggregate record. Do not inject failures into a
shared server; abort/error rejection is covered by local protocol fixtures.

For a target cancellation check, prepare a **new caller-owned scratch directory
on encrypted XFS**, then use Bash to retain the exact benchmark PID:

```sh
./bin/workstationctl performance storage /absolute/owned/nvme/review-scratch artifacts/review-storage-cancel 1 &
benchmark_pid=$!
```

After confirming that the scratch workload is active, execute in the same shell:

```sh
kill -TERM "$benchmark_pid"
wait "$benchmark_pid"
```

Expect shell status 143, a failed aggregate `result.json`, an interrupted child
`run.json`, and no live fio members of that owned process group. The scratch
file remains for explicit owner cleanup. Do not target another process or raw
device. Local regression tests use sleeping subprocesses, a 16-byte temporary
fixture file and mocked storage metadata; they do not execute fio.

### Follow-up local verification

`HOME_LAB_PYTHON=/usr/local/bin/python3.11 PYTHONDONTWRITEBYTECODE=1 make -j2 check`
completed with exit 0: **44 test files**, including 14 Python fixtures and the
new complete HIP/Vulkan qualification invocation-to-parser regression. Shell,
YAML, manifest and available static checks passed. The SIGTERM fixture verifies
TERM-resistant parent/descendant termination, retained failed records, an
unaffected unrelated process and preservation of exit status 7 on a normal
workload failure. A focused ShellCheck pass also covered the synthetic executable.

Unavailable checks remain: shfmt, Bats, working Ansible, Linux systemd
verification, real ccache/CMake/Ninja build fixtures, the local GPU Operator
chart, gaming/Wayland image smoke tests and the Bash-4 USB signal fixture.
Documentation advisory audits and the final whitespace diff check passed.
No dependencies were installed and no hardware qualification was promoted.
