# Hyperloom and TraceLens tools

Use these optional tools to analyze retained evidence and report runs made by
this project's existing SGLang owner workflow. They are separate from monitoring
and the serving image. Installation does not start agents, tune hardware or
change a production default.

## Choose the supported path

| Task | Project command | Boundary |
| --- | --- | --- |
| Inspect the locked environment | `performance hyperloom status`, `help` | Offline; no workload |
| Render an existing Hyperloom breakdown | `performance hyperloom report` | No LLM call |
| Analyze an existing trace | `performance hyperloom trace-analysis` | Offline TraceLens environment; no new capture |
| Import or bind a native SGLang run | `sglang-import`, `sglang-bind` | Evidence/report conversion; not model qualification |
| Delegate a reviewed SGLang owner packet | `sglang-owner` | Checks only by default; explicit execution uses the existing protected owner |

The locked release is **Hyperloom 1.1.5**, commit
`7c0708ec9b75e70c4d9ce5309608f378b7fd8f69`, in
[versions.lock](../infrastructure/hyperloom/versions.lock). Its upstream R9700
runner maps to `gfx1201` but permits vLLM only. That release's general SGLang
support does not open its R9700 framework gate. The project adapter delegates to
an existing SGLang owner instead; it does not bypass that upstream gate.
See the [pinned runner source](https://github.com/AMD-AGI/Hyperloom/blob/7c0708ec9b75e70c4d9ce5309608f378b7fd8f69/src/hyperloom/inference_optimizer/gpu_types.py).

One bounded two-start, eight-request TP2/C1 Qwen3.8-27B benchmark exercised the
adapter's physical handoff and reporting. This is not an autonomous optimization
result, a measured gain or full quality/lifecycle qualification.
[sglang-rocm102.json](../infrastructure/hyperloom/sglang-rocm102.json) records the
owner-selected native stack. Application overlay defaults are separate; see the
[runtime map](README.md#keep-the-runtime-layers-distinct).

Hyperloom and TraceLens are MIT licensed. Provider agents have separate terms;
vendored example kernels also carry their own notices. Retain release and wheel
licences. No provider credentials are needed for the offline commands below.

## Install on the Linux workstation

Status, 9 November 2026: the `/home/developer` paths in this guide are examples
from the runtime wiped by the 2026-11 reinstall. Substitute fresh paths under
the current user's private parent directory.

Use an existing CPython 3.14 SDK with `venv` and `ensurepip`, as the normal
user. Choose fresh absolute paths under a private, caller-owned parent. Do not
run the Linux installer on the Mac controller.

```sh
export WORKSTATION_PYTHON=/usr/bin/python3
bash bin/workstationctl performance hyperloom fetch /home/developer/hyperloom-download-01
bash bin/workstationctl performance hyperloom install \
  /home/developer/hyperloom-download-01/wheelhouse \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
bash bin/workstationctl performance hyperloom status \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
bash bin/workstationctl performance hyperloom help \
  /home/developer/.local/share/workstation/hyperloom/1.1.5
```

Fetch authenticates the release wheel and the 47-wheel Linux x86_64 / CPython
3.14 runtime set. Installation is offline and refuses missing, extra or changed
wheels. The wheel set is about 281 MiB; the installed prefix about 775 MiB.
It contains provider SDKs/CLIs, the release wheel, receipt and empty private home,
without installing Torch, ROCm, SGLang, vLLM or Ray. Failed prefixes and receipts
are retained; use a new prefix after diagnosis.

## Render an existing report

```sh
bash bin/workstationctl performance hyperloom report \
  /home/developer/.local/share/workstation/hyperloom/1.1.5 \
  /absolute/session_breakdown.json /absolute/new-session-report.md
```

The wrapper checks installed code/data against the release wheel, uses an empty
private home, drops inherited credentials and forces `--no-llm`. Its offline
guard denies network, child processes and GPU-device opens. It does not invent a
Hyperloom session from unrelated benchmark results.

## Offline trace analysis with TraceLens

Hyperloom's bypass trace analysis imports `TraceLens.TraceUtils.kernel_source`
for kernel source mapping. `versions.lock` pins TraceLens commit
`614c597bc0c66cabf6a15152037324acc7386090`, the one Hyperloom 1.1.5's
`install_kernel_tools.sh` selects, as a commit-addressed GitHub archive with its
SHA256. TraceLens is MIT licensed.
`infrastructure/hyperloom/tracelens-linux-py314.lock` pins ten binary wheels:
the same Hyperloom release wheel without extras, pandas, NumPy, python-dateutil,
six, tzdata, tqdm, openpyxl, et-xmlfile and itanium-demangler.

TraceLens declares more requirements than this path uses. The import and a
complete bypass run never load `office365-rest-python-client`, `msal`,
matplotlib, tabulate, orjson or PyYAML, so they are not installed. Upstream
installs TraceLens editable at `TRACELENS_ROOT`. Here a site-packages path
entry exposes the authenticated source tree instead, so `setup.py`, a build
backend and the unused requirements never run. `importlib.metadata` therefore
reports no TraceLens version, which upstream handles as a source checkout.

Use a separate prefix; the existing Hyperloom prefix is unchanged:

```sh
export WORKSTATION_PYTHON=/usr/bin/python3
bash bin/workstationctl performance hyperloom tracelens-fetch /home/developer/tracelens-download-01
bash bin/workstationctl performance hyperloom tracelens-install \
  /home/developer/tracelens-download-01/wheelhouse \
  /home/developer/tracelens-download-01/source/TraceLens-614c597bc0c66cabf6a15152037324acc7386090.tar.gz \
  /home/developer/.local/share/workstation/hyperloom-tracelens/614c597b
bash bin/workstationctl performance hyperloom trace-analysis \
  /home/developer/.local/share/workstation/hyperloom-tracelens/614c597b \
  /absolute/trace.json.gz /absolute/new-analysis
```

Add one `--source-path /absolute/source-directory` for each kernel source tree
that TraceLens should search. Paths can contain spaces when quoted. The wrapper
validates the directories and passes them to the pinned TraceLens resolver's
`search_paths` parameter. This adapter operates in memory; installed source
files remain unchanged. `SOURCE-PATHS.json` records the resolved directories.
Omitting the option preserves upstream source discovery. Missing directories
cause the wrapper to refuse the run before it creates the output directory.

For ROCprofiler JSON, first use the offline converter described in
[GPU profiling](GPU-PROFILING.md). The converted trace has kernel timings and
HIP correlations, but no Torch operator attribution.

The fetch downloads about 356 MiB, mostly TraceLens test traces and evaluation
archives. The installed prefix is about 802 MiB, including the retained archive. The installation extracts the complete archive with Python's `data`
filter, keeps the archive and makes the tree's directories read-only. To delete
a retired prefix, first restore owner write permission on its `TraceLens` tree.

Before each analysis, the wrapper checks the installed Hyperloom files against
the release wheel. It also checks every TraceLens package file against the
retained archive and refuses any added file. It runs `bypass_trace_analysis`
with `--framework sglang --target-platform r9700` and sets `TRACELENS_ROOT`.
The run uses the offline guard, the empty private home and caches inside the
new output. Refused network, process and GPU-device operations raise
`PermissionError`, so upstream's best-effort `git`/`rocminfo` provenance probes
record nothing. The output contains Hyperloom's analysis files; it does not
qualify a kernel, select an optimization or launch a workload.

## Research and historical evidence for an optimizer campaign

`sglang-evidence` prepares data for Hyperloom's existing custom-specialist
interface. It authenticates explicitly listed files, verifies supplied seal
membership, and preserves original hashes when compressing traces. It accepts
no more than 128 files, 64 MiB per file and 512 MiB in total. No framework import,
GPU operation or provider call is needed.

```sh
WORKSTATION_PYTHON=/opt/local/bin/python3.11 bash bin/workstationctl performance hyperloom sglang-evidence \
  /absolute/evidence-input.json /absolute/new-evidence-bundle \
  --manifest-sha256 <reviewed-input-sha256> \
  --runtime-root /absolute/bundle-path-on-optimizer-host
```

The version-1 input names the selected baseline artifact, objective, campaign
ceiling (at most 14,400 seconds), fixed constraints, hypotheses and experiment
history. Each artifact has an ID, kind, canonical path, byte count, SHA256 and
description. A `sealed_by` object adds the sealed root and manifest SHA256.
Hypotheses identify their mechanism, falsifying measurement, accuracy risk and
evidence IDs. Historical outcomes retain phases that did not execute; a missing
timing is not a zero timing.

The bundle contains `EVIDENCE-INDEX.json`, authenticated artifacts,
`SPECIALIST-PROMPT.md`, `HYPERLOOM-ARGUMENTS.json` and a checksum manifest.
The arguments use the pinned release's `--custom-specialist-description` and
`--custom-specialist-prompt-file` options. The prompt is limited to the actual
16 KiB CLI bound. `--runtime-root` changes generated references to the final
host path; it does not transfer or execute the bundle.

Preparing evidence does not start the optimizer, qualify the SGLang/gfx12
runner or authorize GPU work. Runtime qualification and new benchmark results
remain separate requirements. Existing offline commands retain their guards.

## Use the SGLang owner adapter

Status, 9 November 2026: the owner workflow and bwrap launch path this adapter
binds to belonged to the op234 runtime, which the 2026-11 reinstall wiped. It
is historical; the current serving path is the
[AI serving stack](../infrastructure/ai-stack/README.md).

The [newer MXFP4/W5 serving direction](SERVING-INTEGRATION.md) is tracked separately.
This adapter still accepts only the historical 4/30-request schedules; tracking
the 176-request W5 results does not qualify or enable that workflow here.

Follow the [owner binding reference](HYPERLOOM-REFERENCE.md) for the packet ABI,
source seals, import commands, compatible controller SDK and explicit execution.
A historical or consumed packet refuses. Execution must retain the owner's
current-boot admission, canonical diagnostic lock, request budget, supervision
and independent resource closeout.

`sglang-import` is diagnostic conversion of separately authenticated files;
`sglang-bind` checks the consumed evidence chain and same-session release.
A matching reviewed quality join can attach task results. Neither operation
promotes full model qualification or passes the optimization-selection gate.
TTFT, ITL, decode rate and energy stay unavailable when the native record does
not measure them; whole-request HTTP throughput includes prefill/instrumentation.

## Rollback

Stop invoking the optional tool and select the previous environment by its path.
No service, model, host SDK or power setting needs changing. Preserve consumed
packets, failed receipts, prefixes and wheelhouses. Do not remove a live prefix;
read-only TraceLens directories need owner write permission before retiring it.
Upgrades use a fresh lock/prefix; upstream 1.1.5 cannot resume older session schemas.
Never run upstream setup/cleanup against an active server or remove the R9700
framework gate to force a launch.
