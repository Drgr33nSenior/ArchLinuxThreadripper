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

## Establish trust before the attempt

Start the evaluator before enabling the agent's tools. The evaluator retains the source,
expected seal, checker identities and registered observations in memory. It never reloads
a replacement pin or source as the authority for that attempt. The capability policy must
remain fixed until the evaluator has finished. A check started only after the attempt
cannot establish that lifetime and returns `attempt-lifetime-not-established`.

The Python API is:

```python
attempt = begin_attempt(
    source, workspace, expected_seal,
    agent_routes=routes,
    protected_configuration=config_paths,
    state=optional_existing_observation_path,
    expected_checker=trusted_checker_manifest,
)
# Enable the agent only after attempt.before["status"] == "passed".
# Keep the same capability policy and evaluator process alive.
# After the agent finishes, observe the external system through the parent:
attempt.observe(parent_observation)
result = check_task(source, workspace, expected_seal, attempt=attempt,
                    answer=answer_path, run_id=run_id, attempt_id=attempt_id)
```

`expected_seal` and `trusted_checker_manifest` come from the trusted parent before the
attempt. They must not be reloaded from an agent-writable pin. An existing `state` file
is automatically included in protection targets and snapshotted before the attempt.
Later file changes cannot replace its contents in the evaluator. For state produced after
the attempt, use a fresh parent observation in memory. Do not pass an agent's completion
report as an observation. A snapshot taken before a task changes external state does not
prove the resulting state; the parent must observe it again.

`Attempt.loaded` and `Attempt.snapshot` return copies. The retained source and observation
have no aliases to caller dictionaries. Source, configuration, observation and checker
identities are checked again at completion; replacement produces failed protection.
The loaded evaluator tree must also be trusted before importing its modules. Use the
private session entry point below for a consumer running in another process.

Every registered route is exercised before and after the attempt:

- Read and non-truncating write-open probes target source files, automatically registered
  observation files, configuration and every Python file in the checker tree.
- Create, rename and unlink probes target **owned sacrificial directories** beside
  protected files and in their evaluator-writable ancestors. Real source directories,
  checker installations, pin files and user data are never renamed or deleted.
- Workspace controls exercise the same operations. Non-sensitive controls permit another
  UID with mode 0666 for files and 0777 for their owned directories. The caller must still
  provide a workspace and path mapping accessible to its actual agent identity.
- A route returns `allowed`, `denied`, `unavailable` or `unsupported`. `unsupported` means
  that the capability is not exposed by that route; it is recorded as not applicable.
  A tool failure is not a denial or an unsupported capability. Each route must have a
  working control for at least one supported operation.

Callbacks are trusted evaluator integration: they must invoke the actual file, command,
browser or broker routes with their real permissions. The unit-test placement guards are
stand-ins and are not deployment evidence. Pre/post probes alone cannot prove that an
arbitrary external broker kept its policy fixed between probes. That lifetime guarantee
belongs to the parent capability lease. The Linux fixture uses the unchanged, network-free
`coding_eval.run_task` sandbox for every command, so submitted processes cannot change that
policy or reach evaluator memory. No separate-UID deployment has been qualified here.

Some ancestors are not writable by the evaluator, so sacrificial entries cannot be
allocated there without privilege. They are not reported as tested. Trust in the source,
pin and loaded code rests on the retained snapshots, not on these original directory
names. If an integration allows a renamed ancestor to expose hidden inputs through a
new route, it has not established the required capability boundary and must stay unavailable.

## Private session protocol for parent harnesses

Run `python3 -I -B lib/workstation/task_acceptance_session.py` before starting the agent.
The parent must pin and protect this bootstrap before launch. It sends newline-delimited
JSON over private stdin and reads stdout. Never give these pipes to agent tools or record
the probe transcript in the agent context. Submitted commands receive `/dev/null` as stdin.

The first message contains:

- `source`, `workspace`, `seal`: the selected task and its parent-held expected seal.
- `checker`: a map of every relative `*.py` path in the checker directory to its trusted
  SHA-256 digest. `checker_identity()` returns this shape, but a deployment must pin it
  before the agent can run. The bootstrap validates all bytes before importing checker
  modules. Its import loader compiles those captured bytes, including later imports;
  a filesystem module swap cannot become the running evaluator.
- `configuration`: non-secret evaluator configuration/pin paths to protect.
- Optional `state_path`: an existing observation to protect and snapshot automatically.
- `routes`: names of all actual enabled route groups.
- `capability_lease`: the parent's fixed-policy handle, retained for the whole attempt.
- Optional `run_id`, `attempt_id`: unchanged result identifiers.

The evaluator emits `{"probe": OP, "path": PATH, "route": NAME, "capability_lease": LEASE}`.
The parent performs the operation through that actual route and replies with
`{"outcome": OUTCOME, "capability_lease": LEASE}`. Operations are `read`, `write`, `create`,
`rename` and `unlink`. The last three receive only an owned sacrificial directory:
create `PATH/created`, rename `PATH` to `PATH.moved`, or unlink `PATH/entry`. The evaluator
cleans up its own controls. Read/write probes open and close the file without returning
contents or changing bytes.

When setup completes, the evaluator emits `{"ready": BOOL, "protection": RECORD,
"capability_lease": LEASE}`. Enable the agent only when `ready` is true. After the attempt,
the parent sends the same lease and optionally `observation` (a fresh independently observed
JSON object), `answer` (a file path), `origins` (allowed origins), and boolean `claim`.
The evaluator performs final probes and emits `{"result": RESULT}`. A changed lease or
missing protocol message is unavailable. Exit codes remain 0 accepted, 1 rejected and
3 unavailable. The parent persists the result outside agent paths and may pass it directly
to `validate_result` or the run-record consumer.

The source keeps its existing layout: `acceptance.json`, visible `fixture/`, protected
`oracle/`, trusted `reference/` and known-bug `mutants/`. A legacy `canary/` directory may
remain sealed as historical fixture data, but it is not executed or represented as a
runtime-patching detector.

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
| `oracle.command` | Optional legacy metadata; never executes assertions beside submitted code |
| `oracle.observations` | Protected JSON file with `module`, `function`, and bounded `cases` of `args` and `expected` |
| `oracle.canary` | Optional legacy metadata; no duplicated canary run or detection claim |
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

An observation file under `oracle/` expects `{"value": <JSON value>}` or `{"error": "ValueError"}`.
The isolated child receives only call arguments and emits a JSON results array. Python
values are serialized before they cross the process boundary; comparison uses bytes in
the evaluator. Existing in-process oracle specifications without `observations` remain
parseable but return `unavailable-unsafe-in-process-oracle`. They cannot be accepted.
Migrate Python call tasks to observations. For another application protocol, use an
independent parent observer with external-state/artifact linkage. Arbitrary legacy
command-oracle execution has not been qualified as protected and is not silently enabled.
An answer-only specification without an oracle, external state, artifact or opened link
returns `independent-observation-required` even when the answer claims completion.

Two complete examples are in `tests/fixtures/task-acceptance/sources/`:
`window-counts` (a repair task with protected tests) and `record-publication` (external
state, answer fields, a produced file and a link).

## Diagnostic commands

`seal`, `workspace` and `validate` retain their existing interfaces. The one-shot `check`
command can still emit a schema-1 diagnostic receipt, but it cannot prove an attempt
lifetime and cannot return accepted. The old `--boundary-adapter` option does not turn a
post-hoc check into a lifetime session. Consumers requiring acceptance must use the
session protocol or hold a Python `Attempt` from before the agent starts.

The final checker opens artifacts and allowed HTTP links itself. It sends no credentials,
uses no proxy and follows no redirects. Origins normalize scheme/host case, default ports
and a root slash; credentials, query strings and non-root paths are invalid origin values.
A declared expected origin mismatch fails before a request. An origin that the evaluator
has not authorized remains unavailable.

Workspace/artifact read errors preserve the receipt. Structurally invalid files fail;
evaluator inability to read a file is unavailable. An unreadable directory contributes an
explicit unreadable entry without erasing known sibling diffs or invalid-test outcomes.
A failed answer remains failed even when no independent observation is available.

## Components and verdict

| Component | Passes when | Otherwise |
| --- | --- | --- |
| `protection` | The source seal matches and actual registered agent routes deny protected access after passing workspace controls | `failed`: overlap, unreadable source or seal mismatch. `unavailable`: missing pin, probes or working route controls |
| `references` | The unchanged fixture and each reference give the declared outcome | `failed`: a reference does not behave as declared, so the oracle is unproven |
| `oracle` | Parent-side observations match for the submitted editable files | `failed`: tests fail or time out, or an editable file is a link or not a regular file |
| `interference` | Not applicable for observation oracles: assertions are outside the submitted process | Records the prevention mechanism and explicitly does not claim runtime-patch detection |
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
`protection:attempt-trust-input-changed`, `protection:seal-mismatch`,
`external_state:record-closed` or `linkage:link-revision-current`.

For a repair task the result records the reproducer: `components.oracle.baseline` (`fail`)
and `components.oracle.post_change` (`pass`). `components.references.outcomes` keeps the
expected and observed outcome of the baseline and every reference. There is no duplicated
canary run. The parent compares semantic stdout and successful exit status; stderr has a
separate diagnostic digest. Trusted non-editable fixture helpers remain available to the
application. Hidden expectations and assertions never enter that process.

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
| Python functions | `seal`, `prepare_workspace`, `begin_attempt`, `checker_identity`, `check_task`, `verdict`, `validate_result`, `load_result` |
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
New evaluations without a pre-attempt lifetime handle and actual route probes are unavailable.

The HARNESS game can keep its oracle inapplicable: the parent browser play-through is
the independent external-state observation, linked to the artifact SHA-256. Its parent
must start the private session before enabling its actual file/command/browser routes,
retain the fixed capability lease, supply its browser observation over the private pipe,
and pin the complete checker manifest and bootstrap. Source
placement, a post-hoc adapter or an old probe summary alone is insufficient. Root coordinates that consumer
change; this repository does not change the harness.
