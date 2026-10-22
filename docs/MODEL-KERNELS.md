# Opt-in model-kernel compilation and tuning

This workflow extends `workstationctl rocm`; it adds no service and does not
change the SGLang baseline. All hardware candidates remain **unqualified**.
Run requests, profiling and restarts only in an owner-approved workstation test
window. No live workload or GPU was exercised during source implementation.

Host-RAM right-sizing is a separate
[measurement workflow](PERFORMANCE-VALIDATION.md#right-size-sglang-host-ram).
After changing the pod cap, regenerate worker budgets. `kernel-quality
--memory-only` allows a smaller equal memory request/limit while requiring all
other runtime/compiler identity to match; ordinary quality checks remain strict.

## Experimental Qwen3-8B BF16 full-b4 profile

The `bf16-full-b4` selection packages configuration only. It changes no image,
model, sampler, kernel or application default. Three balanced startup pairs in
`test-results/bf16-concurrency-confirm.4bSxcRNu` reproduced median paired decode
gains of 3.979342% at concurrency two and 3.890793% at concurrency four. These
are synthetic native-token workloads, not new measurements from the planner,
single-user rates, production tail latency or energy results. The sealed
`bf16-batch3-check.B8XO2yrF` separately passed batch-three numerical and dispatch
checks in one startup pair. It did not confirm batch-three speed independently.
The later bounded code-tracing task failed. Whole-model qualification remains
incomplete; this profile does not resolve that failure or qualify UltraQuant.

The later cross-study diagnostic in
`test-results/bf16-graph-package.MIavE4sx/PLACEMENT-COMPARISON.md` aligned all
generated IDs but rejected 1716 of 3072 repeated generated-score comparisons
between batched full-b4 and singleton controls. Its cause remains unresolved.
The within-study graph and replica results remain separate; do not claim
cross-placement numerical equivalence or add their speedups.

The contract is `infrastructure/sglang/bf16-full-b4/profile.json`. It pins the
tested image `7627df4882498d013f20d72405ab52d5f9b1af814d09f64c341d03d26c018763`,
config `942b88cf00edf4072d17843545347d56183a824614b8c4b9b3ba15b0b4278c72`,
Qwen/Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`, all 14 model
artifacts and 48 installed source files. It also pins Torch/HIP/Triton/SGLang/
RCCL, the image-owned compiler, Torch graph module and unchanged math/TunableOp
states. The source map comes from the graph studies, not the later DP closure.
No FP8 GEMM or MoE table applies to this dense BF16 model.

Both selections use BF16 weights/activations/KV, TP1/DP1, Triton attention,
4096 context and total tokens, static memory .80, chunked prefill 512,
max-running four and disabled overlap scheduling. The baseline is full-b1;
the candidate changes only decode graph `bs` to `[1,2,3,4]` and `max_bs` to 4.
Both disable prefill graphs and graph padding. The tested allocation remains
Guaranteed **two GPUs, 8 CPU, 40 GiB RAM and 4 GiB ephemeral storage**, although
model execution used the c3 card. A one-GPU Pod is not this allocation contract.
Do not transfer the profile to the dirty c9e4/Qwen3.5 application template.

Use the existing offline planner with a fresh output directory:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl rocm kernel-plan \
  DEPLOYMENT.json WORKLOAD.json EVIDENCE_DIR artifacts/bf16-graph-plan-01 \
  --profile bf16-full-b4 --experimental
```

`--graph-observation FILE` selects an explicit bundle; the default is
`EVIDENCE_DIR/graph-observation.json`. Do not supply compilation-worker or GEMM
image controls. The rendered baseline must contain explicit arguments and
environment, the tested immutable image, read-only model mount, writable cache
mount, UID/GID/fsGroup 1000, read-only root, RuntimeDefault seccomp, dropped
capabilities, no privilege escalation or service-account token, and the verified
deny-policy selector. The planner preserves unrelated fields, mounts and limits.
It rejects conflicting flags, unresolved environment references and unreviewed
math/dispatch controls. Unsupported input selects the **unchanged incumbent**;
it never converts another model, image or resource envelope automatically.

The observation bundle uses the existing owner receipt objects without changing
their values:

- `preflight`, `pod`, `image`, `runtime_versions`, `launch`, `singleton_topology`;
- `source_before`, `source_after`, `model_before`, `model_after`;
- `compiler_identity`, `torch_graphs_before`, `torch_graphs_after`, `resolved_contract`;
- `policy_before`, `effective_policy_before`, and optional `caches` snapshots.

Use the full observed baseline Pod for `pod`, the actual image/config receipt
for `image`, and raw before/after identities from the same owner-collected
closure. `resolved_contract` is the native effective-settings receipt, not the
requested CLI. The preflight timestamp must be at most ten minutes old and its
target, boot and kernel must match the measured contract. A changed boot or a
new session policy requires a separately reviewed owner workflow. Never replace
an old timestamp, infer source equality from a version label, or label a
requested device ordinal as an observed UUID/BDF.

The workload keeps native `input_ids`, the model revision, frozen corpus
`source_sha256`, `new-prefix`, concurrency two/four, 512 input and 128 output
tokens, three to twenty repetitions and one request per worker. A case may
contain one native request repeated at the declared concurrency, or an explicit
`requests` array with exactly one declared concurrency. The latter retains each
logical agent's IDs. The sum of active input/output reservations must fit the
single 4096-token pool. C2-2048, C8, context beyond 4096 and batch-three speed
confirmation are outside this profile.

The first plan supplies identity-keyed paths for Triton, TorchInductor, SGLang
JIT and XDG caches. Missing cache evidence selects the incumbent but can emit a
separate `conditional-proposal.json` for a compatible rendered template. That
proposal is **not selected or admitted for launch**. The existing owner must
create private fresh directories, check the entire cache root is empty, retain
headroom and hold FD9 through launch, measurements and cleanup. Feed actual
empty-tree snapshots back as `caches`: each environment name maps to
`{"root":"<proposed path>","files":{},"bytes":0}`. All four paths must match;
missing, nonempty or stale namespaces fail selection. These are filesystem
observations, not booleans to invent. `cache_admission` remains `NOT RUN` in
every offline plan; sampled 8-GiB bounds are not a hard filesystem quota and
cache reuse remains unqualified. Do not run a new GPU experiment only to obtain
planner metadata.

To inspect the historical package without claiming current admission, use a
checkout that retains the local receipts named in the profile's `provenance`.
These `test-results` artifacts are not included in Git or the bootstrap package.
This command retains their timestamps and
omits unavailable initial cache snapshots:

```sh
export WORKSTATION_PYTHON=/path/to/project/python
"$WORKSTATION_PYTHON" -B -c 'import sys; sys.path.insert(0, "lib/workstation"); import graph_profile; graph_profile.historical_example("artifacts/bf16-graph-historical-inputs-01")'
./bin/workstationctl rocm kernel-plan \
  artifacts/bf16-graph-historical-inputs-01/deployment.json \
  artifacts/bf16-graph-historical-inputs-01/workload.json \
  artifacts/bf16-graph-historical-inputs-01 artifacts/bf16-graph-historical-plan-01 \
  --profile bf16-full-b4 --experimental
```

Expected result: unchanged-incumbent selection, explicit historical/cache/Pod
reasons, and a distinct conditional graph-only proposal. The example Deployment
is a labelled offline projection of the historical envelope, not a live or
qualified deployment. It retains the original receipt objects separately.
Reusing the current `apps/base/sglang` Deployment instead must produce a no-op
selection with image/model/resource/security reasons and no compatible proposal.

The private output contains the pinned profile, identity receipt, selected
patch, exact reverse rollback and `SOURCE.sha256`. JSON Patch tests bind the
whole Pod template, including its network labels, before changing only graph
arguments and versioned cache environment. Review and recheck the existing owner
admission before any launch; this command never deploys or starts a service.
Use `incumbent-rollback.json` only while its exact-template test passes. It
restores full-b1 at **the same max-running four**, not concurrency one. Original
eager remains a separate selection: omit graph config/padding flags and use
`--disable-cuda-graph` in a fresh owner launch. Do not silently change concurrency
or infer a C2/C4 gain from that separate choice. Keep old images, caches, models
and failed evidence.

## Accepted Qwen3-Coder M=1 GEMM profile

On 7 October 2026, the owner accepted the retained 4.9653% primary decode gain
as sufficient to carry forward. The sealed study in
`test-results/fp8-rccl-screen.XjtE1iJ1` still records its original failed >=5%
threshold result. This new decision accepts the performance margin only.
Numerical, lifecycle, stability and model-quality checks remain required.
The checkpoint remains unqualified because its sorting task failed.

The explicit `gemm-m1` profile uses the packaged sampler image
`sha256:7627df4882498d013f20d72405ab52d5f9b1af814d09f64c341d03d26c018763`.
Its canonical contract and tables are under `infrastructure/sglang/gemm-m1`.
The profile pins Qwen3-Coder-30B-A3B-Instruct-FP8 revision
`dcaee4d4dfc5ee71ad501f01f530e5652438fde0`, all 14 model/tokenizer/template files,
both GPU UUID/BDF pairs, FP8 and sampler source hashes, Python 3.14,
Torch 2.13.0+rocm10.1.0, HIP 7.16.26385, Triton module 3.8.0 and its exact
`3.8.0+git669b31ac.rocm10.1.0` compiler distribution. It covers only the
retained TP=2, concurrency-one, 4096-token serving contract.

Only M=1 changes for N=2048/2560, K=2048, block shape 128x128. The selected
tile is BM=16, BN=64, BK=128, GROUP_SIZE_M=32, four warps and one stage.
Every M>=2 row, including the M=2 nearest-M guard, keeps the incumbent
BM=64, BN=128, BK=128, four warps and three stages. Both shape-specific files
have SHA256 `69afcbfc5bc1d362ea32ded30f25b373718b670c2a7e7b60720d2431f011eee6`.
FP8 arithmetic, kernel dispatch, MoE, communication and sampler source remain
unchanged. This profile does not qualify concurrency 2/4, 8K/32K or another model.

Create a fresh offline package with the project-configured Python executable:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl \
  build sglang-gemm-package artifacts/gemm-package-01 \
  --parent-layer test-results/sampler-package.55nYEsHR/sampler-layer.tar
```

Alternatively, supply `--parent-record FILE` containing the exact installed
sampler image RECORD. The builder validates its hash and sampler entry. It
emits a deterministic five-file OCI delta: the two tables, updated RECORD,
profile and runtime verifier. `layer.json` records every member, parent,
profile, source and layer hash. It also emits a Dockerfile for a local derived
image. It neither builds nor imports that image.

Reuse the retained owner `regctl image mod --layer-add tar=...` workflow with
the sampler OCI parent. Verify that the parent layers and image configuration
are preserved. Record the resulting immutable image/config digests in a JSON
receipt with `profile`, `parent_image`, `profile_sha256`, `image` and
`config_digest`; copy `profile_sha256` from `layer.json`. Import through the
existing owner workflow under a fresh name. Do not pull, publish, retag the
incumbent or alter a live container.

The named selection is available through the existing kernel planner:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl rocm kernel-plan \
  DEPLOYMENT.json WORKLOAD.json EVIDENCE_DIR artifacts/gemm-plan-01 \
  --profile gemm-m1 --gemm-image BUILT_IMAGE.json
```

`EVIDENCE_DIR/gemm-observation.json` must contain a fresh observation from
`gemm_profile.observe` in the allocated coherent image; `--gemm-observation`
can select another explicit file. Use native tokenizer IDs for the retained
512/128 and 2048/128 cases, concurrency one and at least three repetitions.
The planner checks the actual rendered arguments, two-GPU Guaranteed resources,
the existing cache mount and the reviewed security controls. It emits a
selection patch, an exact-template rollback patch and identity-keyed caches.
Tuning-file volumes must be dedicated to those table mounts. The named planner
rejects shared mounts, sidecar/init-container uses and volume-device uses before
removing an override. The packaged standalone planner supports dedicated
tuning volumes only; use the named repository interface for preservation checks.
The named GEMM-only planner also rejects `SGLANG_MOE_CONFIG_DIR`. A separate
experimental MoE selection must not use the frozen image helper's
`moe=incumbent` receipt; that helper does not validate this environment override.
Do not combine the two profiles or infer a MoE qualification from GEMM loading.
It writes the required offline/backend environment explicitly. Flatten and
review `envFrom` configuration before requesting this narrow profile;
unresolved references select the incumbent. Duplicate environment names are
rejected. Known communication and visibility overrides are removed from the
generated incumbent fallback; unrelated environment entries remain intact.
It does not contact the cluster or launch a server.

Unsupported or stale observations select the incumbent image and remove
tuning-file mounts. The packaged launch wrapper rechecks runtime/source/device,
model-file and argument identities, then confirms production table loading on
each card. It rejects extra controls and option aliases. Failure writes an
`incumbent-required` receipt before refusing server launch; a container cannot
replace its own image. Select the incumbent through the existing owner path.
Each launch creates a private `/tmp/gemm-profile-receipt-*.json` file and logs
its exact path to stderr. Receipts remain available when the `/tmp` volume
survives a container restart. The wrapper does not overwrite or remove earlier
receipts. Rebuild the package and derived image to include this restart fix;
previously built images retain the fixed-name receipt defect.
Model-file verification warms filesystem caches, so startup timing is not a
cold-disk loading measurement.

For rollback, use the generated `incumbent-rollback.json` if its template test
still matches. The standalone `gemm_profile.py plan --profile incumbent` also
selects the sampler parent without tuning mounts. Restore the pinned model and
serving arguments together. Keep old images, caches, model files and evidence.
No application default or persistent deployment is changed by this profile.

## Experimental M=1 MoE table payload

The separate `gemm-moe-m1-candidate3` configuration retains the finite MoE
screen from `test-results/gemm-moe-serving-recovery.qxmA1zod`. It is
experimental. The successor in `test-results/moe-confirmation-next.FQma4y5o`
confirmed positive additional gains in three balanced fresh-startup pairs:
median 3.8728% at 512/128 and 3.6984% at 2048/128, with all 1536 paired
output IDs and logprobs exact. Pair ranges were 3.7651–8.2571% and
3.5595–8.1201%; the slower second-pair baseline is retained, not discarded.
This is bounded concurrency-one repeatability, not model qualification,
tail-latency qualification or a general batch-size gain. The checkpoint
sorting failure remains.
The existing image, kernel arithmetic, routing, quantization and sampler stay
unchanged. Accepted GEMM tables are a pinned dependency, not an added percentage.

The canonical inputs are the ASCII-named `normal.json`, `down.json` and
`profile.json` under `infrastructure/sglang/moe-m1`. Both tables have SHA256
`c59324d8bd2254b7171c8a74187a90f13776958ce02e620757be04acd0a5afbf`.
Only M=1 changes: BM=32, BN=64, BK=128, GROUP_SIZE_M=32, four warps and
two stages. Every M>=2 row keeps the installed BM=64, BN=128, BK=128,
four-warp, two-stage incumbent. The explicit M=2 row guards nearest-M lookup.
Scope is E=128, top-k=8, hidden=2048, TP=2 local intermediate=384,
FP8 e4m3fn block-128 weights, BF16 activations and concurrency one within
the 4096-token contract. Both cards, runtime/compiler distribution, seven
installed source hashes, model revision and all 14 model files are pinned.
KV is `auto` on the BF16 model: BF16 is the source/config-derived expectation,
not an independently inspected buffer dtype. The static configuration hash
does not qualify cache reuse; require fresh boot/runtime/source/model/GPU
observations and a matching owner cache receipt before reuse.

Create a configuration package only; this command does not launch or deploy:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl \
  build sglang-moe-tables artifacts/moe-tables-01
```

The builder refuses changed/stale source identities and existing output paths.
It emits `payload`, deterministic `moe-tables.tar` and `package-identities.json`.
The payload contains the exact SGLang normal and `_down` filenames under
`configs/triton_3_8_0`, `profile.json`, full `gemm-dependency.json`, conditional
`SELECT-ENV.json` and `SOURCE.sha256`. No controller-side Torch dependency,
second image builder, source patch or persistent configuration is added.

The default remains the incumbent. Only the bounded owner workflow may select
this artifact after fresh admission and source/runtime/model/device checks.
Stage the complete payload as the dedicated read-only `/work/source/moe-config`
mount, and explicitly select `SGLANG_MOE_CONFIG_DIR=/work/source/moe-config`.
Before launch, reuse the provenance verifier's canonical runtime-context
initialization and confirm both normal/down tables load on each allocated card.
Check M=1 and the larger-M effective settings; imported API success is not
dispatch proof. Do not use the frozen GEMM-only helper receipt to validate a
combined MoE profile. Keep its separate outer environment guard intact.

Unsupported or stale identities require omission of `SGLANG_MOE_CONFIG_DIR`
and a fresh incumbent-MoE process. For rollback to GEMM-only, remove just this
environment setting and its dedicated mount through the owner workflow.
For the original incumbent, also omit GEMM table mounts. Preserve the sampler,
model, image, evidence and versioned caches. Concurrency 2/4 checks do not
extend the single-token tuning to larger M; 8K/32K remain separate experiments.

## Experimental no-residual RMSNorm source payload

The `rmsnorm-no-residual` package is a reproducible experiment, not an accepted
serving optimization. The bounded screen in
`test-results/rmsnorm-serving-next.Ns9VtTo7` passed both-card operator,
input/weight mutation, alias and graph checks. Its model output IDs diverged
from the mounted-GEMM baseline and failed the unchanged numerical gate.
Do not select it for serving or combine its measured speedup with GEMM/MoE.
No quality budget was granted for this approximation.

Canonical inputs are under `infrastructure/sglang/rmsnorm-no-residual`.
The existing offline build interface emits a deterministic source payload:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl \
  build sglang-rmsnorm-package artifacts/rmsnorm-01 \
  --source /path/to/pinned/sglang/srt/layers/layernorm.py
```

The builder checks the exact source preimage, semantic guard, profile and
accepted GEMM dependency before creating output. It emits a rewritten
`payload/layernorm.py`, profile, full GEMM identity dependency and source
manifest, plus `rmsnorm-source.tar` and `package-identities.json`. It imports
no Torch, builds no image and contacts no cluster. Old evidence is not edited.

For a separately authorized diagnostic only, reuse the owner workflow with
this module mounted read-only at its existing image-local path. Select
`WORKSTATION_RMSNORM_NO_RESIDUAL=1` explicitly in the server environment and
verify the installed module hash. Preserve the accepted GEMM table mounts.
The default is the original operator. The fast path covers only HIP native
fallback, BF16 inference, matching weights, widths128/2048, ranks2/3, at most512
rows and eps1e-6. Residual/post/quantization, cast or variance overrides,
padding, batch invariance, training, Gemma and other dtypes/shapes keep original
dispatch. Unsupported or stale runtime/model/device identities require omission
of the module mount and environment, not a claim that loading was qualified.

Rollback removes only that dedicated module mount and environment, then starts
a fresh mounted-GEMM incumbent. Preserve model files, images and bounded
versioned caches. The failed model gate remains visible despite passing tasks.

## Experimental native-reduction RMSNorm epilogue

The separate `rmsnorm-epilogue` profile keeps the installed FP32 conversion,
square, mean, epsilon addition and reciprocal square root. It fuses only the
two output multiplies and BF16 store. Triton floating-point fusion is disabled.
It does not enable the rejected fully fused `rmsnorm-no-residual` profile.

The operator study in `test-results/norm-epilogue-recovery.2TdZESwH` matched
native BF16 outputs exactly in 102 cases across both cards. These include
held-out seeds, boundary batches, strided Q/K inputs and saved real-model
activations. Independent FP64 checks, input/weight non-mutation, alias and
current-device graph checks passed. A second variant that fused reciprocal
square root failed exactness and was rejected. Operator timing alone does not
qualify serving or the checkpoint.

The three-pair serving study in `test-results/norm-epilogue-serving.lgNAthgp`
preserved all1536 paired output IDs and logprobs exactly. Median additional
gains over GEMM+MoE were3.0539% at512/128 and3.3827% at2048/128. However, one
candidate startup regressed2.1550%/2.0708%. This failed the frozen requirement
that no pair regress more than1%. Keep this package experimental; do not promote
it or discard the slow run. Mounted GEMM+MoE with native RMSNorm remains the
preferred measured selection. No numerical tolerance or performance gate changed.

Create the explicit source package through the existing interface:

```sh
WORKSTATION_PYTHON=/path/to/project/python ./bin/workstationctl \
  build sglang-rmsnorm-package artifacts/rmsnorm-epilogue-01 \
  --source /path/to/pinned/sglang/srt/layers/layernorm.py \
  --profile rmsnorm-epilogue
```

The builder pins the source preimage, profile, kernel, shared semantic guard
and GEMM dependency before creating a deterministic three-file source archive.
The profile is included in the bootstrap and Bridge source inventories. No
image build, live mutation or default selection occurs. The original package
selection remains available for historical reproduction, not serving approval.

Use `WORKSTATION_RMSNORM_EPILOGUE=1` only with the verified dedicated read-only
`layernorm.py` mount and fresh owner admission. The shared guard limits this
path to native HIP fallback, BF16 inference, matching weights, widths128/2048,
ranks2/3, 1–512 rows and epsilon1e-6. Residual, post-addition, quantization,
training, Gemma, batch invariance, padding, dtype and variance overrides retain
original dispatch. Stale source/runtime/model/device identities require omission
of both the mount and environment setting. They are not a cache-reuse approval.

Rollback removes only the dedicated norm mount and its environment setting,
then starts a fresh native-RMSNorm server. The selected verified GEMM/MoE tables,
sampler image, model and historical evidence remain available. Full rollback
also removes the GEMM/MoE selections. The known sorting failure still prevents
complete checkpoint qualification. UltraQuant remains a separate experiment.

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

The [SGLang model-loading guidance](https://docs.sglang.io/docs/advanced_features/model_loading)
described multithread loading and sharded state. It is discovery material only:
the locked image retains its existing defaults until its exact `--help` and
hashed loader source prove a supported candidate control.

[AMD's Radeon guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html)
and existing Triton/AITER settings remain the baseline. No MI300 tile sizes,
GEMM shapes, waves or memory fractions are copied. An FP8 checkpoint or a loaded
BLAS library does not prove the hot operation uses that library.

## Loading, warm state and admission candidates

For native speculative-depth and hybrid-state precision candidates, use the
[SGLang experiment plan procedure](PERFORMANCE-PROFILES.md#native-sglang-experiment-plans).
It shares these retained inputs but has separate support and qualification limits.

`serving_runtime.py` produces only independent JSON Patch candidates. It does
not restart a Pod, send a warmup prompt, delete a cache, or select a profile.
Before it offers loading threads or a queue cap, `kernel-evidence` runs the
selected image's own `--help` and hashes the specific server/loader/scheduler
source fragments that expose the control. A current online SGLang manual does
not override a missing capability in that record.

A loading candidate changes only the exact-image
`--model-loader-extra-config` JSON value when the observed loader source proves
both `enable_multithread_load` and `num_threads`. It explicitly enables the
candidate; it does not assume that a selected image inherited a newer default.
The planner bounds per-rank threads by observed effective
CPU, Pod cgroup memory/current usage, a declared reserve and a declared
per-thread allowance. It preserves weights and the existing default. Pre-sharded
checkpoint creation is deliberately unsupported until the selected loader proves
its derived-artifact format and an owner supplies a separate disk budget.

The optional native queue candidate similarly requires an observed
`--max-queued-requests` capability and is capped at 256 requests. It does not
change `MAX_RUNNING_REQUESTS`, resource envelopes, Kubernetes priority or the
Bridge management-operation queue. The current private clients do not convey a
reviewed SGLang priority field, so interactive/background priority is explicitly
unsupported rather than inferred from the Pod `PriorityClass`. Target testing
must establish the exact overload response and cancellation release behavior;
agent/tool work is never replayed automatically.

Warm status remains separate from `/health` and Kubernetes readiness. A matching
finished `kernel-warmup` with checked memory reports representative `ready` only
while the current Pod is Ready. No warmup record leaves a ready Pod `healthy`
with representative warmup `not-applicable`. A new UID, process/container identity, model/runtime/device
identity, cancellation or failed warmup cannot report ready. An in-progress
state requires an independently observed lifecycle record for the same process;
a caller declaration is not evidence. The fixed synthetic workload is optional,
bounded and owner-invoked only after the normal startup gate, and its prompts or
raw outputs are not status payloads.

Use the controller's owner-only wrappers after they have bound the current Pod
and evidence identities. `workstationctl` exports only plans and status:

```sh
./bin/workstationctl --config config/workstation.conf rocm serving-loading-plan DEPLOYMENT.json EVIDENCE_DIR OUT --threads 1,2,4 --reserve-mib 32768 --per-thread-mib 256
./bin/workstationctl --config config/workstation.conf rocm serving-queue-plan DEPLOYMENT.json EVIDENCE_DIR OUT --maximum-queued 8
./bin/workstationctl --config config/workstation.conf rocm serving-warm-status sglang-EXACT-POD kernel-warmup-result.json OUT/status.json
```

`serving-warm-status` first observes the named scoped Pod. A current
`ContainerCreating` or `PodInitializing` container reports `model-loading`
without reading old ready-only evidence. A current crash loop, termination or
image-start failure reports `unavailable` with a fixed reason. Kubernetes
messages and prior termination text are not exported. A current running,
healthy container ignores historical failures, but still reports `unknown` if
its fresh model, runtime, device or process identity cannot be collected. It
does not run a representative warmup. Use `-` instead of a result file when no
optional warmup was run.

The existing `kernel-warmup` command is an owner-run non-root measurement. The
installed session state and canonical lock are root-owned, so this release does
not pass an unverified environment lock descriptor to that command. Run it only
in an owner-coordinated AI maintenance window with no transition in progress.
Bridge must report extra live warmup as unavailable until a reviewed typed
root-to-non-root lock protocol exists. Do not relax session-state permissions or
reuse a warm record after gaming, cancellation, timeout or process change.

`kernel-evidence` records one read-only `statvfs` snapshot for the exact
`MODEL_PATH`, `TRITON_CACHE_DIR`, and `TORCHINDUCTOR_CACHE_DIR`: path, filesystem
device/inode, total/available bytes, and timestamp. Missing or unsafe roots stay
unknown; the loading plan requires recollection before qualification. This is not
sampled during a model load and is not a pure-loader timing measurement.

Retain fresh cold/warm startup, host/cgroup/VRAM, disk-headroom, numerical and
coding evidence for every candidate. Startup combines loading, JIT and engine
warmup; it is not a pure loader timer. Synthetic source tests do not establish
image execution, performance, queue fairness or target qualification.

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

Run `make check-strict` with the documented private dependency environment before
changing source. Fixture success does not qualify a runtime image, a GPU build,
restart cache reuse, numerical correctness, dispatch behavior or sustained
performance. Run the target procedure above before selecting a candidate.

The normal suite uses synthetic sealed-history fixtures and runs in a clean
checkout. On the workstation checkout that retains the benchmark receipts,
also run `"$HOME_LAB_PYTHON" -B tests/hardware/graph-history-retained.py`.
This separate integration check verifies the original artifact hashes and
confirms that historical observations cannot authorize a current selection.
Missing or changed retained evidence fails that check; it is never fabricated.

Shell cleanup preserves the primary workload or signal outcome, stops only owned
process groups, and removes only its named temporary files. It retains unexpected
temporary contents with a warning. Persistent caches and run evidence are never
cleanup targets.
