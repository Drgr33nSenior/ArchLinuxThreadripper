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
default. For `qwen3.8-27b-mxfp4-tp2`, arm A (`no_buffer`, overlap off, as
op234) is the default for the bring-up baseline. Arm B (`extra_buffer_lazy`
with the overlap scheduler) is the first experiment once A serves correctly.

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

After BetterBench, the Job runs the assistant acceptance menu
([`assistant-quality.json`](../sglang/assistant-quality.json) through
`lib/workstation/assistant_acceptance.py`, both unchanged) against the native
endpoint. No speed change is read without a quality result. Code tasks need
the bubblewrap sandbox, which the pod lacks, and long-context cases need a
tokenizer budget. The runner reports both as NOT RUN, never as passed.
`QUALITY_RESULT` lists the pass rate, the tool round-trip, grounded and
abstention rates, and any failed task. The Job fails if an executed task
fails.

```sh
infrastructure/ai-stack/serving/render.py bench infrastructure/ai-stack/serving/<profile>.json \
  registry.lan.thespry.family/local/sglang-gfx1201@sha256:<digest> --serving <serving-repo> > bench-a.json
kubectl apply -f bench-a.json
kubectl -n ai-home-lab logs job/<name> | grep '^BENCH_RESULT'
kubectl -n ai-home-lab delete job,configmap -l app.kubernetes.io/name=sglang-bench
```

`BENCH_RESULT` gives these results:

- the median decode rate for each category;
- C1 and C2 aggregate throughput;
- the median prefill rate at each depth;
- for each phase, the server's generated tokens per verify call (its
  `generation_tokens_total` and `spec_verify_calls_total` counters).

op233/op234 measured C1 96.3–104.3 and C2 172.8–194.5 tokens/s at a 235 W cap
per card. Compare only at a matched cap. `BENCH_RAW` holds the full BetterBench
results, compressed.
