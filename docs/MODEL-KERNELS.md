# Opt-in model-kernel compilation and tuning

This workflow extends `workstationctl rocm`; it adds no service and does not
change the SGLang baseline. All hardware candidates remain **unqualified**.
Run requests, profiling and restarts only in an owner-approved workstation test
window. No live workload or GPU was exercised during source implementation.

## Exact image and compatibility

Reviewed 9 September 2026: the existing image manifest `51f63a2d…` and config
`8474934b…` in `versions.lock`. Registry history specifies ROCm 10.0.0 and
PyTorch **2.12.0**, with gfx1201 among the device packages. This is build metadata,
not an installed-package observation; do not substitute today's generic matrix.

The digest-verified source layer
`sha256:c31ff2cd4a0364cfb637ffc627ddef4f0de7a2f415301ff2982b775b0f84d86f`
contains the reviewed server arguments, graph runner and Qwen implementation.
Install layer `c56ed05e…` identifies an editable SGLang package
`0.5.15.post1+amd.march.ge35a33b34a`. Selected source hashes are in the existing
version lock. Target evidence must match them and execute the image's own
`--help`. These two layers were inspected, **not the complete merged image or
executable GPU stack**. The image and model baselines remain unchanged.

Both locked configs declare `Qwen3_5ForConditionalGeneration`:
[27B FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8/blob/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a/config.json),
[9B](https://huggingface.co/Qwen/Qwen3.5-9B/blob/c202236235762e1c871ad0ccb60c8ee5ba337b9a/config.json).
The inspected image implements this architecture. That establishes an applicable
source path, not successful FP8 dispatch, capture or compilation.

| Profile | Explicit change | Boundary |
| --- | --- | --- |
| `cache` | Bounded Inductor workers, FX cache enabled, private persistent namespaces | No graph option enabled; the workload may never use Inductor |
| `capture` | Also set `--cuda-graph-bs-decode` to workload concurrency values | Capture is not compilation; ROCm uses CUDA-named PyTorch APIs |
| `legacy-compile` | Also enable torch.compile, capped at the largest captured batch | Requires `--experimental`; model execution may disable or fail compilation |

The inspected runner compiles the captured batches at or below
`--torch-compile-max-bs`. It can disable compilation for an incompatible model.
The separate prefill `tc_piecewise` path is not forced on: bypassing its automatic
compatibility checks is not justified for this Radeon/model combination.
[Current SGLang guidance](https://docs.sglang.io/docs/advanced_features/server_arguments)
also warns that the legacy path is out of maintenance.

[AMD's Radeon guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html)
and existing Triton/AITER settings remain the baseline. No MI300 tile sizes,
GEMM shapes, waves or memory fractions are copied. An FP8 checkpoint or a loaded
BLAS library does not prove the hot operation uses that library.

## Generate independent candidates

Use the existing [serving/tokenizer/maintenance procedures](PERFORMANCE-VALIDATION.md).
Configure the exact dev/tst/int context, namespace and node. Supply one rendered
Deployment JSON and a tokenizer-produced workload for the same model:

```sh
./bin/workstationctl --config config/workstation.conf rocm kernel-evidence sglang-EXACT-POD artifacts/kernel-baseline
./bin/workstationctl rocm kernel-plan artifacts/sglang-deployment.json artifacts/interactive.json artifacts/kernel-baseline artifacts/compile-plan --profile capture --workers 1,2 --reserve-mib 32768 --worker-mib 1024 --reserve-cpus 2
```

Those memory numbers are budgeting examples, not RDNA4 tuning constants. Replace
them with observed requirements. Reserve host-resident model state, pinned
buffers, preprocessing and `/dev/shm`; the reserve must cover current cgroup
usage and the shm limit. The unchanged 38 GiB/24-CPU dual Pod leaves 6 GiB for
compilation in this example. Two workers per TP rank means four workers total.

The per-rank ceiling is the minimum of `(effective CPU minus CPU reserve)/TP`,
`(Pod MiB minus runtime reserve)/(worker MiB × TP)`, and 16. Explicit candidates
that exceed it fail rather than being clipped. CPU quota, affinity and cgroup
memory limits must be observed. A budget is not a guarantee against compiler OOM.
The host/K3s reserve is not available to the Pod. A DIMM upgrade alone never
expands its budget: recollect topology and regenerate the existing resource plan.

Each patch tests the exact baseline Pod template before modifying it. Apply one
through the existing reviewed manifest/maintenance path, not cumulatively.
Replicas, security, networking, model, resource requests and `/cache` PVC stay
unchanged. The planner never applies a patch or restarts a service.

Identity-keyed subdirectories reuse `/cache/triton` and `/cache/torchinductor`.
Different worker candidates use different namespaces for cold comparisons;
retain a candidate's namespace for restart tests. No automatic pruning or cache
deletion occurs. The observer stops at 20 GiB/50,000 files for a retention review.
Local-path PVC requests are not filesystem quotas.

## Warmup, numerical checks and restart reuse

After the owner starts the reviewed candidate, observe it while Running but not
Ready, then collect evidence and send matched requests:

```sh
./bin/workstationctl --config config/workstation.conf rocm serving-startup sglang-COLD-POD cold artifacts/cold-startup
./bin/workstationctl --config config/workstation.conf rocm kernel-evidence sglang-COLD-POD artifacts/cold-evidence
./bin/workstationctl --config config/workstation.conf rocm kernel-warmup artifacts/interactive.json artifacts/cold-evidence artifacts/cold-run
```

Repeat after an owner-controlled restart, retaining the candidate, model,
workload and cache PVC. Use `warm`, the new Pod name and new output directories.
The workflow does not automatically roll out or stop any workload.

Warmup sends three rounds at every supplied context/concurrency, with unchanged
input IDs, fixed output budget and temperature zero. It retains output token IDs
and finite log probabilities, then runs the existing benchmark in `steady/`.
Startup, warmup and steady state are separate. Startup combines load/JIT/capture;
pure compilation time needs engine instrumentation. Filesystem and prefix caches
are not compiler caches.

Retain separate private restart logs for each TP rank. The experimental legacy
profile enables `TORCH_LOGS=+inductor`. Inspect the exact runtime's
`fx graph cache hit` messages and rank attribution:

```sh
./bin/workstationctl rocm kernel-compare artifacts/cold-run artifacts/warm-run artifacts/cold-startup/startup.json artifacts/warm-startup/startup.json artifacts/reuse.json --rank-log artifacts/restart-rank0.log --rank-log artifacts/restart-rank1.log --atol 0.001 --rtol 0.001
./bin/workstationctl rocm kernel-quality artifacts/baseline-run/result.json artifacts/candidate-run/result.json artifacts/quality.json --atol 0.001 --rtol 0.001
```

Select numerical tolerances before comparison. Changed tokens, nonfinite values,
missing cases, identity drift or missing memory checks fail validation. Sampled
log probabilities are not a complete model or coding-task evaluation; retain the
fixed coding suite. Token IDs can disclose output even without saved text.

Reuse needs a different process start, matching startup records, identical
runtime/settings, retained hashed files and positive hit logs for every TP rank.
Log attribution remains owner-reviewed. Triton-only runs without explicit hit
evidence remain inconclusive, even if files persist or startup gets faster.
[PyTorch cache semantics](https://docs.pytorch.org/tutorials/recipes/torch_compile_caching_configuration_tutorial.html).
Never import another user's untrusted cache: cached native code is executable.

Cgroup limits/events/peaks and run-aligned host/pod/per-card VRAM samples are
retained. Missing observations, OOM/limit events or runtime drift prevent success.
Lifetime peaks and sampled VRAM can miss phase-specific spikes. Keep raw token,
trace, path, model and hardware evidence private, outside Git.

## Profile before tuning

Use an exclusive maintenance window without another profiler or user traffic.
The command uses the existing exact-Pod loopback tunnel and credentials. Admin
authorization may be required; never weaken access controls to profile.

```sh
./bin/workstationctl --config config/workstation.conf rocm kernel-profile artifacts/interactive.json artifacts/candidate-evidence artifacts/profile-run
./bin/workstationctl rocm kernel-dispatch artifacts/rank0.trace.json.gz artifacts/profile-run/result.json artifacts/dispatch-rank0.json
```

Profiling is bounded to 32 steps. After a successful start, cleanup attempts
`/stop_profile`, including failures. The pinned default profiler can finish
automatically at that bound. Its subsequent stop raises an untranslated
`RuntimeError` (generic HTTP 500), not a documented success response.

The workflow checks the exact source hashes and rejects the unreviewed V2
profiler. Start/stop success bodies must match the pinned routes. A generic 500
remains pending until the same Pod supplies valid, hashed GPU traces for every
TP rank in this run's unique profile-ID directory, successful per-rank export
logs, and the exact not-in-progress exception. Unknown errors, missing/corrupt
traces, authorization and transport failures are fatal. Log collection is bounded
to 2,000 lines/256 KiB since this session; only a hash and the minimal validation
summary are retained. Truncated, differently formatted or insufficient logs fail
closed. An exclusive profiling window is required for this attribution; no other
user may call profiling endpoints during the run.

Traces remain in the unique recorded
`/cache/xdg/workstation-profiles/…` directory on the existing PVC. Copy only the
exact reviewed files from that Pod. Inspect every TP rank. Do not dump
`/server_info`, environment variables or credentials. The parser accepts Kineto
JSON/gzip, rejects CPU-only traces, and retains GPU kernel durations and host
operators without arbitrary trace arguments. Match files to the recorded path:
a local trace cannot authenticate its own origin.

If Kineto cannot identify library dispatch, collect a separate bounded
[ROCm 10 rocprofv3 trace](https://rocm.docs.amd.com/projects/rocprofiler-sdk/en/docs-10.0.0/how-to/using-rocprofv3.html)
or the library's documented logging in the same runtime. `aten::mm`, a loaded
library or a Tensile-looking name alone is insufficient. Keep unavailable
instrumentation pending; do not guess a backend.

Only after dispatch review, use an installed, supported tuner with the observed
shapes/dtypes/layouts. No generic Triton tuner is invented here. For hipBLASLt,
check the installed CLI, gfx1201 code objects and
[offline tuning contract](https://rocm.docs.amd.com/projects/hipBLASLt/en/docs-7.2.3/how-to-use-hipblaslt-offline-tuning.html).
That older manual explains the mechanism, not compatibility with this image.

Prepare a private review JSON containing `backend`, the exact observed `kernel`,
`shape` (positive integer dimensions), `dtype`, `library_log` (direct sibling
filename) and `library_log_sha256`. Retain actual
`hipblasLtMatmul` or Triton dispatch evidence in that log:

```sh
./bin/workstationctl rocm kernel-seal-tuning artifacts/profile-run/result.json artifacts/dispatch-rank0.json artifacts/dispatch-review.json artifacts/tuning.txt artifacts/sealed-tuning --kind hipblaslt
```

The bundle hashes the result/review and software/GPU/model/workload identity. It
suggests an identity-keyed directory on `/cache/xdg` for owner-controlled staging;
nothing is automatically installed or activated. Verify its identity and hash
before any experimental `HIPBLASLT_TUNING_OVERRIDE_FILE` setting, then repeat
numerical, memory and unprofiled serving checks with that exact result.

## Native llama HIP diagnostics

Set `LLAMA_HIP_EXPORT_METRICS=1` in the existing workstation config and invoke
`rocm build-llama` with a fresh candidate directory. The builder verifies the
source option, CMake cache and effective Ninja flags, and retains `build.log`.
Upstream adds `-Rpass-analysis=kernel-resource-usage --save-temps`: compiler
resource diagnostics, not runtime metrics or faster kernels.
[Pinned implementation](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/ggml/src/ggml-hip/CMakeLists.txt).
Budget disk space for saved temporaries. Existing memory-aware jobs, source locks
and runtime manifests remain active.

## Promotion, rollback and validation

Promote nothing automatically. Require repeated paired cold/restart measurements,
unprofiled TTFT/ITL/throughput spread, numerical/coding correctness, stable memory
and sustained thermals. Faster startup is not a steady-state gain; changes within
noise remain inconclusive.

Restore the reviewed baseline manifest through existing maintenance controls.
Remove candidate compile/log/tuning settings and graph arguments; restore the
original cache paths without deleting evidence. Set `LLAMA_HIP_EXPORT_METRICS=0`
and build a fresh ordinary native candidate. Recollect evidence after any
software/model/device change.

| Coverage | Local tests | Hardware status |
| --- | --- | --- |
| Profile generation | CPU/RAM/TP bounds, overrides, unsupported flags, identities | NOT RUN |
| Warmup/numerical checks | Synthetic response and sampler/cgroup failures | NOT RUN |
| Restart evidence | Process, retained-artifact and positive-hit fixtures | NOT RUN |
| Dispatch/tuning | GPU-event and provenance/review validation | NOT RUN |
| HIP diagnostics | Existing build fixture checks effective flags | Native build NOT RUN |

Run `make check-strict` with the documented private dependency environment.
Fixture success is not hardware qualification. Local evidence is retained under
`test-results/model-kernels.h23HSB/`.

Validation on 9 September 2026:

- Dependency-prepared `make check-strict` exited 0. The runner completed 46 shell
  scripts, including the explicit exceptions below; 32 Python tests, six Bats
  cases and three Ansible syntax checks passed. Strict logs are in
  `test-results/strict-check.SAZ7Pg/`. Ansible reported empty inventories; no
  playbook was applied to a host.
- The first strict run rejected the new source hashes in the existing K3s lock
  allowlist. The allowlist and SHA-256 validation were extended. Both the failed
  log and successful rerun are retained.
- After the final post-probe Pod identity guard, ShellCheck, shfmt, shell syntax
  and `tests/test_performance.sh` passed again, including all 32 Python tests.
- All 14 model-kernel tests also passed under Python 3.14 in an isolated,
  network-disabled Linux container. This was standard-library fixture testing,
  not execution of the AMD SGLang image.
- **Skipped:** gaming-image smoke tests (`HOME_LAB_GAME_IMAGE` unset), isolated
  Wayland process tests (`HOME_LAB_WAYLAND_RUNTIME_IMAGE` unset), and
  `systemd-analyze` service verification (the development host is macOS).

Actual model compilation, restart cache hits, GPU numerical correctness,
dispatch, tuning and sustained performance are **NOT RUN — target workstation
unavailable**. No hardware-qualified improvement or faster default is claimed.

The focused revalidation of installer `67a5060` and Bridge `deb6a93` is recorded
in [VALIDATION-67a5060.md](VALIDATION-67a5060.md). Shell cleanup now preserves the
primary workload/signal outcome, stops only its owned process groups and removes
its three named temporary files, including `compiler-before.json`. Unexpected
temporary contents are retained with a warning. Persistent caches and run
evidence are never cleanup targets.
