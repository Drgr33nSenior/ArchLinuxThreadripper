# Documentation guide

Updated 1 November 2026 against repository configuration and retained target
evidence. This is a guide to the project, not a live inventory or a deployment
approval. Versions belong to the linked locks and runtime manifests.

## Start with your task

| You want to… | Read first | Detail when needed |
| --- | --- | --- |
| Build or write installation media | [ISO](ISO.md) | [ISO reference](ISO-REFERENCE.md) |
| Install, unlock or recover a host | [Installation](INSTALLATION.md) | [Acceptance checklist](INSTALL-READINESS.md) |
| Maintain packages, backups and services | [Operations](OPERATIONS.md) | [Application recovery](SERVICE-RECOVERY.md) |
| Inspect the workstation in Grafana | [Monitoring](TELEMETRY.md) | [Collector and deployment reference](TELEMETRY-REFERENCE.md) |
| Configure bare-metal K3s and GPU ownership | [Home lab](HOME-LAB.md) | [Resource and session policy](AI-PERFORMANCE.md) |
| Select or stage a model | [Models](MODELS.md) | [Retrieval](RAG.md), [IDE agents](AGENT-HARNESSES.md) |
| Provide private browser/API access | [AI service](AI-SERVICE.md) | [Integration evidence](AI-SERVICE-RECONCILIATION.md) |
| Use Sunshine with Moonlight | [Sunshine](SUNSHINE.md) | [Acceptance checklist](INSTALL-READINESS.md#sunshine-and-moonlight) |
| Compare serving configurations | [Performance validation](PERFORMANCE-VALIDATION.md) | [Profiles](PERFORMANCE-PROFILES.md), [kernel packages](MODEL-KERNELS.md) |
| Analyze retained traces or bind an existing owner run | [Hyperloom](HYPERLOOM.md) | [Owner binding reference](HYPERLOOM-REFERENCE.md) |
| Build native applications or experimental kernels | [Workstation setup](WORKSTATION.md) | [ROCm](ROCM.md), [kernel requirements](KERNEL-REQUIREMENTS.md) |
| Understand component and trust boundaries | [Architecture](ARCHITECTURE.md) | [Stack diagram](STACK.md), [security](SECURITY.md), [Bridge contract](BRIDGE-CONTRACT.md) |

The optional VM lab uses [its own operations guide](../ansible/K3S-OPERATIONS.md).
It is not the bare-metal AI cluster and its backup does not cover application PVCs.

## What has actually been exercised

| Area | Retained result | Still open |
| --- | --- | --- |
| Monitoring | Target-deployed dashboards and collectors; drilldowns cover GPUs, host, Kubernetes, serving and power. Serving metric-name normalization is integrated | Check current scrape health; missing GPU channels remain unavailable. Automatic Octopus prices need an exact supported tariff/payment configuration |
| Creative / ComfyUI | One R9700 generated and served a 1024² SDXL PNG; first request 10.74 s, backend readiness 6.62 s | Final recipe rebuild, vulnerable/licensed ImageSharp dependency resolution, WebUI delivery, cancellation, persistence and broader quality |
| Sunshine / Moonlight | Arch candidate captured 1920×1080/60 and started Vulkan encoders; owner confirmed mouse/click/typing in X11 and native Wayland windows on 1 November | Audio, controllers, games, repeat startup, persistent pairing/home recovery, delivered cadence and latency |
| Hyperloom / TraceLens | Offline tools and the project adapter to an existing SGLang owner workflow were exercised | Upstream R9700 runner remains vLLM-only in the inspected release; no autonomous SGLang optimization or model qualification follows |
| Shared chat/API service | Source integration and disposable native gateway/database smoke path | Full target rollout, multiuser acceptance and independent recovery |

These are scoped observations, not a blanket pass. Historical numerical,
model-quality and shutdown failures remain open unless their specific cause was
resolved. Private `test-results/` handoffs retain exact run identities and failed
evidence; they are not bundled into this public documentation.

## Keep the runtime layers distinct

| Layer | Configuration source | Meaning |
| --- | --- | --- |
| Host graphics/compute | Arch packages; [ROCm provider settings](ROCM.md#host-sdk-selection-remains-separate) | Host `amdgpu` and native tools; do not inject these libraries into containers |
| Checked-in single-GPU serving overlay | `SGLANG_SINGLE_ROCM_IMAGE` in [versions.lock](../versions.lock) | Locally built SGLang 0.5.18 / ROCm 10.1 candidate, Qwen3.5-9B |
| Checked-in dual-GPU serving overlay | `SGLANG_ROCM_IMAGE` in [versions.lock](../versions.lock) | Retained AMD SGLang 0.5.15.post1 / ROCm 10.0 candidate, Qwen3.8-27B-FP8 |
| Owner-selected native serving campaign | [sglang-rocm102.json](../infrastructure/hyperloom/sglang-rocm102.json) plus owner package | SGLang 0.5.21, source-built ROCm 10.2 dev, Torch 2.14, HIP 7.17.26416; separate from application overlay defaults |
| Creative candidate | [runtime.json](../infrastructure/creative/runtime.json) | Standalone ROCm 10.2 / Torch 2.14 environment inside a ROCm 10.1-labelled parent image |

The production-stack choice does not automatically update these overlays or
qualify them. Verify the selected image digest, actual source bytes, loaded
libraries, model revision and caches together before promotion. Do not replace
pins merely to make the version labels agree.

## Read results and commands correctly

`implemented` means source exists; `source-validated` means the relevant offline
checks passed. A target result applies only to its recorded boot, image, model
and workload. `deployed` does not mean fully qualified. `NOT RUN` is untested.

Rendering and planning commands operate locally. Applying manifests, activating
host units, changing packages and starting a workload are owner operations on
an explicitly selected target. A command in a guide is not evidence it ran.
Keep application replicas disabled until their acceptance gates pass; preserve
GPU allocation, session handover and recovery controls.

## Maintain this documentation

Put everyday procedures in the task guide and detailed contracts in its reference.
Keep exact versions in locks rather than copying them into unrelated guides.
Record experiments in fresh evidence packages; summarize only conclusions and
remaining limits here. When behavior changes, update the canonical guide and
its package inventory, check local links and anchors, and run affected checks.
Use the [source register](SOURCES.md) to find primary references; refresh rolling
sources before relying on them for an upgrade.
