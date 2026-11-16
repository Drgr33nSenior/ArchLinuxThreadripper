# Run records (run-record-v1)

A run record ties each try at a task to an independent acceptance result, to the
requests it made and to the resources it used. It answers "which declared tasks
were accepted, within which latency target, and what did all attempts, including
failed ones and retries, consume?" without treating tool success as task success.

[`lib/workstation/run_record.py`](../lib/workstation/run_record.py) implements
the interface. `bash tests/test_run_record.sh` tests it offline with the fixtures
in [`tests/fixtures/run-record/`](../tests/fixtures/run-record/). It adds no
service, collector, metric label or dependency.

This is the `run-record-v1` milestone of PLAN-01. The accepted-task cost report
for held-out task families is a later milestone produced with PLAN-02. No live
run, energy or latency result has been recorded with this interface. The tests
use synthetic records, in-process services and one short local command.

## What produces each record

| Record | Producer | State |
| --- | --- | --- |
| Run plan (`run-plan`) | You write it before the run | Interface |
| Attempt start (`run-start.json`) and receipt (`run.json`) | `workstationctl performance run ... --run-plan ... --run-identity ...` | Available |
| Attempt (`attempt`) | `run_record.py attempt <run directory>` | Available. Token counts, tool outcomes and deployment identity are caller-supplied. |
| Request trace (`request-trace`) | The gateway and service-tools, for a request that carries a run context | Available in source. Not observed on the deployed services. |
| PSU sample | `run_record.py resources <telemetry.jsonl>` | Available for the window of a `performance run` |
| GPU sample (`gpu-power-sample`) | `run_record.py resources <telemetry.jsonl>` | Available for the window of a `performance run` |
| Acceptance result (`workstation-task-acceptance-result`) | Trusted parent using `begin_attempt` / `check_task(attempt=...)`, or the private `task_acceptance_session.py` protocol ([Task acceptance](TASK-ACCEPTANCE.md)) | Available with a pre-attempt lifetime handle and actual capability probes. One-shot `check` is diagnostic and returns unavailable. |
| Report (`run-record-report`) | `run_record.py report` | Available |

[Not available](#not-available) lists what has no producer.

## Identity

Each attempt carries:

| Field | Meaning |
| --- | --- |
| `task_id`, `task_version` | The declared task and its version. For a checked task: the acceptance specification's `task_id` and `version`. |
| `run_id` | One execution of a plan |
| `attempt_id` | One try at one task in that run |
| `arm_id` | The configuration arm; every attempt in a run uses the plan's arm |
| `parent_id` | The attempt this one causally follows; `null` for the first try |
| `cause` | `initial` (no parent) or `retry` (names its parent) |

A task's attempts form one chain: one initial attempt, and each retry names an
earlier attempt of the same task that ended before the retry started. An attempt
has at most one retry. The report refuses duplicate IDs, unknown or cross-task
parents, cycles and attempts for undeclared tasks.

Every record has a fixed set of keys. Every identifier matches
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Records therefore hold identifiers, counts,
timings, digests and watts only; they have no field for a prompt, answer, path
or URL. This validation bounds the format and length of an identifier. It cannot
recognise a secret or a personal value that fits the format. Never derive an
identifier from a credential, a user name or other personal data.

## Run plan and its binding

The plan declares the run, the arm, the tasks and the latency target:

```json
{"schema": 1, "kind": "run-plan", "interface": "run-record-v1",
 "run_id": "...", "arm_id": "...", "declared_unix_ns": 0,
 "latency_targets": {"task_wall_seconds": 150},
 "tasks": [{"task_id": "...", "task_version": "..."}]}
```

`plan_sha256` is the SHA-256 of the canonical plan. It is computed when an
attempt starts, not when the report is made:

- `performance run` refuses to start if the identity does not belong to the
  plan, or if the plan's `declared_unix_ns` is later than the start.
- It writes the digest to `run-start.json` before the command starts, and to
  `run.json` when the command ends.
- It gives the digest to the command in the run context. Each request-trace
  event that a service writes for the attempt repeats it.

`run_record.py report` computes the digest of the plan that you give it. It
refuses an attempt or a matched request-trace event that carries a different digest, and
an attempt that started before `declared_unix_ns`. A target, task or arm that
was edited after an attempt started is therefore refused, also when
`declared_unix_ns` was left unchanged.

Limits of the binding:

- The records are not signed. Someone who can rewrite the plan and every record
  that carries the digest can replace the plan.
- The request-trace events are in the service's container log, which the runner
  does not write. They are the only copy of the digest outside the runner's
  files, and they exist only for an attempt that made a request.
- `declared_unix_ns` is the plan author's statement. The binding proves that
  every record agrees with one plan. It does not prove when that plan was
  written.
- An acceptance result does not carry the digest. It joins on the run and
  attempt identifiers.

## Run context

One value carries an attempt's identity and plan digest to the services:

```text
v1;task=<id>;task_version=<id>;run=<id>;attempt=<id>;arm=<id>;parent=<id or ->;cause=<initial|retry>;plan=<sha256>
```

| Carrier | Name |
| --- | --- |
| Environment variable of the measured command | `WORKSTATION_RUN_CONTEXT` |
| Request header | `X-Workstation-Run-Context` |

`performance run` sets the environment variable for the command it starts. The
assistant acceptance runner (`assistant_acceptance.py run` and `behaviour`)
reads it, records it under `settings.run_context` in its report and sends the
header on the `fixture` and `gateway` boundaries. It sends none on the `direct`
and `webui-api` boundaries, which do not read it. A malformed value stops the
runner before its first request.

For another client, print the value to send:

```sh
python3 lib/workstation/run_record.py context --plan plan.json --identity identity.json
```

## Attempt records

```sh
./bin/workstationctl performance run OUT 1800 \
  --run-plan plan.json --run-identity identity.json -- COMMAND...
python3 lib/workstation/run_record.py attempt OUT \
  [--serving-contract ID --model ID --tokenizer-sha256 H --template-sha256 H] \
  [--tokens PROMPT COMPLETION] [--tool-calls SUCCEEDED FAILED] >> attempts.jsonl
```

Wait until the measured runner has stopped before extracting an attempt; file
absence alone does not establish that it died. `identity.json` holds the seven
identity fields. `attempt` prints one attempt
record from `OUT/run.json`. If the runner was killed before it wrote that file,
`attempt` retains the identity and plan from `run-start.json` as a failed
`environment` attempt with source `measurement:no-receipt`. Its interval ends
at the latest valid telemetry timestamp, or at its start if none exists.
This is an observed lower bound: the actual end and any unsampled tail remain
unknown. Do not treat this interval as a complete measurement. A run directory written without a plan and an
identity stores `identity: null` and `plan_sha256: null`; `attempt` refuses it.

The report counts these records under `coverage.attempts_without_final_receipt`
and sets their `wall_time_complete` to false. Consumed wall-time totals are
observed lower bounds when that count is nonzero. If an independently accepted
attempt has no final receipt, its task wall time and latency verdict stay null;
it cannot count as accepted within the target from an unknown end time.

`deployment` records `serving_contract`, `model`, `tokenizer_sha256` and
`template_sha256`. A value that was not captured is `null`, never guessed. The
report lists every distinct deployment and sets `deployment_changed` when a run
spans more than one.

### Failure categories

A failed attempt has exactly one operational category:

| Category | Use for |
| --- | --- |
| `retrieval` | Retrieval or search returned no usable evidence or failed |
| `reasoning-answer` | The model's reasoning or answer failed (for example, no final answer) |
| `edit-schema` | An edit, patch or structured output was invalid |
| `timeout-cancellation` | A deadline expired or the attempt was cancelled |
| `environment` | The runner, sandbox, telemetry or host failed |
| `verification` | The attempt's own verification step failed to run |
| `unknown` | The cause was not established |

An unexplained failure is `unknown`; it is never folded into another category.
A measurement receipt maps as follows: `timeout` and `interrupted` to
`timeout-cancellation`, `telemetry` and `cleanup` to `environment`, and
`workload-exit` and any other workload exception to `unknown`, because an exit
status alone does not say why the workload failed. `failure.source` names the
signal that set the category, for example `measurement:timeout`. No current
runner sets `retrieval`, `reasoning-answer`, `edit-schema` or `verification`.

A completed attempt is not an accepted one. Acceptance comes only from the
acceptance result.

## Request-trace events

For a request that carries a valid run context, the service writes one JSON line
to its container log when the request ends:

```json
{"schema": 1, "kind": "request-trace", "interface": "run-record-v1",
 "service": "gateway", "operation": "chat", "identity": {"...": "..."},
 "plan_sha256": "...", "started_unix_ns": 0, "ended_unix_ns": 0,
 "result": "ok", "status": 200, "code": null}
```

| Service | Requests | `operation` |
| --- | --- | --- |
| `gateway` | `/v1/chat/completions` | `chat` |
| `service-tools` | `/mcp` | `tool:<known tool name>`, `tool:other`, `mcp:tools-list`, `mcp:initialize`, `mcp:ping`, `mcp:notification`, `mcp:other`; `mcp` when the request ended before its method was read |
| `service-tools` | `/webui/images/generations` | `image` |
| `service-tools` | `/webui/search`, `/webui/fetch` | `web` |

`result` is `ok`, `tool_error` (HTTP success with a tool-level error), `refused`,
`limited`, `backend_error`, `cancelled` or `error` (not classified). `code` is a
fixed refusal or deadline code of the service, or `null`.

- An event holds validated identifiers, fixed names, an HTTP status and two
  timestamps. It never holds a path, query, header, body, user, key, tool
  argument or exception text.
- A request without the header writes nothing. Ordinary traffic stays unlogged,
  and the gateway still discards all other process output
  ([AI service](AI-SERVICE.md)).
- A malformed header is refused with HTTP 400 `invalid_run_context` and writes
  nothing.
- The gateway reads the header and never forwards it to LiteLLM or the model.
- A service writes the event for any request with a valid context that reaches
  the route, including one that it then refuses for an invalid key. An event
  shows that such a request reached the service. It does not prove who sent it,
  and it never decides acceptance or a task status.
- The collector forwards no container log, and no component on the request path
  produces an OTLP span. The events are therefore not sent to the NAS receivers.
  The collector configuration and its attribute allow-list are unchanged.

Collect the events of one run after the run. The kubelet rotates container
logs; this repository sets no retention for them.

```sh
kubectl -n ai-home-lab logs deploy/litellm -c gateway --since=2h > gateway.log
kubectl -n ai-home-lab logs deploy/service-tools -c tools --since=2h > tools.log
python3 lib/workstation/run_record.py traces --run-id RUN_ID --output traces.jsonl gateway.log tools.log
```

`traces` copies only valid events of that run to the new file. It counts every
other line and never copies it. At report time, an event for an unknown attempt
is counted as unmatched, including one carrying another plan digest. An event
for a recorded attempt must still match its full identity and bound plan.
The raw service-tools log can hold other process
output: keep it private and delete it after the extraction.

## Acceptance results

The acceptance record is the task acceptance checker's result
(`workstation-task-acceptance-result`, schema 1), exactly as
[`task_acceptance.py`](../lib/workstation/task_acceptance.py) wrote it. There is
no second acceptance format. `run_record.py` calls the checker's own
`validate_result()`, so the checker's rule decides whether a record is well
formed and whether its verdict follows from its components. A record with an
edited verdict is refused.

The parent must establish the acceptance lifetime **before** exposing any
agent route. A one-shot `task_acceptance.py check` cannot establish that history
and yields `unavailable`, even for a correct repair. Do not use it as the
acceptance producer.

For the private session producer:

1. Pin the bootstrap and complete checker manifest in the trusted parent, then
   start `python3 -I -B lib/workstation/task_acceptance_session.py` with private
   pipes. Keep the pipes and pins outside the agent's capabilities.
2. Send the start message with `source`, `workspace`, `seal`, `checker`,
   `configuration`, `routes`, `capability_lease`, `run_id` and `attempt_id`.
   Service each probe through the actual named route. Enable tools only after
   `ready:true`, and enforce the same capability policy for the whole attempt.
3. Run the measured command with that identity and plan. Wait until it ends,
   including failure or cancellation, before final evaluation. A result created
   before the measured attempt ends cannot join.
4. Obtain the independent final observation. Send it and the same lease through
   the private parent channel, then retain the final `{"result": ...}` message
   as `session-final.json`. A protocol failure is unavailable, never acceptance.

The in-process alternative is `begin_attempt(...)` before the command and
`check_task(..., attempt=handle, run_id=..., attempt_id=...)` after it. It has the
same boundary and lifetime requirements; a caller must not substitute a
path-placement check or model self-report for an actual route or observer.

Extract the unchanged schema-1 result from the final session message, then
list the result files as JSON lines. Check each command's exit status before
using its output:

```sh
jq -e '.result | select(.kind == "workstation-task-acceptance-result")' session-final.json > result.json
python3 lib/workstation/run_record.py acceptance result.json > acceptance.jsonl
```

`acceptance` validates each file and prints the checker's record unchanged.

| The join uses | From the result |
| --- | --- |
| Run and attempt | `subject.run_id`, `subject.attempt_id` |
| Task and version | `task_id`, `spec_version` |
| Verdict | `verdict`: `accepted`, `rejected` or `unavailable` |
| What was checked, and with which checker | `spec_sha256`, `checker_sha256` |
| Evaluation time | `created_utc` |

The join refuses a result that:

- names no run or attempt (the checker was run without `--run-id` and
  `--attempt-id`);
- names no recorded attempt, or another run, task or task version;
- was created before the attempt ended;
- is the second result for one attempt;
- has a `spec_version` outside the identifier format. The checker permits any
  text of 1 to 64 characters; a plan can name only a version in the identifier
  format.

A result whose acceptance source was not verified records no task. Its verdict
is always `unavailable`, and it joins on run and attempt alone. No input other
than the result can supply or change a task, an attempt or a verdict.

A task's status is:

| Status | Meaning |
| --- | --- |
| `accepted` | At least one attempt has an `accepted` result |
| `rejected` | No attempt was accepted and the final attempt was rejected |
| `unverified` | No attempt was accepted and the final attempt's result is missing or `unavailable` |
| `not-attempted` | Declared in the plan but never attempted |

Tool-call counts are reported as tool outcomes only. They never change a status.

The checker defines this contract and can change it. `run_record.py` and the
checker ship together. The report's `acceptance_checkers` lists the shipped
checker digest, every observed digest, any that differ from the shipped one,
and whether several checkers were used. Historical results remain historical;
structural validation does not upgrade them to current protection proof or
attest their producer.

The tests join a newly produced result from the shipped lifetime checker after
a real local measured command. That authored fixture uses a host executor and
scoped route stand-in; it is not platform or deployment isolation proof. The
tests also validate every saved acceptance fixture. The four results in
`tests/fixtures/run-record/publication/results/` were written by the original
pre-lifetime checker at `ab2f750` from `record-publication` fixtures (rejected,
two unavailable, accepted). They test historical format compatibility only.
`acceptance.jsonl` contains authored records for the three fixture tasks; each
one says so in `limitations`.

## Resource records

```sh
python3 lib/workstation/run_record.py resources OUT/telemetry.jsonl --psu psu.jsonl --gpu gpu.jsonl
```

`performance run` samples the host once a second into `OUT/telemetry.jsonl`.
`resources` writes two new files from it:

- **PSU samples**: one per row, in the shape of `telemetry_power.collect()`
  without the rails. A row without one valid Corsair PSU reading yields an
  unavailable sample, which becomes a gap. A `telemetry.jsonl` written before
  the row gained `boot_id` yields only unavailable samples.
- **GPU samples** (`gpu-power-sample`): one per `amdgpu` card that reported
  board power: `power1_average` when the driver has it, else `power1_input`. A
  suspended or unreadable card yields no sample, never zero watts.

A malformed, deeply nested or torn telemetry row counts in
`invalid_telemetry_rows` and emits an unavailable PSU sample with no GPU sample.
It breaks energy coverage; samples on either side cannot bridge the gap.
Only consume the outputs after a successful exit. If conversion fails, files
created by that call are removed; existing files are never replaced or removed,
so a failed conversion can be retried with the same new output names.

For several attempts, concatenate their PSU files in time order, and their GPU
files in any order.

Three attribution levels are kept apart:

| Level | Source | What the report gives |
| --- | --- | --- |
| `system-psu-dc-output` | Corsair hwmon DC output samples | Energy over covered intervals in each attempt window and the run window |
| `gpu-board-component` | GPU board power samples | Per-device sample count, mean and peak watts; never integrated or added to PSU output |
| Task-attributable energy | None available | Always `null`, with the reason |

PSU samples are passed in recorded order through `telemetry_power.advance()`, so
the PSU sampler's rules decide which intervals count. Only an `available`
interval contributes. It is integrated with the trapezoid rule and linearly
interpolated where it straddles a window edge. Every uncovered stretch is listed
as a gap with its reason: `sample_unavailable`, `sample_gap` (more than 90
seconds between samples), `boot_changed`, `device_changed`,
`clock_discontinuity` or `no_samples`. `resets` counts boot and device changes
inside the window.

A sample whose wall clock is not later than the latest sample seen (the clock
stepped back) adds no interval. The stretch up to the next later sample is a
`clock_discontinuity` gap, so replayed wall time is never counted twice.
`coverage.psu_clock_steps_back` counts such samples. The acceptance, trace and
GPU parts of the report are not affected.

PSU output is whole-system DC power, not AC wall energy. It includes idle load
and any concurrent work, including other attempts (`overlapping_attempts` counts
these). It is therefore never presented as one task's energy.

## Report

```sh
python3 lib/workstation/run_record.py report \
  --plan plan.json --attempts attempts.jsonl --acceptance acceptance.jsonl \
  [--psu psu.jsonl] [--gpu gpu.jsonl] [--traces traces.jsonl]
```

Inputs are local files. The command contacts nothing and writes the JSON report
to standard output. Invalid input exits with status 1 and
`run record rejected: <reason>`.

- **Latency.** A task's wall time runs from the start of its first attempt to
  the end of its first accepted attempt, so retries count against
  `task_wall_seconds`. `tasks_accepted_within_target` counts only accepted tasks
  that meet the target. Request throughput and tool success are not inputs.
- **Totals.** The summary reports declared, accepted, accepted-within-target,
  rejected, unverified and not-attempted tasks, and every attempt's consumed
  work: attempts, failed attempts, retries, summed attempt wall time, prompt and
  completion tokens, tool outcomes and failures by category (every category,
  including zeros).
- **Coverage.** `coverage` reports how many attempts have an acceptance result,
  token counts and request-trace events, the events that match no recorded
  attempt, the PSU coverage of the run window and the sample counts. A missing
  token record is not counted as zero tokens.
- **Per attempt.** Each attempt row gives its outcome, failure category,
  acceptance verdict with the specification and checker digests, a request-trace
  summary by service, result and operation, the PSU and GPU windows and
  `overlapping_attempts`.

## Limits

One report reads at most 10,000 declared tasks, 10,000 attempts, 10,000
acceptance results, 100,000 PSU samples, 100,000 GPU samples and 100,000
request-trace events. A line is at most 4 MiB. 100,000 PSU samples are 27.8
hours at the one-second interval of `performance run`. Report a longer run in
parts.

The PSU, GPU and overlap passes sort once and then search; they do not scan
every sample for each attempt. Measured on the development machine used for
this change, not on the workstation, with synthetic records already in memory:

| Input | Report time |
| --- | --- |
| 200 attempts, 2,000 PSU samples | 0.02 s |
| 10,000 attempts with 100,000 PSU samples, 100,000 GPU samples and 100,000 request-trace events | 1.7 s; 15 MB report; about 310 MiB peak memory, including the inputs |

## Not available

- **Token counts, tool outcomes and deployment identity from a runner.** No
  adapter reads them from the assistant acceptance report or from the gateway.
  Supply them to `attempt`, or they stay `null`.
- **Other benchmark runners.** The storage, TuneD, memory and ROCm benchmarks
  and `serving.py` call the measurement runner without a plan. Their runs are
  not task attempts and cannot be joined.
- **PSU and GPU samples outside a `performance run`.** The 30-second
  `workstation-telemetry` timer writes Prometheus text only. It writes no
  sample file that a report can read.
- **Spans.** See [Request-trace events](#request-trace-events).
- **Independent per-task energy.** No meter exists; the value stays `null`.
- **A live report.** The accepted-task cost report waits for PLAN-02.

## Identifiers stay out of metrics

Run, task, attempt and arm IDs belong in records, request-trace events and
joins. They must never become Prometheus labels: the assistant tool metrics take
only fixed label values ([Assistant tool metrics](TELEMETRY.md#assistant-tool-metrics)),
and the tests fail if an identity label is added or if an identifier appears in
the metrics text. The request counts that service-tools publishes
([Request activity](TELEMETRY.md#request-activity-of-service-tools)) are labelled
by fixed route only.
