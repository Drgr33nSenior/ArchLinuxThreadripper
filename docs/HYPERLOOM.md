# Hyperloom development tools

Hyperloom is an opt-in optimization tool, separate from monitoring and the
serving runtime. The workstation integration installs its complete `runtime`
extra in a private environment. It does not install Torch, ROCm, SGLang, vLLM
or Ray, change a serving image, launch agents or select a production default.

## Pinned release and actual applicability

`infrastructure/hyperloom/versions.lock` pins Hyperloom **1.1.5**, source commit
`7c0708ec9b75e70c4d9ce5309608f378b7fd8f69`, the official release wheel URL and
SHA256. `infrastructure/hyperloom/runtime-linux-py314.lock` pins all 47 runtime
wheels and their hashes for Linux x86_64 / CPython 3.14. The initial wheel set
is about 281 MiB; its installed environment is about 775 MiB. Most download
bytes belong to the provider CLI binaries. No provider credentials are copied
or requested during installation.

The [1.1.5 release](https://github.com/AMD-AGI/Hyperloom/releases/tag/v1.1.5)
adds explicit R9700 support. The released source maps `r9700` to `gfx1201`,
64 compute units and the `gfx12` Magpie runner. Its framework gate currently
allows **vLLM**, and rejects **SGLang** and **custom** for that runner. The
targeted-build/AITER path is explicitly unvalidated on this board. General
SGLang support elsewhere in Hyperloom does not remove that particular gate.
Our existing SGLang workload runs independently of Hyperloom.

This was checked against the release wheel and current upstream commit
`f418170261a59665d31ab37428b962135f8bd27c` on 30 October 2026. The latter still
uses a vLLM-only R9700 policy. See the
[merged R9700 change](https://github.com/AMD-AGI/Hyperloom/pull/1782) and
[released runner gate](https://github.com/AMD-AGI/Hyperloom/blob/7c0708ec9b75e70c4d9ce5309608f378b7fd8f69/src/hyperloom/inference_optimizer/gpu_types.py).
The installed package's `status` output is authoritative for its own release;
it does not claim that a workload has been physically qualified.

Hyperloom is MIT licensed. Provider agents have separate terms. Some vendored
example kernels are Apache-2.0; see the pinned
[third-party notices](https://github.com/AMD-AGI/Hyperloom/blob/7c0708ec9b75e70c4d9ce5309608f378b7fd8f69/THIRD_PARTY.md).
Dependency licences remain in the installed wheel metadata and licence files.

## Installation and offline use

Run these commands on the Linux workstation, not the Mac controller. Choose
fresh absolute directories with a private, caller-owned installation parent.
The SDK must already provide CPython 3.14, `venv` and `ensurepip`. No system
Python package installation is needed. The normal developer user runs the tool.

```sh
export WORKSTATION_PYTHON=/usr/bin/python3
# Fetch only authenticated binary wheels through GitHub and public PyPI:
bash bin/workstationctl performance hyperloom fetch /home/developer/hyperloom-download-01
# Install completely offline; refuse any missing, extra or changed wheel:
bash bin/workstationctl performance hyperloom install \
  /home/developer/hyperloom-download-01/wheelhouse \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
bash bin/workstationctl performance hyperloom status \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
bash bin/workstationctl performance hyperloom help \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
```

The installed prefix contains `venv/bin/hyperloom`, `venv/bin/kernelforge`, the
provider SDKs, an installation receipt, an empty private home and the checked
release wheel. The project wrapper verifies installed Hyperloom code/data
against that wheel before invoking offline tools. A failed install retains its
prefix and failure receipt; choose a new prefix after diagnosis.

To render an existing Hyperloom breakdown without an LLM call:

```sh
bash bin/workstationctl performance hyperloom report \
  /home/developer/.local/share/workstation/hyperloom/1.1.5 \
  /absolute/session_breakdown.json /absolute/new-session-report.md
```

The offline wrapper uses an empty private home, drops inherited credentials,
sets `--no-llm`, and denies network connections, child processes and GPU device
opens in the reporting process. It does not translate workstation benchmark
results into a fictitious Hyperloom session. The upstream CLI remains available
for separately reviewed operation; wrapper commands do not start optimization.

## GPU campaign boundary and rollback

### Delegate a registered owner packet

`sglang-owner` connects the controller to the existing native owner procedure.
It does not create a resource grant, choose a kernel, change request bodies or
replace the owner's lock, supervision and independent closeout. It is separate
from Hyperloom's upstream runner gate. The adapter completed one bounded
two-start, eight-request TP2/C1 physical benchmark on the selected Qwen3.8-27B
profile. This validates that handoff and reporting path; general optimization,
deployment and full model quality remain unqualified.

Check a freshly prepared owner packet from the Mac controller:

```sh
WORKSTATION_PYTHON=/usr/local/bin/python3.11 bash bin/workstationctl performance hyperloom sglang-owner \
  /absolute/fresh-controller-session \
  --authorization-sha256 <reviewed-RUN-AUTHORIZATION-sha256> \
  --selection /absolute/Spry.ai-rdna4-serving/provenance/CURRENT-SESSION-SELECTION.json \
  --boot-id <current-target-boot-id>
```

This check imports no owner code and makes no connection. It verifies the
controller handoff, not physical readiness. The native owner still verifies
the complete source seal, actual configuration, current boot and admission.
Historical sealed or consumed packets refuse before any launch.
The output separately reports `controller_runtime_ready`. The currently
configured Mac Python 3.11.1 lacks the unchanged collector's `tarfile` data
filter; execution refuses before consuming the packet. Use a declared and
qualified compatible controller SDK before execution. Python 3.14.7 passed
this capability check in native CPU tests. Do not remove the extraction filter
or substitute the workload's remote interpreter for the controller interpreter.
The retained successful controller procedure used `/opt/local/bin/python3.11`.
That existing owner runtime currently reports Python 3.11.6 and passed the
full CPU suite with the real capability gate. For owner execution, set
`WORKSTATION_PYTHON=/opt/local/bin/python3.11`; the project's configured
`/usr/local/bin/python3.11` remains unchanged. Current interpreter and `tarfile`
hashes are recorded in `test-results/hyperloom-owner.wKAlWXzQ/owner-controller-runtime.json`.

The existing `RUN-AUTHORIZATION.json` must contain `hyperloom_controller` with
`files` (SHA256 pins for the seven controller Python files), `binding_sha256`,
`identities_sha256` (the collector's `PACKAGE-IDENTITIES.json`),
`phase_seconds` (`upload`, `launch`, `collect`, `closeout`) and `campaign_seconds`.
These fields are part of that reviewed authorization, not a separate grant.
The current selection hash must match the authorization. The summed supervised
stage allowances include termination/reap headroom; they are not a filesystem
quota or a bound on later offline report generation.
The delegated procedure uses 5,400 owner seconds and two model starts. It
supports the existing four-request benchmark and thirty-request quality
schedules. External request counts must equal twice the request count in the
author's sealed, hash-checked `review/FINAL-SETTINGS.json`: eight or sixty.
A smaller authorization refuses; it cannot shorten the consumed schedule.
Other schedules require separate owner-side qualification.

For the fixed C1 benchmark, report groups are projected from the sealed client
intent and response timestamps. The first request is the frozen excluded
warmup; all three remaining requests are retained. Raw summaries remain intact.
The older reader adds speculative-response details while the client adds a raw
HTTP hash. The bridge checks both against the sealed raw response and compares
every other field exactly. This translation changes no model arithmetic or
numerical tolerance. Rates include the complete non-stream HTTP request wall,
including prefill and instrumentation; isolated decode, TTFT and ITL remain
unavailable.

Only after the fresh packet passes its normal owner preflight, repeat the
command with `--execute-owner --output /absolute/fresh-reports --wheel
/absolute/hyperloom_inference_optimizer-1.1.5-py3-none-any.whl`. The wheel must
match `versions.lock`. The controller loads only its pure report writer; it
needs no macOS installation of the Linux runtime or provider SDKs.

Execution consumes the nonce before the first child. It delegates upload,
launch, collection and the appropriate independent closeout. Launch failure
still attempts collection and failure closeout. Collection failure still
attempts closeout. Failed stages and Python interrupts are retained without automatic
retry. A failed closeout records `closeout_observed:false`; do not infer release
from a free lock or attempt a replacement campaign. Use the existing owner's
recovery procedure. Reports are imported only after all four stages pass and
retain `qualification:false`. Fresh model quality needs its own matching join.

Rollback requires no serving change: omit `--execute-owner` and continue using
the selected owner workflow with its compatible declared interpreter. Bypassing
the adapter does not fix the collector ABI. Keep consumed packets and failed receipts.

### SGLang ROCm 10.2 bridge

The owner-selected target runtime is the source-built ROCm
`10.2.0.dev20261020+git535d2990`, Torch
`2.14.0+git2e66fe81.rocm10.2.0` and HIP `7.17.26416`. The retained serving
record identifies SGLang 0.5.21 and RCCL 2.32.3. Triton's base distribution
still labels itself `3.8.0+git669b31ac.rocm10.1.0`; the effective compiler and
source inventories must be checked separately. The host's Arch ROCm packages
are a separate installation. The owner's production-stack selection does not
establish production qualification of the Hyperloom adapter.

`infrastructure/hyperloom/sglang-rocm102.json` records the bridge's expected
SDK, Torch, HIP and SDK inventory. The following offline command imports the
existing `native-http-fp8-sprint01-v1` owner's `RUN-SUMMARY.json` using the
installed Hyperloom report writer:

```sh
bash bin/workstationctl performance hyperloom sglang-import \
  /home/developer/.local/share/workstation/hyperloom/1.1.5 \
  /absolute/STACK-PROOF.json /absolute/RUN-SUMMARY.json /absolute/new-import \
  --stack-sha256 <reviewed-proof-sha256> \
  --summary-sha256 <reviewed-summary-sha256>
```

Use the SHA256 values from reviewed evidence. Each timing group gets its own
`benchmark_report.json`; concurrency levels are never pooled. Token counts
must agree with the native input/output IDs. Warmups and other non-timing
requests stay excluded and are listed in `IMPORT.json`. Throughput uses the
native HTTP group wall, including prefill and instrumentation. It is not decode
throughput. Unavailable TTFT, inter-token latency, decode throughput and energy
remain null. These historical measurements are not a new performance run.

The importer authenticates the two input files independently. It does not
establish that they belong to the same physical run, authenticate the model or
complete SGLang source closure, or replace native quality and cleanup readers.
Every imported report therefore has `diagnostic_only: true`,
`qualification: false` and a failing selection quality gate, even if an input
claims qualification. It cannot qualify or select an optimization.

The report writer is loaded directly from the wheel-verified installation.
Importing its enclosing executor package would initialize an executor and
attempt a Magpie subprocess probe. The offline guard continues to deny that
execution, network access and GPU device access.

For one sealed native owner run, use the binding command instead:

```sh
bash bin/workstationctl performance hyperloom sglang-bind \
  /home/developer/.local/share/workstation/hyperloom/1.1.5 \
  /absolute/owner-evidence /absolute/new-bound-import \
  --seal-sha256 <reviewed-FINAL-ARTIFACTS-sha256> --arm bf16
```

`--arm` selects the owner's historical `bf16` or `kv4` label. The label does
not establish the model's weight or cache precision. The reviewed SHA256
authenticates the owner's `FINAL-ARTIFACTS.sha256`; the reader then checks every
member it consumes. It does not reverify unrelated members such as compiler
outputs. `IMPORT.json` lists the consumed member hashes.

The binding follows the prepared package, controller artifact receipts,
embedded client summary, native worker identities, parsed configuration,
model revision/inventory, source inventory and complete request schedule.
It also requires the same-session independent release, all-user device-holder
visibility, zero GPU allocations, reaped owner child and absent owner scope.
The release observation must follow workload completion on the same boot.
Unfilled session/spec fields are allowed in configuration templates; native
runtime records must carry the actual identities.

Bound imports set `owner_run_binding_verified: true` and
`cross_run_stack_binding_verified: true`. The model/source identities are
bound to the historical owner's attestation; model files and loaded libraries
are not freshly rehashed. The output preserves the native quality verdict and
keeps `qualification: false`, `diagnostic_only: true` and a failing selection
gate. Independent CPU grading outside that owner seal is not inferred.
No imported status enables automatic optimization or authorizes another launch.

To attach a reviewed retained native CPU quality join, add both options:

```sh
  --quality /absolute/COORDINATOR-FINAL-QUALITY-JOIN.json \
  --quality-sha256 <reviewed-quality-file-sha256>
```

The quality join must name the same workload seal and controller-result hash.
All fourteen frozen arm/ordinal/case grades must pass, with native CPU release
validated. Failed, incomplete, duplicated or foreign-run grades refuse the
import. The importer reads the reviewed verdict; it does not execute supplied
graders, generate replacement answers or widen an oracle.

Attached task results appear under `owner_binding.external_quality`, with
`task_checks_passed: true` on the index and reports. Native quality fields,
sampling-verification gaps, unmatched timed work and deferred review remain
visible. Task success does not change the failing selection gate or establish
full model/runner qualification. Revalidate retained quality with its existing
read-only reader before reviewing the quality-file digest for a new import.

A launch-capable R9700/SGLang runner, full quality/lifecycle admission and
physical runner qualification remain unfinished.

## Retained physical benchmark: 30 October 2026

The protected owner completed two fresh starts of the same selected
Qwen/Qwen3.8-27B-FP8 configuration on two R9700 GPUs, with tensor parallelism 2
and concurrency 1. Each start used one excluded warmup and three measured
requests, each with 2,047 input tokens and 1,024 forced output tokens. The
configuration used DFlash with eight draft tokens, FP8 model weights and BF16
target/draft attention KV. The historical `bf16` and `kv4` arm labels identify
the two runs; neither arm used KV4 compression.

| Start | Median output tokens / HTTP second | Median request time |
| --- | --- | --- |
| 1 | 120.77456 | 8.47861 s |
| 2 | 120.97877 | 8.46429 s |

These rates include prefill and the complete non-stream HTTP request. The
pooled rate was 120.84857 across six measured requests and 6,144 output tokens.
The 0.16909% difference between start medians measures repeatability of the same
configuration. It is not an optimization gain. Including both warmups, eight
external requests delivered 8,192 output tokens. Internal startup work is
separate. Isolated decode, TTFT, inter-token latency and energy were not measured.

All three corresponding measured output sequences matched exactly (3,072
aligned output IDs). The warmup sequences first differed at output index 536
(zero-based). Raw evidence is retained, the cause remains unresolved, and no
logprobs were checked. Full numerical and model qualification remain open.
Timing exclusion does not waive the warmup discrepancy. This timing run did
not repeat task, natural-stop or cancellation/recovery qualification.

Native evidence confirmed graph replay and completed commits on both ranks.
All four owner stages passed and six diagnostic Hyperloom reports were
imported. Independent cleanup and a final host check verified owned processes
gone, no GPU holders or allocations, diagnostic lock released, node Ready and
unchanged network policy. Graceful server shutdown was not newly qualified.

The model revision was `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a` and both
arms used selected kernel bytes with SHA256
`b101e71751ce995b23b327dd3ee87662eb0efd3ef7dc2d35387d1acc58d8e069`.
The base image manifest was
`1d3ebef7da1df35f0c103ed038521933ebaeaf6f8c8d710168d8c294efdc59d9`.
The local evidence root is `test-results/hyperloom-benchmark.7lcBptGy`, whose
`FINAL-ARTIFACTS.sha256` hashes to
`7d2b050b2d7f2e9e42fea6012d2e5ca82d4e466fc0d313fad0a3a7c665d227b0`.
Its `HANDOFF.md`, `ANALYSIS.json` and raw owner evidence give the full identities,
per-request results and limitations. Raw evidence remains local and is not
included in the source distribution. Consumed or sealed launchers must not be
rerun. This result validates the owner/reporting integration; autonomous
Hyperloom tuning and a new performance gain were not demonstrated.
The upstream R9700 framework gate remains intact. Do not invoke the upstream
default launcher as a substitute: it starts a separate server and client and
does not implement this project's owner lifecycle. Continue through a fresh
existing owner package; do not reuse historical consumed grants.

Installation/import/help success is not a measured gain or permission to run an
autonomous campaign. Preserve the existing source/image/model identities,
diagnostic lock, GPU admission, request protocol, numerical/task gates and
cleanup supervision. Review agent credentials, budgets, working copies,
profiling and provider costs before a campaign. Do not run upstream setup or
cleanup recipes against an active serving environment. Do not spoof an Instinct
GPU identity or remove the framework gate to make SGLang launch.

To use Hyperloom with our SGLang workload, first supply and qualify the missing
R9700/SGLang runner against the existing owner workflow. The vLLM route is an
alternative experiment, not a replacement authorized by installing this tool.
No automatic publication, power setting, host reset or model download is enabled.

Rollback requires no service or model change: stop invoking the private tool
and select the prior environment by its explicit path. Preserve the prefix and
wheelhouse for review. Never remove it while one of its processes is running.
An upgrade uses a new locked dependency set and fresh prefix; do not overwrite
an in-flight Hyperloom session. Upstream 1.1.5 cannot resume older session schemas.
