# AI serving stack pipeline

Builds the gfx1201 SGLang serving image from upstream `main` of every
component and stages models on the NAS, so upgrades and model swaps are a
repeatable routine rather than a hand-assembled runtime.

| Input | Pinned by |
| --- | --- |
| ROCm (TheRock), PyTorch, TorchVision, SGLang | Full commit in [`stack.lock`](stack.lock), refreshed from `main` |
| Triton | The commit the pinned PyTorch names in `.ci/docker/ci_commit_pins/triton.txt` |
| Build container | TheRock CI's image digest at the pinned TheRock commit |
| Runtime base | `python:3.14-slim-trixie`, resolved to a digest |
| SGLang patch series | Content hash of the serving repository's `serving/patches/sglang-main` |
| Models | Repository revision plus every file's published hash, in [`models/`](models) |

## Stages

`build-all.sh` runs the stages in order on the workstation, each in the
pinned build container under rootless Podman. Stage outputs live under
`~/ai-stack/builds/<stage>-<key>/out/`, where the key hashes every pin that
affects the stage. A stage whose key already has a `RECEIPT.json` is reused, so
a refresh that only moves SGLang rebuilds only the SGLang stage and the image.

| Stage | Produces |
| --- | --- |
| `rocm` | ROCm SDK Python packages for gfx1201 from TheRock |
| `torch` | PyTorch, Triton and TorchVision wheels, built with TheRock's helpers |
| `sglang` | The patched SGLang and gfx1201 AOT kernel wheels, plus the complete resolved wheelhouse |
| `image` | `registry.lan.thespry.family/local/sglang-gfx1201:<key>`, installed offline from the wheelhouse |

Each stage also records its progress for monitoring: `stage-metrics.py` writes
`/var/lib/workstation-ai-stack/ai-stack.prom`, which node-exporter reads. The
Workloads dashboard shows each stage as building, succeeded or failed, with its
duration and compiler-cache hit ratio. The alert rule for a failed or stuck
stage lives in the NAS repository,
[HP-ProliantGen10-DebianNAS](https://github.com/Drgr33nSenior/HP-ProliantGen10-DebianNAS),
not here. node-exporter reads the directory through
`--collector.textfile.directory` in
[`host-metrics.yaml`](../observability/host-metrics.yaml); the series are named
`workstation_ai_stack_*`. Create the directory once, owned by the build
user (the postinstall `telemetry-apply` gate prints the same command):

```sh
sudo install -d -m 0755 -o "$USER" -g "$USER" /var/lib/workstation-ai-stack
```

Without it, builds still run and only print a warning.

Each receipt records its inputs and the SHA-256 of every output. The sglang
receipt's wheelhouse list is the resolved dependency lock for the image.

## Routine update

From a checkout of this repository, with the serving repository beside it:

```sh
infrastructure/ai-stack/refresh.sh ../Spry.ai-rdna4-serving   # rewrite pins, print the diff
bash tests/test_ai_stack.sh
infrastructure/ai-stack/sync.sh zarren@gr33nstein ../Spry.ai-rdna4-serving
```

Then on the workstation:

```sh
systemd-run --user --unit=ai-stack-build --collect \
  bash ~/ai-stack/pipeline/current/build-all.sh
journalctl --user -fu ai-stack-build
```

`sync.sh` copies the pipeline to a content-addressed directory and points
`current` at it, so a build that is already running keeps its own scripts.
Commit the refreshed `stack.lock` with the receipts' outcome.

If a patch in the series stops applying, the `sglang` stage stops at
`git apply --check`. Port the patch in the serving repository, or delete it if
upstream absorbed the change, then refresh again to pick up the new series hash.

The `image` stage is keyed by the sglang stage plus `stack.lock`,
`image/Containerfile` and `image/smoke.py`, so a change to any of them builds,
smoke-tests and (with push) publishes a new tag.

Set `AI_STACK_PUSH=1` for the `image` stage to push it to the lab registry.
This needs a one-time `podman login registry.lan.thespry.family` as the
publisher account.

### Scanning and signing

With `AI_STACK_PUSH=1` the `image` stage, after its GPU smoke test:

1. Scans the image with Trivy (`TRIVY_IMAGE` in `stack.lock`) and writes, under
   the image stage's `builds/image-<key>/out/scan/`, the full report (`vulns.json`), a CycloneDX
   SBOM, a cosign vulnerability predicate and the scanner/database versions.
2. Applies [`image/scan-policy.py`](image/scan-policy.py): the push stops for a
   CRITICAL finding with a fixed version available unless
   [`image/scan-allowlist.json`](image/scan-allowlist.json) covers it. Every
   allowlist entry needs `id` (and optionally `package`), `reason`, `reviewed`
   and `expires`; expired entries stop applying. Other findings are recorded,
   not blocking.
3. Pushes the image, then signs it and attests the SBOM and the vulnerability
   report with cosign (`COSIGN_IMAGE`), and verifies all three against
   [`signing/cosign.pub`](signing/cosign.pub). The stage writes `image.digest`
   only after verification, so a failed step reruns next time.

[`signing/signing-config.json`](signing/signing-config.json) lists no
transparency log or timestamp service, so nothing is uploaded to Rekor or any
other public service; signatures and attestations are stored beside the image
in the lab registry. Verification therefore uses `--insecure-ignore-tlog`.

One-time setup on the build host, in an interactive terminal:

```sh
infrastructure/ai-stack/signing/init-key.sh   # prompts for a new passphrase
```

It writes the passphrase-encrypted private key to
`~/.config/ai-stack/signing/cosign.key` and seals the passphrase for this user
with `systemd-creds --user`, so unattended builds can sign. Keep a copy of the
passphrase in your password manager: the sealed copy is bound to this host and
user. Commit the printed public key as `signing/cosign.pub`. Pushing stops with
a message until all three exist.

Before deploying an image, verify it:

```sh
infrastructure/ai-stack/signing/verify-image.sh registry.lan.thespry.family/local/sglang-gfx1201@sha256:<digest>
```

A cluster admission policy that rejects unsigned `local/` images is a planned
follow-up; until then this check is the enforcement point.

## Models

Each catalog entry pins a model and an optional draft: repository, revision and
every file's hash. It holds no serving settings; those go in a profile under
[`serving/`](serving). To add one, for example a Qwen4 checkpoint:

```sh
infrastructure/ai-stack/models/inventory.py OWNER/NAME FULL_COMMIT > /tmp/inventory.json
```

Copy the inventory into a new `models/<alias>.json` beside the existing entry,
add a `serving/<profile>.json` for it, and run the tests. To stage it on the
NAS:

```sh
kubectl apply -f infrastructure/ai-stack/k8s/models.yaml      # once
infrastructure/ai-stack/models/render-fetch.py \
  infrastructure/ai-stack/models/<alias>.json infrastructure/ai-stack/stack.lock | kubectl apply -f -
```

**Namespace conflict.** `k8s/models.yaml` also defines the `ai-home-lab`
Namespace with `enforce: restricted`. The `apps/` layer defines the same
Namespace in [`apps/base/namespace.yaml`](../../apps/base/namespace.yaml) with
`enforce: baseline` (audit and warn `restricted`). Whichever is applied last
sets enforcement, without warning: applying the `apps/` layer after this file
relaxes the namespace to baseline, and applying this file after the `apps/`
layer makes the namespace reject new Pods that meet only baseline. The live
namespace enforces restricted from this file; the `apps/` layer has not been
applied since the reinstall. The owner must choose one source for the
Namespace and one enforcement level before both layers are applied. Until
then, apply only one of them.

The `ai-models` claim uses the `nas-nfs` StorageClass from
[`infrastructure/nfs-csi/`](../nfs-csi); install it first
([NAS-backed volumes](../../docs/HOME-LAB.md#nas-backed-volumes-nfs-csi)).

The Job downloads into the `ai-models` volume at
`<owner>/<name>/<revision>/`, checks each file's size and hash, and writes
`VERIFIED.json` last. Earlier revisions stay in place for rollback.

## Serving

A serving profile in [`serving/`](serving) is the single record of the
server's flags and environment for one model on this hardware. Each setting
carries its value, reason, source (usually a file in SGLang at the pinned
commit) and confidence, and says whether a GPU measurement still has to settle
it. Settings deliberately left out, and variables that must stay unset, are
recorded too. The research behind the current profile is in
[`serving/RESEARCH-20261108.md`](serving/RESEARCH-20261108.md); the op234
runtime it replaces is guidance there, not a target. The model catalog keeps
only model and draft identity.

A profile has arms for the A/B decisions that need measurement; one is the
default. For `qwen3.8-27b-mxfp4-tp2`, arm U is the default: arm A's flags
(`no_buffer`, overlap off, as op234) with the six qualified gfx1201 ports
switched on, SGLang's custom all-reduce in place of RCCL for verify and
decode reductions, the P1 FP8 route for prefill GEMMs, 8-warp prefill
attention, the fused split-K reduce, the fused MXFP4 quantization of the
norm and SiLU*mul outputs, the one-request verify-attention tile and the kq3
verify GEMM. Arm A stays as the baseline that every other arm is compared
with; arms K, M, N, O, Q, R, S and T each lack one later addition (the custom
all-reduce, P1, 8-warp prefill attention, the fused reduce, the fused norm
read, the fused SiLU*mul, the attention tile, kq3). Arm B
(`extra_buffer_lazy` with the overlap scheduler) is an experiment.

An arm may list base flags under `drop` (arm `M` drops
`--disable-custom-all-reduce`); `render.py` stops if a dropped flag is not in
the profile.

`serving/render.py` renders a profile:

```sh
# The server command line for the default arm (add --arm B for the experiment).
infrastructure/ai-stack/serving/render.py argv infrastructure/ai-stack/serving/<profile>.json
# A qualification Job (and its probe ConfigMap) for an image pinned by digest.
infrastructure/ai-stack/serving/render.py job infrastructure/ai-stack/serving/<profile>.json \
  registry.lan.thespry.family/local/sglang-gfx1201@sha256:<digest> > qualify-a.json
infrastructure/ai-stack/signing/verify-image.sh registry.lan.thespry.family/local/sglang-gfx1201@sha256:<digest>
kubectl apply -f qualify-a.json
kubectl -n ai-home-lab logs -f job/<name>
```

Before applying the Job:

- Free both GPUs. Only one Job or server can hold them, so stop any gaming
  session and any other SGLang server or GPU Job first.
- Stage the model with `render-fetch.py` and wait for its `VERIFIED.json`.
- The `apps/` layer is not required; the Job needs only the namespace, the
  `ai-models` claim and the image.

Each rendered Job also includes a Service named `sglang`, the address the
telemetry stack scrapes as the `sglang` job, so the Workloads dashboard's
serving panels follow whichever qualification or bench server is running.
Do not run a Job while the serving Deployment is up: only one can hold the
GPUs, and both use that Service name.

To clean up a run, including its probe ConfigMap:

```sh
kubectl -n ai-home-lab delete job,configmap -l app.kubernetes.io/name=sglang-qualify
```

The Job runs the server and `serving/qualify.py` in one pod on both GPUs, with
the verified model revision from the `ai-models` volume, a read-only root and
restricted pod security. The probe checks the served name, reasoning output, a
streamed decode, tool-call parsing, a long-context prefill and prefix-cache
reuse, prints one `QUALIFY_RESULT` line and sets the Job's result. The startup
log records the memory pools. Only one Job (or server) can hold both GPUs at a
time. Record each measured result in the profile before changing its status.

The probe's timings are single measurements. For throughput and DFlash
acceptance, use the bench Job. It runs the same pod with `serving/bench.py`,
which replays the BetterBench v0.6.1 profile the op233/op234 selection was
measured with. The client, corpus and profile come unchanged from the serving
repository's `benchmarks/betterbench/native-v2`. The profile is 53 requests:

- eight categories × (one warm-up + three measured), capped at 4096 tokens;
- prefill at 2K, 8K and 16K tokens;
- six requests each at concurrency 1 and 2;
- sampling at temperature 0.7, top-p 0.95, top-k 20 and seed 1729, with
  thinking at low effort and a unique cache salt per request.

`--greedy` runs the same profile at temperature 0, which separates sampling
effects from draft or target defects. `--top-p P` changes only the client's
top-p. Arm `C` is arm A plus `--speculative-use-block-verification`.

Arms `D` to `H` measure the opt-in verify-path ports (serving patches
`0006` to `0009`) against arm A. An arm may set its own environment
variables, which the Job adds to the profile's.

| Arm | Adds to arm A |
| --- | --- |
| `D` | `SGLANG_GFX1201_SPLITK_DOWNOUT=1` (S4 split-K down/out) |
| `E` | `SGLANG_GFX1201_PACKED_GDN_IN_PROJ=1` (packed GDN input projection) |
| `F` | `SGLANG_GFX1201_FP8_LM_HEAD=1` (FP8 target and draft heads; lossy) |
| `G` | `--enable-linear-replayssm-spec` (GDN ReplaySSM for DFLASH) |
| `I` | `SGLANG_GFX1201_GDN_NORM_QUANT=1` (fused GDN gated-norm MXFP4 quantization) |
| `H` | `D`, `E`, `F` and `I` together (not `G`) |
| `J` | `H` plus `SGLANG_GFX1201_VERIFY_ATTENTION=1` (split-KV verify attention) |
| `K` | `J` plus `SGLANG_GFX1201_SPLITK_WIDE=1` (split-K for QKV, GDN input and gate/up); the RCCL comparison for `M` |
| `L` | `K` with the draft quantized to FP8 at load (`--speculative-draft-model-quantization fp8`); not adopted |
| `M` | `K` with SGLang's custom all-reduce for verify/decode reductions (`SGLANG_GFX1201_PCIE_CUSTOM_AR=1`, `--disable-custom-all-reduce` dropped; serving patch 0014); the comparison for `N` |
| `N` | `M` plus `SGLANG_GFX1201_P1_PREFILL=1` (prefill chunks of 1024-2048 rows on the P1 FP8 GEMM route; serving patch 0015); the comparison for `O` |
| `O` | `N` plus `SGLANG_GFX1201_EXTEND_WARPS8=1` (8-warp prefill attention for head dim 256, C4; serving patch 0016); the comparison for `Q` |
| `P` | `O` with the draft quantized to FP8 at load (as `L`) and `SGLANG_DFLASH_FUSED_KV_FP8=1`, which keeps DFLASH's fused KV materialization for it (serving patch 0017) |
| `Q` | `O` plus `SGLANG_GFX1201_SPLITK_FUSED_REDUCE=1` (split-K verify GEMMs as one kernel with the reduce fused, bitwise equal; serving patch 0018); the comparison for `R` and `S` |
| `R` | `Q` plus `SGLANG_GFX1201_ADD_NORM_MXFP4=1` (the boundary norm read also quantizes to MXFP4 on verify batches, bitwise equal; serving patch 0019); the comparison for `S` |
| `S` | `R` plus `SGLANG_GFX1201_SILU_MUL_MXFP4=1` (SiLU*mul and the down projection's MXFP4 quantization in one kernel on verify batches, bitwise equal; serving patch 0020); the comparison for `T` |
| `T` | `S` plus `SGLANG_GFX1201_VERIFY_ATTENTION_B1_TILE=1` (one-request verify batches run the verify-attention kernel with a wider tile, BT 64 / 8 warps / QC 64; serving patch 0021; not bitwise, so it needs the quality phase); the comparison for `U` |
| `U` | `T` plus `SGLANG_GFX1201_MXFP4_KQ3=1` (verify GEMMs of at most 16 rows run the kq3 kernel over pre-packed weight records, bitwise equal; the records replace the weight and prefill rebuilds it into a scratch, about 26 ms per forward; serving patch 0022), **the default** |

Enable an item in arm A only after its arm matches arm A's quality result and
greedy acceptance.

Results on 2026-11-09 (image `sha256:044646c4`, median verify step from the
server's one-request decode windows; quality relative to arm A):

| Arm | Verify step | vs A | C1 / C2 tokens/s | Quality |
| --- | --- | --- | --- | --- |
| `A` | 41.65 ms | | 92.9 / 163.6 | 9 passed, 3 known format failures |
| `D` | 40.68 ms | -1.0 ms | 93.7 / 157.6 | same |
| `E` | 41.21 ms | -0.4 ms | 93.1 / 179.1 | same |
| `F` | 40.04 ms | -1.6 ms | 104.8 / 184.2 | same |
| `G` | 43.58 ms | +1.9 ms | 89.9 / 170.7 | one case ends with no answer |

Results on 2026-11-09 (image `sha256:0c30a932`, series `a97349f2`; long-context
step is the median gap between streamed updates):

| Arm | Verify step | C1 / C2 tokens/s | Step at 23.5k / 47k / 88k prompt | Quality |
| --- | --- | --- | --- | --- |
| `A` | 42.07 ms | 94.5 / 156.7 | 109 / 177 / 295 ms | 9 passed, 3 known format failures |
| `I` | 42.18 ms | 96.0 / 175.6 | 110 / 177 / 296 ms | same |
| `H` | 38.18 ms | 109.4 / 192.9 | 105.5 / 173.8 / 292 ms | same |

Arm `I` showed no gain and 0010 logged nothing, so 0011 adds route markers.
Verify cost grows about 2.9 ms per 1k tokens of context because target verify
ran `extend_attention_fwd`; arm `J` adds the split-KV verify kernel (0012).
The long-context phase requests `min_tokens`, but the 88k requests of `A` and
`I` stopped after 560 and 168 tokens, so its output length is not guaranteed.

Results on 2026-11-09 (image `sha256:2919f947`, series `3f507acc`, run back to
back):

| Arm | Verify step | C1 / C2 tokens/s | Step at 23.5k / 47k / 88k prompt | Quality |
| --- | --- | --- | --- | --- |
| `A` | 41.88 ms | 98.5 / 172.1 | 109 / 178 / 297 ms | 9 passed, 3 known format failures |
| `H` | 38.11 ms | 103.4 / 190.0 | 105 / 174 / 293 ms | same |
| `J` | 35.31 ms | 108.8 / 190.2 | 37.6 / 39.3 / 42.4 ms | 10 passed, 1 failed; nothing that passed in `A` failed |

Arm `J`'s split-KV verify attention makes long-context decode 3-7 times
faster (73 tokens/s at an 88k prompt, against 10).

Results on 2026-11-09 (image `sha256:0d2cc0e7`, series `97629063`, run back to
back; median and 25th percentile verify step):

| Arm | Verify step | Acceptance | C1 / C2 tokens/s | Step at 23.5k / 47k / 88k prompt | Quality |
| --- | --- | --- | --- | --- | --- |
| `J` | 35.31 / 34.98 ms | 4.45 | 118.8 / 202.9 | 37.5 / 39.3 / 42.3 ms | 10 passed, 1 failed |
| `K` | 34.61 / 34.30 ms | 4.50 | 113.8 / 207.5 | 37.0 / 38.6 / 41.6 ms | same |
| `L` | 34.52 / 34.26 ms | 4.50 | 106.0 / 201.4 | 36.9 / 38.5 / 41.5 ms | 10 passed, 2 failed (`python-bugfix`, a known format failure) |

Nothing that passed in `J` failed in `K` or `L`. Arm `K` became the default.
Arm `L`'s FP8 draft saved 0.1 ms: quantizing the draft disables DFLASH's fused
KV materialization. C1 varies with acceptance and output length between runs
(106-119 tokens/s at almost the same step time); compare the verify step.

Results on 2026-11-09 (image `sha256:a5ef9601`, series `3bed3db3`, run back to
back; median and 25th percentile verify step):

| Arm | Verify step | C1 / C2 tokens/s | Step at 23.5k / 47k / 88k prompt | Quality |
| --- | --- | --- | --- | --- |
| `K` | 34.55 / 34.31 ms | 112.9 / 207.6 | 37.0 / 38.5 / 41.6 ms | 10 passed, 1 failed (`k8s-troubleshoot`) |
| `M` | 33.96 / 33.73 ms | 118.7 / 179.6 | 36.2 / 37.9 / 40.9 ms | same |

Arm `M` became the default: 0.6 ms faster per verify step, the same quality
result and no GPU page faults. Its C2 figure comes from one request with a
2.7 s time to first token, which coincided with a Triton compile of the
extend-attention kernel after the server was ready; the two-request verify
step was 36.48 ms against arm K's 36.77 ms. Prefill reductions (over
512 KiB) stay on RCCL, so prefill throughput is unchanged.

Results on 2026-11-09 (image `sha256:76ad07b8`, series `af2dfb72`, run back to
back; prefill is BetterBench's median prompt-processing rate, first token is
the long-context phase's time to first token):

| Arm | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Verify step | C1 / C2 tokens/s | Quality |
| --- | --- | --- | --- | --- | --- |
| `M` | 1,626 / 1,694 / 1,637 tokens/s | 15.4 / 34.6 / 77.0 s | 34.02 / 33.67 ms | 116.6 / 207.9 | 10 passed, 1 failed (`k8s-troubleshoot`) |
| `N` | 3,778 / 3,734 / 3,454 tokens/s | 7.8 / 19.6 / 49.6 s | 34.12 / 33.80 ms | 118.2 / 210.6 | same |

Arm `N` became the default: P1 more than doubles prefill and leaves the verify
step and quality unchanged. Both cards logged `P1_PREFILL_READY` (every
admitted shard validated) and a route for all five shapes; the KV pool stayed
at 262,144 tokens. The gain falls with prompt length because attention, which
P1 does not change, grows with the prefix; arm `O` (0016) targets it.

Results on 2026-11-09 (image `sha256:8852c5aa`, series `b9250b93`, run back to
back):

| Arm | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Verify step | C1 / C2 tokens/s | Quality |
| --- | --- | --- | --- | --- | --- |
| `N` | 2,664 / 3,746 / 3,457 tokens/s | 7.81 / 19.75 / 49.89 s | 34.11 / 33.78 ms | 112.2 / 201.2 | 10 passed, 1 failed (`k8s-troubleshoot`) |
| `O` | 3,830 / 3,776 / 3,549 tokens/s | 7.96 / 18.08 / 44.70 s | 34.05 / 33.80 ms | 110.2 / 191.2 | same |

Arm `O` became the default: 8-warp prefill attention saves 1.7 s at a 47K
prompt and 5.2 s at 88K, as the component measurement predicts (the saving
grows with the square of the prompt length), with the verify step and quality
unchanged. Arm N's 2K figure is a low outlier (3,778 tokens/s in the previous
run). Arm O's lower C2 figure comes from shorter sampled outputs (7,982
against 8,953 tokens in that phase) at a higher per-request decode rate.

Results on 2026-11-09 (image `sha256:fb839951`, series `b0323183`, run back to
back; median and 25th percentile verify step):

| Arm | Verify step | Two-request step | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Quality |
| --- | --- | --- | --- | --- | --- |
| `O` | 34.00 / 33.74 ms | 36.41 ms | 3,809 / 3,771 / 3,550 tokens/s | 7.50 / 18.07 / 44.67 s | 10 passed, 1 failed (`k8s-troubleshoot`) |
| `Q` | 33.15 / 32.78 ms | 35.53 ms | 3,825 / 3,771 / 3,545 tokens/s | 7.54 / 18.08 / 44.61 s | same |

Arm `Q` became the default: the fused split-K reduce removes 256 kernel
launches per verify round (about 2.2 us of dispatch each inside a graph) for
0.85 ms per step, with bitwise-equal output. All ten fused routes engaged and
none was disabled. Because the arithmetic is identical, the runs also measure
sampling noise: accepted length 4.59 against 4.41 and C2 204 against 187
tokens/s from the same computation.

Results on 2026-11-10 (image `sha256:8d68cdc5`, series `299e2f58`, run back to
back; median and 25th percentile verify step):

| Arm | Verify step | Two-request step | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Quality |
| --- | --- | --- | --- | --- | --- |
| `Q` | 32.97 / 32.67 ms | 35.41 ms | 2,684 / 3,796 / 3,557 tokens/s | 7.49 / 18.61 / 44.59 s | 10 passed, 1 failed (`k8s-troubleshoot`) |
| `R` | 32.84 / 32.55 ms | 35.46 ms | 3,851 / 3,770 / 3,547 tokens/s | 7.53 / 18.04 / 44.13 s | same |
| `S` | 32.58 / 32.32 ms | 35.28 ms | 3,852 / 3,772 / 3,547 tokens/s | 7.50 / 18.28 / 44.15 s | same |

Arm `S` became the default: both fusions keep the output bitwise equal and
together take 0.39 ms off the verify step. The fused norm read gave 0.13 ms,
about half of its isolated measurement; the fused SiLU*mul gave 0.26 ms. Arm
`Q` reproduced its previous verify step within 0.03 ms; its 2K prefill figure
is the low first-of-sequence outlier seen before.

Results on 2026-11-10 (image `sha256:cf668efe`, series `b91fcdb3`, run back to
back; median and 25th percentile verify step below 8K, then the median at 47K
and 88K):

| Arm | Verify step | At 47K / 88K | Two-request step | C1 / C2 | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Quality |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `T` | 32.37 / 32.12 ms | 35.90 / 38.18 ms | 35.04 ms | 116.7 / 202.0 tokens/s | 3,830 / 3,779 / 3,558 tokens/s | 7.49 / 18.01 / 44.37 s | 9 passed, 2 failed (`incident-analysis`, `k8s-troubleshoot`) |
| `U` | 26.95 / 26.76 ms | 30.16 / 32.44 ms | 29.87 ms | 153.1 / 245.0 tokens/s | 3,572 / 3,600 / 3,386 tokens/s | 7.83 / 18.96 / 45.74 s | same, identical answers |

Arm `U` became the default: kq3 takes 5.4-5.7 ms off every verify step with
the output bitwise equal to arm `T`'s; prefill pays 5-7% for rebuilding the
weight. Arm `T`'s tile gave 0.67 / 1.26 ms at 47K / 88K against arm `S`
(image `sha256:99c96e62`). The two failures are format only: re-sending the
JSON-graded cases and capturing the raw answers showed correct JSON wrapped in
a ```` ```json ```` fence (and `k8s-troubleshoot` naming `CACHE_SHARDS=4` where
the grader expects the variable name).

After BetterBench, the Job runs the assistant acceptance menu
([`assistant-quality.json`](../sglang/assistant-quality.json) through
`lib/workstation/assistant_acceptance.py`, both unchanged) against the native
endpoint. No speed change is read without a quality result. Code tasks need
the bubblewrap sandbox, which the pod lacks, and long-context cases need a
tokenizer budget. The runner reports both as NOT RUN, never as passed.
`QUALITY_RESULT` lists the pass rate, the tool round-trip, grounded and
abstention rates, and any failed task.

The bench Job fails only when the bench does: a failed BetterBench or
long-context request, or a quality runner error. Graded quality failures are
results in `QUALITY_RESULT`, not Job failures; compare them with arm `A`.

```sh
infrastructure/ai-stack/serving/render.py bench infrastructure/ai-stack/serving/<profile>.json \
  registry.lan.thespry.family/local/sglang-gfx1201@sha256:<digest> --serving <serving-repo> > bench-a.json
kubectl apply -f bench-a.json
kubectl -n ai-home-lab logs job/<name> | grep '^BENCH_RESULT'
kubectl -n ai-home-lab delete job,configmap -l app.kubernetes.io/name=sglang-bench
```

After BetterBench and before the quality phase, a long-context phase sends one
request at each of 32K, 64K and 120K prompt tokens: a unique filler document
with exactly 1024 generated tokens (`min_tokens`). It is not part of op233's
profile and measures verify cost at depth.

`BENCH_RESULT` gives these results:

- the median decode rate for each category;
- C1 and C2 aggregate throughput;
- the median prefill rate at each depth;
- for each long-context depth: prompt tokens, time to first token, decode rate
  and tokens per verify;
- for each phase, the server's generated tokens per verify call (its
  `generation_tokens_total` and `spec_verify_calls_total` counters).

op233/op234 measured C1 96.3–104.3 and C2 172.8–194.5 tokens/s at a 235 W cap
per card. Compare only at a matched cap. `BENCH_RAW` holds the full BetterBench
results, compressed.
