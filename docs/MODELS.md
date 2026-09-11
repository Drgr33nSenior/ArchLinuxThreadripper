# Model defaults and qualification

Reviewed on 2026-09-08. Prefer current open-weight models that fit the selected
workload and serving backend. Pin each release; do not use a moving `latest`
checkpoint or assume that a newer model is faster on this workstation.

## Selected defaults

| Profile | Chat model | Allocation | Configured context / concurrent requests |
| --- | --- | --- | --- |
| `default`, `dual-gpu` | Qwen3.8-27B-FP8 | Two R9700s, tensor parallelism 2 | 32,768 tokens / 2 |
| `rag`, `rag-dense` | Same Qwen3.8 model, plus Qwen3-Embedding-0.6B on CPU | Two GPUs for chat; separate CPU retrieval budget | Same chat limits |
| `single-gpu`, dependent family profiles | Qwen3.5-9B, BF16 | One R9700 | 4,096 tokens / 2 |

`default` is an alias of `dual-gpu`; it does not change resource ownership or
start services. All workloads retain zero replicas pending qualification.
For browser chat with document retrieval, use `rag`. Its pinned Open WebUI
image and separate data volumes are described in [RAG.md](RAG.md). The plain
AI overlays still require Open WebUI image/admin bootstrap qualification.

The single-card choice is the current smaller Qwen release, not a claim that
9B matches the quality of 27B. Qwen3.8-27B-FP8 needs roughly 28.5 GB for weights
according to the [SGLang model guide](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B).
Splitting it over two GPUs leaves room for activations, Gated DeltaNet state,
attention KV cache and communication buffers. A single 32 GiB card leaves much
less room. Two GPUs have separate memory pools; TP does not make one 64 GiB device.

FP8 is the official serialized blockwise checkpoint, not a runtime conversion
from BF16 or an AWQ/NVFP4 substitute. The checkpoint configuration selects its
quantization; `--dtype bfloat16` controls the remaining tensors. The old forced
`--quantization awq` has been removed from both profiles.

## Complete model inventory

| Area and source | Previous selection | Decision and evidence |
| --- | --- | --- |
| `apps/overlays/dual-gpu/kustomization.yaml` | Llama-3.3-70B-Instruct-AWQ | Replace with official [Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8); prefer the current agent/chat candidate with more runtime headroom |
| `apps/base/sglang/kustomization.yaml` | Qwen2.5-Coder-32B-Instruct-AWQ | Replace with official [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) for the explicit one-card option; this smaller release is not Qwen3.8 |
| `apps/overlays/rag/`, `lib/workstation/rag.sh` | MiniLM, 384-dimensional embeddings | Replace with [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), 1,024 dimensions; CPU-only, separate index/PVCs and mandatory reingestion |
| RAG reranker | None | Remain disabled; evaluate Qwen3-Reranker-0.6B separately after measuring retrieval errors, CPU latency and memory |
| `apps/base/swarmui/` | No model selected; generic FLUX/SDXL prose only | No checkpoint to upgrade. The ROCm/ComfyUI image is unqualified. Review current SwarmUI-supported FLUX.2 Klein and Qwen Image workflows before choosing and pinning a complete model/encoder/VAE bundle |
| `lib/workstation/rocm.sh` llama.cpp CLI/benchmarks | Operator-supplied GGUF | Remain model-neutral. Use a reviewed conversion of the selected current model when testing HIP/Vulkan; record its hash and quantization, not just the family name |
| Legacy Intel `llm` commands | Operator-supplied model directory | Remain Intel-only and outside the R9700 deployment; no hidden default weights |
| Steam/Sunshine, ISO, host packages | None | No inference models to refresh; no model weights are bundled in the ISO |

Version numbers differ by task. Qwen3-Embedding-0.6B is the compact text encoder
selected for this CPU-only Markdown/text pipeline, not Qwen's newest model
across every modality. The newer [Qwen3-VL-Embedding 2B/8B series](https://github.com/QwenLM/Qwen3-VL-Embedding)
is a follow-up for image/video retrieval with a different memory and ingestion
budget. Hosted embedding APIs are not local checkpoint replacements.

For creative workflows, consult [SwarmUI model support](https://github.com/mcmonkeyprojects/SwarmUI/blob/master/docs/Model%20Support.md).
That list is capability evidence, not a validated image/model combination here.
In particular, Qwen Image FP8 loading can exceed the existing 12 GiB SwarmUI
host-memory budget. Do not select a large new model without its dependency and
memory review. Hosted API models are not interchangeable with downloadable weights.

## Revisions and serving compatibility

`versions.lock` records the two chat repository/revision pairs and the embedding
pair. Rendered chat paths include their revisions; tests reject drift between
the lock and profile. Chat templates, tokenizers, configs and weights must all
come from the same revision. Retain the model licenses with the staged files.

The pinned AMD SGLang 0.5.15.post1 / ROCm 10 image already contains
`Qwen3_5ForConditionalGeneration`, the architecture used by these chat models,
and the `qwen3` reasoning and `qwen3_coder` tool-call parsers. Those parsers are
now configured. Later [Qwen3.8 upstream work](https://github.com/sgl-project/sglang/pull/34859)
includes CUDA-specific optimizations; its existence is not proof that the older
AMD image cannot load the shared architecture, nor that it reaches current
upstream performance. Inspect exact source changes before updating the image.

Retain Triton attention and the three AITER/MLA switches set to `false` in the
profile, following [AMD's Radeon guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html).
Do not substitute an NVIDIA image, enable CUDA-only NVFP4 kernels, spoof gfx1201
or enable remote model code. General framework support is not a tested
Qwen3.8/R9700/Arch result. A matching architecture does not prove all FP8/GDN
kernel paths, model outputs or tool-call parsing work correctly.

`MODEL_REVISION` and the revision directory identify intended inputs. SGLang's
`--revision` does not checksum an arbitrary local directory. Verify transferred
files independently; never qualify a workload from its path or model name alone.

## Stage chat weights explicitly — not run here

Use an already reviewed Hugging Face CLI environment on a machine with storage
and network access. No CLI installation or model download occurs in CI. The
[upstream CLI](https://huggingface.co/docs/huggingface_hub/guides/cli#hf-download)
supports a download preview and revision-specific checksum verification.

For the default model, first check the proposed download size and reserve at
least 40 GiB for this snapshot and temporary transfer space. Use a fresh directory:

```sh
hf download Qwen/Qwen3.8-27B-FP8 \
  --revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --local-dir artifacts/chat-models-01/Qwen3.8-27B-FP8/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --dry-run
```

Review every file and its size. Remove only `--dry-run` to download the reviewed
snapshot. Then verify against that exact Hub revision:

```sh
hf cache verify Qwen/Qwen3.8-27B-FP8 \
  --revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --local-dir artifacts/chat-models-01/Qwen3.8-27B-FP8/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --fail-on-missing-files --fail-on-extra-files
```

For the single-card option, use `Qwen/Qwen3.5-9B`, revision
`c202236235762e1c871ad0ccb60c8ee5ba337b9a`, and the corresponding
`Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a` directory. Preview its
download separately. Do not place both snapshots in the same revision directory.

Copy the selected model/revision directory into the existing `llm-models` PVC
through the reviewed storage process, preserving its relative path. Verify the
transferred files, retain a checksum report with the run evidence and grant
read access to UID/GID 1000. The container mounts `/models` read-only and keeps
`HF_HUB_OFFLINE=1`; it will not fetch missing shards on startup. Do not store
weights in container writable layers or the installer image.

## Render, qualify and compare

These commands do not contact a cluster:

```sh
kubectl kustomize apps/overlays/default
kubectl kustomize apps/overlays/single-gpu
kubectl kustomize apps/overlays/rag
bash tests/test_model_defaults.sh
bash tests/test_home_lab.sh
bash tests/test_rag.sh
```

On the workstation, follow [HOME-LAB.md](HOME-LAB.md#6-validation-and-promotion)
before enabling a workload. First test short inputs at a 4,096-token context,
then test the default 32,768-token profile with one and two concurrent requests.
Record per-card VRAM, host/pod peak memory, startup time, generation correctness,
time to first token, prompt/decode throughput and GDN/FP8 backend selections.
Keep the existing host/K3s/eviction reservations and 0.80 static-memory fraction.
The FP8 chat checkpoint has an initial 38 GiB host-memory request/limit. This is
budget arithmetic, not observed consumption or proof that loading fits. The
original 64 GiB plan left 46 GiB after host reserves, then allowed 6 GiB for RAG
and 2 GiB for other Pods. Full telemetry is additional demand; that original
combination no longer fits unchanged. Use the
[host-memory qualification workflow](PERFORMANCE-VALIDATION.md#right-size-sglang-host-ram)
to generate measured-input candidates. Count other Pods and actual discovered
RAM before admission. No swap or CPU offload is enabled to hide an OOM. Stop
unrelated compilation during qualification.

Check `/v1/models` from an authorized internal client: the default must report
`Qwen3.8-27B-FP8`. Test thinking/non-thinking responses, streamed reasoning and
tool-call JSON, including a second turn containing tool results. Tool parsing
does not grant an agent file, shell, database or cluster permissions. Inspect
context accounting before exposing the service to an IDE client.

Compare model changes using matched questions, context limits, sampling,
reasoning effort and concurrency. Record completed-task/citation accuracy as
well as latency; newer model quality and tokens/second are separate measures.
Do not compare the new 32K profile with the old 4K profile and attribute the
difference solely to the model. No local performance improvement is claimed.

For gaming, stop/unload a TP=2 model before allocating a GPU to the game. The
existing session controller stops the managed AI workload; it does not rewrite
it into the one-GPU profile or keep half a sharded model running.

For rollback, restore the previous reviewed model, profile and image together
through maintenance. Leave replicas at zero if no combination has passed.
Retain old model and RAG data volumes; do not overwrite, delete or reinterpret
MiniLM indexes as Qwen embeddings. The [RAG migration](RAG.md) uses new volumes.

## Local verification record — 2026-09-08

- `HOME_LAB_PYTHON=/usr/local/bin/python3.11 make check`: passed all 38 test
  files using the configured development interpreter. This includes model-lock
  validation, rendered defaults, RAG staging fixtures and session regression tests.
- Strict kubeconform validation against Kubernetes 1.35: `default` and
  `single-gpu` each passed 24 resources; `rag` and `rag-dense` each passed 28.
  No resources were skipped. The default and dual-GPU renders are identical.
- Independent source review found no defects in the selected model configuration
  or RAG migration. Documentation audits and changed-file whitespace checks passed.

The full suite reported unavailable optional checks: shfmt, Bats, Ansible,
Linux systemd verification, the real ccache/CMake fixtures, Bash 4 USB signal
fixtures, the local GPU Operator chart and a built streaming image. These were
skipped, not passed. No dependencies were installed to run the checks.

Physical validation: **NOT RUN — target hardware unavailable**. Local rendering,
lock checks and synthetic model-file tests do not prove runtime compatibility,
inference speed, embedding quality, memory fit or GPU handover.
