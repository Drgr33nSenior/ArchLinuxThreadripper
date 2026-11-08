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
duration and compiler-cache hit ratio, and the NAS alerts on a failed or stuck
stage. Create the directory once, owned by the build user:

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

Set `AI_STACK_PUSH=1` for the `image` stage to push it to the lab registry.
This needs a one-time `podman login registry.lan.thespry.family` as the
publisher account.

## Models

Each catalog entry pins a model, an optional draft and its serving settings.
To add one, for example a Qwen4 checkpoint:

```sh
infrastructure/ai-stack/models/inventory.py OWNER/NAME FULL_COMMIT > /tmp/inventory.json
```

Copy the inventory into a new `models/<alias>.json` beside the existing entry,
set the serving settings, and run the tests. To stage it on the NAS:

```sh
kubectl apply -f infrastructure/ai-stack/k8s/models.yaml      # once
infrastructure/ai-stack/models/render-fetch.py \
  infrastructure/ai-stack/models/<alias>.json infrastructure/ai-stack/stack.lock | kubectl apply -f -
```

The Job downloads into the `ai-models` volume at
`<owner>/<name>/<revision>/`, checks each file's size and hash, and writes
`VERIFIED.json` last. Earlier revisions stay in place for rollback.

## Serving

`render-serve.py` turns a catalog entry and an image into a Deployment and
stable Service named `sglang`, which clients and the telemetry collector use
whichever model is loaded. It mounts the verified revision read-only,
claims `tp_size` GPUs, and replaces the old pod before starting the new one
(`Recreate`), because one server owns both GPUs. The API key comes from the
`sglang-api-key` Secret, which is created on the host and never rendered:

```sh
infrastructure/ai-stack/models/render-serve.py \
  infrastructure/ai-stack/models/<alias>.json registry.lan.thespry.family/local/sglang-gfx1201:<key> |
  kubectl apply -f -
```

Rolling back means applying the previous image key or catalog entry. Both stay
in the registry and on the NAS.
