# Native ROCm builds and validation

The current target is the TRX50 AI TOP, Threadripper 9960X, two Radeon AI PRO
R9700 AI TOP cards, and 64 GiB DDR5-5600 ECC RDIMM. Both GPUs belong to the Arch
host. The existing K3s VM remains optional and receives neither GPU by default.
The policy is LUKS2/Argon2id, no swap, no zram and no hibernation.

The 2026-09-04 audit found an offline-tested shell architecture for the
RAID0/UKI installer, AUR/kernel builds, workstation tools and KVM/K3s. It found
Intel-specific packages/tests, fixed compilation job counts, no ccache workflow,
and no ROCm provenance or dual-GPU checks. There were no commits or TODO files;
the implementation was untracked alongside pre-staged IDE files. Those files
were preserved. Installation is a one-shot operation, not a resumable installer;
do not rerun it over a partial installation.
The ISO milestone now packages the UKI synchronization helper and ALPM hook as
`arch-workstation-boot`. The optional `arch-workstation-backup` package owns the
Restic runtime. Existing unowned installations require reviewed manual migration;
these packages do not make the pending ROCm source build a packaged release.

## Implementation and remaining qualification

| Area | Implemented | Still pending |
| --- | --- | --- |
| Hardware | Raw probes, JSON status, two-device/driver/target assertions | Actual PCI IDs, GPU architecture, memory topology and boot validation |
| Builds | Native makepkg flags, ccache, memory limits, Ninja job pools, native git-kernel build controls | Target kernel/package builds, benchmarks and actual cache-hit measurement |
| ROCm | Exact source pins, targeted TheRock plan/inventory, llama.cpp HIP build with official Arch or reviewed ROCm 10 AUR SDK | Full source dependency lock, TheRock build and source-built packaging |
| Recovery | Signed local package snapshots and verified rollback command plans | A qualified ROCm package set and restore drill |
| AI | HIP per device, PyTorch numerical checks, RCCL and inference commands | Execution, reboot comparison, correctness review and soak |

`rocm plan` produces `status: plan-not-built`. A source inventory is not a build
manifest proving reproducibility. `rocm build-llama` builds the application,
not ROCm itself, and records `built-not-qualified`. The full ROCm source build
and automatic installation workflow remains pending. See the
[AI audit and measurement procedure](AI-PERFORMANCE.md) for the 2026-09-07 changes.

## Reviewed ROCm 10 SDK provider

The default `ROCM_SDK_PROVIDER=arch` keeps official Arch ROCm package checks.
The opt-in `aur-gfx120x-bin` provider supports only the reviewed
`rocm-gfx120x-bin` 10.0.0-2 package. It repackages AMD's RDNA4 release binaries;
it does not compile ROCm or make those binaries native-CPU builds. SGLang in K3s
uses its own [AMD image candidate](HOME-LAB.md#rocm-10-sglang-candidate), so this
host SDK is not a prerequisite for that container's userspace libraries.

`versions.lock` records the AUR repository, exact commit, PKGBUILD hash, package
version, AMD archive URL and recipe-declared archive hash. The immutable
[reviewed recipe](https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?h=rocm-gfx120x-bin&id=ccac18259575a393b402ea90cd9ef3552081721e)
was inspected on 2026-09-07. Its 2.42 GB source archive was not downloaded or
independently hashed during this code change. Verify it during the reviewed
package build; retain `.BUILDINFO`, package hashes and signatures. Installed
package name/version/ownership checks do not prove how a package was built.

The recipe installs under `/opt/rocm/core` and supplies compatibility links at
`/opt/rocm/bin`, `lib` and `include`. It also changes loader/PATH and OpenCL
configuration and declares conflicts with individual Arch ROCm packages.
Before installing it, preserve a signed complete baseline package snapshot and
review dependants, including host PyTorch. Perform any migration as an explicit
operator-controlled package transaction; neither the installer nor this provider
removes packages or replaces the host `amdgpu` driver. Do not rerun the official
AI package selector over an AUR SDK installation without reviewing conflicts.

After installing and checking the reviewed SDK, create a separate workstation
configuration using the existing example and set:

```ini
ROCM_SDK_PROVIDER=aur-gfx120x-bin
```

Use that file explicitly for the existing native build command:

```sh
./bin/workstationctl --config config/workstation-rocm10.conf \
  ccache configure
./bin/workstationctl --config config/workstation-rocm10.conf \
  hardware collect artifacts/rocm10-boot-01
./bin/workstationctl --config config/workstation-rocm10.conf \
  rocm build-llama /path/to/locked/llama.cpp \
  artifacts/rocm10-boot-01/hardware.json artifacts/llama-rocm10-01
```

The builder checks the selected prefix, compiler/CMake package ownership and
installed package version. It rejects a mixed SDK or unreviewed provider while
retaining official-package checks for the host C/C++ compiler and Vulkan shader
compiler. Source, hardware and build-result gates remain in force. The output
includes `rocm-sdk-provider.txt`, package metadata and `build-result.json` with
the selected provider/prefix. No build result is marked runtime-qualified.
Actual archive file layout, native compilation and GPU execution still require
target checks; a missing required compiler/configuration fails before building.

Nightly SDK packages, `sglang-git` from AUR and arbitrary foreign packages are not
accepted by this provider. A future release needs a reviewed lock and provider
compatibility update, not a floating package name or a global path override.

For rollback, inspect the existing `packages restore-plan` for the saved complete
baseline, execute the reviewed package transaction manually, then select
`ROCM_SDK_PROVIDER=arch` again. A configuration change alone does not reinstall
the old SDK or repair dependent Python binaries. Recollect hardware and rebuild
in a new output directory; do not reuse a ROCm 10 CMake cache for the baseline.

## Collect the target evidence

Install the official Arch baseline with a complete `pacman -Syu` transaction.
For a custom-ISO installation, first review the
[dated-mirror transition](OPERATIONS.md#move-from-the-installation-snapshot-to-rolling-arch).
`templates/workstation/packages.pacman` now selects AMD firmware, RADV, the
official ROCm HIP SDK and ROCm PyTorch package. The legacy Intel list is retained
as `packages-intel.pacman`; its `ai validate` and `llm` commands remain Intel-only.
Keep upstream `amdgpu`; no DKMS replacement or architecture override is added.

Use a new output directory for each observation:

```sh
./bin/workstationctl --config config/workstation.conf hardware collect artifacts/boot-01
./bin/workstationctl swap validate
```

Collection records `lscpu`, `lspci -nnk`, `free -h`, the requested `lsblk` fields,
GCC/Clang versions, GCC native features, `rocminfo`, provider-aware AMD SMI or
legacy ROCm SMI inventory, installed packages,
PCI topology and boot ID. Missing tools remain visible in `commands.json`.
On another OS/architecture, `hardware.json` says `pending` and has no GPU target.
Inspect `gpu-monitor.json` for the selected provider and command/schema status.
See the [audit follow-up](validation/AUDIT-FOLLOWUP-2026-09-09.md) for runtime-library
manifests and matching benchmark/quality configuration. Older llama builds
without sealed runtime manifests need rebuilding in new directories.
An observed report requires two distinct AMD PCI devices bound to `amdgpu` and
two ROCm agents with the same `gfx*` target. HIP testing also checks the R9700
model and independent PCI addresses. Never substitute a guessed architecture.

Check `/dev/kfd` and the selected render nodes are accessible to the user. Add
only the device groups required by their actual ownership, then start a fresh
login. Keep Above 4G Decoding, ReBAR, SVM and IOMMU configured as in the hardware
runbook. Record negotiated PCIe width/speed and thermals before benchmarking.

## Native compilation and ccache

```sh
./bin/workstationctl --config config/workstation.conf ccache configure
./bin/workstationctl --config config/workstation.conf makepkg configure
./bin/workstationctl --config config/workstation.conf build environment normal
./bin/workstationctl --config config/workstation.conf ccache test
./bin/workstationctl --config config/workstation.conf ccache stats
./bin/workstationctl --config config/workstation.conf ccache cleanup
```

The makepkg override preserves the installed Arch hardening, C++ assertions,
frame-pointer, linker and LTO policy. It changes only the known generic CPU
tokens to `-march=native -mtune=native`, retains `-O2 -pipe`, adds Rust's
`target-cpu=native`, and enables `ccache` in `BUILDENV`. Unexpected CPU or
optimization flags cause a refusal. Existing differing user configuration is
never replaced. Generate a candidate filename and review its diff to update it.
The override requires `WORKSTATION_BUILD_JOBS` from a freshly measured build
environment. It does not persist a job count based on memory available when
the profile was generated.

The persistent cache defaults to `$XDG_CACHE_HOME/workstation/ccache`, or
`$HOME/.cache/workstation/ccache`. It is user-owned, compressed, limited to
`CCACHE_MAX_SIZE` (initially 100G), and checks compiler contents. Cleanup uses
ccache's size policy; it does not erase the cache. The repeat test compiles the
same source twice in a unique cache namespace, requires a measured cache hit,
compares the object bytes, links and executes the result. It never zeros shared
statistics or reports a mocked hit as real evidence.

Load the printed build environment in a build shell immediately before each
build. Inspect the output of `build environment` and export its assignments;
do not reuse a budget from a previous session. For CMake/Ninja, also pass
the explicit launchers and job pools. This Bash example reads one argument per
line; preserve the semicolon inside `CMAKE_JOB_POOLS`:

```bash
pool_args=$(./bin/workstationctl --config config/workstation.conf build cmake-ninja-args normal)
# Continue only if that command succeeds; do not reuse an older value.
mapfile -t cmake_pools <<< "$pool_args"
cmake -S SOURCE -B BUILD -G Ninja \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache "${cmake_pools[@]}"
cmake --build BUILD --parallel "$CMAKE_BUILD_PARALLEL_LEVEL"
```

The pools cap generated compile/link rules independently, so the one-link RAM
reservation has a corresponding concurrency limit. They apply to Ninja, not
Make, and cannot constrain arbitrary nested sub-builds or project-specific
pool overrides. Inspect the generated rules. See
[CMake job pools](https://cmake.org/cmake/help/latest/prop_gbl/JOB_POOLS.html).

For compatible Make/Autotools projects, use `CC='ccache gcc'` and
`CXX='ccache g++'` only in that project's build shell. Ninja itself is a job
runner; it benefits from CMake-generated launcher commands. Do not double-wrap
compilers already handled by makepkg. Clean chroots need an explicit user-owned
cache bind and their own ccache configuration; a host environment variable
alone does not make that cache visible inside a chroot.

Do not cache linking, unsupported compiler wrappers, unreproducible generated
inputs, or PCH/modules without the documented compiler-specific conditions.
Keep `hard_link=false` and no global sloppiness. Sensitive/proprietary build
outputs belong in a private cache on encrypted storage. Rust needs a different
cache mechanism; this milestone does not claim ccache accelerates Rust.

No `CMAKE_HIP_COMPILER_LAUNCHER` is enabled. Before enabling it, test the installed
ccache with the exact HIP compiler, `--offload-arch`, `--offload-compress`, cold
and warm compilations, invalidation after header/compiler changes, and execution
on each GPU. TheRock requires a recent ccache and its compiler fingerprint logic
because it bootstraps LLVM and shared compiler libraries. Use the pinned
`build_tools/setup_ccache.py` in an isolated ROCm build workspace after review;
keep its special configuration separate from the normal makepkg cache policy.
Do not evaluate arbitrary downloaded shell output.

## Memory budget without swap

The job limit is the minimum of available CPUs, the configured job ceiling and:

```text
floor((available MiB - other reserve MiB - link jobs × link MiB) / compile-job MiB)
```

Available RAM comes from `MemAvailable`, bounded by the remaining memory limit
of every applicable cgroup v2 ancestor. Defaults reserve 16 GiB for other work
and 8 GiB for one link. Each ordinary compile budgets 2 GiB; a memory-heavy
compile budgets 4 GiB. With 64 GiB actually available, these give at most 20
ordinary or 10 heavy jobs, before CPU ceilings. Real available RAM is lower.
Insufficient headroom fails rather than forcing a job that may exhaust memory.

Recalculate immediately before building, and stop training, games or VMs when
needed. This is an admission estimate, not a guarantee against changing memory
pressure. For TheRock, cap background subprojects at one, LLVM links at one and
Flang compilation at one. Inspect nested Ninja commands: outer `-j` alone does
not bound all sub-builds. Never disable the OOM safeguards to conceal an issue.
Record peak RSS, memory pressure, failed processes and wall time per build.

## ROCm source and package progression

```sh
./bin/workstationctl --config config/workstation.conf \
  rocm plan artifacts/boot-01/hardware.json artifacts/rocm-plan-01
./bin/workstationctl rocm source-manifest /path/to/prepared/TheRock artifacts/rocm-sources.json
```

The entry-point commit in `versions.lock` transitively pins TheRock gitlinks.
The plan sets build, distribution and test target lists to the same observed
GPU architecture and keeps targeted testing enabled. It does not silently
compile every architecture or disable the test suite to shorten a build.
The source-inventory command checks that commit/origin, rejects missing or dirty
submodules, and records every initialized recursive submodule's origin, expected
gitlink, actual commit and tree. Patched commits are marked for review. Local Git
signature verification is recorded honestly; an unavailable public key does not
become a verified signature. Review external Git entries in `BUILD_TOPOLOGY.toml`
and all downloaded archive hashes as well.

The next source-build milestone must complete these steps before promotion:

1. Review the pinned source's requirements, patches and external fetches. Lock
   all Python transitive dependencies with hashes, the compiler/tool package
   set, and all non-git archives. The current upstream `requirements.txt` has
   open version ranges, so pinning TheRock alone is insufficient.
2. Build any required patched patchelf as a PKGBUILD. Inspect ELF program headers
   after relocation. Never use the upstream example that installs it directly
   into `/usr/local`.
3. Fetch only into a fresh workspace. TheRock's `fetch_sources.py` can reset
   submodules and apply patches; do not rerun it on a developer's working tree.
   Archive the prepared source inventory and hashes before compiling.
4. Use distinct build directories for stable/experimental revisions. A clean
   rebuild starts in a new directory; incremental builds reuse only the same
   source, compiler, options and cache configuration. Keep old directories until
   artifacts and logs are archived. No broad cleanup command is supplied.
5. Apply the generated target-specific options plus the reviewed Arch native
   host flags. Verify per-component Release flags remain `-O2`; TheRock can
   override parent flags. Do not pass host flags as device compiler options.
6. Stage `build/dist/rocm`, then package it through a PKGBUILD under a versioned
   `/opt/rocm-workstation/...` prefix. Include licences and a machine-readable
   build manifest with sources, build-root packages, compiler identities,
   flags, GPU targets, cache policy, CMake caches, test results and artifact
   hashes. Check dependencies, RPATH and relocation with Namcap and runtime tests.
7. Pin and build PyTorch/llama.cpp against that same ROCm generation. Use
   `CMAKE_HIP_ARCHITECTURES` for the single detected target in llama.cpp; keep
   PyTorch environments separate. Avoid global PATH/LD_LIBRARY_PATH overrides
   that can load a mixture of official and experimental libraries.

The signed snapshot commands already accept a complete, reviewed package set:

```sh
./bin/workstationctl packages snapshot experimental local-repo/rocm-candidate-01 \
  PUBLIC_SIGNING_FINGERPRINT artifacts/rocm-candidate/*.pkg.tar.zst
./bin/workstationctl packages restore-plan local-repo/rocm-known-good-01 \
  PUBLIC_SIGNING_FINGERPRINT
```

Every package needs a valid detached signature from that explicit fingerprint.
The snapshot retains the archives/signatures, signs a local repo database and
manifest, and rejects an existing destination. The rollback command verifies
the signatures and hashes, then prints one `pacman -U` transaction. It does not
install anything. Use `SigLevel = Required DatabaseRequired` with a local
`file:///...` pacman repository after manually establishing trust in the public
key. Do not use `TrustAll`. Copy accepted package sets off the RAID0 array.

| Package class | Evidence required |
| --- | --- |
| Official binaries | Arch package identity/signature; no local native-build claim |
| Native CPU rebuild | Reviewed PKGBUILD, source hashes, effective compiler flags, tests and package hash |
| CPU + GPU targeted | All native-build evidence plus exact ROCm generation, detected target and GPU tests |
| Excluded | Kernel/recovery packages, portable artifacts, unsupported caches and correctness-sensitive packages until tested |

Snapshot entries default to `optimization: unverified-see-build-manifest`. No
package or complete OS is claimed native-optimized merely because a profile
exists.

## Validate and promote on both GPUs

Select one coherent ROCm/PyTorch environment, ensure its `rocminfo`, `hipcc`
and provider-appropriate AMD SMI or legacy `rocm-smi` are available, and use its
Python executable. The collector retains explicit unavailable/error observations;
the presence of a monitoring command does not qualify sensor coverage.

```sh
./bin/workstationctl --config config/workstation.conf \
  rocm validate artifacts/rocm-validation-01 /path/to/environment/bin/python
./bin/workstationctl --config config/workstation.conf \
  rocm inference /path/to/llama-cli /path/to/model.gguf artifacts/inference-01
```

The suite refuses visibility filters and architecture overrides. It compiles
HIP for one detected target and executes it separately on every device. PyTorch
must report a HIP build and exactly two R9700s. FP32/FP16 and supported hardware
BF16 are compared with CPU references; unsupported BF16 is explicit. A separate
two-process RCCL all-reduce uses PyTorch's `nccl` backend name and verifies the
sum. Timeouts bound collective and inference failures. RCCL success does not
prove direct GPU peer DMA: inspect topology and benchmark peer transfers before
making bandwidth or interconnect claims.

The standalone `rocm inference` command is an **unsealed fixed-config smoke
test**, not a benchmark or promotion gate. It writes `smoke.json`, does not check
the shared runtime manifest, fixes all-device layer splitting and equal shares,
and leaves threads/KV types at the executable's defaults. It does not implement
the benchmark's effective-inference configuration. Use `benchmark-llama` and
`qualify-llama` for sealed, matched performance and numerical evidence.

The smoke command checks the locked llama.cpp version, enumerates actual
ROCm device names, selects both, and requires positive model allocations on
both in the log. Review generated output, but do not use this run for performance
comparisons. Test a small,
local model first. Model VRAM, KV cache and desktop overhead must fit; 64 GiB of
VRAM across two cards is not a single automatic 64 GiB allocation.

Record provider-aware AMD SMI/legacy SMI observations, temperatures, clocks,
host RAM, PCIe errors and kernel logs
during a sustained workload. Compare repeated runs with identical model hashes,
precision, context and batch size. Reboot manually, rerun into a new directory,
compare boot IDs/PCI addresses/architectures, and repeat the same model. Keep
the tested official/LTS UKI and full last-known-good ROCm package set available.

## Encryption and troubleshooting

```sh
./bin/workstationctl encryption calibrate config/install.conf artifacts/argon2-01
```

This calls only `cryptsetup benchmark`, without a device or passphrase. Review
its measured memory, iterations and parallelism and merge the three generated
limits into the installation config. Installation recalibrates for the requested
unlock time and records actual keyslot costs in
`/etc/cryptsetup/argon2id-parameters.json`. Defaults remain a 2-second target,
1 GiB maximum memory and four lanes; validate cold unlock on stable and LTS.
Argon2id affects unlocking, not dm-crypt streaming throughput. Benchmark the
data path separately with a disposable regular file, never a raw root device.

Keep a protected LUKS header backup on separately encrypted offline media after
each keyslot change. The installation runbook describes the manual copy and
verification boundary. A header backup is sensitive and does not recover a
failed RAID0 member.

For missing GPUs, inspect `lspci -nnk`, device permissions, firmware logs and
visibility variables. For source OOM, recalculate the memory budget and inspect
the nested link jobs. For cache misses, use build-specific statistics and compare
compiler/header paths before changing correctness options. For a failed GPU
upgrade, stop workloads, select the known-good UKI and restore the complete
accepted ROCm set; record the failure before another candidate is promoted.
