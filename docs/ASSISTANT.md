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

For cross-session coding and home-assistant continuity, prioritise the
[primary Graphiti memory component](MEMORY.md), paired with a compact project handoff.
Keep Exa for public research and local retrieval for private code/documents.
GraphRAG remains optional on-demand document analysis. Shared memory integration,
local-model reliability and resource use still need qualification; this decision
adds no installed service or installation gate.

The [verification record](VERIFICATION.md) covers passing Qwen-independent
gateway, retrieval, Graphiti storage and telemetry fixtures. Those checks do not
establish a persistent native model endpoint or end-to-end assistant acceptance.

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
| Session continuity (opt-in) | Browser/coding clients → service-tools memory tools → Graphiti, plus project handoff | `assistant-memory` profile packaged on Neo4j Community with per-user memory groups; Neo4j storage/restart, extraction and recall quality not yet tested; no automatic writer |
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

Without `--serving-profile`, `large` keeps the existing two-GPU Qwen3.8-27B-FP8
source composition and disables image generation in WebUI. With it, every
consumer is rendered from the [serving contract](#serving-contract) instead. `split` explicitly selects the existing one-GPU
Qwen3.5-9B contract and enables SDXL on the other GPU. It never silently exchanges
the chat model to make room for an image. The native serving candidate (below)
must still complete its own packaging and model-specific qualification; this
profile does not substitute it for the existing published image.

Both renders keep application replicas at zero. Use `service plan` with fresh
resource evidence and the selected telemetry composition before activation.
The workstation has 64 GB installed RAM. The retained Linux-visible observation
is about 62 GiB.
Existing split limits exceed the reviewed CPU budget; this change does not lower
them to obtain admission. Shared UI configuration is not physical GPU ownership.

## Native candidate integration

Status, 9 November 2026: the 2026-11 reinstall wiped the op234 runtime
(SGLang 0.5.21, Torch 2.14, TheRock SDK 10.2.0.dev20261020, owner sandbox,
private package, `/home/developer` trees, base image `1d3ebef7…`). Its
successor is the [AI serving stack](../infrastructure/ai-stack/README.md):
the image built from [`stack.lock`](../infrastructure/ai-stack/stack.lock)
(SGLang main `fcb32746…` with the serving patch series, PyTorch 2.16.0a0,
TheRock ROCm 10.3.0.dev20261108), the
[serving profile](../infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json)
(unqualified) and its qualification Job. The op234 material below, including
the `SELECTED-PACKAGE-V2` planner commands and the composition-image blocker,
is historical.

### Serving contract

`service render --serving-profile` reads the AI-stack serving profile, its model
catalogue and `stack.lock`, and builds one contract
(`serving-contract.json`). Every consumer in that render comes from it: the
persistent SGLang Deployment, the gateway policy, the LiteLLM route, the Open
WebUI defaults, the Qwen Code client values (`client.conf`) and the
`service-tools` contract metric. The serving owner's files are read, never
edited, and their `render.py` builds the launch argv and environment, so the
Deployment starts exactly what the qualification Job starts.

```sh
./bin/workstationctl service render /private/service-site.json /private/assistant-contract \
  --experience assistant --mode large --existing-claims /private/service-pvcs.json \
  --serving-profile infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json \
  --runtime-image registry.lan.thespry.family/local/sglang-gfx1201@sha256:DIGEST_FROM_SERVING_OWNER
```

| Contract field | Source |
| --- | --- |
| `contract` (`<profile>.<arm>`), `selection` | Profile name and the arm the profile marks `default`, unless `--arm` names one |
| `model` (alias, checkpoint and draft revisions) | Profile `served_model_name`; model catalogue |
| `tokenizer`, `chat_template` | Catalogue file identities; accepted reasoning values from the template itself (below) |
| `runtime` | `--runtime-image` (lab registry or its `ghcr.io` copy, pinned by digest); SGLang commit, patch series and GPU target from `stack.lock`; digest of the launch argv and environment |
| `parsers` | Profile `--reasoning-parser`, `--tool-call-parser`, `--grammar-backend` |
| `reasoning` | Profile `--default-chat-template-kwargs`; template-accepted efforts; client mapping |
| `budgets`, `concurrency`, `deadlines` | Profile context, pool and running-request limits; `--output-tokens` (default 12,288, at most half the context, unqualified); gateway admission 4; deadline 1,200 s in large mode and 600 s in shared mode, so Think (effort `xhigh`, which runs to the context limit with no output cap) can finish; ordinary replies keep the 12,288 cap |
| `assets`, `requirements` | Model and draft paths from the argv; `ai-models` claim from `k8s/models.yaml`; GPU count, groups and node label from the profile, `render.py` and `stack.lock` |

`status` is `incomplete` while a deployment input is missing, `provisional`
when the inputs are present but the profile is not `qualified` or the catalogue
is not promoted, and `active` only when both are. An incomplete render still
writes every consumer so it can be reviewed, but the Deployment image is
`registry.invalid/…:CONTRACT-INCOMPLETE`, replicas stay zero, and
`evidence.json` lists `missing_inputs`. The catalogue's promoted value is
assumed to be `selected`; any status other than `candidate` or `selected` is
refused until the serving owner names it.

The render cannot silently target the older FP8/32K profile. It replaces the
FP8 LiteLLM route, gateway allowlist, Open WebUI default, memory extraction
model and client model, drops the unused FP8 `sglang-profile` ConfigMap, and
then refuses to write anything if `Qwen3.8-27B-FP8` remains in the rendered
configuration or any layer disagrees with the contract (route, gateway and
client context, output cap, input budget, deadline order, request defaults,
Deployment argv). The gateway reads the rendered `gateway-policy.json` at
startup and replaces its source defaults, so an FP8 request is refused as
`unknown_model`. The 9B split route is kept. The contract needs both GPUs, so
`--mode split` is refused. Virtual keys allowlisted only for the FP8 alias do
not reach the new alias: issue keys for `Qwen3.8-27B-MXFP4` with
`service provision` (the alias is now in its allowlist) and revoke the old ones
as before.

### Reasoning choices in the model menu

`service render` writes `webui-presets.json` from the contract. These are two Open WebUI
presets on the contract model, shown in the model dropdown next to the base model, which
keeps the contract's default (thinking at `low` effort):

| Preset | Request |
| --- | --- |
| `<model> · Think` | thinking on, `reasoning_effort: xhigh`, no output cap: runs until done, the context is full, or the deadline (10 min shared, 20 min large) |
| `<model> · Fast` | thinking off (`enable_thinking: false`) |

Open WebUI keeps presets in its database. Apply them with the admin account's **session
token** (Settings → Account → API keys → JWT Token), saved in a `chmod 600` file. An API
key does not work here: `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` limits `sk-` keys to the
coding clients' endpoints, so the groups and model-import calls answer 403 even for an
admin's key. Run it from a LAN client such as your Mac, not on the workstation itself: the
host's own requests reach Traefik from the pod network, outside the `private-clients`
allowlist, and are refused with 403 before Open WebUI sees them. Each `--group` gets read
access, so presets are never public. Delete the token file afterwards:

```sh
python3 lib/workstation/webui_presets.py apply RENDER/webui-presets.json \
  --url https://chat.example --key-file /private/webui-admin-token --group Parents
```

Re-running updates the presets in place. Admins see every preset.

Each preset also switches on the **Workstation tools** server (`server:mcp:workstation`): Open
WebUI does not pass the base model's default tools on to a preset, and without them a Think or
Fast chat cannot make videos, recall memory or search the web. The server stays visible to every
user, and service-tools still checks each call against the user's own permissions (videos need
picture access), so this changes the default, not who may do what.

### Shared mode (chat and pictures together)

`--mode shared` keeps the two-GPU contract model and adds image generation in the VRAM
it leaves free on both cards. It renders the profile's shared default arm, UQS (`--arm`
can still choose another); the large mode renders the general default, UQL (FP8 KV, the
model's full 262,112-token context). Each arm has its own qualification record on its own
image, so each mode's contract is `active`:

```sh
./bin/workstationctl service render /private/service-site.json /private/assistant-shared \
  --experience assistant --mode shared --existing-claims /private/service-pvcs.json \
  --serving-profile infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json --arm UQS
```

- Arm `UQS` caps the KV pool at 163,840 tokens (81,888 per conversation) and leaves about
  9 GiB free per card after startup (measured 9.04 GiB).
- SwarmUI and its self-started ComfyUI use `gpu-1`. A second ComfyUI
  (`apps/base/comfyui-gpu0`) uses `gpu-0`, the matching gap SGLang leaves there, and
  SwarmUI reaches it as a remote `comfyui_api` backend.
- The rendered gateway policy lists the contract model in `image_models`, so the gateway
  accepts `ready <model> --images` for it. Other modes keep images on the 9B split profile.
- Image access stays per user: bind the Admin and Parents accounts with
  `service browser-bind --images`.
- Shared mode needs the `assistant` or `assistant-memory` experience and a serving
  profile.

Switch to it with `session switch shared` once the host config sets
`SESSION_IMAGE2_DEPLOYMENT=comfyui-gpu0` beside `SESSION_IMAGE_DEPLOYMENT=swarmui`
(docs/AI-SERVICE.md step 8). The switch:
- refuses unless the running contract model is `Qwen3.8-27B-MXFP4` on `gpu-0` and
  `gpu-1`, with each image generator on one of those cards and on different cards;
- checks CPU and memory for all three pods together, before anything stops;
- starts SGLang first, so it sizes its KV pool before any image VRAM is in use, then
  SwarmUI, then the second ComfyUI;
- admits images at the gateway only after all three are ready. If either generator
  fails to start, the session is marked failed and the gateway stays closed.

`maintenance`, `split` and `ai` stop the second generator with everything else.

#### Short videos (shared mode)

In shared mode the assistant can make a 5-second silent clip in the chat. `service-tools` lists
an MCP tool, `generate_video`, when `VIDEO_ENABLED=true` (the shared overlay sets it). The model
calls the tool when someone asks for a video. The clip appears in the chat as an animated WEBP:
Open WebUI stores an MCP `image` result under the requesting user and shows it inline.

**Model and settings.** The model is Wan 2.2 T2V A14B (catalog entry
`infrastructure/ai-stack/models/wan2.2-t2v-a14b.json`):

- the two 14B experts in ComfyUI's fp8-scaled repack;
- lightx2v's 4-step distill LoRAs (`1217`);
- umt5-xxl fp8 as the text encoder;
- the Wan 2.1 VAE.

The operator-owned workflow is `apps/base/shared-api/wan-t2v-workflow.json`. `video.py` checks
it against `video-profile.json` at startup. Its settings:

- 4 steps, split 2 + 2 between the high-noise and low-noise experts;
- euler/simple sampler, shift 5, CFG 1;
- 81 frames at 16 fps.

Callers choose only the prompt and an orientation: landscape 832x480, portrait 480x832 or
square 624x624. No size has more pixels than the measured 832x480.

**Access.** Video access follows picture access:

- only users bound with `service browser-bind --images` (Admin and Parents) may call the tool;
- the gateway must be `ready` with images on the contract model;
- one clip runs at a time.

Open WebUI stores each clip as a chat file, so its `RAG_FILE_MAX_SIZE` cap (4 MiB) bounds the clip; a
5-second clip is about 2.3 MiB at WEBP quality 80. The profile's `max_bytes` matches that cap, so a
larger clip fails as `video_artifact_too_large` instead of an upload error in the chat.

On every later turn Open WebUI resends each clip in the chat to the model as base64. The gateway
accepts chat bodies up to 32 MiB (other routes keep 1 MiB) and replaces each animated WEBP with a
one-line note before the model sees it; still images, such as an uploaded photo, pass unchanged.
A chat with many clips can still reach 32 MiB and fail with `request_too_large`: start a new chat.

Busy, not-ready and permission refusals come back as tool errors the model can explain. Like
every service-tools MCP tool, an account with the Open WebUI `admin` role is refused; use the
daily account.

**Where it runs.** The clip runs on `comfyui-gpu0`, so pictures and videos on `gpu-0` share
one queue. That ComfyUI reads the Wan files from the local-NVMe `creative-models` volume
(`Video/`), not from the NAS. With `--fast-disk` it streams the experts from disk on every
step instead of holding 28.6 GB in RAM.

**Staging the models.** Stage them once on the workstation, as root, then hand them to the
pod's user:

```sh
sudo python3 infrastructure/ai-stack/models/stage-video.py \
  infrastructure/ai-stack/models/wan2.2-t2v-a14b.json \
  /var/lib/rancher/k3s/storage/<creative-models pv>_ai-home-lab_creative-models/Video
sudo chown -R 1000:1000 /var/lib/rancher/k3s/storage/<creative-models pv>_ai-home-lab_creative-models/Video
```

Add `--check` to verify the staged files without downloading. The stage is about 36.8 GB,
fetched from pinned revisions and checked against each file's sha256.

**Measured on 2026-11-12** (R9700, shared mode, a 5-second 832x480 clip, chat idle at
86 tok/s):

| `comfyui-gpu0` setting | Clip | Chat decode during the clip |
| --- | --- | --- |
| ComfyUI 0.45 defaults + `--fast-disk` | 92–102 s | 13–14 tok/s |
| + `--use-ck-attention` | 71–76 s | 21–24 tok/s |
| + `HSA_CU_MASK=0:0-47` (shipped) | 78–82 s | 34–35 tok/s |

- **Why chat slows.** SGLang's two TP ranks run in lockstep, so a clip that saturates one card
  slows every chat token. The compute-unit mask keeps 16 of the 64 CUs out of ComfyUI's reach.
  A 32-CU mask was worse on both measures (104 s, 15 tok/s).
- **ck attention.** It is not bit-identical to PyTorch attention: same-seed frames differ by
  14–17 dB PSNR, and an SDXL picture by 25.8 dB. Side by side, the clips (including faces) and
  the SDXL portraits were of the same quality, and SDXL time did not change (about 9.4 s).
- **Other options tried.** FP8 matmul (`--fast fp8_matrix_mult`) gave no gain, and `--fast`
  with every feature took 203 s. AOTriton flash attention is already in use: 0.28 s per
  attention call, whatever the layout.
- **LoRA choice.** The Comfy-Org v1.1 LoRAs run at the same speed as the `1217` LoRAs.
- **Memory.** Anonymous memory peaked at 7.3 GiB with SDXL and Wan in one process. Each clip
  reads 25–47 GB from the NVMe. The tool returns a WEBP of about 2–4.6 MB.

To check `comfyui-gpu0`'s queue before blaming the tool, use the `/queue` command in the
picture recovery steps below. A cancelled chat request drops its clip from the queue, or
interrupts it if it is the job running now (ComfyUI's `/interrupt` with that `prompt_id` only).

#### Reasoning parameters

The catalogue's `chat_template.jinja` (git blob `c0c686f9…`, 8,952 bytes; the
same bytes as the FP8 checkpoint's template) was read directly:

- thinking is on unless `enable_thinking` is false;
- `reasoning_effort` defaults to `xhigh` and accepts only `xhigh`, `medium`
  and `low` (lines 46–49); any other value, including `high` and numeric
  efforts, raises a template error;
- it also reads `preserve_thinking` (line 116), which clients may not set.

SGLang at the pinned commit (`fcb32746…`) accepts the tiers `none`, `minimal`,
`low`, `medium`, `high`, `xhigh`, `ultra` and `max`, or a float from 0 to 0.99
(`entrypoints/openai/protocol.py`, `ReasoningEffortTier`), and passes the
effort to the template as `reasoning_effort` (`serving_chat.py`
`_apply_jinja_template`). The template, not SGLang, therefore limits the
values, and the gateway's `high` → `xhigh` mapping is still required. The
contract derives that mapping from the template's accepted list.

The pinned `serving_chat.py` merges `--default-chat-template-kwargs` with
`setdefault` after it has moved a request's `chat_template_kwargs.reasoning_effort`
into the top-level field, and at render time those kwargs override the
top-level effort. Because the profile's server default includes
`reasoning_effort: low`, a per-request effort appears not to reach the
template. This is a source reading, not an observation: the serving owner
should confirm it with one rendered prompt (the research note's open item 8)
and, if confirmed, drop `reasoning_effort` from the server default or patch the
merge. Until then the contract's client defaults equal the server default
(`enable_thinking: true`, `reasoning_effort: low`), and sampling is left to the
checkpoint's generation defaults.

### op234 selection (historical)

The serving project records the owner's interim selection until Qwen4 work
resumes. The workstation imports it through
[serving-project.json](../infrastructure/sglang/serving-project.json), which
pins the public records by SHA-256:

| Item | Value |
| --- | --- |
| Serving revision | `3f5ba24` (import `splitk-selection-20261107`, current) |
| Selected arm | Operation 234 `mtp` (physical `kv4`, start 2): three-member cap16 plus S4 split-K down/out |
| Rollback | Operation 232 `mtp` (physical `bf16`, start 1), the previous selection |
| Private pointer | `2fda6127…`; superseded pointers `4e19fc56…` and `fe92916c…` are refused as stale |
| Frozen verdict | `INCONCLUSIVE_PARK`; selection is an owner decision |
| Weights / KV | Qwen3.8-27B Quark MXFP4; explicit BF16 target and draft KV; DFlash width 8; TP2 |
| Limits | Context 131,040; aggregate token pool 262,144; two running requests |
| Native alias | `fp8-serving-screen` (historical name; not the weight precision) |
| Client alias | `Qwen3.8-27B-MXFP4` (proposed; not yet routed) |
| Retained deviations | cap3 recurrent path, `LLVM_PATH`, mandatory startup numerical probes |

The 1 November import of the earlier C4 candidate remains in the tracking file
as a historical import. Its bytes describe that candidate only.

This is a private experimental selection. It is not an installed service.
Natural-answer grades exist for the rollback arm (operation 232) and the
cleanup packet (operation 207). The selected arm has only BetterBench results,
which have no answer-quality oracle. Neither qualifies the browser or
coding-agent workflow; the acceptance suite below does that.

### op234 runtime identity (historical)

The measured runtime was SGLang 0.5.21 at `e00930c5`, Torch 2.14, TheRock
SDK 10.2.0.dev20261020 and Python 3.14.4. Its launch overlaid separately
built site-packages, devel and composition trees on the base image
`1d3ebef7…`, so a version tag cannot identify it. The contract records the
package manifest, model and tokenizer digests, the composition argv digest and
the runtime environment instead.

The checkpoint revision op234 used has no separate `chat_template.jinja`. Its
template is in `tokenizer_config.json` (`b11349aa…`). The template:

- opens the thinking block in the generation prompt, so raw output has no
  opening `<think>` tag;
- accepts `reasoning_effort` values `low`, `medium` and `xhigh` only, and
  raises an error for others, including `high`. The gateway therefore sends
  `high` as `xhigh`, so clients that offer `high` do not get an error;
- renders tool calls as `<tool_call><function=…><parameter=…>` XML and tool
  results as `<tool_response>` blocks.

The AI-stack revision `5233554c…` ships `chat_template.jinja`, listed in the
[model catalog](../infrastructure/ai-stack/models/qwen3.8-27b-mxfp4.json).
The serving research found that it renders identically to Qwen's template and
reads `enable_thinking`, `reasoning_effort` and `preserve_thinking`. Its
`chat_template.jinja` has the same git blob as the template read for this
section, so the accepted values and the `high` to `xhigh` mapping hold for
`5233554c…` (see [Reasoning parameters](#reasoning-parameters)).

### Prepare the integration plan (historical)

The planner is offline. It authenticates the pointer against the imported
selection, then the package files through the pointer's manifest digest:

```sh
./bin/workstationctl service candidate-plan /private/CANDIDATE-POINTER.json \
  /private/assistant-candidate-plan --pointer-sha256 EXACT_64_HEX_DIGEST \
  --package-root /private/SELECTED-PACKAGE-V2 --output-tokens 4096
```

It writes these files:

- `candidate-contract.json`: allowlisted identities, limits, request defaults,
  parser status, deviations and rollback.
- `candidate-render.json`: inactive gateway, LiteLLM, Open WebUI and client
  values, all derived from that contract, with cross-layer checks.
- `candidate-sglang.yaml`: an inactive persistent Deployment (zero replicas).
- `candidate-runtime.json`: deviations, blockers, readiness and rollback.
- `integration-plan.json`: the comparison with the active profile and the
  evidence levels.

It refuses a stale or superseded pointer, mixed arms, CLI and resolved-limit
drift, a changed package member and any launch flag outside the reviewed set.
It never prints credentials, machine paths or arbitrary CLI values. The status
shows the remaining gate: `blocked-missing-evidence` without `--package-root`,
otherwise `blocked-integration`. `deployment_ready` stays false.
`runtime_observed` stays false until the acceptance suite runs on the native
endpoint.

The active profile (`Qwen3.8-27B-FP8`, 32,768 context, 4,096 output) is not
changed. Rollback before activation is to leave the render unapplied and
re-render the active profile with `service render`.

### Request profile and transport

The baseline recipe is thinking enabled at low effort with greedy decoding.
The selected arm was throughput-measured with a sampled profile (temperature
0.7, top_k 20, top_p 0.95); the menu labels that as a separate recipe.

| Layer | Behaviour at the pinned versions |
| --- | --- |
| Open WebUI 0.12.5 | Copies `DEFAULT_MODEL_PARAMS` to the top level of each request; sends `max_tokens`; drops prior reasoning on the next turn |
| Gateway boundary | Accepts `reasoning_effort` `low`, `medium`, `high` and `xhigh`; sends `high` as `xhigh`, the nearest level the template accepts. Accepts `chat_template_kwargs` with only a boolean `enable_thinking`; refuses other fields |
| LiteLLM 1.106.2 | `drop_params: false`; forwards `reasoning_effort` and `max_completion_tokens` unchanged; passes `top_k` and `chat_template_kwargs` through `extra_body` |
| SGLang 0.5.21 | Passes `reasoning_effort` and `chat_template_kwargs` to the template |
| SGLang `fcb32746…` (AI stack) | Same, plus server defaults from `--default-chat-template-kwargs`; see [Reasoning parameters](#reasoning-parameters) for the per-request effort caveat |

Without parser flags, SGLang returns reasoning and tool-call markup in
`content`, and `finish_reason` stays `stop`. The persistent descriptor adds
`--reasoning-parser qwen3 --tool-call-parser qwen3_coder`. These are the
parsers of the existing service profile and match the template format. The
descriptor records them as a labelled deviation from the measured CLI. The
frozen serving benchmark contract is unchanged.

Admission and execution are separate limits. The gateway admits four
concurrent requests, and the native runtime runs two. With four active
requests, two wait in the native queue and count against the client deadline.

### Persistent endpoint blockers

These rows describe the op234 package (historical). For the successor, the
AI-stack image replaces the composition image, `render-fetch.py` stages and
verifies the model, and the serving profile records what still needs a GPU
measurement.

| Missing artifact or capability | Affected step |
| --- | --- |
| A runtime image reproducing the measured composition (base `1d3ebef7…`, site-packages, devel and composition trees, package overlays), passed as a local digest | Startup, every native qualification |
| The MXFP4 checkpoint and DFlash draft staged at the rendered `/models` paths and checked against the inventory | Startup |
| A persistent entry that runs the package's startup numerical probes without the campaign client and shutdown (an owner-reviewed serving change) | Readiness |
| Fresh `service plan` capacity with collected memory evidence (64 GB installed; about 62 GiB was last visible to Linux) | Activation |
| Native assistant API and quality-menu results through the gateway and the browser | Activation |

The serving launcher stops its runtime when its bound client finishes. Its
owner, deadline and release checks stay unchanged.

### Acceptance suite

The menu is [assistant-quality.json](../infrastructure/sglang/assistant-quality.json).
It has 15 unique tasks: the eight original cases and seven held-out tasks.

- Python and Go bug fixes, each with real tests.
- Terraform and Ansible compatibility fixes, checked with `terraform validate`
  and `ansible-playbook --syntax-check`.
- Kubernetes troubleshooting from supplied evidence.
- Version selection.
- Incident analysis.

Repetitions are counted separately from tasks. Human-rubric items stay
`needs-review` until a person decides.

```sh
./bin/workstationctl service acceptance plan /private/acceptance-plan.json \
  --model Qwen3.8-27B-MXFP4 --tokenizer-dir /private/model-tokenizer
./bin/workstationctl service acceptance run /private/gateway-run.json \
  --boundary gateway --base-url https://API_HOST --model Qwen3.8-27B-MXFP4 \
  --key-file /private/virtual-key
```

Each run names one boundary: `direct`, `gateway` or `webui-api`. An
unreachable endpoint gives `NOT RUN`. The report contains the following:

- Task pass rate, tool round-trip success, grounded-answer accuracy,
  abstention, truncation and time to a usable answer.
- Request and response hashes, finish reasons, token counts and the elapsed
  time for each attempt.

Code and configuration answers pass only when the task's real tests or
validators pass inside the network-free Linux sandbox. That sandbox uses the
serving project's pinned grader design: bubblewrap with every namespace
unshared, a cleared environment and a timeout. On other hosts, those tasks
are `NOT RUN`. The coding-client and browser scenarios in the menu are manual
checklists for the preferred client (Qwen Code): repository edits, a
failed-test-to-fix cycle, interrupt and resume, and context compaction.

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

The adapter runs one picture at a time, or two in shared mode (`IMAGE_CONCURRENCY=2`
on `service-tools`), one per SwarmUI backend. Each running picture owns its own
marker under `/state`: `image-inflight.json` for the first and `image-inflight-1.json`
for the second. A marker no running request owns blocks new pictures in every mode
(HTTP 503 `image_backend_needs_operator_check`, no `Retry-After`; a full adapter
returns 429 `image_busy` instead). `/health` reports `image_active`, `image_slots` and
`image_recovery_required`. A cancelled picture also keeps its marker, because
SwarmUI acknowledging an interrupt does not prove the GPU work stopped.

To recover, confirm both backends are idle, then remove the stale marker:

```sh
kubectl -n ai-home-lab exec deploy/comfyui-gpu0 -c comfyui -- /SwarmUI/dlbackend/ComfyUI/venv/bin/python -c \
  "import json,urllib.request; q=json.load(urllib.request.urlopen('http://127.0.0.1:7821/queue',timeout=5)); print(len(q['queue_running']), len(q['queue_pending']))"
kubectl -n ai-home-lab exec deploy/service-tools -c tools -- ls /state      # which image-inflight*.json
kubectl -n ai-home-lab exec deploy/service-tools -c tools -- rm /state/image-inflight-1.json
```

Check SwarmUI's own backend the same way (its server log shows no running generation).
Restarting either image backend while pictures are in flight leaves a marker; the
session tooling only starts them before images are admitted.

Requested sizes snap to SDXL's native sizes: an exact `512x512`, or the nearest aspect
of 1024x1024, 1152x896, 1216x832 or 1344x768 (and their portrait forms). Open WebUI's
`generate_image` tool lets the model choose any `WIDTHxHEIGHT`; refusing those made it
retry in a loop. None of the sizes has more pixels than the measured 1024x1024.

## Speech and later model experiments

Voice input is local: Open WebUI's built-in faster-whisper (`Systran/faster-whisper-base`,
CPU int8) loads the pinned, hash-checked model from the RAG models volume
(`rag stage-whisper`, docs/RAG.md), and the microphone is allowed for every account.
Audio never leaves the workstation. Server speech synthesis is not deployed; browser
speech recognition can use external services and is not a local-ASR substitute.
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
