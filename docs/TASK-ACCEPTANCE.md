# Task acceptance

`lib/workstation/task_acceptance.py` decides whether one agent attempt satisfied its
task. The verdict comes only from checks that the agent cannot rewrite: protected tests,
an observation of the external state, and the files and links that the attempt produced.
The model's completion claim, the agent's own tests and its IDE or project feedback are
recorded as evidence. They never decide the verdict.

The checker calls no model and changes no external system. It is contract version
`schema: 1`. Other tools depend on the [interface](#interface-for-other-tools).

## Terms

| Term | Meaning |
| --- | --- |
| Acceptance source | The directory that holds the specification, the protected tests and the references for one task |
| Workspace | The directory that the agent may read and edit |
| Evaluator | The harness or person that runs the checker after the attempt. The evaluator is not the agent |
| Seal | SHA-256 over the path and content of every file in the acceptance source |
| Editable file | A file that the specification names as the task's deliverable source |
| Reference | A trusted solution (must pass) or a known-bug mutant (must fail) for the editable files |
| Canary | A variant of the protected tests that must fail for every implementation |
| Component | One independent check group that decides the verdict |
| Evidence | Recorded observations that do not decide the verdict |

## Protect the acceptance source

Keep the acceptance source outside every path that the agent can read or edit. Do not
mount, link or copy it into the workspace.

```text
<acceptance source>/
  acceptance.json      specification
  fixture/             files the agent starts from (visible)
  oracle/              observation inputs and expected results (hidden from the agent)
  canary/              legacy fixture metadata; never executed with submitted code
  reference/           trusted solution: editable files only
  mutants/<name>/      known-bug variants: editable files only
```

1. Print the seal: `python3 lib/workstation/task_acceptance.py seal <acceptance source>`.
2. Pin the seal in the evaluator's own code or configuration. Do not store it in the
   acceptance source or in the workspace.
3. Give the agent a copy of `fixture/` only:
   `python3 lib/workstation/task_acceptance.py workspace <acceptance source> <new workspace>`.

The checker enforces the following on every run:

- The acceptance source and the workspace must not contain each other.
- The acceptance source must hold only regular files of at most 2 MiB. A symbolic link is
  refused.
- The seal must equal the pinned seal. If it does not, the checker runs nothing from the
  acceptance source and the verdict is `unavailable`.
- Only editable files cross into the submitted process. Expected observations, protected
  tests and evaluator code remain outside its mount namespace. The evaluator compares
  the digest of the complete JSON output against its own expected result and requires
  a successful process exit. An assertion patch or custom Python equality method cannot change this comparison.
- Every registered agent route must pass a workspace read/write control and deny read
  and non-truncating write-open access to each source file, checker and evaluator
  configuration file. The source seal alone does not establish protection.

The checker cannot stop a process that already has write access to the acceptance source
and to the evaluator. Run the agent with write access to its workspace only.

The evaluator supplies `agent_routes={name: probe}` and `protected_configuration=[...]`
to `check_task`. A trusted probe calls the actual agent route with `(operation, absolute_path)`
and returns `allowed`, `denied` or `unavailable`. Reads must open the target without returning
its contents. Writes must attempt a non-truncating write-open and close it without changing
bytes. Register every enabled file, command, browser and broker route, including routes
that can replace or rename files. Apply the same capability policy for the whole attempt.
The configuration list must include the pinned seal and every evaluator configuration
file. The checker also probes itself and every file in the source.

For the CLI, `--boundary-adapter <executable>` invokes the trusted evaluator adapter as
`<executable> read|write <absolute-path>`. It must perform the operation through **all**
enabled agent routes. Exit 0 means all denied; exit 1 means at least one allowed; exit 2
means unavailable. The workspace control must return 1. It must not print protected
contents. Supply configuration with repeated `--protected-configuration` arguments.
The adapter itself is included in the access probes. Missing probes, failed controls,
or missing configuration pins produce `unavailable`; an allowed protected access fails
protection. Do not use an adapter that guesses from path placement or returns constant
denials. These callbacks are trusted evaluator integration, never submitted agent code.

The Linux validation fixture uses the same `coding_eval.run_task` command route for its
ordinary agent probes and submitted code. It verifies denied absolute-path reads and
writes under the real bubblewrap namespace. This proves that fixture capability boundary;
it does not prove that a separate harness registered all its routes.

## Specification

`acceptance.json` declares every component. A component that does not apply must say so:
`{"applicable": false, "reason": "..."}`. No member and no command-line option disables a
declared check. An unknown member is refused.

| Member | Meaning |
| --- | --- |
| `schema`, `kind` | `1` and `workstation-task-acceptance-spec` |
| `task_id`, `version` | Task name and specification version |
| `editable` | Relative paths that cross from the workspace into the evaluation copy |
| `ignore` | File or directory names that the workspace comparison skips, for example `__pycache__` |
| `oracle.command` | Legacy command metadata, validated but never used to execute protected tests with submitted code |
| `oracle.observations` | Protected JSON file with `module`, `function`, and bounded `cases` of `args` and `expected` |
| `oracle.canary` | Legacy canary source metadata. The evaluator now applies the must-fail expectation itself |
| `oracle.seconds` | Time limit for each run, 1 to 300 |
| `oracle.baseline` | `fail` when the unchanged fixture must fail the protected tests (a repair task), else `pass` |
| `oracle.references` | `id`, `overlay` directory and `expected` (`pass` or `fail`). At least one trusted reference and one known bug are required |
| `tests` | Optional. `command` runs the agent-visible tests; `patterns` match test file names |
| `external_state.checks` | `id`, `path` and `equals`: values that the state observation must hold |
| `deliverable.answer` | Required fields of the final answer: `id`, `path` and either `equals` or `type` |
| `deliverable.artifacts` | Produced files: `id`, `format` (`json` or `bytes`), `path` or `answer_path`, optional `fields` |
| `deliverable.links` | Produced URLs: `id`, `format`, `answer_path`, optional `fields` and expected `origin` |
| `linkage.checks` | `id`, `left`, `right`: one value from the state and one from the deliverable that must be equal |

A `path` is a list of object keys and array indexes. A linkage selector is
`{"from": "state" | "answer", "path": [...]}`,
`{"from": "artifact" | "link", "name": "<id>", "path": [...]}` or
`{"from": "artifact-sha256", "name": "<id>"}`.

An observation expects `{"value": <JSON value>}` or `{"error": "ValueError"}`.
The isolated child receives only call arguments and emits a JSON results array. Python
values are serialized before they cross the process boundary; comparison uses bytes in
the evaluator. Existing in-process oracle specifications without `observations` remain
parseable but return `unavailable-unsafe-in-process-oracle`. They cannot be accepted.
An answer-only specification without an oracle, external state, artifact or opened link
returns `independent-observation-required` even when the answer claims completion.

Two complete examples are in `tests/fixtures/task-acceptance/sources/`:
`window-counts` (a repair task with protected tests) and `record-publication` (external
state, answer fields, a produced file and a link).

## Check an attempt

Preconditions:

- The attempt is finished. The evaluator has the pinned seal.
- For a task with an oracle: a Linux host with `/usr/bin/bwrap`, `/usr/bin/timeout` and
  `/usr/bin/prlimit`. On another host the oracle is `unavailable`.
- For a task with external state: a JSON object that the evaluator observed itself, in a
  file outside the workspace.

```sh
python3 lib/workstation/task_acceptance.py check <acceptance source> <new result.json> \
  --workspace <workspace> --seal <pinned seal> \
  --boundary-adapter <trusted adapter> --protected-configuration <pin file> \
  [--answer <final answer file>] [--state <state.json>] [--allow-origin https://HOST] \
  [--claim completed|not-completed] [--guidance <feedback.json>] \
  [--run-id ID] [--attempt-id ID]
```

- `--answer` is the final answer exactly as the agent returned it: one JSON object, or one
  JSON object in one enclosing code fence. An empty or prose answer fails the deliverable.
- `--allow-origin` names an origin whose links the checker may open. The checker sends one
  GET request without credentials, proxy or redirect. A link on another origin is not
  opened and stays `unavailable`. If the specification supplies an expected `origin`,
  a different origin fails with `link-origin-mismatch` before any network request.
- `--guidance` is a JSON object with the booleans `failures` and `unavailable`, the shape
  of the coding agent's post-edit verification result.
- The result file must be new and outside the workspace.

Exit status: `0` accepted, `1` rejected, `3` unavailable, `2` input or usage error (no
result is written). Workspace/artifact read failures remain component outcomes. Unreadable
optional guidance or test evidence remains explicit without discarding the result.

## Components and verdict

| Component | Passes when | Otherwise |
| --- | --- | --- |
| `protection` | The source seal matches and actual registered agent routes deny protected access after passing workspace controls | `failed`: overlap, unreadable source or seal mismatch. `unavailable`: missing pin, probes or working route controls |
| `references` | The unchanged fixture, each reference and the canary on the trusted reference give the declared outcome | `failed`: a reference does not behave as declared, so the oracle is unproven |
| `oracle` | Parent-side observations match for the submitted editable files | `failed`: tests fail or time out, or an editable file is a link or not a regular file |
| `interference` | The parent-side must-fail expectation fails with the submitted editable files | `failed`: the canary passes, so the submitted files manufactured the pass |
| `external_state` | Every state check holds in the evaluator's observation | `unavailable`: no observation, an unreadable one, or one inside the workspace |
| `deliverable` | The answer has the required fields and every declared file and link opens with the required fields | `failed`: missing or wrong field, missing file, path outside the workspace, HTTP error. `unavailable`: no answer supplied, origin not allowed, origin unreachable |
| `linkage` | Each declared pair of state value and deliverable value is equal | `failed`: a value is missing or differs, for example a link to an older revision |

Each component has the status `passed`, `failed`, `unavailable` or `not-applicable`. The
verdict follows from the statuses by one fixed rule:

1. `accepted`: `protection` passed and every other component is `passed` or
   `not-applicable`.
2. `rejected`: `protection` passed and at least one of `oracle`, `interference`,
   `external_state`, `deliverable` and `linkage` is `failed`.
3. `unavailable`: every other case. This includes an unverified acceptance source, a
   reference that does not behave as declared, an oracle that could not run and an
   unresolved external state.

`unavailable` is not a pass and not a measured failure of the attempt. Do not remove a
check to make two runs comparable; report the unavailable result.

`reasons` lists a stable code for every component that did not pass:
`<component>:<check id or reason>`, for example `oracle:post-change-failed`,
`interference:oracle-canary-passed`, `protection:seal-mismatch`,
`external_state:record-closed` or `linkage:link-revision-current`.

For a repair task the result records the reproducer: `components.oracle.baseline` (`fail`)
and `components.oracle.post_change` (`pass`). `components.references.outcomes` keeps the
expected and observed outcome of the baseline, every reference and the canary for each run.

## Evidence

| Evidence | Content |
| --- | --- |
| `test_diff` | Every file that the agent added, changed or deleted beside the editable files: path, change, digests, and a bounded unified diff for a changed visible file |
| `workspace_suite` | The agent-visible tests with the agent's changes, run against the baseline, each reference and the submitted files |
| `generated_tests` | The test files that the agent added or changed, run alone in the same way |
| `guidance` | Digest and the two booleans of the supplied IDE or project feedback. `authoritative` is always `false` |
| `claim` | The completion claim. `decides_verdict` is always `false` |

`workspace_suite` and `generated_tests` are `validated` when the tests pass on the trusted
reference and fail on at least one known bug. For a repair task they must also fail on
the broken baseline; otherwise they are `invalid` with `baseline-reproducer-passed`,
even if an unrelated mutant fails. They are `invalid` with
`detects-no-known-bug` when they pass on every known bug: the tests are vacuous, or a test
file or hook replaced the application under test. They are `invalid` with `fails-trusted`
when they fail on the trusted reference. `reproducer` records the outcome on the baseline
and on the submitted files.

The coding agent's post-edit IDE diagnostics and project checks remain repair guidance for
the agent. Tests in the workspace are agent-editable; the checker never uses them as the
oracle. `claim.contradicted` and `guidance.contradicted` show that the claim or the
feedback reported success and the verdict is not `accepted`.

## Interface for other tools

| Item | Value |
| --- | --- |
| Contract version | `schema: 1` (`task_acceptance.SCHEMA`) |
| Specification kind | `workstation-task-acceptance-spec` |
| Result kind | `workstation-task-acceptance-result` |
| Location | `lib/workstation/task_acceptance.py`; examples in `tests/fixtures/task-acceptance/` |
| Python functions | `seal`, `prepare_workspace`, `check_task`, `verdict`, `validate_result`, `load_result` |
| Constants | `COMPONENTS`, `STATUSES`, `VERDICTS`, `EXIT_STATUS` |
| Commands | `seal`, `workspace`, `check`, `validate` |

- **Join a result to a run record.** `subject.run_id` and `subject.attempt_id` carry the
  caller's identifiers unchanged. `task_id`, `spec_sha256`,
  `components.protection.seal_sha256` and `checker_sha256` identify what was checked and
  with which checker. Read a result with `load_result`: it refuses a record whose verdict
  or reasons do not follow from its components.
- **Use a check that this tool cannot run**, for example a scripted play-through in
  another runner. Keep the script in a sealed acceptance source. Let the evaluator run it
  and write its observation as the state file. Link the observation to the evaluated file
  with an `artifact-sha256` selector, so that an observation of other content is rejected.
- **Compatibility.** A change that removes or renames a member, status, verdict or reason
  prefix, or that changes the verdict rule, increments `schema`. A consumer must ignore
  members that it does not know inside `components` and `evidence`.

## Limits

- Commands run only in the network-free Linux sandbox of `coding_eval.run_task`. The
  `acceptance-isolation` CI job requires real execution and uploads complete receipts.
  It does not skip namespace failures or relax host security policy. macOS fixture tests
  use an injected executor with `isolated:false` and are not isolation evidence.
- A caller with unrestricted host tools can rewrite evaluator code or configuration.
  Such a route fails the boundary probes. The trusted evaluator must register every
  route available to its agent, protect its own configuration, and keep that policy in
  force during execution. Fixture success does not prove deployment integration.
- The evaluation copy holds files of at most 2 MiB, written as regular files without
  execute permission.
- A deleted editable file is evaluated as deleted. A new file that is not in `editable`
  never reaches the oracle run.
- The fixtures are synthetic. They test the checker and are not model evidence.

## Consumer migration

The result remains schema 1, with unchanged component names, verdict rules and
`validate_result` interface. PLAN01 run records need no format adapter. Existing saved
receipts remain structurally valid; their checker digest identifies the old evaluator.
New evaluations without actual route probes are unavailable.

The HARNESS game can keep its oracle inapplicable: the parent browser play-through is
the independent external-state observation, linked to the artifact SHA-256. Its parent
must connect its actual file/command/browser permission probes through the boundary
adapter, include its pin/configuration files, and re-pin the checker identity. Source
placement or an old probe summary alone is insufficient. Root coordinates that consumer
change; this repository does not change the harness.
