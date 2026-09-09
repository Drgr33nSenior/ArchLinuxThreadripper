# Validation of e5281734

> Dated evidence for the revisions named below, not a current installation
> procedure. Use [ISO.md](../ISO.md), [INSTALLATION.md](../INSTALLATION.md) and the
> relevant workload runbook for new work. Preserve the recorded failures and skips.


Validation started on 9 September 2026 with a clean checkout at
`e5281734a3b0e1a12f4b0e31e93324fefb7c9098`. The development host is Darwin
25.6.0/arm64, not the installed Arch workstation. **Local tests are not hardware
qualification.** No performance default or software/model lock was changed.

Latest plain `make check` exited **0** with 44 test files, 15 Python tests, six
Bats cases and all three Ansible syntax checks. Three environment-dependent
checks still skipped. **Overall
qualification remains incomplete** until those checks and target hardware
procedures have observed results.

## Retained evidence

Private, ignored output is in `test-results/validation-e5281734.JEFO5V/`.
Keep that directory when handing off this checkout; Git does not retain it.
It contains command logs and exit codes, dependency archives/checksums, the
rendered operator chart, the actual development-host hardware report and the
refused target-validation attempt. Do not publish raw hardware evidence.

| Run | Result | Interpretation |
| --- | --- | --- |
| `make-check-initial.log` | Exit 0; 44 test files | Original commit with existing tools; missing tools caused skips |
| `make-check-prepared.log` | Exit 2 | Added tools exposed shfmt drift and an incorrectly isolated Bats test |
| `focused-fixes.log` | Exit 0 | shfmt and all six Bats cases pass after correction |
| `make-check-after-fixes.log` | Exit 0; 44 test files, 15 Python tests, six Bats cases | Full `make -k -j2 check` with the prepared tools; remaining skips listed below |
| `make-check-final.log` | Exit 2 | Bash 5 exposed a space-padded `wc -l` string comparison in the peer-copy fixture |
| `make-check-final-fixed.log` | Exit 0 | Final plain `make check` under Bash 5.3.15; four skips remain |
| `ansible-syntax-initial.log` | Exit 0 | All three playbooks pass syntax validation in the approved private environment |
| `make-check-with-ansible.log` | Exit 0 | Latest plain `make check` with private Python/Jinja2/PyYAML/Ansible; three skips remain |
| `peer-copy-fixed-bash5.log`, `peer-copy-fixed-bash3.log` | Exit 0 each | Corrected numeric assertion passes on both Bash versions |
| `format-verification.log` | Exit 0 | Before the numeric-assertion correction, every formatter-selected file equalled shfmt output of that file from the original commit |
| `usb-bash5.log` | Exit 0 | USB safety fixtures including Bash-4+ signal injection; no real device operations |
| `gpu-operator-render.exit` | Exit 0 | Locked chart checksum verified; local Helm render, not deployment |
| `hardware/hardware.json` | `pending` | Actual Darwin/arm64 observation; GPU target is null and `hardware_workloads_validated` is false |
| `rocm-target.log` | Exit 1 | Existing Arch platform gate refused the target suite before GPU execution |

The prepared parallel run reports a Ninja warning about the old Make
pipe-based jobserver. The small CMake/Ninja fixture still compiles successfully;
the final serial run avoids that warning. Neither run measures target build
performance. Expected negative-test error messages are not failed test results;
use the retained command exit codes and final test summaries.

## Dependencies and corrections

Initial runs used the configured PyCharm interpreter,
`HOME_LAB_PYTHON=/usr/local/bin/python3.11`: Python 3.11.1, Jinja2 3.1.6 and
PyYAML 6.0. The full suite exercised the offline Jinja/TOML checks. That system
interpreter remains unchanged; the approved Ansible environment is described below.

The validation directory contains private shfmt 3.14.1, ccache 4.14, Ninja
1.13.2, CMake 4.4.3 and Bats 1.14.0. The binary archives were checked against
the SHA-256 digests from their upstream release metadata before execution;
`tools/SHA256SUMS` retains the expected digests. Bats source is pinned to
`eb7f42f8d608ac693d7a4b67474f6714ea68cfc5`, the commit referenced by its
v1.14.0 tag. These are validation tools, not workstation software upgrades.
[shfmt release](https://github.com/mvdan/sh/releases/tag/v3.14.1),
[ccache release](https://github.com/ccache/ccache/releases/tag/v4.14),
[Ninja release](https://github.com/ninja-build/ninja/releases/tag/v1.13.2),
[CMake release](https://github.com/Kitware/CMake/releases/tag/v4.4.3),
[Bats source](https://github.com/bats-core/bats-core/tree/eb7f42f8d608ac693d7a4b67474f6714ea68cfc5).

Private Bash 5.3.15 was built with Apple Clang using `--disable-readline` and
four build jobs. The GNU 5.3 archive and patches 001–015 were verified against
the checksums in the [Homebrew Bash recipe](https://github.com/Homebrew/homebrew-core/blob/HEAD/Formula/b/bash.rb).
`tools/bash-SHA256SUMS`, verification, patch, configure and build logs retain
the inputs and result. The binary runs from its private build directory; no
`make install`, login-shell change or global package installation occurred.

Existing tools include ShellCheck 0.11.0, Ruby 2.6.10, Helm 4.2.4,
kubectl 1.37.0/Kustomize 5.8.1 and Apple Clang 21.0.0. Helm received the
repository's locked target Kubernetes version through `render.sh`; a successful
client-side render does not validate server admission or runtime compatibility.
The downloaded GPU Operator v1.5.1 chart matches `chart.lock` exactly:
`efbd23fe2297350ac23dedf1b916874138f57dd2796b444d1a7f241eaaefc6a8`.

Corrections are limited to:

- Mechanical `shfmt -w -i 2 -ci` formatting of 85 shell files selected by the
  existing Makefile rule. Syntax, ShellCheck and functional tests were rerun.
- `tests/workstation/runtime.bats`: mock only platform/user gates for the
  immutable-image unit test, and forbid device/container execution. A positive
  immutable-image case must reach the mocked device gate. Production guards
  remain unchanged.
- `tests/test_peer_copy_source.sh`: compare the line count numerically so
  macOS `wc` padding cannot cause a false failure under Bash 5. The exact
  five-line requirement and all peer-copy correctness/failure assertions remain.

`lib/workstation/quality_metrics.py` is unchanged. The complete qualification
invocation/parser fixture passed; it is still synthetic HIP/Vulkan execution.
Real native ccache validation recorded one hit from two compilations and
identical executable output. This proves cache operation on the Mac, not a
Threadripper speedup. Real CMake/Ninja compile/link-pool validation also passed.
All 53 rendered GPU Operator objects passed the existing lifecycle and
host-driver-boundary assertions. Final whitespace checks and both documentation
advisory audits passed.

To reproduce from this checkout while the private tools remain present:

```sh
source test-results/validation-e5281734.JEFO5V/environment-ansible.sh
make check
```

The environment script changes only the current shell. Start a fresh shell to
restore its original PATH. No Homebrew package, login shell, driver, cluster,
disk layout, boot configuration or system service was changed. Source corrections are
uncommitted and reviewable; no Git history was changed.

## Approved private Ansible environment

After the owner approved private pip installation, Python 3.11.1 created
`test-results/validation-e5281734.JEFO5V/ansible-env/` without system-site
packages. Installed `ansible-core==2.19.13` from PyPI using binary wheels only.
This is a Python-3.11-compatible validation dependency, not a workstation
Ansible upgrade. [Release metadata](https://pypi.org/project/ansible-core/2.19.13/).

The environment contains Jinja2 3.1.6 and PyYAML 6.0.3. `environment-ansible.sh`
selects its Python as `HOME_LAB_PYTHON` and prepends its executables to PATH.
It selects the private `ansible.cfg`, empty inventory groups and private
collection path. The Makefile still supplies the repository's empty example
inventory explicitly for the bare-metal playbook. No live inventory, secrets,
remote connection or playbook execution was used.

`make ansible` passed all three syntax checks with Ansible Core 2.19.13:
`ansible/k3s.yml`, `ansible/k3s-restore-test.yml` and
`infrastructure/ansible/site.yml`. Empty-host warnings are expected for these
offline checks. Syntax success does not establish runtime idempotence or
compatibility with every Ansible controller version. `pip check` also passed.
The subsequent complete `make check` passed with this environment, including
Jinja/TOML rendering, measurement and qualification fixtures, and all three
Ansible syntax checks. No further runtime code changes were needed.

Retained artifacts include `ansible-install-report.json`,
`ansible-requirements-resolved.txt`, `ansible-requirements.lock`,
`ansible-wheelhouse/`, `ansible-version.txt` and `ansible-syntax-initial.log`
with its exit code. The lock contains all nine exact package versions and the
wheel hashes from pip's install report. A `pip download --require-hashes` run
verified the retained wheels. They are specific to this Python/platform.

The first wheel-cache export used the newer pip-report hash field against
pip 22.3.1's older report schema and failed hash validation. The retained
`ansible-wheelhouse.log` records that failure. The corrected export reads and
validates the report's actual SHA-256 field; `ansible-wheelhouse-verified.log`
records success. Hash checks were not bypassed. Pip's experimental-report
warning and upgrade notice were retained; pip itself was not upgraded.
`validation-summary-with-ansible.json` and `evidence-with-ansible.sha256` record
the updated result. Earlier run summaries and checksums remain historical records.

## Every remaining skip or blocker

| Check | Status and required input |
| --- | --- |
| Linux `systemd-analyze verify` | SKIPPED: this host is macOS. Run the existing `make systemd` on the prepared Linux target. |
| `tests/test_sunshine_image.sh` | SKIPPED: no reviewed, already-built gaming image ID supplied as `HOME_LAB_GAME_IMAGE`. Ordinary checks do not build or pull this image. |
| `tests/test_wayland_processes.sh` | SKIPPED: no reviewed local Linux/amd64 runtime image ID supplied as `HOME_LAB_WAYLAND_RUNTIME_IMAGE`. Local Wayland shell fixtures are not a substitute. |
| Actual Threadripper/R9700/NVMe inventory and resource plan | BLOCKED: installed workstation access was not supplied. Do not substitute the Mac or synthetic hardware reports. |
| HIP smoke, peer-copy, IPC, PyTorch FP32/FP16/BF16 and RCCL size sweep, on host and in K3s | NOT RUN: target hardware, coherent native/container environments and an exact approved Pod are unavailable. |
| HIP/Vulkan numerical/model qualification, same-model one/two-GPU throughput | NOT RUN: target GPUs, retained candidate binaries and reviewed model/corpus inputs are unavailable. |
| SGLang load/startup, streaming/prefix-cache/latency, TP scaling and telemetry | NOT RUN: no mapped target cluster/Pod or staged workload provenance. No endpoint was exposed or contacted. |
| CPU scheduling/TuneD, memory-channel/worker scaling, target clean/warm builds | NOT RUN: no target topology or workload. Mac compiler/cache fixture success is not target measurement. |
| Encrypted XFS/mdadm/NVMe fio, cancellation, sustained thermals and model loading | NOT RUN: no installed encrypted target filesystem or owner-selected scratch directory. No raw-device writes or fio workloads were executed. |
| Gaming capture/encode/input, distinct-frame cadence and AI/gaming handover | NOT RUN: no target GPU/session/client or qualified gaming image. |
| Reboot repeat, sustained soak, stable/LTS boot, TPM/PIN and recovery/backup restore | NOT RUN: target hardware and separately approved maintenance/recovery conditions are unavailable. No reboot or recovery transaction was attempted. |
| Complete source-built TheRock package/rollback validation | BLOCKED by the previously documented incomplete dependency/build/package workflow; it was not implemented by this validation task. |

Earlier skips for shfmt, Bats, ccache, CMake/Ninja and the local GPU Operator
chart are resolved. The USB signal fixture also passed under private Bash 5.3.15.
Ansible syntax validation is now resolved through the approved private environment.
Jinja2/PyYAML rendering was enabled in every recorded full run.

## Resume hardware qualification

Follow [ROCM.md](../ROCM.md#collect-the-target-evidence) and
[PERFORMANCE-VALIDATION.md](../PERFORMANCE-VALIDATION.md#prepare-target-evidence)
on the installed workstation. First identify the approved target and whether it
is serving users. Use the existing maintenance process before releasing GPUs
or replacing workloads. Select the coherent ROCm Python, staged model/corpus,
candidate builds and exact Pod from real inventory, not this report.

Start with a new private output directory and the owner's real configuration:

```sh
umask 077
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/qualification-e5281734-boot01
./bin/workstationctl swap validate
./bin/workstationctl --config config/workstation.conf resources plan artifacts/qualification-e5281734-boot01/hardware.json artifacts/qualification-e5281734-resources
./bin/workstationctl --config config/workstation.conf rocm validate artifacts/qualification-e5281734-rocm /path/to/coherent-rocm/bin/python
```

Then run the existing [llama and Pod commands](../PERFORMANCE-VALIDATION.md#llama-and-multi-gpu-commands),
[serving comparisons](../PERFORMANCE-VALIDATION.md#interactive-and-batch-serving),
[CPU/storage comparisons](../PERFORMANCE-VALIDATION.md#cpu-build-and-encrypted-storage-comparisons)
and [gaming checks](../PERFORMANCE-VALIDATION.md#gaming-refresh-and-rollback).
Retain failures, raw measurements, hardware/boot identities, model/corpus hashes
and effective settings in separate directories for every run. Reboot and repeat
only in the owner's maintenance window. Keep current qualification gates and
baseline defaults until comparable repeated measurements and correctness,
stability, memory and recovery checks pass.
