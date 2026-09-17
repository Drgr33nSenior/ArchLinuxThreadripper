# Performance comparison and measured profiles

This workflow compares retained workstation evidence. It does not start a Pod,
change a Deployment, select an engine setting at runtime, or establish a
hardware result. Use it only in an owner-approved maintenance window after the
existing startup, serving, memory and numerical-quality workflows complete.

## Typed Bridge bundle

Use `performance_bundle.py` to seal one private analysis directory before a
Bridge owner-only export. The bundle has one kind: `comparison`, `coding-eval`,
`loading`, `queue`, `warm-status`, `cache`, `profile-selection` or
`profile-status`. Its
`spec.json` selects only typed, relative inputs. It cannot supply a shell
command, URL, executable, environment variable map, cache root or deployment
patch.

Every `spec.json` contains `"schema": 1` and its exact `"kind"`, plus only the
keys below. Input paths are relative to the private bundle. A directory input
includes its complete retained evidence tree, not a symlink to another location.
Unknown keys and unsupported schema versions are refused.

| Kind | Additional required keys |
| --- | --- |
| `comparison` | `baseline_bundle`, `candidate_bundle`, `policy_json` (paths); `declared_variables` (string array) |
| `coding-eval` | `responses`, `corpus` (paths; use the reviewed `coding_tasks.json` corpus) |
| `loading` | `deployment`, `evidence` (paths); `threads` (integer array); `reserve_mib`, `per_thread_mib` (integers) |
| `queue` | `deployment`, `evidence` (paths); `maximum_queued` (integer) |
| `warm-status` | `evidence` (path); `warmup`, `lifecycle` (paths or explicit `null` when unavailable) |
| `cache`, inventory | `mode: "inventory"`, `registry` (path to the reviewed namespace registry) |
| `cache`, prune plan | `mode: "plan"`, `inventory` (path to the retained inventory output directory) |
| `profile-selection` | `comparison` (path), `candidate_id` (exact ID), `previous_selection` (path or `null` for the first selection) |
| `profile-status` | `selection`, `current_identity` (paths to retained selection and sealed owner observation) |

For example, place this spec beside private `baseline/`, `candidate/` evidence
directories and the threshold file described below:

```json
{
  "schema": 1,
  "kind": "comparison",
  "baseline_bundle": "baseline",
  "candidate_bundle": "candidate",
  "policy_json": "thresholds.json",
  "declared_variables": ["compiler_backend", "software", "launch"]
}
```

Declare only the intended experiment. The comparison still displays every
observed identity difference and refuses undeclared changes. Cache root and
free-space reserve are administrator configuration, never spec fields. The
owner identity for selection comes from authenticated Bridge execution, not a
bundle value.

```sh
python3 lib/workstation/performance_bundle.py seal artifacts/analysis-bundle \
  --kind comparison --target reviewed-workstation \
  --source-revision EXACT_64_HEX_BRIDGE_CONFIGURATION_CONTENT_REVISION
```

The directory and every file must already be owner-private, regular and
symlink-free. The sealer rejects more than 256 files, a file above 64 MiB, a
total above 256 MiB, unknown files after sealing, changed hashes and output
collisions. It creates the immutable `manifest.json`. Retain its SHA-256 with
the managed evidence ID. The owner-only helper must resolve that ID to its
root-policy-approved directory; do not pass this command a browser or CLI path.

The helper uses this fixed interface after it verifies the manifest hash,
target, Bridge managed-configuration content revision and owner identity. This
64-character value is the Bridge configuration `ContentRevision`, not an
installer Git commit, source archive revision or runtime-artifact approval.
The installed-tool closure hashes remain separately checked by root-owned
policy; comparison inputs retain their own evidence-file identities.

```sh
python3 lib/workstation/performance_bundle.py run BUNDLE MANIFEST_SHA256 NEW_OUTPUT \
  --target reviewed-workstation --source-revision EXACT_64_HEX_BRIDGE_CONFIGURATION_CONTENT_REVISION \
  --owner owner-main
```

`NEW_OUTPUT` must not exist. The dispatcher creates a private output tree and
`summary.json` that matches Bridge's bounded performance-summary contract. The
summary contains only statuses, bounded fields, hashes and artifact metadata.
Detailed JSON and `report.txt` stay private. A refused input produces a retained
failed summary. It does not turn a partial observation into success.

## Evidence bundle

Place a private `manifest.json` in a new owner-owned directory. Use schema 1
and kind `workstation-performance-evidence`. The required sources are one
finalized `serving` result, its original `runtime` evidence JSON, and one or
more `startup` records. A serving result is eligible only after the existing
provenance step changes its status to `measured-not-qualified`.

`experiment` contains exactly `variables`, `profile`, and `prefix_state`. The
profile and prefix values must match the retained serving result. If they differ
between profiles, declare `profile` or `prefix_state` in the comparison; the
report treats either difference as non-equivalent conditions.

The reader derives the seven manifest identity fields from producer evidence and
rejects copied values. It binds model revision to `runtime.settings.MODEL_REVISION`,
tokenizer identity to the retained tokenizer-file records, workload identity to
the serving workload hash, hardware identity to the observed node/device records,
software identity to the image/image-ID/package/HIP subset, and launch identity
to the observed Pod launch-spec hash. It also verifies that the original runtime
file hash equals `serving.runtime_sha256`. Quantization comes from the observed
model contract. The source record must identify an immutable image and image ID,
Guaranteed Pod resources, distinct observed `gfx1201` GPU UUIDs, package and HIP
identity, model-file hashes and tokenizer files.

The reader also records source-condition schema 2. It retains the raw runtime
hash as provenance and compares normalized model files, model settings (including
`MODEL_DTYPE`), the observed engine launch, environment-derived launch settings,
and Pod resources. The observed launch is a validated, allowlisted
`runtime.launch` command map. It is separate from the Pod launch-spec hash: the
former records effective engine arguments such as queue or attention settings;
the latter identifies the Deployment template. This prevents a changed weight
file, engine argument, dtype, or resource budget from being concealed by an
unchanged model revision.

Optional sources are the existing memory plan, coding evaluation and device-power
evidence. If a numerical-quality result is present, include exactly two retained
finalized `kernel_runs`, listed in baseline then candidate order. Both must pass
the existing `model_kernels.compare_quality` contract, including schema 1,
`memory.status: checked`, complete warmup cases and sampled output checks. The
canonical comparison must contain the baseline hash of the first run and the
candidate hash of the second run. The candidate run must exactly match this
profile's retained serving runtime, Pod identity and resources, workload, model
files, settings, and observed launch. A matching digest does not make a run
qualified.

The comparison reader rejects symlinks, path escapes, oversized files and
unknown manifest fields. It records source hashes. Keep this bundle private:
the source artifacts can contain resource settings, model identities and other
operational metadata. Retain the prescribed sampled token IDs and log probabilities
for numerical revalidation, and only the collector's allowlisted runtime settings.
Do not include prompts, generated prose, full environment dumps, credentials or
full logs. These private checked-run records are not cloud-adviser inputs.

The Bridge owner-only export action resolves a policy-approved bundle ID to a
sealed private directory and invokes the same reader. It does not accept a path
or command from an API client. The returned public summary contains hashes and
bounded status fields. `comparison.json` and `report.txt` remain private.
Bridge keeps its analysis-output size limits. A comparison whose retained
quality evidence exceeds those limits remains an offline private result until
the owner supplies a smaller, complete evidence set; the export refuses it
rather than dropping evidence or claiming a result.

## Compare evidence

Create an explicit threshold file:

```json
{
  "schema": 1,
  "minimum_practical_gain_percent": 5,
  "maximum_regression_percent": 5,
  "noise_percent": 2
}
```

Then create an offline report:

```sh
python3 lib/workstation/performance_profiles.py compare \
  artifacts/profile-baseline artifacts/profile-candidate artifacts/thresholds.json \
  artifacts/profile-comparison --declare compiler_backend
```

The supported declared variables are `compiler_backend`, `image`,
`concurrency`, `loading_strategy`, `loading_threads`, `engine_queue`,
`interactive_priority`, `model`, `tokenizer`, `workload`, `hardware`,
`software`, `launch`, `resources`, `quantization`, `coding_corpus`, `generation`,
`profile` and `prefix_state`. The command rejects every other difference. It
preserves and matches each serving case by context-token and concurrency labels;
it never pairs list positions as
if they were comparable. Model, tokenizer, quantization, workload, hardware,
concurrency, coding-corpus and generation variants may be retained as declared
experiments, but cannot receive an equivalent-quality throughput recommendation.
A changed model plus changed hardware is not a GPU-scaling result.

The report retains failed or incomplete evidence. It reports failures and
missing measurements as failures or unknown values, never as zero. It preserves
cold and warm startup separately. It labels loading/JIT/warmup/readiness as a
combined timing when the underlying startup evidence has that scope. A p95 is
unknown unless at least 20 samples support it. Device-power evidence is always
labelled device power, not wall power. A small/noisy change is inconclusive.

A candidate recommendation requires at least three throughput repetitions in
each matched serving case, complete successful cold and warm startup evidence,
and observed latency and TTFT medians for both profiles. When both p95 values
exist, the same regression tolerance applies to them. The configured maximum
regression percentage applies independently to cold/warm startup, latency and
TTFT; a token-rate increase cannot override any regression. The report also
uses the larger of the configured noise allowance and the observed combined
throughput variation. A memory plan is optional, but a retained plan with a
refusal or explicit failure/pressure status blocks selection. Missing pressure
counters remain unknown; the comparison does not infer them from an envelope.

Numerical quality and coding evaluation are independent gates for both baseline
and candidate. Equivalent-quality comparison additionally requires matching
coding corpus, template hashes and complete generation settings. The comparison
re-runs the canonical numerical comparison on the two retained candidate runs
that represent the profiles; each run retains its ordered baseline/candidate
provenance. Missing, malformed, swapped, or incompatible retained runs make the
quality gate inconclusive. The comparison does not replace numerical checks with
a coding score or promote a candidate.

## Select and check a profile

An owner can record an evidence-bound selected candidate. This is a
configuration record only; it does not apply a patch, update source defaults or
restart SGLang.

```sh
python3 lib/workstation/performance_profiles.py select \
  artifacts/profile-comparison/comparison.json owner-main candidate-a \
  artifacts/profile-selection.json --previous artifacts/previous-selection.json

python3 lib/workstation/performance_profiles.py status \
  artifacts/profile-selection.json artifacts/current-runtime-identity.json \
  --conditions artifacts/current-runtime-conditions.json
```

`select` requires the exact candidate ID and an eligible `candidate`
recommendation from the comparison; it has no implicit owner override for a
regression, incomplete quality evidence or incomparable experiment. It retains
the previous selection by hash. Before selecting, it recomputes the canonical
comparison from its retained inspected evidence, declared variables and
thresholds; a free-form recommendation or altered quality/case report is
refused. `status` marks the selection stale when model, tokenizer, workload,
hardware, software, launch or quantization identity changes. It also compares
source-condition schema 2: raw runtime provenance, model files, model settings,
observed engine launch, environment-derived launch settings, and resources. The
conditions file contains the schema marker and these SHA-256 values only; it has
no credentials or full runtime record. A new selection stores the candidate
conditions. A legacy selection, old condition schema, or current observation
without valid conditions reports `unknown`; it never reports a profile as
current. A known seven-field identity change remains `stale` even when
conditions are missing. Recollect evidence before treating a stale selection as
relevant.

For Bridge's read-only `profile-status` bundle, `current_identity` is a
root-policy-approved owner observation in sealed evidence. It is not a
client-supplied identity or an automatic all-identity probe. The helper verifies
the current boot, source and hardware conditions before export. It has schema 1, kind
`workstation-performance-profile-identity-observation`, status
`observed-not-qualified`, an RFC3339 UTC `observed_at`, the current Linux
`boot_id`, all seven runtime identity fields, and the schema 2 normalized
source conditions. The dispatcher accepts an
observation only when it is no more than 15 minutes old. A missing, future,
stale or malformed observation reports `unknown`; it never reports a selected
profile as current. A matching observation maps the exported summary to
`current-unqualified`, while the retained canonical status remains
`selected-unqualified-current`. The result is only true as of `observed_at` and
does not establish live qualification or trigger reconciliation.

## Native SGLang experiment plans

Keep the existing SGLang image, target weights and resource envelope. The
following commands read a retained Deployment and exact-image kernel evidence;
they do not contact Kubernetes or start an inference request. Collect the inputs
with the [kernel evidence procedure](MODEL-KERNELS.md#loading-warm-state-and-admission-candidates).
Use a new private output directory for each plan.

```sh
workstationctl rocm serving-experiment-plan deployment.json artifacts/kernel-evidence \
  artifacts/ngram-plan --axis speculation --algorithm NGRAM --values off,2,4
workstationctl rocm serving-experiment-plan deployment.json artifacts/kernel-evidence \
  artifacts/state-plan --axis hybrid-state --values float32,float16
```

Expect `plan.json` with `status: plan-only-unqualified` and one JSON Patch per
candidate. Directories are mode 0700; files are mode 0600. Each patch tests the
whole original Pod spec before it changes only container arguments. An existing
output directory, ambiguous arguments, missing exact-image flags, or experiment
controls that differ between manifest and observed launch cause refusal. Supply
explicit matching experiment values, not environment substitutions.

| Axis | Candidates | Boundary |
| --- | --- | --- |
| Speculation, NGRAM | `off`, fixed draft-token limits `1,2,3,4,7,8` | No draft model; CLI presence is not proof of HIP/model support |
| Speculation, EAGLE or NEXTN | `off`, fixed steps `1,2,3,4,7` with top-k 1 | Requires an already observed setup with that algorithm and top-k; retains its staged draft arguments |
| Speculation, EAGLE only | `adaptive` | Uses native default tiers; starts at 3 steps/4 slots, may allocate up to 7 steps/8 slots; custom adaptive configs are refused by the planner |
| Hybrid state | `float32`, `float16`, `bfloat16` | Observed Qwen3.5-family architecture only; changes recurrent-state dtype, not target weights or full-attention KV dtype |

For an owner-prepared EAGLE setup, replace the first command's algorithm with
`EAGLE` and values with `off,3,adaptive`. Do not combine these plans with the
separate experimental KV backend. No plan changes the installed default or
selects a profile. Bridge can consume the resulting measured comparison through
the existing bundle contract; it does not apply these experiment patches.

The reviewed behavior comes from the selected upstream SGLang
[server arguments](https://github.com/sgl-project/sglang/blob/0b3bb0cbe31873994c9f989fddfe2f87ca839fdd/python/sglang/srt/server_args.py)
and [adaptive decoding contract](https://github.com/sgl-project/sglang/blob/0b3bb0cbe31873994c9f989fddfe2f87ca839fdd/docs/advanced_features/adaptive_speculative_decoding.md).
`versions.lock` remains the image and source-hash authority. Requested arguments
are not evidence of effective adaptation, kernel dispatch or absence of fallback.

The runtime probe now retains allowlisted quantization, speculative and hybrid
controls. A draft checkpoint or adaptive config must be a regular, symlink-free
artifact under `/models`; remote identifiers are refused. Draft inventories are
bounded to 2048 files; a config is bounded to 64 KiB. File hashes bind these inputs
to launch conditions independently of target-weight identity. Recollect older
evidence for experiments with draft/config paths: missing artifact identities
are refused, not inferred from a path or revision. Ordinary earlier evidence
without these controls remains compatible.

Weight-format kernel experiments remain separate. Online Quark MXFP4 is blocked
by the selected source's gfx95 check; do not override it for gfx1201. Prequantized
MXFP4 needs verified gfx1201 GEMM dispatch and independent weight/quality evidence.
No reviewed SGLang ParoQuant implementation is supplied. The selected
`extra_buffer_lazy` hybrid-cache path excludes HIP and speculative decoding.
These cases stay unsupported or unqualified in the plan; no kernels or model
conversions are fabricated to make them available.

### Petit weight-kernel option

Petit is an explicit **blocked experiment option**, not an enabled R9700 backend.
Use the same retained Deployment and exact-image evidence as above:

```sh
workstationctl rocm serving-experiment-plan deployment.json artifacts/kernel-evidence \
  artifacts/petit-plan --axis weight-kernel --values petit_nvfp4
```

With valid inputs, expect exit status **2**, a refusal on stderr, and a private
`plan.json` with `status: blocked`, `qualification: unqualified`, the requested
quantization, the separately observed launch value, and an empty `cases` object.
No JSON Patch is written. An existing output directory or invalid evidence is
refused without overwriting it. Do not suppress this failure in automation.
Installing a package, supplying a capability label, or observing the requested
CLI flag does not override the hardware gate. Other planner axes are unchanged.

The selected SGLang source already includes the
[Petit adapter](https://github.com/sgl-project/sglang/blob/0b3bb0cbe31873994c9f989fddfe2f87ca839fdd/python/sglang/srt/layers/quantization/petit.py).
That adapter selects prequantized **NVFP4, group size 16** dense linear weights;
it is not a general Petit MXFP4, MoE, or KV-cache selector. The library's broader
capabilities do not establish support in this adapter. See its
[checkpoint and dispatch checks](https://github.com/sgl-project/sglang/blob/0b3bb0cbe31873994c9f989fddfe2f87ca839fdd/python/sglang/srt/layers/quantization/petit_utils.py).
The command does not convert FP8/BF16 weights or download a different checkpoint.

Petit source revision `d6f5289987ac01234830682840b65bad8256f1b0`
[documents CDNA2/3/4 support](https://github.com/causalflow-ai/petit-kernel/blob/d6f5289987ac01234830682840b65bad8256f1b0/README.md).
Its [matrix intrinsics and layouts](https://github.com/causalflow-ai/petit-kernel/blob/d6f5289987ac01234830682840b65bad8256f1b0/lib/gemm/rocm/amd_intrinsics.cuh)
use CDNA MFMA and 64-lane waves. A `gfx1201` BF16 conversion utility in that file
does not establish an RDNA4 matrix path. This reviewed source identity is not a
new dependency pin: the installer neither fetches nor builds Petit. The existing
image authority remains `versions.lock`; Petit package presence in that image
has not been established by these source checks.

Any necessary GPU-kernel port belongs in `Spry.ai-sglang-runtime`, separate from
the UltraQuant KV implementation. Keep the existing SGLang adapter when its
semantics are sufficient. Bridge continues to own experiment orchestration and
evidence consumption, not GPU kernels. Neither repository needs a placeholder
adapter to represent this blocked selection.

Before enabling the option, the owner must retain an exact runtime build and
dependency inventory, demonstrate `gfx1201` compilation and actual Petit
dispatch without fallback, and review the adapter/checkpoint relationship.
Compare the same identified NVFP4 weights across the baseline and candidate
backends; compare quantized weights with a higher-precision reference separately.
Retain numerical/coding checks, long-context behavior, cancellation, host RAM and
each GPU's peak/steady VRAM independently. Do not combine this experiment with
the separate KV backend or promote it automatically. Until those source and
runtime requirements are met, leave the gate closed and use the unchanged
baseline. A blocked report requires no rollback because nothing was applied.

### Diagnostic interpretation

Serving `result.json` retains diagnostic snapshots in `queue_before` and
`queue_after`. Missing, malformed or oversized metrics remain unavailable.
Prometheus timestamps are not metric values. Distinct label sets retain opaque
series hashes; the collector does not sum ranks or expose label values.

Native speculative acceptance rate/length are batch gauges, not workload-wide
acceptance statistics. Acceptance length includes the bonus token. Native step
and draft-token gauges can expose adaptation, but two snapshots cannot prove
the complete adaptation history. Hybrid Mamba capacity gauges count state slots,
not full-attention KV tokens or bytes. Keep each GPU's memory separate from host
RAM and from the other GPU. See the pinned
[metric definitions](https://github.com/sgl-project/sglang/blob/0b3bb0cbe31873994c9f989fddfe2f87ca839fdd/python/sglang/srt/observability/metrics_collector.py).

These snapshots do not time GPU kernels. Keep request TTFT/latency, host
scheduling or IPC time, synchronized GPU timing, and profiler instrumentation
overhead separate. Use the existing [profiling procedure](MODEL-KERNELS.md#profile-before-tuning)
for owner-run dispatch and timing evidence; do not infer a GPU speedup from a
shorter host enqueue interval.

## Coding and tool evaluation

The versioned suite has ten small local tasks. It covers structured output,
fixed synthetic tool names and arguments, negative/refusal cases, and two code
tasks with required compile or unit-test verification. It does not call an LLM.

The current Bridge build worker accepts only named source-build recipes. It is
not an arbitrary-code executor, so code task results are
`unavailable-no-reviewed-isolated-executor` after syntax parsing. They are not
reported as compile or unit-test success. A future evaluator must use a reviewed
isolated executor with bounded CPU, memory, output, concurrency, no network and
no credentials. It must supply sealed execution attestations; do not execute
model response text in an ordinary workstation shell.

Use a response file that binds the installed corpus hash and records complete
generation settings (`model`, `temperature`, `max_output_tokens`, `seed`) plus
bounded per-task output hashes and timing/token accounting:

```sh
python3 lib/workstation/coding_eval.py artifacts/coding-responses.json \
  artifacts/coding-evaluation.json
```

The result omits prompt and response text. Missing latency or token accounting
is unknown, not zero. The suite supplements the existing numerical checks and
does not qualify a model, quantization or profile.

### Streaming tool-call pairs

For each tool task, optionally add `protocol` to its existing response row:

```json
{
  "protocol": {
    "non_streaming": {"choices": []},
    "streaming": "RAW_SSE_BODY"
  }
}
```

This illustrates the fields, not a passing response. Replace both values with
the actual paired `/v1/chat/completions` responses: the parsed non-streaming JSON
and the raw streaming SSE body. Retain the required `response` field and use the
same task, model, prompt and generation settings for both requests. Captures
are owner-collected, private and bounded to 64 KiB per pair; this evaluator does
not make requests or execute tool calls.

Both responses must select the corpus's single expected function with complete
JSON arguments. The checker assembles fragmented stream arguments and requires
`finish_reason: tool_calls` followed by `[DONE]`. Truncated markup, disconnects,
cancellation/length finishes, extra calls, duplicate JSON keys and mismatched
arguments fail. Separate requests may have different tool-call IDs.

Legacy or missing captures remain `unknown-not-collected`, not protocol success.
The private result records only the capture hash and outcome. Bridge's public
summary reports `protocol_passed`, `protocol_failed` and `protocol_missing`
counts without function names, arguments or raw responses. This narrow check is
not full OpenAI-protocol conformance or a substitute for runtime cancellation,
long-recurrence and speculative-rollback qualification.

## Target qualification

For an owner-run candidate, collect matched cold and warm fresh-Pod startup
records, serving results, memory windows, numerical quality and coding-suite
evidence. Use the same model, tokenizer, workload, hardware and launch identity
unless the declared experiment permits one difference. Re-run correctness,
latency, throughput, memory-pressure and restart/recovery checks at the selected
candidate. Retain the old profile as rollback evidence. Do not infer a speedup,
RAM reduction, GPU scaling or wall-power result from these source fixtures.
