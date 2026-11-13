# Models

Every model the workstation runs, what it is for and where it comes from. Each one is pinned to
an exact repository revision and its files' hashes, so an upgrade is a deliberate catalogue
change, never a moving `latest`.

## In use

| Purpose | Model | Pinned revision | Runs on | Catalogue |
| --- | --- | --- | --- | --- |
| Chat and coding | `amd/Qwen3.8-27B-Quark-AWQ-MXFP4`, served as `Qwen3.8-27B-MXFP4` | `5233554c…` | SGLang on both GPUs | [`qwen3.8-27b-mxfp4.json`](../infrastructure/ai-stack/models/qwen3.8-27b-mxfp4.json) |
| Speculative draft | `incoai/Qwen3.8-27B-DFlash2` | `015e7956…` | Inside SGLang, beside the chat model | Same entry |
| Pictures | `stabilityai/stable-diffusion-xl-base-1.0` (single-file checkpoint) | `46216598…` | ComfyUI on both GPUs, read from the NAS | [`sdxl-base-1.0.json`](../infrastructure/ai-stack/models/sdxl-base-1.0.json) |
| Video | Wan 2.2 T2V A14B: two 14B experts (FP8), lightx2v 4-step LoRAs (`1217`), umt5-xxl text encoder, Wan 2.1 VAE | `ee6f4a40…` (Comfy-Org repack), `57adea58…` (LoRAs) | ComfyUI on GPU 0, shared mode only | [`wan2.2-t2v-a14b.json`](../infrastructure/ai-stack/models/wan2.2-t2v-a14b.json) |
| Document and memory embeddings | `Qwen/Qwen3-Embedding-0.6B` | Hash-checked on staging | CPU (Open WebUI, and Text Embeddings Inference for memory) | [RAG](RAG.md) |
| Search reranking | `cross-encoder/ms-marco-MiniLM-L6-v2` | Hash-checked on staging | CPU | [RAG](RAG.md) |
| Voice input | `Systran/faster-whisper-base` (int8) | Hash-checked on staging | CPU, inside Open WebUI | [RAG](RAG.md) (`rag stage-whisper`) |

The chat model's context depends on the mode: 100,768 tokens per conversation in shared mode
and 262,112 in large mode ([service modes](MODES.md)). Its serving settings live in the
[serving profile](../infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json), which is
`qualified`; the catalogue holds only identity and file hashes.

Licences: Qwen3.8 and the DFlash2 draft follow their model cards; SDXL base 1.0 uses the
CreativeML Open RAIL++-M licence (fetched with the weights); Wan 2.2, umt5 and the lightx2v
LoRAs are Apache 2.0.

### Where the weights live

- **Chat model, draft and SDXL:** the NAS `ai-models` volume, staged by a `render-fetch.py`
  Job into `/models/<owner>/<name>/<revision>/` with `VERIFIED.json` written last. Both
  ComfyUIs mount the SDXL checkpoint read-only from there. See
  [AI stack models](../infrastructure/ai-stack/README.md#models).
  The NAS is the archive of every revision. **SGLang reads a local NVMe copy** of exactly
  the model and draft its serving contract names, from the `sglang-model-cache` claim
  (`local-path`, so deleting the claim deletes the copy). Before SGLang starts, the
  `model-cache` init container runs
  [`cache.py`](../infrastructure/ai-stack/models/cache.py). If the revision is cached and its
  `VERIFIED.json` matches the catalogue, it does nothing. Otherwise it copies from the NAS's
  verified copy, checking every file's size and hash in the same pass, publishes the revision
  with one rename, and deletes every revision the contract no longer names, so old models
  never accumulate on the NVMe. The paths are the same as on the NAS, so the launch
  arguments do not change. The first start after a model change runs at NAS speed (about
  150 MB/s, measured 2026-11-12); later starts read at local-array speed.
- **Wan 2.2:** the local-NVMe `creative-models` volume (`Video/`), staged with
  `stage-video.py` (about 36.8 GB) because ComfyUI streams it from disk on every step; see
  [short videos](ASSISTANT.md#short-videos-shared-mode).
- **Embeddings, reranker and Whisper:** the `webui-rag-qwen3-models` volume, staged by the
  `rag` commands.

### Adding or changing a model

Create a catalogue entry with `models/inventory.py`, add or update a serving profile, run the
tests, then qualify it with the bench Job before any profile marks it the default. The steps
are in [AI stack models](../infrastructure/ai-stack/README.md#models) and
[serving](../infrastructure/ai-stack/README.md#serving).

Fine-tuning, including QLoRA, is optional future work; the
[programming-book plan](RAG.md#programming-books-and-later-model-adaptation) starts with
retrieval and uses observed failures to decide whether an adapter is worth training. Qwen4
preparation is parked in [Qwen4 readiness](QWEN4-READINESS.md).

## Legacy container profiles

Everything below describes the older `apps/` overlays (Qwen3.8-27B-FP8 and Qwen3.5-9B
containers). They have not been applied since the November 2026 reinstall and are kept for
comparison.

### Selected defaults (legacy `apps/` overlays)

These container profiles are legacy. They have not been applied since the
2026-11 reinstall.

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

The single-card choice is the locked smaller Qwen release, not a claim that
9B matches the quality of 27B. Qwen3.8-27B-FP8 needs roughly 28.5 GB for weights
according to the [SGLang model guide](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B).
Splitting it over two GPUs leaves room for activations, Gated DeltaNet state,
attention KV cache and communication buffers. A single 32 GiB card leaves much
less room. Two GPUs have separate memory pools; TP does not make one 64 GiB device.

FP8 is the official serialized blockwise checkpoint, not a runtime conversion
from BF16 or an AWQ/NVFP4 substitute. The checkpoint configuration selects its
quantization; `--dtype bfloat16` controls the remaining tensors. The old forced
`--quantization awq` has been removed from both profiles.

### Model inventory

| Area and source | Previous selection | Decision and evidence |
| --- | --- | --- |
| `apps/overlays/dual-gpu/kustomization.yaml` | Llama-3.3-70B-Instruct-AWQ | Recorded container replacement: official [Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8), with 32K context. The native 128K candidate has a separate integration handoff |
| `apps/base/sglang/kustomization.yaml` | Qwen2.5-Coder-32B-Instruct-AWQ | Replace with official [Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B) for the explicit one-card option; this smaller release is not Qwen3.8 |
| `apps/overlays/rag/`, `lib/workstation/rag.sh` | MiniLM, 384-dimensional embeddings | Replace with [Qwen3-Embedding-0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), 1,024 dimensions; CPU-only, separate index/PVCs and mandatory reingestion |
| RAG reranker | None | Remain disabled; evaluate Qwen3-Reranker-0.6B separately after measuring retrieval errors, CPU latency and memory |
| `apps/base/swarmui/`, `infrastructure/creative/runtime.json` | Earlier generic FLUX/SDXL guidance | The creative runtime pins SDXL base 1.0 at revision `462165984030d82259a11f4367a4eed129e94a7b`, with a fixed SDXL workflow and isolated ROCm 10.2/Torch 2.14 closure. One 1024² native generation and PNG fetch passed on an R9700. Deployment, dependency, lifecycle and broader quality checks remain open; see [AI service](AI-SERVICE.md). Alternative image models are separate experiments |
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

### Revisions and serving compatibility

`versions.lock` records the two chat repository/revision pairs and the embedding
pair. Rendered chat paths include their revisions; tests reject drift between
the lock and profile. Chat templates, tokenizers, configs and weights must all
come from the same revision. Retain the model licenses with the staged files.

The checked-in dual-GPU overlay retains the AMD SGLang 0.5.15.post1 / ROCm 10.0
image. The single-GPU overlay selects the local SGLang 0.5.18 / ROCm 10.1 image.
Both have a source contract for `Qwen3_5ForConditionalGeneration` and configure
the `qwen3` reasoning and `qwen3_coder` tool-call parsers. The separately selected
native campaign uses SGLang 0.5.21 / ROCm 10.2; that choice has not promoted a
replacement image into these overlays. See the [runtime map](README.md#keep-the-runtime-layers-distinct).
Upstream model support or a matching architecture does not qualify the actual
FP8/GDN kernels, outputs, cache behavior or tool parser on this workstation.

Retain Triton attention and the three AITER/MLA switches set to `false` in the
profile, following [AMD's Radeon guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/inference/sglang.html).
Do not substitute an NVIDIA image, enable CUDA-only NVFP4 kernels, spoof gfx1201
or enable remote model code. General framework support is not a tested
Qwen3.8/R9700/Arch result. A matching architecture does not prove all FP8/GDN
kernel paths, model outputs or tool-call parsing work correctly.

This paragraph applies to the old pinned `apps/` image only. At the AI-stack
SGLang pin, upstream supports `extra_buffer` on HIP; the serving profile tests
it as arm B. The old pinned image rejects its automatically selected
`extra_buffer` hybrid-state cache on ROCm. Both legacy Qwen profiles therefore
explicitly use
`--mamba-radix-cache-strategy no_buffer --disable-overlap-schedule`, as
required by that image's `server_args.py` validation. This is a compatibility
setting, not a tuning result. With that image, do not enable `extra_buffer` or
overlap scheduling from instructions for another image or GPU platform.
Requalify startup, prefix-cache behavior, numerical outputs and request latency
after a reviewed image change.

`MODEL_REVISION` and the revision directory identify intended inputs. SGLang's
`--revision` does not checksum an arbitrary local directory. Verify transferred
files independently; never qualify a workload from its path or model name alone.

### Stage chat weights explicitly (legacy)

This procedure stages weights into the `llm-models` volume of the legacy
`apps/` overlays. For the current model, use the
[AI-stack model catalog](../infrastructure/ai-stack/README.md#models).

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

### Render, qualify and compare

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

Check `/v1/models` from an authorized internal client: the legacy default must
report `Qwen3.8-27B-FP8` (the AI-stack profile serves `Qwen3.8-27B-MXFP4`).
Test thinking/non-thinking responses, streamed reasoning and tool-call JSON,
including a second turn containing tool results. Tool parsing
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

### Qualification boundary

Source validation covers model locks, rendered defaults, model staging refusal
paths and RAG migration fixtures. It does not prove runtime compatibility,
inference speed, embedding quality, memory fit or GPU handover.
