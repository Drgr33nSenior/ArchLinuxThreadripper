# Serving results log, 9–11 November 2026

A dated record, moved unchanged from the [AI serving stack README](../README.md#serving) on
12 November 2026. Each table describes the image and patch series it names. The current
qualified results are the `qualification` records in the
[serving profile](qwen3.8-27b-mxfp4-tp2.json); do not compare these figures with a different
image or power cap.

Arms are defined in the [arm reference](../README.md#arm-reference). The default moved
A → K → M → N → O → Q → S → U → Y over these runs; since 11 November the defaults are `UQS`
(shared mode) and `UQL` (large mode).

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

Results on 2026-11-10 (image `sha256:4b157a4e`, series `bf388330`, U-X run back
to back, then Y three times; the verify step was 26.93-26.96 ms in every arm,
so C1 / C2 differences between single runs are sampling noise):

| Arm | C1 / C2 | Prefill at 2K / 8K / 16K | First token at 23.5K / 47K / 88K | Quality |
| --- | --- | --- | --- | --- |
| `U` | 138.0 / 222.9 tokens/s | 2,544 / 3,593 / 3,383 tokens/s | 7.81 / 18.61 / 45.58 s | 11 passed, 0 failed |
| `V` | 145.0 / 242.9 tokens/s | 3,629 / 3,639 / 3,451 tokens/s | 7.59 / 18.16 / 42.99 s | 11 passed, 1 failed (`rag-and-web-version`) |
| `W` | 139.2 / 242.0 tokens/s | 3,846 / 3,842 / 3,642 tokens/s | 7.20 / 17.24 / 41.79 s | as `V` |
| `X` | 148.4 / 259.1 tokens/s | 3,913 / 3,904 / 3,695 tokens/s | 7.69 / 16.91 / 41.53 s | as `V` |
| `Y` (median of 3) | 139.2 / 254.7 tokens/s | 3,868 / 3,853 / 3,618 tokens/s | 7.41 / 17.87 / 44.21 s | 11 passed, 0 failed (each run) |

Arm `Y` became the default. P1 expansion from kq3 records and the prefill
custom all-reduce are bitwise equal and together add 7-9% prefill throughput.
The 32 x 32 prefill-attention tile (arm `V`) took a further 2.6 s off an 88K
prompt but is not bitwise: with it, `rag-and-web-version` thinks for 712
tokens and stops without an answer, deterministically, where arm `U` answers.
Against op233/op234's 96.3-104.3 / 172.8-194.5 tokens/s, arm `Y`'s three runs
measured C1 135.0-145.3 and C2 230.2-257.8 (per-request decode medians 156-160).
