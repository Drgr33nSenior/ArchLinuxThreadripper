#!/usr/bin/env bash
# Exercise the real Makefile recipes with bounded discovery/checker fixtures.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/bin"
GATE_SORT=$(command -v sort)
export GATE_SORT GATE_LOG="$work/checker.log"
export GATE_FILE="$work/fixture sample.sh"
printf ':\n' >"$GATE_FILE"
cat >"$work/bin/probe" <<'STUB'
#!/usr/bin/env bash
set -eu
tool=${0##*/}
case $tool in
  rg | find)
    if [[ $tool == "$GATE_FAIL_TOOL" ]]; then
      case $GATE_CASE in
        failed) exit 2 ;;
        partial) printf '%s\n' "$GATE_FILE"; exit 2 ;;
        empty) exit 0 ;;
      esac
    fi
    printf '%s\n' "$GATE_FILE"
    ;;
  sort)
    if [[ $GATE_FAIL_TOOL == sort ]]; then
      "$GATE_SORT" "$@"
      exit 2
    fi
    exec "$GATE_SORT" "$@"
    ;;
  shellcheck | shfmt | ruby)
    printf '%s\n' "$tool" >>"$GATE_LOG"
    [[ $GATE_CASE != checker-failed ]] || exit 19
    if [[ $GATE_CASE == arguments ]]; then
      found=0
      for arg in "$@"; do
        [[ $arg != "$GATE_FILE" ]] || found=1
      done
      ((found == 1)) || { printf 'file argument was split\n' >&2; exit 20; }
    fi
    ;;
esac
STUB
chmod +x "$work/bin/probe"
for tool in rg find sort shellcheck shfmt ruby; do ln -s probe "$work/bin/$tool"; done
export PATH="$work/bin:$PATH"
failures=0
for target in shellcheck shfmt yaml; do
  discovery_tools=(rg sort)
  [[ $target != yaml ]] || discovery_tools=(find rg sort)
  for tool in "${discovery_tools[@]}"; do
    scenarios=(failed partial empty)
    [[ $tool != sort ]] || scenarios=(partial)
    for scenario in "${scenarios[@]}"; do
      if GATE_FAIL_TOOL=$tool GATE_CASE=$scenario make -s -C "$root" "$target" >"$work/result.log" 2>&1; then
        printf 'FAIL: %s accepted %s %s discovery\n' "$target" "$tool" "$scenario" >&2
        failures=$((failures + 1))
      fi
    done
  done
  if GATE_FAIL_TOOL=none GATE_CASE=checker-failed make -s -C "$root" "$target" >"$work/result.log" 2>&1; then
    printf 'FAIL: %s accepted checker failure\n' "$target" >&2
    failures=$((failures + 1))
  fi
  if ! GATE_FAIL_TOOL=none GATE_CASE=arguments make -s -C "$root" "$target" >"$work/result.log" 2>&1; then
    printf 'FAIL: %s did not preserve file arguments\n' "$target" >&2
    failures=$((failures + 1))
  fi
done
# Missing discovery tools must not become an empty successful check.
for tool in bash dirname make; do ln -s "$(command -v "$tool")" "$work/bin/$tool"; done
for tool in rg find; do
  mv "$work/bin/$tool" "$work/$tool"
  targets=(yaml)
  [[ $tool != rg ]] || targets=(shellcheck shfmt yaml)
  for target in "${targets[@]}"; do
    if PATH="$work/bin" GATE_FAIL_TOOL=none GATE_CASE=valid make -s -C "$root" "$target" >"$work/result.log" 2>&1; then
      printf 'FAIL: %s accepted missing %s\n' "$target" "$tool" >&2
      failures=$((failures + 1))
    fi
  done
  mv "$work/$tool" "$work/bin/$tool"
done
((failures == 0)) || exit 1
printf 'Makefile discovery, checker failures and file arguments passed\n'
