# Hyperloom owner binding reference

> **Historical.** Reference for the op234-era owner packets; see [Hyperloom](HYPERLOOM.md).

Use [Hyperloom tools](HYPERLOOM.md) for installation, offline reports and scope.
This reference records the existing owner-packet ABI and evidence import rules.
Historical packets are consumed; prepare a fresh reviewed packet for execution.

## Delegate a registered owner packet

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
The output separately reports `controller_runtime_ready`. The configured
Mac Python 3.11.1 in the retained check lacked the collector's `tarfile` data
filter; execution refuses before consuming the packet. Use a declared and
qualified compatible controller SDK before execution. Python 3.14.7 passed
this capability check in native CPU tests. Do not remove the extraction filter
or substitute the workload's remote interpreter for the controller interpreter.
The retained successful controller procedure used `/opt/local/bin/python3.11`.
That owner runtime reported Python 3.11.6 and passed the
full CPU suite with the real capability gate. For owner execution, set
`WORKSTATION_PYTHON=/opt/local/bin/python3.11`; the project's configured
`/usr/local/bin/python3.11` was unchanged. Interpreter and `tarfile`
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

## SGLang ROCm 10.2 bridge

Status, 9 November 2026: this section is historical. The 2026-11 reinstall
wiped the op234 runtime it describes, including its owner sandbox and the
`/home/developer` prefixes in the commands below. The current serving image is
built by the [AI serving stack](../infrastructure/ai-stack/README.md).

The owner-selected target runtime was the source-built ROCm
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

The project owner adapter has a bounded physical handoff pass. A general
upstream R9700/SGLang runner and full model/lifecycle qualification remain unfinished.
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

Use the project owner adapter for a matching reviewed SGLang packet. Installing
the tool does not authorize upstream vLLM launches or a serving-stack replacement.
No automatic publication, power setting, host reset or model download is enabled.

Rollback requires no service or model change: stop invoking the private tool
and select the prior environment by its explicit path. Preserve the prefix and
wheelhouse for review. Never remove it while one of its processes is running.
An upgrade uses a new locked dependency set and fresh prefix; do not overwrite
an in-flight Hyperloom session. Upstream 1.1.5 cannot resume older session schemas.

## Experimental optimizer backend

The following prototype is separate from the registered owner handoff above.
Its GPU and optimization-campaign qualification remains open.

The owner-backend implementation is in progress. Its CPU prototype is under
`infrastructure/hyperloom/sglang-owner-adapter/`. The builder verifies the
pinned wheel's complete `RECORD`, copies it to a fresh private directory and
adds `workstation_owner` to the real benchmark-backend resolver. It does not
change the stock gfx12 framework gate, install the adapter or start a campaign.
The private CLI instead requires a source-bound native qualification proof
when the owner backend is selected. Its `gfx12` runner alias and `r9700`
physical type resolve to the same qualification. Framework preflight checks
that proof without importing SGLang into the CPU optimizer. No such proof
has been issued.

The client uses `hyperloom_exchange.py` to submit immutable requests. Requests
and controller replies use separate trees; the optimizer sandbox mounts
replies read-only. Every result binds its request, candidate source set,
controller and owner seal. Cancellation, source drift, missing release and
duplicate invocation refuse without retry. These CPU checks do not establish
that a live controller or native cleanup path has been qualified.
The trusted exchange endpoint snapshots and rechecks candidate bytes before
export, then binds publication to its single-use claim. Its SSH transport uses
the existing pinned connection and finite receipt helper. A workstation CPU
test exercised list, claim, cancellation observation and result publication
over this SSH path. It invoked no owner. The exchange has not yet been
connected to a running optimization campaign.

`hyperloom_latency.py` compares repeated C1/C2 serving samples. It weights C1
and C2 equally and checks each case's request latency and group makespan for
regressions. Excessive dispersion is inconclusive. Its score is a dimensionless
latency ratio; physical throughput is unchanged. The sample count and noise
policy must be fixed in the campaign binding. The module requires passing
task quality, unprofiled serving records and independent release. It does not
authenticate those records by itself or authorize adoption.
Each observation has an owner nonce, arm and group identity. Comparisons also
require the same job's selected-C4 control. Control drift or materially different
completion-token counts makes the result inconclusive. A paired regression
cannot be hidden by a comparison with a prior job.

The private builder now applies SDK hooks that preserve structured latency
evidence through measurement extraction, variant records, shared grading and
integration decisions. Native CPU tests exercised the actual package
initializer and public report parser, including a high-throughput C2
regression and missing latency data. The actual profile executor also routes to
the owner client and propagates refusals without a launcher fallback. The
current native suite passes 47 tests. It also exercises real CLI session
initialization with the selected model and tokenizer metadata, the private
runner gate, paired-control vetoes, generated-work checks and frozen-option
refusals, the real baseline executor and baseline/profile writeback. A frozen
benchmark menu reaches the actual CLI and session state. These tests use no
GPU or LLM provider.

An explicit ChatGPT mode now uses a dedicated native Codex login for the
coordinator, critic and specialist. The user completed sign-in, and two real
provider turns verified SDK read-only tools and a specialist CLI workspace
write with `gpt-6.1-sol`. No API key is needed in this mode. Credentials remain
outside every agent workspace. Tested Codex permission profiles deny direct
and symlink access to that directory. The code-mode host needs a large sparse
virtual address allowance; the tested runtime keeps a separate 4 GiB data
limit. This is a transport proof, not a serving or optimization result.

`build_owner.py` derives fresh inert serving and profiling packets. The serving
menu has three repetitions of six cases: short cold, long cold and long warm
requests at C1 and C2. Short prompts use the selected service-configuration
task, whose answer requires at least 512 final tokens. Messages, native token
IDs and answer oracles come from the selected corpus. A separate five-request
diagnostic profiles the long cold C2 pair after warmup. Its bookkeeping
records actual batch membership and refuses to label single-request decode
as C2 coverage. GPU behavior remains unverified.

The profile adapter verifies seven separate windows, each with both TP rank
traces, before copying them into analysis directories. It binds compressed
trace hashes to the originating owner result and candidate, checks file and
decompression limits, and rejects reused traces or single-request decode.
The binding explicitly selects an informative primary window, normally the
first decode window; all seven captures remain available for analysis.
Cancellation of an asynchronous wait writes a cancellation marker and joins
that wait; it does not claim that an active GPU owner has released.

The derived caller retains the existing 6,360-second owner and 7,160-second
caller ceilings. A job also reserves 320 seconds for post-release native
answer grading, 120 seconds for replay/reporting and 60 seconds for admission.
The complete 7,660-second serving envelope must fit the campaign's remaining
time before admission. Unused time can be reclaimed only after release.

The serving report projection joins every HTTP row with its native decoded
answer and grade. It retains reasoning and final-token counts, uses client wall
time, and keeps C2 pairs together. Its fixture tests do not authenticate a new
physical run; the controller must first use the original owner and CPU readers.

`owner_operation.py` now composes fresh packet preparation, registration,
one finite caller, original retained replay and native answer grading. The
profile route reuses the sealed packet's capture validator, binds both ranks
and all seven windows to the graded C2 request pair, and returns no selectable
latency series. Grading failure, uncertain release or a conflicting checkpoint
parks the campaign. It preserves the evidence and does not retry.

`controller.py` connects the SSH exchange to that operation. It verifies the
controller and builder source closure, requires a retained campaign grant,
checks cancellation before invocation and reserves result-publication time
inside the job envelope. Its scope includes serving, profile and fixed C4
component packets. A changed source must pass native CPU compilation and
the two-card component screen before a serving comparison. Direct KernelForge
and GEAK lanes remain disabled; other kernels need their own harnesses.

`optimizer_runtime.py` keeps the client and agents in a device-free Linux
sandbox with read-only replies, SDK, evidence and input files. Native CPU
checks prove those mounts and timeout cleanup. The specialist works on a
plain source copy, refuses an unsettled integration and rejects a stale patch.
No Git operation is needed. The runtime uses one absolute campaign deadline.
Hyperloom receives only the remaining hours, with a separate CLOSE reserve.

`campaign.py` stages pinned files without starting the clock, then keeps one
controller alive across qualification and optimization. Controller failure or
deadline expiry stops the owned CPU client. A trusted terminal marker makes
later requests fail promptly; cancellation never asserts GPU release. Failed
transport and timeout paths retain evidence. The real inert builders and
proposal steps finish in about 12 seconds against their 60-second admission
allowance.

`qualification.py` authenticates the three real qualification results, full
owner seals, release checkpoints, A/A stability and fresh two-rank C2 traces.
The baseline and profile executors reuse pinned qualification reports with
their original observation identities. Reuse is not another measurement.
Malformed evidence or failed quality parks the campaign; correct negative
performance remains available to the optimizer.

The independent execution review is resolved. A focused failure-path test now
proves that a cancelled optimizer still attempts result and artifact collection
once, within the deadline. Failed collection retains the native root and reason.
A read-only clock comparison is within the five-second tolerance.

Still required: real component, unchanged-C4 A/A and C2 profile qualification,
then actual optimization. The staged package has started no campaign clock or
GPU work. Serving/profile submission must occur by campaign second 6740 because
each reserves its full envelope. Zero or one candidate serving A/B is a
realistic first-campaign expectation. CPU checks establish no measured gain.

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
