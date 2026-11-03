# Dense Qwen4 readiness

Status: source-validated preparation; target execution **NOT RUN**. The
experimental profile is disabled and cannot activate a service. No official
dense Qwen4 checkpoint was found in the Qwen organization search on 2026-10-25.
Repository, revision, licence, tokenizer, tensor inventory and runtime image
remain unresolved. Qwen3.8-Flash-Next is a different MoE model, not a substitute.

## Compatibility matrix

The reviewed source pins and file hashes are in
`infrastructure/sglang/qwen4-readiness/profile.json`. S denotes the pinned
SGLang source, T the pinned Transformers source, and I the Intel PR head.
Source support is not correct execution or hardware qualification.

| Component | Existing support and source | AMD path and missing work | Validation |
| --- | --- | --- | --- |
| Configuration, loading, TP | T Qwen4-Exp config/TP plan; S Qwen4-Exp loader; I dense H5120 TP2 clues | Installed image contracts cover Qwen3.5-family models. Dense release schema, weights and TP mapping unresolved; do not inherit MoE defaults | Config/asset/shape refusal fixtures; dense load NOT RUN |
| FP8, scales, GEMM | S FP8/Triton dispatch; I E4M3FN W8A16/scalar scales | gfx1201 path must prove actual GEMM, E4M3FN versus FNUZ, block/scalar scale layout, accumulator and excluded tensors | Existing Qwen3.8 evidence is model-specific; Qwen4 NOT RUN |
| Residual, norm, hyperconnections | T gated residual; S grouped norm and hyperconnection implementation; I H5120/four-stream kernels | Torch/Triton candidates; no SYCL transplant or inherited FP16 rounding policy | Independent FP32 reference plus explicit cast boundaries required; NOT RUN |
| Gated DeltaNet | Existing SGLang hybrid attention; S PLE-aware state commits; I GDN kernels | ROCm implementation exists for earlier model; Qwen4 dimensions, state precision and rollback need qualification | Rejected-token rollback and request reuse NOT RUN |
| QSA | T eager/SDPA reference; S indexer, Triton sparse attention and CUDA-specialized alternatives; I TP2 local Q12/KV2 | HIP branches exist in source, but dispatch and dependencies on gfx1201 are unqualified. Sparse selection does not shrink full KV automatically | Causal selection, partial blocks, live cache and graph replay NOT RUN |
| PLE, n-grams, convolution, offload | T cache state; S sharded tables, host-table and convolution paths; I PLE kernels | Enable only from actual config. Account for table materialization, host pages, device state and transfer latency | EOS history reset, request isolation and offload peaks NOT RUN |
| MTP and external draft | S Qwen4-Exp MTP and speculative state paths; retained Qwen3.8 DFlash2 already works in experiments | Baseline has no speculation. Native MTP requires matching checkpoint tensors; external draft requires checkpoint-specific provenance | QSA/PLE cropping is unsupported in T; S rollback cannot be inferred from T or old model. NOT RUN |
| Graphs, workspaces, cancellation | S graph metadata and persistent buffers; I stream/alias contracts | Begin eager; qualify live inputs, buffer lifetime, stream ownership and cancellation before capture | Independent lifecycle checks required; NOT RUN |
| Tokenizer, thinking, tools, structured output | Official Flash-Next assets; existing gateway and parser controls | Dense assets/parsers unresolved. Preserve exact requested model and bounded unavailable response | Existing gateway fixtures reusable; dense normal-client path NOT RUN |

## Exact evidence and boundaries

Reviewed on 2026-10-25:

- **I:** [Intel PR 22](https://github.com/analytics-zoo/vllm-xpu-kernels/pull/22),
  head `265bbe2b85a2caf0f0a0c1835f2f0d4fd4a162e2`, merge
  `f597f304a6e3d4874b733bc8e5ce31806ec35785`;
  [kernel inventory at that head](https://github.com/analytics-zoo/vllm-xpu-kernels/blob/265bbe2b85a2caf0f0a0c1835f2f0d4fd4a162e2/custom-kernels-inventory.md).
  Its dense H5120/four-stream/TP2 kernels are implementation clues. It explicitly
  lacks real Qwen4 QSA checkpoint validation and leaves QSA+MTP disabled. Intel
  B70 results do not qualify Radeon. Random-prefix and HC output differences
  remain unresolved; do not classify them as hardware jitter.
  [Companion integration PR 51](https://github.com/intel-innersource/applications.ai.gpu.llm-scaler-vllm/pull/51)
  still returned HTTP 404. Its quoted commit
  `5c37e9b46f07e38190c2649f0b992b45f74e1bfa` was not independently inspected.
- **T:** [Transformers Qwen4-Exp documentation](https://github.com/huggingface/transformers/blob/05a2d95bf36096f79a85af682ac387beb68f447b/docs/source/en/model_doc/qwen4_exp.md),
  config and modeling files at `05a2d95bf36096f79a85af682ac387beb68f447b`.
  This is a source revision, not a dependency installed here. Its config
  validates positive MoE expert counts; do not synthesize a dense config from
  its defaults. QSA fields must form a complete set. PLE layer IDs are one-based.
- **S:** [SGLang Qwen4-Exp](https://github.com/sgl-project/sglang/blob/39b54ae0eeca90f7097b22fe016e9212523a5968/python/sglang/srt/models/qwen4_exp.py),
  MTP, QSA modules, config, FP8 quantization and Mamba state sizing at
  `39b54ae0eeca90f7097b22fe016e9212523a5968`. The profile hashes these inspected
  files. This is an isolated source-review pin, not a runtime upgrade.
- [Official Qwen3.8-Flash-Next card and configuration](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/tree/de4b8e4d43b917e7706784d8bb445c9af86a3540),
  revision `de4b8e4d43b917e7706784d8bb445c9af86a3540`: 125B MoE/6B active,
  plus 51B n-gram embeddings and 4B MTP, hidden width 2560. Assets include a
  licence, tokenizer and chat template. Those assets and its Qwen community
  licence do not establish the dense model's assets or licence.
  The [official organization search](https://huggingface.co/api/models?author=Qwen&search=Qwen4&limit=100)
  returned `[]`. No dense release identifier is assigned in this repository.

The SGLang QSA index projection is replicated; the Transformers TP plan is a
different implementation. Preflight therefore does not require indexer heads
to divide by TP. It checks the SGLang block top-k widths (512 or 2048), ratio
at least two, and an explicitly specified attention rotary dimension that fits
the indexer head. It refuses missing geometry instead of selecting defaults
from another engine/version. GDN projection heads and attention Q/KV heads have
their own partition constraints; KV replication and the replicated GR low-rank
output must not be treated as ordinary sharded tensors.

SGLang has Torch/Triton paths and HIP conditionals, plus TileLang, CUDA and
FlashInfer alternatives. `Tensor.is_cuda` is also true on ROCm; it is not proof
of CUDA-only execution or of AMD compatibility. QSA sparse-attention tuning
tables name H20/L20, so their presence does not qualify gfx1201. Capture the
executed kernel trace, source/DSO identity, dtype/scales and per-rank outputs.
Keep AITER switches, precision and communication settings under the existing
reviewed image contract until a new image is qualified.

### Installation pins versus tuning evidence

`versions.lock` remains unchanged:

| Contract | Exact identity | Scope |
| --- | --- | --- |
| Retained dual AMD image | `docker.io/rocm/sgl-dev:v0.5.15.post1-ubuntu24.04-py3.14-rocm10.0.0@sha256:51f63a2d201ca944a43b8f1a18ac7da73ed9f6743c45bb82bbc40b44e6cf18d2` | Installation/rollback contract; not current upstream SGLang |
| Selected single BF16 image | `localhost/workstation/sglang@sha256:c9e4d0e0ea8818de29ccef4249119fc1c096d7a924b86c63567a6b9f9d5c3c8b` | Local ROCm 10.1 selection, Qwen3.5-9B/TP1 only |
| Dual model | `Qwen/Qwen3.8-27B-FP8` at `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` | Existing 32768 context / two-request profile |
| Single model | `Qwen/Qwen3.5-9B` at `c202236235762e1c871ad0ccb60c8ee5ba337b9a` | Existing 4096 context / two-request profile |
| Dense Qwen4 | Repository, revision, architecture, licence, tokenizer and image **unresolved** | Disabled readiness record, no deployable overlay |

The audit starts at
`scratchpad/fp8-final-betterbench-native.49dd17d29f69f55f/collected/workload/fp8-kv-benchmark.49dd17d29f69f55f/`.
Its `review/DRAFT-MODEL.json` pins `incoai/Qwen3.8-27B-DFlash2` at
`015e795645c74b1a0eeef3b570031fb62e769bc5`, block size eight and BF16 draft
weights. Retained server arguments show `enable_fp32_lm_head=True` and
`mamba_ssm_dtype=float32`; target/draft KV remain BF16. The arm names `bf16`
and `kv4` are historical labels, not permission to infer their effective
precision. Preserve the retained numerical reference and its source overrides.

The experimental `review/SERVING-STACK.json` pins Torch
`2.14.0+git2e66fe81.rocm10.2.0`, source
`2e66fe8153890424e76464545bc5de6391bf9d8c`, HIP `7.17.26416`, and SDK
`10.2.0.dev20261020+git535d2990`. Its file inventories/receipts identify the
composed SGLang runtime; an upstream package version alone cannot identify
those overrides. It is separate from both installation contracts.
`COMMON-STACK-RECEIPT.json` identifies the underlying SGLang 0.5.21/Rust and
sglang-kernel 0.4.7 components, while its older Torch 2.13/ROCm 10.1 base fields
are superseded for this run by the composed serving stack. Do not relabel the
entire benchmark as the base image.

The 98.6 tok/s point is retained in
`results/kv4-http-client-tree/betterbench/results.json.gz` at
`single_stream.prose[0].decode_tps`: **98.62350752446089**, 84 prompt tokens,
4096 completion tokens, TTFT 88.6437 ms, finish reason `length`. The other two
prose runs are 56.7277 and 68.4670 tok/s; the three-run median is 68.4670.
BetterBench 0.6.1 used thinking enabled/effort low, temperature 0.7 and context
32768. Both arm summaries report cache state unknown for all 53 requests;
unique cache salts are not server-confirmed uncached execution. The controller
reports `serving_quality_qualified=false`. This is retained physical benchmark
evidence, not a qualified coding score or a Qwen4 result.

The three retained final BetterBench bundles and newer closeout records were
checked. The only later closeout found was host cleanup
`fp8-host-cleanup-native.4463f3a0961a2a6d`; no later final benchmark was found.
This bounded audit does not claim that every scratch experiment was qualified.
Compare future different models as model-and-runtime configurations, not as
isolated engine speedups.

### Hardware evidence

This task ran on Darwin arm64. A strict-host-key, batch-mode read-only SSH
attempt to the previously identified `tst` workstation at `192.168.2.15`
failed authentication. No model, session transition or service mutation ran.
Current hardware collection and Qwen4 execution are **NOT RUN**.

The retained 2026-10-23 read-only inventory is
`test-results/workstation-audit-20261023.ACXiKY/{REPORT.md,host-state.log}`:
Threadripper 9960X, 24 cores/48 threads, two R9700 devices, and Linux
`MemTotal=65148308 KiB` (about 62.130 GiB). The 2026-10-25 benchmark admission
still identifies two gfx1201 devices. These are historical observations, not
current free-capacity evidence. The workstation has 64 GB installed RAM.
Recollect DIMM population, active
channels, bandwidth and available capacity before qualification.

## Offline preflight and memory

Run from the repository root with its prepared Python environment:

```sh
mkdir -p artifacts
./bin/workstationctl rocm model-preflight artifacts/qwen4-readiness.json
```

Expected: exit **2**, a valid JSON report with `status=blocked`, specific
capability/asset/resource blockers, and `automatic_apply=false`. The command
creates no deployment or launch arguments and does not overwrite an existing
report. Optional `--model-dir` inspects staged metadata and safetensors headers;
it cannot bind supplied files to an unreleased official model. Optional
`--resource-plan` consumes the existing observed-memory planner's JSON.

Preflight checks explicit architecture/features, TP shapes, known embedding
and head shapes, FP8 block inverse-scale shapes and formats, asset presence,
shard/index consistency, byte extents, duplicate keys, nonfinite JSON and
symlinks. Header hashes are **not weight-body authentication**. Verify every
weight against its official immutable snapshot before a later runtime review.
No pickle, remote model code, alternate quantization or cloud fallback runs.

Memory output separates serialized main/other unquantized tensors, scales,
PLE tables and MTP tensors. MTP storage is counted even while speculation is
disabled; the base loader may skip it, so half of checkpoint storage is only
arithmetic, not a per-rank residency bound. Actual rank allocation stays null.
For valid explicit geometry, the report calculates a BF16 full-context KV
envelope and two-active-request FP32 recurrent/BF16 GDN convolution estimates.
Convolution history uses the reviewed SGLang `kernel_size - 1` allocation.
Prefix/speculative slots, QSA index caches, PLE state, graph pools, temporary
workspaces and loading peaks remain separate unknowns. It never declares fit.

For scale only, 27 billion one-byte weights occupy 25.15 GiB and two-byte
weights 50.29 GiB before all other tensors and runtime costs. These are
**estimates, not released-model sizes or measurements**. GPUs have separate
32 GB memory pools. Sparse attention selection is not a proportional KV
storage reduction. Host offload needs a table-loading peak, page/pinning
budget, transfer bytes per token, bandwidth and measured tail latency; more
host RAM does not add GPU VRAM. Keep context/concurrency limits unchanged.

No runtime dependency is added. New source/profile files are in the bootstrap
source and Bridge runtime inventories; this document is in the reference
inventory. Offline staging must include the reviewed dense asset closure,
licence, source hashes, a separate OCI image by digest and its complete
Torch/ROCm/Triton/Transformers/native-library ABI closure. The research source
pins alone are not an installable wheel/image closure. Do not install a moving
upstream branch into the selected environment.

## Target acceptance and rollback — NOT RUN

Prerequisites: an official dense snapshot, reviewed separate image/loader,
successful capability and resource review, and a current authorized `dev`,
`tst` or `int` target mapping. The present profile intentionally cannot meet
these conditions. There is no valid dense Qwen4 launch command yet; filling
nulls in the profile cannot bypass this refusal.

These existing CLI commands collect evidence once an owner restores access.
Use a fresh output directory per attempt and a reviewed local config. The
named Pod must be the exact isolated test Pod, not a guessed production Pod:

```sh
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/qwen4-hardware
./bin/workstationctl --config config/workstation.conf resources plan \
  artifacts/qwen4-hardware/hardware.json artifacts/qwen4-resources
./bin/workstationctl rocm model-preflight artifacts/qwen4-staged.json \
  --model-dir /absolute/path/to/verified-dense-snapshot \
  --resource-plan artifacts/qwen4-resources/resource-plan.json
```

Expected now: collected hardware must identify this boot and both gfx1201
devices; preflight still exits 2 for the unresolved release/runtime contract.
Stop at that refusal. Do not relabel a deployment as qualified to make session
control accept it. After a later reviewed runtime integration, use the existing
session/Bridge canonical lock, drain API admission, stop managed GPU workloads,
and verify Pod exit and DRM-holder release before testing. Do not invoke the OS
installer, firmware commands, production services, or direct unlock shortcuts.

For an authorized running candidate, existing evidence commands are:

```sh
./bin/workstationctl --config config/workstation.conf rocm serving-startup \
  sglang-EXACT-POD cold artifacts/qwen4-cold --memory
./bin/workstationctl --config config/workstation.conf rocm serving-evidence \
  sglang-EXACT-POD artifacts/qwen4-serving
./bin/workstationctl --config config/workstation.conf rocm kernel-evidence \
  sglang-EXACT-POD artifacts/qwen4-kernels
./bin/workstationctl --config config/workstation.conf rocm benchmark-serving \
  /absolute/path/to/reviewed-workload.json artifacts/qwen4-serving artifacts/qwen4-run
```

`serving-startup` must begin before the exact fresh Pod becomes Ready. Use the
existing [workload schema and memory workflow](PERFORMANCE-VALIDATION.md), with
actual tokenized prompt/output lengths, repeated cold/warm starts, concurrency
one then two, and short context before the retained ceiling. These generic
commands do not qualify a model or authorize its launch. Keep every failure.

| Acceptance sequence | Required result before proceeding |
| --- | --- |
| Eager, no speculation | Correct outputs against independent reference; observed FP8 GEMM/scales, FP32 protected math and per-rank loading/steady peaks |
| GR/norm/gate | Small FP32 reference, stream mixing and injection checks, explicit BF16/FP16 cast boundaries, extreme values; preserve reproducible differences |
| QSA, only if configured | Reference block scores/selection, causal mask, incomplete blocks, duplicate/tied indices, cache appends, cached-versus-uncached logits |
| PLE, only if configured | N-gram history, EOS reset, dilated convolution across chunk boundaries, interleaved request isolation, cancelled-slot reuse, separate offload experiment |
| Native MTP, then matching external draft | Rejected-token rollback of GDN, convolution, QSA index/compression and PLE history; stop/EOS acceptance boundaries; no Qwen3.8 DFlash2 reuse |
| Graph capture | Change live tokens, positions, KV and weights across replays; stable owned output/workspace buffers; cross-stream dependencies, cancellation cleanup and slot reuse |
| Prefix reuse | Compare against uncached execution; obtain server-confirmed hits/misses and state restoration, not salts or inferred cache status |
| Gateway/client | Exact model discovery; streamed content/reasoning and final usage; thinking modes; constrained JSON/tool call and tool-result turn; cancellation; unavailable model returns bounded error without substitution |
| Repeated workload/task trials | Actual prompts/outputs/context/concurrency, TTFT, decode tok/s, stream-update gaps, memory, coding/tool task success and preserved failures; no unsupported tail claim from tiny samples |

Transformers expressly disallows cache cropping with QSA or PLE. Its KV offload
also leaves GDN, QSA-indexer and PLE states on device. SGLang's separate
speculative state machinery needs independent acceptance; old Qwen3.8 rollback
assumptions do not apply automatically. Native MTP clears draft PLE layer IDs
and uses widened HC input state in the reviewed source, which makes an
ordinary hidden-width or shared-cache assumption unsafe.

Rollback: close candidate admission, drain/cancel through the existing session
controller, stop only the owned experimental workload, verify child/Pod exit
and GPU release, then restore the previously qualified image/model/template
together under the same lock. Keep evidence, model snapshots and caches
separate. If cleanup or identity checks fail, leave admission unavailable and
retain the lock/state for owner recovery; do not automatically resume service.

## Remaining inputs and recommendation

Gather together: official dense repository/revision/config/licence/tokenizer;
complete verified weights and optional MTP/drafter provenance; separate pinned
ROCm runtime/ABI closure and executed kernel evidence; authorized target access
and same-boot hardware/resource observations; per-rank loading/workspace/state
budgets; numerical, lifecycle and normal gateway acceptance.

Ready for offline metadata/evidence testing: **yes**. Ready for dense Qwen4
hardware evaluation: **blocked by those inputs**. Ready to adopt: **no**.
Source review and synthetic tests are not hardware qualification or deployment.
