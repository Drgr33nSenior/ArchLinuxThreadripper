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
identity, model-file hashes and tokenizer files. The reader retains the raw
runtime hash as provenance and separately compares normalized model-file,
model-setting, launch-setting and resource conditions. This prevents a changed
weight file or resource budget from being concealed by an unchanged model
revision.

Optional sources are the existing memory plan, coding evaluation and device-power
evidence. If a numerical-quality result is present, include exactly two retained
finalized `kernel_runs`, listed in baseline then candidate order. Both must pass
the existing `model_kernels.compare_quality` contract, including schema 1,
`memory.status: checked`, complete warmup cases and sampled output checks. The
canonical comparison must contain the baseline hash of the first run and the
candidate hash of the second run. At least one run must also match the serving
runtime, Pod resources and derived profile identity. A matching digest does not
make a run qualified.

The comparison reader rejects symlinks, path escapes, oversized files and
unknown manifest fields. It records source hashes. Keep this bundle private:
the source artifacts can contain resource settings, model identities and other
operational metadata. Do not put prompts, environment values, credentials, raw
model output or full logs in the bundle.

The Bridge owner-only export action resolves a policy-approved bundle ID to a
sealed private directory and invokes the same reader. It does not accept a path
or command from an API client. The returned public summary contains hashes and
bounded status fields. `comparison.json` and `report.txt` remain private.

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
does not replace numerical checks with a coding score or promote a candidate.

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
the retained normalized source conditions: runtime provenance, model files,
model settings, launch settings and resources. The conditions file contains
exactly those five SHA-256 fields and no credentials or full runtime record. A
new selection stores the candidate conditions. A legacy selection or a current
observation without them reports `unknown`; it never reports a profile as
current. A known seven-field identity change remains `stale` even when
conditions are missing. Recollect evidence before treating a stale selection as
relevant.

For Bridge's read-only `profile-status` bundle, `current_identity` is a
root-policy-approved owner observation in sealed evidence. It is not a
client-supplied identity or an automatic all-identity probe. The helper verifies
the current boot, source and hardware conditions before export. It has schema 1, kind
`workstation-performance-profile-identity-observation`, status
`observed-not-qualified`, an RFC3339 UTC `observed_at`, the current Linux
`boot_id`, all seven runtime identity fields, and the five normalized
source-condition hashes. The dispatcher accepts an
observation only when it is no more than 15 minutes old. A missing, future,
stale or malformed observation reports `unknown`; it never reports a selected
profile as current. A matching observation maps the exported summary to
`current-unqualified`, while the retained canonical status remains
`selected-unqualified-current`. The result is only true as of `observed_at` and
does not establish live qualification or trigger reconciliation.

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

## Target qualification

For an owner-run candidate, collect matched cold and warm fresh-Pod startup
records, serving results, memory windows, numerical quality and coding-suite
evidence. Use the same model, tokenizer, workload, hardware and launch identity
unless the declared experiment permits one difference. Re-run correctness,
latency, throughput, memory-pressure and restart/recovery checks at the selected
candidate. Retain the old profile as rollback evidence. Do not infer a speedup,
RAM reduction, GPU scaling or wall-power result from these source fixtures.
