# Component GPU profiling

Two explicit profiling routes support model-free component packets. They are
separate from the serving runtime. Every GPU invocation needs a fresh, one-use
owner approval that names the exact packet hashes. Importing the adapter and
running offline analysis creates no GPU authority.

`lib/workstation/component_profiling.py` returns the command, extra sandbox
mounts and environment for a packet. `configure(payload)` defaults to `off`:
it returns the original command with no extra mounts or environment. An
unknown route or profiling options without an explicit route cause refusal.
The packet must pin this adapter and apply its returned mounts and environment
before appending its returned command to the existing bwrap arguments.

## rocprofv3 kernel tracing

Select `route='rocprofv3'` and supply the interpreter, ROCm SDK root and trace
directory **as paths inside the sandbox**. The adapter invokes the SDK's
`bin/rocprofv3` with kernel and HIP runtime tracing, JSON output, and process
sync disabled. It requests no performance counters. This route uses the
serving Torch unchanged.

Use a single-process component with no compiler subprocesses under the tracer
preload. A retained model trace attempt exhausted its task limit when compiler
children inherited profiler initialization. The first smoke therefore uses
three small eager GEMMs. It does not load a model or invoke Triton compilation.

Let the payload exit normally so rocprofv3 can finalize its JSON. A killed
process or missing final trace is a failed capture. Preserve the failure;
do not retry automatically. The existing owner retains resource limits,
deadlines, independent release checks and source authentication. The smoke
uses a 16 MiB per-file limit and a sampled 32 MiB aggregate output limit.
The aggregate check is not a filesystem quota.

Convert a retained rocprof JSON file on CPU:

```sh
python3 -B lib/workstation/rocprof_trace.py \
  /absolute/123_results.json /absolute/new-kineto-shaped.json
```

The converter accepts one process, at most 100,000 kernel/HIP events, and
16 MiB of input and output. It requires positive kernel durations and a
complete timestamp envelope. Invalid identities, duplicate dispatches,
truncated JSON, linked inputs and existing outputs are refused. Output is
created privately and exclusively.

The output preserves kernel names, durations, GPU identities, queues, streams,
HIP correlations and launch geometry. Timestamps use a common relative origin
in microseconds. ROCprof grid sizes count work items; the `grid` field converts
them to workgroup counts, and `rocprof_grid_workitems` retains the original.
`cuda_runtime` is the Chrome-trace compatibility category for HIP API records.
It does not identify a CUDA runtime.

Torch operators, tensor shapes, stacks, allocations and non-kernel/non-HIP
records are absent. Omitted record counts and the original file hash remain in
`rocprof_conversion`. The converter creates no `cpu_op` or `External id`
records. Hyperloom therefore reports these kernels as unlinked to operators.
Keep the raw trace alongside the conversion.

The adapter passed CPU conversion of a retained ROCprofiler-SDK 1.4.1 capture:
10 kernels, 27,080 ns of total kernel duration and 116 HIP API events. The
installed Hyperloom/TraceLens pipeline read all 10 kernels and reported zero
operator-attributed kernels. Live capture with the serving SDK's 1.4.2 tracer
still requires its approved smoke operation.

## Kineto profiling twin

Status, 9 November 2026: this twin and the `/home/developer` prefix below
belong to the op234 ROCm 10.2 / Torch 2.14 runtime, which the 2026-11
reinstall wiped. The record is historical.

Select `route='kineto'` and supply the **host** path of the independently
verified twin's site-packages. The adapter adds a read-only bind of its `torch`
directory over Torch inside the component sandbox. The serving files remain
unchanged. Runtime dependencies and package version metadata are shared with
the same-version serving runtime; this is not a general Torch version override.

The twin reproduces Torch commit
`2e66fe8153890424e76464545bc5de6391bf9d8c` and Kineto gitlink
`15f4784a3471d4db8b31720d528f2f57cd2b1f40`, with the original ROCm SDK
`10.2.0.dev20261020+git535d2990` build recipe. The build-setting change
is `USE_KINETO=OFF` to `ON`. This Kineto revision uses ROCprofiler-SDK for ROCm.
The original build, SDK and serving prefixes are read-only inputs.

The 2026-11-01 CPU build completed in 819.5 seconds. Its wheel SHA-256 is
`6d7eaa4a1f123b6b4a3bee57610bb151d2bb077c7bc7aaa831879ea5297ad4b5`.
The private prefix is
`/home/developer/torch214-kineto-profiling-20261101-v6/sitepackages`.
Kineto availability changed from false to true. The effective configuration
adds only `USE_KINETO` and `HAS_ROCTRACER`; `libtorch_hip.so` is byte-identical.
The inventory adds 19 Kineto headers and changes six existing files. These
include the CPU/Python libraries, build metadata and a relinked
`torch_shm_manager`; that executable's input objects are byte-identical.

Without GPU devices, this pinned Torch revision advertises only CPU profiling:
its CUDA-activity branch requires `at::getNumGPUs() > 0`. Both baseline and twin
left CUDA uninitialized in the CPU checks. CUDA activity and GPU capture are
pending the separately approved smoke.

Before a packet uses the twin, authenticate its wheel and complete installed
Torch inventory, compare the build configuration with serving, and check
Kineto availability in a CPU sandbox without GPU devices. A CPU result cannot
prove that kernels will appear in a GPU trace.

Compile and warm the Triton sentinel before starting `torch.profiler.profile`.
The approved smoke must retain positive-duration aten and Triton sentinel
kernels and exact arithmetic results. Record arithmetic and profiler outcomes
separately. Profiling failure must not change a component's correctness or
timing decision. Never use profiled timings as unprofiled serving performance.

## Offline source lookup

```sh
bash bin/workstationctl performance hyperloom trace-analysis \
  /absolute/hyperloom-tracelens-prefix \
  /absolute/kineto-shaped.json /absolute/new-analysis \
  --source-path /absolute/pytorch/aten/src \
  --source-path /absolute/component-sources
```

Analysis uses an empty home and caches inside the new output. It checks the
installed tool identities, denies GPU access and external execution/network,
and leaves the installed prefix unchanged. Source lookup remains best effort:
binary-only kernels and unavailable sources may remain unresolved. The native
CPU test resolved a Triton sentinel to its exact file and line only after the
source directory was supplied.

The profiling handoff permits the existing protected admission/release
observer's sudo use only. CPU builds, analysis and installation into private
prefixes use no sudo. This exception creates no GPU execution approval and
does not authorize system package or service changes.
