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

## Analyze an existing trace with TraceLens

Use a separate prefix. The lock selects TraceLens commit
`614c597bc0c66cabf6a15152037324acc7386090`, matching Hyperloom 1.1.5's setup
selection, plus ten hash-pinned binary wheels. Unused TraceLens cloud/plotting
requirements and its build backend are not executed.

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

Fetch is about 356 MiB, including upstream test/evaluation archives; the installed
prefix about 802 MiB. Extraction uses Python's `data` filter. The complete source
archive remains retained and its tree directories are read-only. A site-packages
path entry exposes the checked source without an editable install; the absence
of a TraceLens distribution version is expected for this path.

Before analysis, the wrapper authenticates Hyperloom and every TraceLens package
file, refusing additions. It runs the bypass analyzer with `--framework sglang
--target-platform r9700` and `TRACELENS_ROOT`, using private output caches and the
same offline guard. Best-effort upstream git/rocminfo probes are denied. Analysis
does not qualify kernels, launch a profiler or select a serving optimization.

## Use the SGLang owner adapter

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
