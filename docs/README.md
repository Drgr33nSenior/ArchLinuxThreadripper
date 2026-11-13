# Documentation guide

Every document in this repository, grouped by what you want to do. Current guides come
first; dated records and superseded designs are listed under [History](#history) so they
don't get mistaken for how things work today. Terms are explained in the
[glossary](#glossary).

For a one-page overview of what is running, start with the [README](../README.md).

## Use the assistant

| Document | What it covers |
| --- | --- |
| [Family assistant](ASSISTANT.md) | What the family can do (chat, reasoning effort, pictures, video, voice, search, memory), who may do what, and how a request flows |
| [Service modes](MODES.md) | Shared vs large mode, what each GPU holds, and how to switch |
| [Coding clients](AGENT-HARNESSES.md) | Qwen Code and other IDE agents against the gateway |
| [Session memory](MEMORY.md) | Graphiti memory: per-user facts across chat and coding sessions |
| [Web search](WEB-SEARCH.md) | The Exa adapter, its budget and the public-page reader |
| [Document retrieval](RAG.md) | Open WebUI knowledge collections, embeddings, reranking and upload limits |
| [GraphRAG pilot](GRAPHRAG.md) | Optional on-demand analysis of a document set |

## Operate the workstation

| Document | What it covers |
| --- | --- |
| [Operations](OPERATIONS.md) | Day-to-day host and AI-service tasks: status, updates, power cap, restarts |
| [Service recovery](SERVICE-RECOVERY.md) | Backups of the AI service's data and how to restore them |
| [Host maintenance](HOST-MAINTENANCE.md) | SMART and hardware-error monitoring, advisory reports, package cache and the report-only build retention inventory |
| [Host upgrade plan](HOST-UPGRADE-PLAN.md) | Moving off the installation package snapshot: policy options, risks, procedure and rollback |
| [Monitoring](TELEMETRY.md) | Grafana dashboards: GPUs, host, Kubernetes, serving, power and cost |
| [Monitoring reference](TELEMETRY-REFERENCE.md) | Collector and dashboard contracts |
| [Home lab](HOME-LAB.md) | Bare-metal K3s, GPU sharing with DRA, storage, Secure Boot and TPM |
| [Resource and session policy](AI-PERFORMANCE.md) | CPU and memory budgets, and how `session switch` hands GPUs between workloads |
| [AI service setup](AI-SERVICE.md) | First rollout: DNS, TLS, accounts, gateway keys and image permissions |
| [GPU profiling](GPU-PROFILING.md) | Component profiling and the GPU power policy |
| [Game streaming](SUNSHINE.md) | Sunshine and Moonlight |

## Build and install

| Document | What it covers |
| --- | --- |
| [AI serving stack](../infrastructure/ai-stack/README.md) | Building the SGLang and picture images, staging models, the serving profile and its arms |
| [Models](MODELS.md) | Which models are installed, for what, and from which pinned revision |
| [Installation](INSTALLATION.md) | Installing or recovering the host |
| [Installation acceptance](INSTALL-READINESS.md) | The checklist a fresh install must pass |
| [Installer ISO](ISO.md) and [ISO reference](ISO-REFERENCE.md) | Building and assembling the installation media |
| [Workstation setup](WORKSTATION.md) | Native builds and the git kernel |
| [ROCm on the host](ROCM.md) | The host SDK and how it stays separate from container images |
| [Kubernetes add-ons](../kubernetes/README.md) and [cert-manager](../kubernetes/cert-manager/README.md) | Cluster add-ons and DNS-01 certificates |
| [VM lab](../ansible/K3S-OPERATIONS.md) | The optional AlmaLinux K3s VM; separate from the AI cluster |

## Reference

| Document | What it covers |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Components, request paths, boot and storage design |
| [Stack diagram](STACK.md) | Layers from hardware to applications |
| [Security](SECURITY.md) | Trust boundaries and what is exposed where |
| [Bridge contract](BRIDGE-CONTRACT.md) | The management bridge's session API |
| [Performance validation](PERFORMANCE-VALIDATION.md) | How serving measurements are taken and compared |
| [Source register](SOURCES.md) | Primary references and when to refresh them |

## History

These describe the system on the date they name. They are kept as records and are not
edited; where they disagree with a current guide, the current guide wins.

| Record | Date or era | Superseded by |
| --- | --- | --- |
| [Verification record](VERIFICATION.md) | 3 November 2026 component results | The serving profile's qualification records and [MODES](MODES.md) |
| [AI service reconciliation](AI-SERVICE-RECONCILIATION.md) | Early-November evidence matrix | [AI service setup](AI-SERVICE.md) |
| [Serving integration](SERVING-INTEGRATION.md) | The op234 candidate, before the November reinstall | [AI serving stack](../infrastructure/ai-stack/README.md) |
| [Profile research](../infrastructure/ai-stack/serving/RESEARCH-20261108.md) | 8 November 2026 | The [serving profile](../infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json) |
| [Serving results log](../infrastructure/ai-stack/serving/RESULTS.md) | 9–11 November 2026 arm measurements | The serving profile's `qualification` records |
| [Qwen4 readiness](QWEN4-READINESS.md) | Parked Qwen4 preparation | — |
| [Kernel requirements](KERNEL-REQUIREMENTS.md) | October 2026 kernel audit | [Workstation setup](WORKSTATION.md) |
| [Model kernels](MODEL-KERNELS.md), [performance profiles](PERFORMANCE-PROFILES.md) | Qwen3-Coder-era tuning | [Performance validation](PERFORMANCE-VALIDATION.md) |
| [Hyperloom](HYPERLOOM.md), [Hyperloom reference](HYPERLOOM-REFERENCE.md) | op234-era trace tooling | — |
| [DRA pilot](../infrastructure/gpu-operator/dra-pilot/README.md) | The GPU-sharing pilot, now in use | [Home lab](HOME-LAB.md) |

## Glossary

| Term | Meaning |
| --- | --- |
| **Arm** | One named set of serving settings in the serving profile (for example `UQS`). Each arm is measured and qualified separately |
| **UQS / UQL** | The two arms in use: `UQS` for shared mode (smaller cache, room for pictures), `UQL` for large mode (the model's full context). Both keep the chat cache in FP8 |
| **Y** | The previous default arm (BF16 cache), qualified on 10 November 2026 |
| **Mode** | What the GPUs are doing: shared, large, split, gaming or maintenance. See [MODES](MODES.md) |
| **Render** | `service render`: writes every Kubernetes manifest and client setting for a mode from the serving profile. Rendering changes nothing live |
| **Session** | `session switch`: stops and starts the rendered GPU workloads for a mode |
| **TP2** | Tensor parallel 2: one model split across both GPUs, which work in lockstep |
| **KV cache / pool** | Memory holding each conversation's context. Its size sets how long a conversation can be |
| **FP8 KV** | The cache stored in 8-bit floats, half the size of BF16, read back as BF16 for the maths |
| **MXFP4** | The 4-bit weight format of the chat model. The R9700 has no FP4 hardware, so it is unpacked in software |
| **DFlash2 / speculative draft** | A small model that guesses several tokens ahead; the big model checks them in one pass, which speeds up replies |
| **C1 / C2** | Benchmark throughput with one or two concurrent conversations |
| **op234** | The serving candidate selected on 7 November 2026 on the old runtime; wiped by the November reinstall |
| **DRA / claim** | Kubernetes Dynamic Resource Allocation. Each GPU is a named claim (`gpu-0`, `gpu-1`) that several pods can hold at once |
| **CU mask** | `HSA_CU_MASK`: limits a process to some of a GPU's 64 compute units |
| **MCP** | Model Context Protocol: how Open WebUI and coding agents call `service-tools` |
| **Gateway** | LiteLLM wrapped by our boundary (`apps/base/shared-api/boundary.py`), which maps user keys, checks limits and admits pictures |
| **Reasoning Effort** | Open WebUI's Chat Controls setting for how long the model thinks (`none` to `xhigh`) |

## Keep the runtime layers distinct

Several ROCm and PyTorch stacks exist side by side. Each is pinned in its own place; changing
one never updates or qualifies another.

| Layer | Pinned in | What it is |
| --- | --- | --- |
| Host graphics and compute | Arch packages; [ROCm provider settings](ROCM.md#host-sdk-selection-remains-separate) | Host `amdgpu` and native tools. Never injected into containers |
| **AI serving stack (live)** | [stack.lock](../infrastructure/ai-stack/stack.lock); deployments pin the image by digest | SGLang main (0.5.23 + 957) with the gfx1201 patch series, Python 3.14, PyTorch 2.16.0a0, TheRock ROCm 10.3.0-dev (HIP 7.18), Triton 3.9.0; no AITER |
| **Picture and video image (live)** | The same [stack.lock](../infrastructure/ai-stack/stack.lock) (`creative` stage) | SwarmUI and ComfyUI 0.45 on the AI stack's ROCm and PyTorch builds |
| Legacy single-GPU overlay | `SGLANG_SINGLE_ROCM_IMAGE` in [versions.lock](../versions.lock) | SGLang 0.5.18 / ROCm 10.1, Qwen3.5-9B; not applied since the reinstall |
| Legacy dual-GPU overlay | `SGLANG_ROCM_IMAGE` in [versions.lock](../versions.lock) | AMD SGLang 0.5.15.post1 / ROCm 10.0, Qwen3.8-27B-FP8; not applied since the reinstall |
| op234 campaign (historical) | [sglang-rocm102.json](../infrastructure/hyperloom/sglang-rocm102.json) | SGLang 0.5.21, ROCm 10.2 dev, Torch 2.14; wiped by the November 2026 reinstall |
| Earlier creative package (historical) | [runtime.json](../infrastructure/creative/runtime.json) | Standalone ROCm 10.2 / Torch 2.14 environment; replaced by the AI stack's `creative` stage |

Before promoting a change, verify the image digest, source, loaded libraries, model revision
and caches together. Do not change pins just to make version labels agree.

## Read results and commands correctly

- `implemented` means the source exists. `source-validated` means offline checks passed.
  A target result applies only to the boot, image, model and workload it records. `NOT RUN`
  means untested.
- A command in a guide is not evidence that it ran. Rendering and planning are local;
  applying manifests, changing packages and switching sessions are owner actions.
- Rendered manifests leave replicas at zero. The session tooling starts workloads.

## Keep this documentation useful

- Put everyday procedures in the task guide and contracts in its reference.
- Keep exact versions in the locks and the serving profile, not copied into guides. The
  README's "Running now" table is the one dated exception.
- Record experiments as new dated documents and link them from History; never edit a
  dated record.
- When behaviour changes, update the guide in the same change, check links and anchors,
  and render any Mermaid diagram you touched.
