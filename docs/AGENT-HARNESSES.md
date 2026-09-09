# Local agent harnesses

This guide describes optional client-side agent harnesses for the qualified local
SGLang service. A harness runs on the developer's Mac or Linux client. It is not
installed by the ISO, a Kubernetes workload, or a host service. The bundle
generator writes configuration only. It does not install a client, start a
profile, expose SGLang, download a model, or enable a workload.

The preference order is Qwen Code, DeepSeek Harness (DSH), then Hermes. This
order is a local qualification plan, not a claim that one client is more capable
for every task. Each client uses the same OpenAI-compatible model endpoint and
the same selected model. There is no automatic fallback between clients.

## Preconditions and boundary

Do not configure a live endpoint until the selected SGLang image, model, tool
parsing, context accounting, and profile have passed the target-machine
qualification in [MODELS.md](MODELS.md). The checked-in SGLang Deployment starts
at zero replicas and remains pending qualification. This guide does not make an
endpoint available.

The normal client path is a temporary, owner-operated loopback tunnel. It is not
an Ingress, NodePort, or broad cluster credential. On the client Mac, after the
workload is qualified and a reviewed Kubernetes context has been selected, run:

```sh
kubectl --context <reviewed-context> --namespace ai-home-lab \
  port-forward svc/sglang --address 127.0.0.1 18000:30000
```

The command binds only the client loopback address and forwards local port 18000
to the internal Service port 30000. It uses the Kubernetes API and the owner's
`pods/portforward` RBAC permission. That API tunnel is not ordinary Pod-to-Pod
traffic, so the SGLang NetworkPolicy that allows only Open WebUI does not grant
or constrain this path. Do not grant a harness a kubeconfig or broad token to
make the tunnel work. An owner may instead use a reviewed SSH local tunnel to a
separately operated port-forward process. Do not expose either tunnel on a LAN
interface.

An HTTPS endpoint is a separate reviewed design. It requires real authentication
and an owner-supplied credential. This repository does not add an SGLang Ingress,
an API token, a Secret, or a credential distribution path.

## Generate a client bundle

The portable command works on the client Mac or Linux machine and writes only to
a new output directory. Use absolute paths for ACP (Agent Client Protocol)
registration, because the IDE may start the process with another working
directory.

```sh
/absolute/path/to/ArchLinuxThreadripperAI/bin/workstationctl \
  --config /absolute/path/to/config/workstation.conf \
  agent configure /absolute/path/to/agent-bundle
```

The generated manifest records the preference order `[qwen, dsh, hermes]` and
the selected values. It generates native configuration for Qwen Code, DSH, and
Hermes, but does not install any of them. The command rejects an existing bundle
directory. The manifest also records a SHA-256 hash for every native file. The
launcher verifies the selected file before it starts a client. If the client or
user changes that file, the launcher rejects the bundle. Generate a fresh bundle
and recheck it; do not edit a checked bundle or delete an existing client session
directory as a recovery shortcut.

`config/workstation.conf` has these non-secret defaults:

```sh
AGENT_HARNESS=qwen
AGENT_BASE_URL=http://127.0.0.1:18000/v1
AGENT_MODEL=Qwen3.8-27B-FP8
AGENT_CONTEXT_TOKENS=32768
AGENT_MAX_OUTPUT_TOKENS=4096
```

`AGENT_HARNESS` selects the default only. It never starts another harness if the
selected client is absent or fails. The model name must match the reviewed
SGLang `SERVED_MODEL_NAME`; it is not a request to change the deployed model.
The context limit is a client request budget. It does not prove that the server
has capacity for the request. `AGENT_MAX_OUTPUT_TOKENS` is written as a request
limit for Qwen Code and DSH. Hermes 0.21.1 exposes output-token limits as a
provider-owned setting, not user configuration. Its generated profile receives
the 32,768-token context cap but cannot enforce this output limit. Do not make a
strictly matched Hermes comparison until the server can enforce the same output
budget; record it as an unqualified limitation instead.

Set `WORKSTATION_AGENT_API_KEY` only in the client process environment. The
native client configurations refer to that environment variable; neither they
nor the bundle stores a credential. Do not put it in `workstation.conf`, a
command line, a Kubernetes manifest, or a repository file. A bundle for the
loopback tunnel can use its explicit, non-secret local-tunnel marker when no
credential is set. That exception is for the loopback tunnel only. An HTTPS URL
requires `WORKSTATION_AGENT_API_KEY`; the launcher does not create a placeholder
credential or silently downgrade the connection.

## Start one client

Install the selected client manually from its pinned upstream source before this
step. The project does not run npm, pip, or installer scripts for harnesses.
The reviewed sources are [Qwen Code 0.23.2 at
`f56de980b316cd5410f067fbb62357481ebd66b8`](https://github.com/QwenLM/qwen-code/commit/f56de980b316cd5410f067fbb62357481ebd66b8),
[DSH at `c389f96bf3a9b6807cb71ed6bdad5849be0df6d8`](https://github.com/deepseek-ai/deepseek-harness/commit/c389f96bf3a9b6807cb71ed6bdad5849be0df6d8),
and [Hermes Agent 0.21.1 at
`13fb5e1eceba51fc45a48b5d95a357e144d42689`](https://github.com/NousResearch/hermes-agent/commit/13fb5e1eceba51fc45a48b5d95a357e144d42689).
Recheck the source-specific installation instructions and the pin before
installing an update. The bundle source metadata is not runtime attestation.
Verify the installed client version and source yourself, and do not accept an
automatic floating update.

Qwen Code disables automatic update in its generated configuration and disables
usage statistics and telemetry in both that configuration and its process
environment. DSH starts with `DSH_TELEMETRY_MODE=DISABLED`. These controls do
not make tool use or optional web features offline. Local inference means that
the model endpoint is local; a client browser, web-search tool, MCP server, or
other client extension can still make network requests.

Use the generated bundle and name the client explicitly:

```sh
/absolute/path/to/ArchLinuxThreadripperAI/bin/workstationctl \
  agent launch /absolute/path/to/agent-bundle qwen cli
```

For PyCharm or another ACP-capable IDE, register an external agent command with
absolute paths. For Qwen Code, use:

```text
/absolute/path/to/ArchLinuxThreadripperAI/bin/workstationctl agent launch /absolute/path/to/agent-bundle qwen acp
```

Replace `qwen` with `dsh` or `hermes` only after choosing that client. `cli` is
the default mode when it is omitted. If both optional arguments are omitted,
the launcher selects `AGENT_HARNESS` and `cli`; it does not try the next client
in the preference order. The pinned DSH has no interactive CLI profile, so
`agent launch ... dsh cli` and an implicit DSH/`cli` selection are rejected.
Use DSH only through ACP:

```text
/absolute/path/to/ArchLinuxThreadripperAI/bin/workstationctl agent launch /absolute/path/to/agent-bundle dsh acp
```

No DSH web daemon is added. `acp` starts the selected client's ACP transport;
it does not give the IDE, model, or client extra host permissions.
Use the IDE's normal external-agent registration procedure. Do not register an
unreviewed shell wrapper that changes the bundle or credentials.

For Qwen Code, the launcher sets a process-local
`QWEN_CODE_SYSTEM_SETTINGS_PATH` to the checked bundle configuration. This
prevents a project setting from replacing the reviewed model route. If the
operating system already has a Qwen system policy, or the caller set that path
to another value, the launcher refuses to start. It does not overwrite either
policy. On a managed client, an owner must merge the required settings into the
managed policy and use its approved launch path; this wrapper continues to
refuse replacement of that policy. Existing legacy profiles and Codex
configuration are unchanged.

## Permissions, tools, memory, and RAG

The launcher requests Qwen Code's `default` approval mode and DSH's
`workspace-write` permission mode. The generated Hermes configuration requests
manual approvals. Confirm each client's actual prompt and filesystem behaviour
during qualification. In particular, Hermes manual approval is not a universal
per-file write approval or a sandbox guarantee. Do not enable unattended shell
execution, broad filesystem write access, browser automation, or unrestricted
MCP servers during initial qualification. A client approval prompt is a
usability control, not a security boundary. The host account, sandbox,
filesystem permissions, SSH configuration, and cluster RBAC remain the security
boundary. See the upstream [Qwen Code settings
policy](https://github.com/QwenLM/qwen-code/blob/98a9c964158697dd5631d15a62174684ff7bbb53/docs/users/configuration/settings.md),
[DSH permission presets](https://deepseek-harness.github.io/deepseek-harness/en/reference/subsystems/permission-presets),
and [Hermes configuration
example](https://github.com/NousResearch/hermes-agent/blob/13fb5e1eceba51fc45a48b5d95a357e144d42689/cli-config.yaml.example)
before changing these modes.

The generated clients do not automatically inherit the RAG pilot, an Open WebUI
collection, persistent agent memory, or database access. The RAG pilot has no
IDE tool endpoint. Add an MCP server only after its tool scope, authentication,
input handling, audit record, and read/write permissions have been reviewed. A
first MCP integration should be an authenticated, read-only retrieval tool; it
must not expose raw Chroma data or Kubernetes credentials.

All clients address the same SGLang model instance. A second harness does not
create another GPU allocation, extra model replica, or independent VRAM pool.
Serialize comparisons and stay within the selected profile's request and memory
budgets. Stop or unload the AI profile before gaming as described in
[HOME-LAB.md](HOME-LAB.md).

## Qualification scorecard

Qualify each harness separately. Use the same model revision, endpoint, context
limit, sampling settings, allowed capabilities, tool tasks, repository fixture,
and client hardware for every comparison. Qwen Code and DSH must also use the
same output limit. Hermes cannot currently receive that user-configured cap, so
do not rank its result against those matched runs. Run one harness at a time. Do
not add published benchmark percentages or results from different budgets.

| Check | Record | Keep only if |
| --- | --- | --- |
| Basic chat and code task | Completion quality, tool-call format, first-token and total latency | Output is correct and tool parsing is stable |
| Tool round trips | Calls, arguments, tool-result follow-up, failures | Calls are correctly bounded and recover from a rejected tool |
| Permission handling | Approval prompt, denied command, filesystem scope | Denial stops the action without a bypass |
| Context compaction | Token accounting, summary quality, lost constraints | Important task constraints survive compaction |
| Resume and cancellation | Resume state, cancellation latency, duplicate action count | A cancelled or resumed task does not repeat a side effect |
| Local endpoint behaviour | Connection/authentication errors, retries, disconnect recovery | The client fails closed when the tunnel or credential is absent |
| Resource use | Client CPU/RAM, server request count, GPU/pod memory, output length | It remains within the qualified SGLang and host budgets |

Record client source/version, bundle manifest, model/image revisions, command
mode, timestamps, tool policy, endpoint type, and raw result artifacts. Measure
median and tail latency over repeated comparable runs. Keep failures and
rejections in the report. A harness that is convenient but cannot reliably
preserve permissions or resume state is not qualified for unattended work.

## Recovery

Stop the local port-forward or SSH tunnel, close the client, and remove the
client's native credential when it is no longer needed. Do not delete a bundle
or model volume as a recovery shortcut. To restore the baseline, select the
existing approved client configuration and leave the SGLang Deployment at zero
replicas unless it has already passed the documented workload promotion process.
