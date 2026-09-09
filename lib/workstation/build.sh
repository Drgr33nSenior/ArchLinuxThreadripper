#!/usr/bin/env bash
# Native user-space builds. Kernel configuration is a separate operation.

ws_build_defaults() {
  : "${BUILD_RESERVE_MIB:=16384}" "${BUILD_JOB_MIB:=2048}" "${BUILD_HEAVY_JOB_MIB:=4096}"
  : "${BUILD_LINK_MIB:=8192}" "${BUILD_LINK_JOBS:=1}" "${CCACHE_MAX_SIZE:=100G}"
  : "${CCACHE_DIRECTORY:=${XDG_CACHE_HOME:-$HOME/.cache}/workstation/ccache}"
  : "${EXPECTED_GPU_COUNT:=2}" "${EXPECTED_GPU_MODEL:=R9700}"
}

ws_positive_integer() { [[ $1 =~ ^[1-9][0-9]{0,8}$ ]]; }

ws_build_config_validate() {
  ws_build_defaults
  local key
  for key in BUILD_RESERVE_MIB BUILD_JOB_MIB BUILD_HEAVY_JOB_MIB BUILD_LINK_MIB BUILD_LINK_JOBS EXPECTED_GPU_COUNT; do
    ws_positive_integer "${!key}" || {
      ws_die "$key must be a positive integer"
      return 1
    }
  done
  [[ $CCACHE_MAX_SIZE =~ ^[1-9][0-9]{0,4}[GM]$ ]] || {
    ws_die 'CCACHE_MAX_SIZE must be a size such as 100G'
    return 1
  }
  [[ $CCACHE_DIRECTORY == /* && $CCACHE_DIRECTORY != / && $CCACHE_DIRECTORY != *$'\n'* ]] || {
    ws_die 'CCACHE_DIRECTORY must be an absolute cache directory'
    return 1
  }
  [[ $EXPECTED_GPU_MODEL =~ ^[A-Za-z0-9_-]+$ ]] || {
    ws_die 'EXPECTED_GPU_MODEL must be a model token'
    return 1
  }
}

# Inputs are explicit to make the resource policy testable without spoofing the
# real host. The link reserve is additional to the other-workload reserve.
ws_calculate_jobs() {
  local available=$1 cpus=$2 cap=$3 per_job=$4 reserve=$5 link_mib=$6 link_jobs=$7 jobs budget
  local n
  for n in "$available" "$cpus" "$cap" "$per_job" "$reserve" "$link_mib" "$link_jobs"; do
    ws_positive_integer "$n" || ws_die 'invalid build resource measurement'
  done
  budget=$((available - reserve - link_mib * link_jobs))
  ((budget >= per_job)) || ws_die 'insufficient available RAM after workload and link reserves; stop workloads or lower explicit budgets'
  jobs=$((budget / per_job))
  ((jobs <= cap)) || jobs=$cap
  ((jobs <= cpus)) || jobs=$cpus
  printf '%s\n' "$jobs"
}

ws_available_memory_mib() {
  local available group path limit used remaining
  available=$(awk '$1 == "MemAvailable:" {print int($2 / 1024)}' /proc/meminfo)
  ws_positive_integer "$available" || ws_die 'MemAvailable is unavailable'
  # Bound by every applicable cgroup v2 ancestor, including a systemd scope.
  group=$(awk -F: '$1 == "0" {print $3}' /proc/self/cgroup)
  if [[ -n $group && $group == /* && $group != *..* ]]; then
    path="/sys/fs/cgroup${group%/}"
    while [[ $path == /sys/fs/cgroup* ]]; do
      if [[ -r $path/memory.max && -r $path/memory.current ]]; then
        read -r limit <"$path/memory.max"
        read -r used <"$path/memory.current"
        if [[ $limit =~ ^[0-9]+$ && $used =~ ^[0-9]+$ ]]; then
          remaining=$(((limit - used) / 1048576))
          ((remaining >= 0)) || remaining=0
          ((remaining >= available)) || available=$remaining
        fi
      fi
      [[ $path != /sys/fs/cgroup ]] || break
      path=${path%/*}
    done
  fi
  printf '%s\n' "$available"
}

ws_build_session_inhibit_path() { printf '%s\n' /run/workstation/build-inhibit; }

# The session controller owns this root-created marker. Its content is not an
# input to builds: its presence alone cooperatively prevents *new* managed
# compilations. Existing or unrelated processes are deliberately untouched.
ws_build_session_guard() {
  local marker
  marker=$(ws_build_session_inhibit_path) || {
    ws_die 'could not resolve the managed-build inhibit marker'
    return 1
  }
  [[ $marker == /* && $marker != / && $marker != *$'\n'* ]] ||
    {
      ws_die 'managed-build inhibit marker path is invalid'
      return 1
    }
  if [[ -e $marker || -L $marker ]]; then
    [[ -f $marker && ! -L $marker && -r $marker ]] ||
      {
        ws_die 'managed-build inhibit marker exists but is unreadable or unsafe; refuse new compilation'
        return 1
      }
    ws_die 'new managed compilation is inhibited by the active workstation session'
    return 1
  fi
}

ws_build_jobs() {
  local kind=${1:-normal} cap per_job available cpus jobs
  ws_build_config_validate
  ws_build_session_guard
  [[ $(uname -s) == Linux ]] || ws_die 'build concurrency must be measured on the Linux build host'
  case $kind in
    normal)
      cap=${WORKSTATION_MAKE_JOBS:-24}
      per_job=$BUILD_JOB_MIB
      ;;
    memory-heavy)
      cap=${WORKSTATION_MEMORY_HEAVY_JOBS:-16}
      per_job=$BUILD_HEAVY_JOB_MIB
      ;;
    *) ws_die 'build kind must be normal or memory-heavy' ;;
  esac
  available=$(ws_available_memory_mib)
  cpus=$(nproc)
  jobs=$(ws_calculate_jobs "$available" "$cpus" "$cap" "$per_job" "$BUILD_RESERVE_MIB" "$BUILD_LINK_MIB" "$BUILD_LINK_JOBS") || return 1
  printf 'Build budget: %s jobs; %s MiB available, %s MiB workload reserve, %s MiB link reserve, %s MiB/job; CPU/user caps %s/%s\n' \
    "$jobs" "$available" "$BUILD_RESERVE_MIB" "$((BUILD_LINK_MIB * BUILD_LINK_JOBS))" "$per_job" "$cpus" "$cap" >&2
  printf '%s\n' "$jobs"
}

# Emit arguments for a CMake build configured with the Ninja generator.
# These pools govern only CMake-generated Ninja builds; use the measured job
# helper separately for other build systems.
ws_cmake_ninja_args() {
  local kind=${1:-normal} jobs
  ws_build_config_validate || return 1
  jobs=$(ws_build_jobs "$kind") || return 1
  printf '%s\n' "-DCMAKE_JOB_POOLS=compile=$jobs;link=$BUILD_LINK_JOBS"
  printf '%s\n' '-DCMAKE_JOB_POOL_COMPILE=compile'
  printf '%s\n' '-DCMAKE_JOB_POOL_LINK=link'
}

ws_emit_build_environment() {
  local jobs
  ws_build_config_validate
  jobs=$(ws_build_jobs "${1:-normal}") || return 1
  printf 'export WORKSTATION_BUILD_JOBS=%q\n' "$jobs"
  printf 'export MAKEFLAGS=%q\nexport CMAKE_BUILD_PARALLEL_LEVEL=%q\nexport MAX_JOBS=%q\nexport CARGO_BUILD_JOBS=%q\n' "-j$jobs" "$jobs" "$jobs" "$jobs"
  printf 'export CCACHE_DIR=%q\nexport CCACHE_CONFIGPATH=%q\n' "$CCACHE_DIRECTORY" "$CCACHE_DIRECTORY/ccache.conf"
  printf 'export CMAKE_C_COMPILER_LAUNCHER=ccache\nexport CMAKE_CXX_COMPILER_LAUNCHER=ccache\n'
  printf '# Link jobs: %s; reserved link memory: %s MiB; other reserve: %s MiB\n' "$BUILD_LINK_JOBS" "$((BUILD_LINK_MIB * BUILD_LINK_JOBS))" "$BUILD_RESERVE_MIB"
}

# Preserve every installed Arch hardening flag; fail if its known target/level
# shape changes instead of silently replacing an unknown policy.
ws_native_flags() {
  local flags=$1 token march=0 tune=0 optimization=0 result='' tokens=()
  read -r -a tokens <<<"$flags"
  for token in "${tokens[@]}"; do
    case $token in
      -march=x86-64)
        token=-march=native
        march=$((march + 1))
        ;;
      -mtune=generic)
        token=-mtune=native
        tune=$((tune + 1))
        ;;
      -O2) optimization=$((optimization + 1)) ;;
      -march=* | -mtune=* | -O* | -ffast-math | -funsafe-math-optimizations) ws_die "unexpected system build flag: $token" ;;
    esac
    result+="${result:+ }$token"
  done
  ((march == 1 && tune == 1 && optimization == 1)) || ws_die 'expected exactly one generic CPU target, tuning flag and -O2 in Arch flags'
  printf '%s\n' "$result"
}

ws_write_new_or_identical() {
  local staged=$1 destination=$2
  if [[ -e $destination || -L $destination ]]; then
    if [[ ! -f $destination || -L $destination ]] || ! cmp -s "$staged" "$destination"; then
      ws_die "existing configuration differs; review it before replacing: $destination"
    fi
    rm -- "$staged"
  else
    mv -- "$staged" "$destination"
  fi
}

ws_ccache_configure() {
  ws_require_user
  ws_build_config_validate
  command -v ccache >/dev/null || ws_die 'install the official ccache package first'
  [[ ! -L $CCACHE_DIRECTORY ]] || ws_die 'cache directory must not be a symbolic link'
  mkdir -p -- "$CCACHE_DIRECTORY"
  [[ -O $CCACHE_DIRECTORY ]] || ws_die 'cache must belong to the build user'
  chmod 0700 "$CCACHE_DIRECTORY"
  local staged
  staged=$(mktemp "$CCACHE_DIRECTORY/.config.XXXXXX")
  {
    printf 'cache_dir = %s\nmax_size = %s\n' "$CCACHE_DIRECTORY" "$CCACHE_MAX_SIZE"
    printf 'compression = true\ncompiler_check = content\nstats = true\n'
    printf 'hard_link = false\nsloppiness =\n'
  } >"$staged"
  ws_write_new_or_identical "$staged" "$CCACHE_DIRECTORY/ccache.conf"
  ws_note "ccache configured: $CCACHE_DIRECTORY ($CCACHE_MAX_SIZE, compressed, user-owned)"
}

ws_ccache_action() {
  ws_require_user
  ws_build_config_validate
  local action=$1
  [[ -r $CCACHE_DIRECTORY/ccache.conf ]] || ws_die 'run ccache configure first'
  case $action in
    stats) CCACHE_CONFIGPATH="$CCACHE_DIRECTORY/ccache.conf" ccache --show-stats --verbose ;;
    cleanup) CCACHE_CONFIGPATH="$CCACHE_DIRECTORY/ccache.conf" ccache --cleanup ;;
    test) ws_ccache_test ;;
    *) ws_die 'ccache action must be configure, stats, cleanup or test' ;;
  esac
}

ws_ccache_test() (
  set -euo pipefail
  local work compiler before after
  compiler=$(command -v "${CC:-cc}") || ws_die 'C compiler is unavailable'
  ws_build_session_guard
  work=$(mktemp -d "$CCACHE_DIRECTORY/repeat-test.XXXXXX")
  trap 'rm -rf -- "$work"' EXIT
  export CCACHE_CONFIGPATH="$CCACHE_DIRECTORY/ccache.conf"
  export CCACHE_NAMESPACE="workstation-test-${work##*/}"
  export CCACHE_STATSLOG="$work/stats.log"
  ccache "$compiler" -O2 -c "$(ws_repo_root)/tests/fixtures/ccache-smoke.c" -o "$work/first.o"
  before=$(ccache --print-log-stats | awk '$1 == "direct_cache_hit" || $1 == "preprocessed_cache_hit" {n+=$2} END {print n+0}')
  ccache "$compiler" -O2 -c "$(ws_repo_root)/tests/fixtures/ccache-smoke.c" -o "$work/second.o"
  after=$(ccache --print-log-stats | awk '$1 == "direct_cache_hit" || $1 == "preprocessed_cache_hit" {n+=$2} END {print n+0}')
  ((after > before)) || ws_die 'repeat compile produced no ccache hit'
  cmp "$work/first.o" "$work/second.o"
  "$compiler" "$work/second.o" -o "$work/smoke"
  "$work/smoke"
  ccache --show-log-stats
  ws_note 'PASS: repeated compilation hit ccache and produced identical, executable code'
)

ws_makepkg_native_configure() (
  ws_require_arch
  ws_require_user
  [[ $(uname -m) == x86_64 ]] || ws_die 'native Arch profile must be generated on the x86_64 target'
  ws_build_config_validate
  ws_no_swap_check
  local output=${1:-${XDG_CONFIG_HOME:-$HOME/.config}/pacman/makepkg.conf} system=/etc/makepkg.conf
  local cflags cxxflags rustflags staged option buildenv=()
  [[ -r $system && $(stat -c %u "$system") == 0 ]] || ws_die '/etc/makepkg.conf must be root-owned'
  [[ $(stat -c %a "$system") =~ ^[0-7]*[0145][0145]$ ]] || ws_die '/etc/makepkg.conf is group/world writable'
  set +u
  # shellcheck disable=SC1090
  source "$system"
  set -u
  cflags=$(ws_native_flags "${CFLAGS:-}") || exit 1
  cxxflags=$(ws_native_flags "${CXXFLAGS:-}") || exit 1
  rustflags=${RUSTFLAGS:-}
  [[ $rustflags != *target-cpu* ]] || ws_die 'review the existing Rust CPU target before replacing it'
  rustflags="${rustflags:+$rustflags }-C target-cpu=native"
  # BUILDENV comes from the validated, root-owned makepkg.conf above.
  # shellcheck disable=SC2153
  for option in "${BUILDENV[@]}"; do
    [[ $option != ccache && $option != '!ccache' ]] && buildenv+=("$option")
  done
  buildenv+=(ccache)
  mkdir -p -- "$(dirname -- "$output")"
  staged=$(mktemp "$(dirname -- "$output")/.makepkg.XXXXXX")
  trap '[[ ! -f $staged ]] || rm -- "$staged"' EXIT
  {
    printf '# Local-only user-space profile; regenerate after Arch makepkg policy updates.\n'
    printf '# Official packages and kernels remain generic unless separately recorded.\n'
    printf 'CFLAGS=%q\nCXXFLAGS=%q\nRUSTFLAGS=%q\n' "$cflags" "$cxxflags" "$rustflags"
    # Do not freeze yesterday's available memory into a persistent profile.
    # shellcheck disable=SC2016
    printf '%s\n' 'MAKEFLAGS="-j${WORKSTATION_BUILD_JOBS:?Load workstationctl build environment immediately before building}"'
    printf 'BUILDENV=('
    printf '%q ' "${buildenv[@]}"
    printf ')\n'
    printf 'export CCACHE_DIR=%q\nexport CCACHE_CONFIGPATH=%q\n' "$CCACHE_DIRECTORY" "$CCACHE_DIRECTORY/ccache.conf"
  } >"$staged"
  ws_write_new_or_identical "$staged" "$output"
  ws_note "wrote native makepkg profile preserving installed Arch hardening: $output"
)

ws_kernel_cpu_report() {
  ws_require_arch
  local source=$1 compiler=${2:-gcc} report=$3 symbol=n kernel_version
  [[ $(uname -m) == x86_64 && -r $source/arch/x86/Kconfig.cpu && -r $source/Makefile ]] || ws_die 'an x86_64 build host and unpacked kernel source are required'
  command -v "$compiler" >/dev/null || ws_die 'selected kernel compiler is unavailable'
  ws_build_session_guard
  [[ ! -e $report ]] || ws_die 'kernel CPU report already exists'
  # Kconfig itself resolves compiler-version dependencies. This command reports
  # the candidate only; olddefconfig and the resolved .config are the final gate.
  if grep -q '^config X86_NATIVE_CPU$' "$source/arch/x86/Kconfig.cpu" &&
    printf 'int x;\n' | "$compiler" -march=native -x c -fsyntax-only - >/dev/null 2>&1; then
    symbol=X86_NATIVE_CPU
  fi
  kernel_version=$(awk '/^(VERSION|PATCHLEVEL|SUBLEVEL) =/ {printf "%s%s", separator,$3; separator="."}' "$source/Makefile")
  jq -n --arg version "$kernel_version" --arg compiler "$("$compiler" --version | head -n1)" \
    --arg candidate "$symbol" --arg hash "$(common::sha256_file "$source/arch/x86/Kconfig.cpu")" \
    '{kernel_version:$version,compiler:$compiler,kconfig_sha256:$hash,candidate:$candidate,validated:false,
      next:"Use scripts/config in a separate kernel build tree; run olddefconfig with the selected compiler and confirm CONFIG_X86_NATIVE_CPU=y. Otherwise retain the packaged generic configuration."}' >"$report"
}
