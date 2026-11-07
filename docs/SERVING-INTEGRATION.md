# Serving project integration

Reviewed 7 November 2026. The latest published serving source is
[`3f5ba24`](https://github.com/Drgr33nSenior/Spry.ai-rdna4-serving/tree/3f5ba24e292d43e0bc643f2c51cf5940afabc606).
There are no published release tags or GitHub releases. Track the exact commit
and evidence bytes, not a release version inferred from SGLang.

The workstation's machine-readable record is
[serving-project.json](../infrastructure/sglang/serving-project.json). It keeps
each import separately, with its own revision, check date and record hashes,
and ships the records' MIT licence:

| Import | Revision | Status | Records |
| --- | --- | --- | --- |
| `mxfp4-128k-20261101` | `d3f0db8` | Historical | Phase 1 and 128K/C4 candidate records as published then |
| `splitk-selection-20261107` | `3f5ba24` | Current | Split-K S4 selection (operation 234, with operation 232 as rollback) and the finalization record |

`stack_inventory.serving_project` checks both imports and reports the current
selection only from the current import. Later upstream edits to a historical
record do not change the pinned bytes; that record still describes the earlier
candidate. `workstationctl performance stack` includes the summary on Arch.
Inventory is offline: the check date is the last source review, not a live
deployment check. For the assistant integration of the current selection, see
[the assistant guide](ASSISTANT.md#native-candidate-integration).

## Historical import: 1 November 2026

The rest of this section describes the `d3f0db8` import and the C4 candidate
that the owner later replaced.

### What changed upstream at that import

| Area | Current evidence | Limit |
| --- | --- | --- |
| Candidate | Qwen3.8-27B Quark AWQ MXFP4 weights, BF16 DFlash width 8 and BF16 target/draft KV, TP2 | Weight precision and KV precision are separate; this is not KV4 storage |
| Context/capacity | 131,040-token context; 262,144-token pool; at most two running requests; 2048-token prefill chunks | No C3–C8 or single-card full-stack qualification; do not copy its 0.92 memory fraction into another profile |
| Stock-attention endurance | Two starts, 176 natural-stop requests each, 352/352 graded answers; about 61 serving minutes at 235 W/card | This preceded C4; stock endurance does not qualify C4 |
| C4 prefill attention | Cold ~120K prefill 96.5 to 74.0 s, 23.3% less time; 32/32 graded answers and 16/16 equal paired final answers | One stock-then-C4 pair; full token paths equal in 9/16; no C4 endurance |
| Owner disposition | C4 adopted privately with the failed warm/branch latency gate retained as an accepted exception | Original `ADOPTION_CRITERIA_NOT_MET` verdict and threshold remain; extra reasoning confounds the two slower HTTP cases |
| Stability | Later bounded runs had clean faults/release evidence | One earlier sampled MXFP4 GEMM memory fault in 12 starts remains unexplained |

C4 is the **attention candidate name**, not concurrency four. It changes the
eligible gfx1201/head-dimension-256 extend tile from `(64,64,4,1)` to
`(64,64,8,1)`. Other shapes retain their existing route. Stock operation 174 start 1
is the recorded rollback; C4 was operation 174 start 2.

The seven preceding overlays cover fused norm, exact FP4-to-FP8 prefill expansion,
FP8 target/draft heads, GDN copy/layout fusion, the row-wise verify adapter and
packed QKVZ+BA. FP8 heads are an owner-accepted precision change. These results
do not transfer the old Qwen3-Coder GEMM/MoE tables to this model.

Read the pinned [Phase 1 account](https://github.com/Drgr33nSenior/Spry.ai-rdna4-serving/blob/d3f0db85997389ebd8836af17a7048659b930770/docs/MXFP4-CANDIDATE-PHASE1.md)
and [128K/C4 account](https://github.com/Drgr33nSenior/Spry.ai-rdna4-serving/blob/d3f0db85997389ebd8836af17a7048659b930770/docs/MXFP4-CANDIDATE-128K.md).
HTTP throughput, server decode rates, cold prefill and endurance are separate
measurements; the 300-W and 235-W runs also changed profile/instrumentation.
Do not add gains or treat those rates as a controlled power comparison.

## Workstation integration status

| Surface | Current state | Next required integration |
| --- | --- | --- |
| Serving source/results | This checkout tracks `3f5ba24` (current) and `d3f0db8` (historical) with their record hashes | Add a new import when upstream evidence changes; keep older imports |
| Application images | `versions.lock` still selects the retained ROCm 10.0 dual-GPU and 10.1 single-GPU images | A complete reviewable runtime/image artifact, model inventory and package contract are needed before replacing pins |
| Native 10.2 contract | [sglang-rocm102.json](../infrastructure/hyperloom/sglang-rocm102.json) is the existing expected SGLang/Torch/HIP contract | A version match alone does not bind the newer MXFP4 model, operators or complete loaded source |
| Hyperloom owner adapter | Authenticates the older 4/30-request schedules and delegates existing owner controls | W5 has 176 requests/start and a changed evidence ABI; it is not admitted by this adapter |
| Model/service profiles | Public FP8 selection remains unchanged; upstream reports no deployment | Preserve existing model IDs, context limits, resource budgets, user access and rollback until a full packaged profile is qualified |
| Cleanup/config refactor | Newer local `serving/` code is active, uncommitted work | Do not package that changing tree or treat CPU source-review bundles as deployable launch packets |

The serving repository's newer local cleanup work contains fail-open observation,
source assembly and configuration-binding changes. Its own README still marks
Phase 1 and Phase 3 incomplete: production import/mount cleanup, fresh-session
configuration consumers, complete evidence capacity and native closure/equality
remain open. This local observation is separate from the published `d3f0db8`
record; the workstation sync neither changes that tree nor certifies those edits.

The separate `Spry.ai-sglang-runtime` repository has no published release either;
its observed `main` head was `502f41a2ca38d657a13f905d37a95febc2246eb8` from
14 September. It is not proof of the complete current private serving runtime.

## Next integration and rollback

1. Finish the serving owner's cleanup/configuration gates and obtain its complete
   runtime, model, source and image identities. Keep stock/C4 and production/
   instrumented evidence distinct; carry the accepted exception and fault forward.
2. Adapt the existing package/owner and Hyperloom boundary to the actual new
   request/evidence contract, with parsed-config and refusal tests. Do not simply
   enlarge schedule allowlists or import a changing source directory.
3. Prepare an opt-in deployment profile through the existing image, manifest,
   resource, session and recovery interfaces. Recheck current target admission;
   source review does not authorize a workload or change power caps.
4. Run only the remaining profile-specific quality, lifecycle, restart and C4
   endurance checks under the serving owner's bounded protocol before promotion.

This sync adds reporting and reference assets. It does not change images, launch
arguments, tables, caches, model IDs, power policy or workload replicas. Roll back
by restoring the previous reviewed source/package; no workstation runtime
rollback is needed because this task performed no deployment or GPU execution.
