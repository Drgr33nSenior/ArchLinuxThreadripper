# Performance increment source validation — 11 September 2026

> Dated source-validation evidence. This record is not an installation
> procedure or target qualification. Use the current performance, telemetry,
> memory and session runbooks before owner-run workstation work.

The candidate started from these checkout baselines:

- ArchLinuxThreadripper: `40d2607e794b67220b1d8b139302e6e2baa869fe`.
- Spry.ai-workstation-bridge: `e4c746f949518eec713a54116e51c683deec7b8a`.

The candidate contained local, uncommitted source changes. No commit, tag,
push, package install, deployment, credential use or live workload operation
occurred. The development host is not the Arch workstation. Source-test success
does not qualify hardware, an installed service, an engine image, a cgroup or a
performance result.

## Retained evidence

| Evidence | Result | Boundary |
| --- | --- | --- |
| `test-results/performance-check.ZUl1ac/check.log` | Initial `make check` failed in ShellCheck. | `status` was written from a subshell trap in `performance.sh`; ShellCheck reported SC2030 and SC2031. |
| `test-results/performance-check.ZUl1ac/check-rerun.log` | First corrected full `make check` exited 0 and reported `PASS: 57 test file(s)`. | Before the final disk-evidence and comparison-regression additions; source fixtures and offline syntax/render checks only. |
| `test-results/performance-check.ZUl1ac/check-final.log` | Final-source `make check` exited 0; all 57 fixture scripts passed. | Includes the final disk/path/schema, comparison-regression and sealed-contract changes, with the explicit exclusions below. |
| `test-results/telemetry-images.8Sw6cf/` | Isolated pinned-image telemetry validation passed. | Prometheus config/rules, Alloy configuration, non-root bind-mount readability, Loki processing and OTLP sanitization fixtures ran against local images. This is not a target Linux service deployment. |
| `test-results/performance-check.ZUl1ac/source-export/` | Earlier source export passed. | Archive SHA-256: `fd15688291a6cbfd050c6744abde3e791202a38029a30413846cbd143a28e7ee`; retained as earlier evidence. |
| `test-results/performance-check.ZUl1ac/source-final/` | Final source export and all extracted `SOURCE-MANIFEST.sha256` entries passed. | Archive SHA-256: `98dc4b1b7e2d045d7b26c8c584a963c5f2d4cf273400f2f48f62fdf3493f2014`. No package, signature or ISO was created. |
| Bridge `test-results/performance-check.9jLNHc/export-pair-final.log` | Final-export candidate memory and performance contracts passed with `-race -count=1`. | The actual exported CLI, source closure and negative cases ran; source hashes are not installed-runtime approval. |
| Bridge `test-results/performance-check.9jLNHc/catalog-export-final.log` | Known-good and final-export candidate catalog contracts passed. | The `67a5060` fixture was retained independently; no client was installed or started. |

The ShellCheck failure was corrected by using `warm_probe_status` for the
subshell-specific state. No ShellCheck directive, suppression or weakened exit
handling was added. The complete rerun and final-source check both exited 0.

Final review added point-in-time disk-headroom evidence from the existing
in-Pod probe, with path-boundary checks and no fallback to another filesystem.
Comparison recommendations now account for failed/incomplete startup,
latency/TTFT regressions, explicit memory refusal/pressure, minimum repetitions
and observed variability. Missing measurements stay unknown; combined startup
time is not a pure loading timer. Focused runtime (nine), comparison (ten) and
dispatcher (ten) tests passed; the performance aggregate also passed. These are
fixtures, not actual SGLang execution.

The fresh Bridge pair completed the memory contract runtime and six negative
cases: inflated capacity, average-not-peak, missing phase, forged Pod, unknown
field and shared-memory attribution. It also completed the performance source
closure contract. Both are fixture/source contracts; neither runs an SGLang
image, loads a model or measures host RAM.

## Final suite skips and warnings

The final successful `make check` retained these environmental skips and
warnings. They are not passes.

| Item | Status | Required follow-up or interpretation |
| --- | --- | --- |
| `shfmt` | SKIPPED | The formatter is not installed on this development host. |
| Repeated ccache build | SKIPPED | Requires `ccache` and a C compiler. |
| CMake/Ninja pool fixture | SKIPPED | Requires CMake and Ninja. |
| HIP IPC | NOT RUN | Fixtures cover fork/exec and corruption only; real HIP IPC requires the target runtime. |
| Operator chart render | SKIPPED | `HOME_LAB_GPU_CHART` was not set to the reviewed local chart archive. |
| Gaming image smoke | SKIPPED | `HOME_LAB_GAME_IMAGE` was not set to an already-built local image ID. |
| Wayland process-group check | SKIPPED | `HOME_LAB_WAYLAND_RUNTIME_IMAGE` was not set to a reviewed local Linux runtime image. |
| Bats suite | SKIPPED | `bats` is unavailable. |
| systemd verification | SKIPPED | `systemd-analyze` verification requires Linux. |
| UEFI fixture | WARNING | The synthetic inventory contained a non-unique `Arch Linux (stable)` entry. No firmware was changed. |
| Ansible syntax | WARNING | Empty-inventory and unmatched `k3s_servers` warnings occurred during syntax-only checks. No host was contacted. |

Expected refusal messages from negative fixtures are not suite failures. The
final exit status and the retained log are the authoritative source-test result.

## Not qualified

**NOT RUN — target workstation unavailable:** target hardware discovery,
Linux systemd/cgroup enforcement, installed Bridge helper peer checks, cold and
warm SGLang startup, model loading, GPU/host memory measurement, numerical and
coding quality, HIP/Vulkan builds, telemetry resource overhead, inference queue
overload, AI/gaming handover, interruption/recovery and sustained performance.

The image evidence used local Linux/arm64 images. It verifies the named parser
and fixture paths only. It does not verify Linux/amd64 image behavior, K3s
admission, network policy, collector delivery, service permissions or a GPU.

Before an owner selects or promotes a target profile, collect repeated matched
cold and warm evidence under the existing qualification runbooks. Verify the
current model, runtime, workload, hardware, launch, memory, telemetry and
session identities. Retain the previous selected profile and rollback evidence.
Do not infer a speedup, RAM saving, GPU scaling, wall-power value or safe live
deployment from this record.
