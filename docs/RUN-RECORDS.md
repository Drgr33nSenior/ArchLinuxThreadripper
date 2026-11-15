# Run records (run-record-v1)

A run record ties each try at a task to an independent acceptance verdict and to
the resources it used. It answers "which declared tasks were accepted, within
which latency target, and what did all attempts, including failed ones and
retries, consume?" without treating tool success as task success.

The interface is implemented by
[`lib/workstation/run_record.py`](../lib/workstation/run_record.py) and tested
offline by `bash tests/test_run_record.sh` with the fixtures in
[`tests/fixtures/run-record/`](../tests/fixtures/run-record/). It adds no new
service, collector or metric. It reuses the existing measurement receipts
(`measurement.py` `run.json`), the PSU sampler's interval rules
(`telemetry_power.advance`) and the GPU board power readings.

This is the `run-record-v1` milestone of PLAN-01. The accepted-task cost report
for held-out task families is a later milestone produced with PLAN-02. No
live run, energy or latency result has been recorded with this interface yet.

## Records

Every record has `schema: 1`, a `kind` and an `interface`. Every object has a
fixed set of keys, and every identifier matches
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Records therefore hold identifiers,
counts, timings, digests and watts only. Prompts, answers, file paths, URLs,
user names and credentials are rejected, not redacted.

| Kind | Interface | Producer | Purpose |
| --- | --- | --- | --- |
| `run-plan` | `run-record-v1` | Whoever starts the run, before the first attempt | Run, arm, declared tasks and latency targets |
| `attempt` | `run-record-v1` | The runner, one per try at a task | Identity, deployment, timing, outcome, failure, tokens and tool outcomes |
| `task-acceptance` | `task-acceptance-v1` | The independent acceptance checker (PLAN-04) | One verdict per attempt |
| `gpu-power-sample` | `run-record-v1` | A GPU board power reading | Component power observation |
| PSU sample | `telemetry_power.collect()` output | The PSU sampler | Whole-system DC output |

### Identity

Each attempt carries:

| Field | Meaning |
| --- | --- |
| `task_id`, `task_version` | The declared task and its version, for example a menu case and its menu version |
| `run_id` | One execution of a plan |
| `attempt_id` | One try at one task in that run |
| `arm_id` | The configuration arm; every attempt in a run uses the plan's arm |
| `parent_id` | The attempt this one causally follows; `null` for the first try |
| `cause` | `initial` (no parent) or `retry` (names its parent) |

A task's attempts form one chain: one initial attempt, and each retry names an
earlier attempt of the same task that ended before the retry started. An
attempt has at most one retry. Duplicate IDs, unknown or cross-task parents,
cycles and attempts for undeclared tasks are rejected.

`measurement.run_command(..., identity=...)` stores the identity and
`started_unix_ns` in `run.json`. `run_record.attempt_from_receipt()` turns such a
receipt into an attempt record. Receipts written without an identity are
unchanged in meaning, store `identity: null` and cannot be joined.

### Deployment

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
Measurement receipts map as follows: `timeout` and `interrupted` to
`timeout-cancellation`, `telemetry` and `cleanup` to `environment`, and
`workload-exit` and any other workload exception to `unknown`, because an exit
status alone does not say why the workload failed. `failure.source` names the
signal that set the category, for example `measurement:timeout`.

A completed attempt is not an accepted one. Acceptance comes only from the
independent verdict.

## Consumed acceptance interface (task-acceptance-v1)

The report consumes one verdict per attempt from the independent checker:

```json
{"schema": 1, "kind": "task-acceptance", "interface": "task-acceptance-v1",
 "task_id": "...", "task_version": "...", "run_id": "...", "attempt_id": "...",
 "verdict": "accepted | rejected | unavailable",
 "checker": {"id": "...", "sha256": "<checker digest>"},
 "evaluated_unix_ns": 0}
```

This file defines only the fields the join needs. The protected checker and its
evidence belong to PLAN-04; its output must be emitted in, or adapted to, this
form. The join rejects a verdict that names no recorded attempt, disagrees with
the attempt's task or run, predates the attempt's end or duplicates another
verdict for the same attempt.

A task's status is:

| Status | Meaning |
| --- | --- |
| `accepted` | At least one attempt has an `accepted` verdict |
| `rejected` | No attempt was accepted and the final attempt was rejected |
| `unverified` | No attempt was accepted and the final attempt's verdict is missing or `unavailable` |
| `not-attempted` | Declared in the plan but never attempted |

Tool-call counts are reported as tool outcomes only. They never change a status.

## Latency targets

The plan declares `latency_targets.task_wall_seconds` and `declared_unix_ns`
before the run. The report rejects a plan declared after the first attempt
started, and records the plan's SHA-256. A task's wall time runs from the start
of its first attempt to the end of its first accepted attempt, so retries count
against the target. `tasks_accepted_within_target` counts only accepted tasks
meeting it. Request throughput and tool success are not inputs.

## Resource joins

### Totals and coverage

The summary reports declared, accepted, accepted-within-target, rejected,
unverified and not-attempted tasks, plus every attempt's consumed work:
attempts, failed attempts, retries, summed attempt wall time, prompt and
completion tokens, tool outcomes and failures by category (every category,
including zeros). Coverage reports how many attempts have a verdict and token
counts, and the PSU coverage of the run window. Missing token records are not
counted as zero tokens.

### Energy and power

Three attribution levels are kept apart:

| Level | Source | What the report gives |
| --- | --- | --- |
| `system-psu-dc-output` | Corsair hwmon DC output samples | Energy over covered intervals in each attempt window and the run window |
| `gpu-board-component` | GPU board power samples | Per-device sample count, mean and peak watts; never integrated or added to PSU output |
| Task-attributable energy | None available | Always `null`, with the reason |

PSU samples are passed in recorded order through `telemetry_power.advance()`,
so the PSU sampler's rules decide which intervals count. Only an `available`
interval contributes. It is integrated with the trapezoid rule and linearly
interpolated where it straddles a window edge. Every uncovered stretch is
listed as a gap with its reason: `sample_unavailable`, `sample_gap`,
`boot_changed`, `device_changed`, `clock_discontinuity` or `no_samples`.
`resets` counts boot and device changes inside the window. Samples whose wall
clock goes backwards are rejected.

PSU output is whole-system DC power, not AC wall energy. It includes idle load
and any concurrent work, including other attempts (`overlapping_attempts`
counts these). It is therefore never presented as one task's energy.

## Produce a report

```sh
python3 lib/workstation/run_record.py report \
  --plan plan.json --attempts attempts.jsonl --acceptance acceptance.jsonl \
  [--psu psu.jsonl] [--gpu gpu.jsonl]
```

Inputs are local files. The command reads nothing else, contacts nothing and
writes the JSON report to standard output. Invalid input exits with status 1
and `run record rejected: <reason>`.

## Identifiers stay out of metrics

Run, task, attempt and arm IDs belong in records, traces and joins. They must
never become Prometheus labels: the assistant tool metrics take only fixed label
values ([Assistant tool metrics](TELEMETRY.md#assistant-tool-metrics)), and
`tests/workstation/test_run_record.py` fails if an identity label is added. The
gateway's content-safe logging boundary is unchanged; run identity is carried
by the runner's records, not by gateway logs.
