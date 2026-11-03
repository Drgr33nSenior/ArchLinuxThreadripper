# Family AI starting point

The `assistant` experience connects the existing Open WebUI, private model
gateway, RAG profile and SwarmUI/ComfyUI image adapter. It is an opt-in source
configuration. Rendering does not deploy it or qualify a new model.

## Current delivery scope

Complete the workstation installation on the current 64 GB RAM configuration,
then establish the selected native 128K serving candidate and verify the
private Open WebUI, Exa and local RAG paths together. Preserve the existing
boot, recovery, account, document-permission and resource-admission gates.

The next service work is packaging a persistent authenticated native endpoint,
reconciling model identity and limits across the gateway and clients, and
running the prepared quality menu through the model and browser. The older
container defaults below remain a separate source configuration.

QLoRA and other model fine-tuning are optional post-install projects. They are
outside the current delivery requirements and add no installation dependency
or acceptance gate. Revisit them after stable operation provides useful
corrections and separate evaluation tasks. Training would use its own reviewed
environment and resource budget.

## Request paths

| Use | Path | Current boundary |
| --- | --- | --- |
| Conversation | Open WebUI → per-user gateway key → SGLang | Preserve the selected model identity and existing context limits |
| Documents | Open WebUI → CPU Qwen embedding model → embedded Chroma | Preserve document permissions; answer/citation quality remains unqualified |
| Web research | Open WebUI/Qwen Code → shared Exa adapter | Auto search and public-page extraction; one durable request ceiling; Tavily optional |
| Image creation | Open WebUI → bounded image adapter → SwarmUI → embedded ComfyUI | SDXL only, one image job, explicit split-mode admission |
| Creative controls | Operator → SwarmUI; ComfyUI remains its embedded backend | No new public ComfyUI port or unauthenticated creative Ingress |
| Spoken answers | Open WebUI browser speech synthesis | Quality and voice availability depend on the browser/operating system |

The family profile adds a common name, four prompt suggestions and an explicit
default model. Signup remains closed. Each person needs a WebUI account and a
mapped gateway key. Image permission remains a separate per-user grant.
Configuration comes from source rather than persisted administrator settings.

The [web-search guide](WEB-SEARCH.md) covers provider-key provisioning, budget
limits, shared coding-client tools and the separate direct Codex OAuth path.
Web results remain temporary prompt evidence; the existing local RAG store and
document permissions remain authoritative. End-to-end search is not yet qualified.

## Select and render

Use the existing [service setup](AI-SERVICE.md) for private DNS, TLS, credentials,
retained storage, account bootstrap and owner handover. Supply actual site values
and existing claim metadata; do not apply example DNS or invent a new identity.

```sh
./bin/workstationctl service render /private/service-site.json /private/assistant-large \
  --experience assistant --mode large --existing-claims /private/service-pvcs.json
./bin/workstationctl service render /private/service-site.json /private/assistant-split \
  --experience assistant --mode split --existing-claims /private/service-pvcs.json
```

`large` keeps the existing two-GPU Qwen3.8-27B-FP8 service contract and disables
image generation in WebUI. `split` explicitly selects the existing one-GPU
Qwen3.5-9B contract and enables SDXL on the other GPU. It never silently exchanges
the chat model to make room for an image. The native ROCm 10.2 serving candidate
must still complete its own packaging and model-specific qualification; this
profile does not substitute it for the existing published image.

Both renders keep application replicas at zero. Use `service plan` with fresh
resource evidence and the selected telemetry composition before activation.
The workstation has 64 GB installed RAM. The retained Linux-visible observation
is about 62 GiB.
Existing split limits exceed the reviewed CPU budget; this change does not lower
them to obtain admission. Shared UI configuration is not physical GPU ownership.

## Native 128K candidate integration

The selected private native candidate uses Qwen3.8-27B Quark AWQ MXFP4,
BF16 target/draft KV, DFlash width 8 and C4 attention on two R9700s.
Its context is 131,040 tokens; its shared token pool is 262,144 tokens for
at most two running requests. Its tested served-model identifier is
`fp8-serving-screen`; that historical name does not identify weight precision.
This candidate is separate from the older container composition above.
Its source-built runtime uses SGLang 0.5.21, ROCm 10.2 and Torch 2.14.

The selected pointer checked on 3 November 2026 has SHA-256
`fe92916ca06f43ec719d3a66345e4345908106212596d7044c03713b575c3394`.
The integration readout found six differences: upstream model identity,
gateway, router, client and container context, and container model revision.
Recheck the then-current pointer before preparing another handoff; this digest
records metadata provenance and does not qualify an installed runtime.

Prepare an offline comparison from the current selected pointer and its
independently checked SHA-256:

```sh
./bin/workstationctl service candidate-plan /private/CANDIDATE-POINTER.json \
  /private/assistant-candidate-plan --pointer-sha256 EXACT_64_HEX_DIGEST \
  --output-tokens 4096
```

The command authenticates the supplied pointer bytes and checks its selected
arm and declared CLI/resolved-contract agreement. It does not authenticate
the installed runtime, model weights or recorded approval. It writes a
contract, proposed client values, an eight-case quality menu and an integration
report. It omits arbitrary CLI values and credentials. It performs no network
request, model launch, source promotion or workload deployment.

The current UI/gateway still routes the older FP8 model with 32K context.
The report identifies this mismatch. Context capacity and generated-output
allowance are separate: `--output-tokens` proposes a policy for evaluation;
it does not establish that allowance as qualified. The command succeeds when
it writes the report; `blocked-integration` and `deployment_ready: false`
describe integration readiness.

Before activation, package and qualify a persistent authenticated native
endpoint. Reconcile its identity, router metadata, gateway policy and client
settings together. Use the actual tokenizer and template to budget history,
tools, retrieved text, output and scheduler reserve. Run the menu through the
gateway and browser, including reasoning on/off, tool-result follow-ups and
facts at the beginning, middle and end of long prompts. Retain failures and
measure output truncation and deadlines. The offline gateway transport tests
use deterministic backends; they do not qualify native model tool use.

Operation 207's serving cleanup passed all 352 reference-vector comparisons
and natural-answer grades across eight distinct prompt sequences. This bounded
evidence does not establish broad assistant quality or an installed service.

## Pinned inputs and gates

The new experience pins Open WebUI **0.12.5**, source
`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`, Linux amd64 manifest
`sha256:0caf3d68199bcd8b7051775778691dfa5f3c8a355d1d6484000171b2974f40f9`.
The legacy `service` experience retains its previous selection. The older RAG
functional gate is evidence for its recorded image, not qualification of 0.12.5.
Run a fresh UI/account, upload/ACL, retrieval, web-search/tool and image-return
gate before use.

`apps/base/shared-api/image-profile.json` records the existing SDXL revision,
weight hash, workflow, dimensions and sampling settings. The adapter loads and
checks the operator-owned workflow at startup, copies it for each request and
records its hash in health/in-flight evidence. Clients cannot choose a workflow,
checkpoint, URL or unqualified model. Declared hashes do not prove installed
weights; the existing creative staging and runtime checks remain required.
Cancellation uncertainty retains the durable in-flight marker and blocks another
job. An interrupt acknowledgement is not proof that GPU work has stopped.

## Speech and later model experiments

Server speech is not deployed by this profile. Default regular-user microphone
transcription permission is disabled until a local provider is qualified; browser speech
recognition can use external services and is not a local-ASR substitute.
No hosted speech credentials or automatic model downloads are configured.

The refreshed official Qwen sources have incompatible dependency pins:

| Candidate | Pinned source / model revision | Requirement |
| --- | --- | --- |
| Qwen3-ASR-1.7B | Source `7c6daf77a2421100f5fb066495372c00129d39ff`; model `7278e1e70fe206f11671096ffdd38061171dd6e5` | qwen-asr 0.0.6 requires Transformers 4.57.6 |
| Qwen3-TTS-12Hz-1.7B-CustomVoice | Source `022e286b98fbec7e1e916cb940cdf532cd9f488e`; model `0c0e3051f131929182e2c023b9537f8b1c68adfe` | qwen-tts 0.1.1 requires Transformers 4.57.3 |

Use separate environments when packaging these Apache-2.0 candidates. Do not
override their pins or install either into the serving/creative environment.
Their OpenAI-compatible provider wiring, runtime build, quality and resource
admission remain unfinished. This configuration makes no voice latency claim.

Gemma 4, Qwen-Image, FLUX and LTX remain model experiments. Each needs a complete
pipeline memory plan, version-compatible workflow and local quality checks.
Image understanding also needs a qualified multimodal serving route; uploading
an image does not establish that the current text-serving configuration can read
it. No new model collection was downloaded for this starting point.

## Validation and rollback

Run `HOME_LAB_PYTHON=<prepared-python> bash tests/test_assistant_stack.sh` and
`tests/test_shared_service.sh`, then the repository checks. Tests resolve the
generated ConfigMap references and environment precedence, inspect both image
references, preserve ownership/policies/storage, and reject substituted workflows.
These are software checks; browser behavior and physical inference remain separate.

Before first deployment, rollback is a fresh `service render` without
`--experience assistant`. Preserve the generated evidence and previous package.
After an actual WebUI upgrade, use the [backup/restore procedure](SERVICE-RECOVERY.md)
for the database and uploads. Do not point an older image at a migrated database
as a shortcut. No rollback requires deleting a model, cache or PVC.

Sources refreshed 1 November 2026:
[WebUI 0.12.5](https://github.com/open-webui/open-webui/releases/tag/v0.12.5),
[image connector source](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/routers/images.py),
[ASR dependencies](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/pyproject.toml),
[TTS dependencies](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/pyproject.toml).
