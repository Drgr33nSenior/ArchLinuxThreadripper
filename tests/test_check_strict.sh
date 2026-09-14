#!/usr/bin/env bash
# Run the canonical strict wrapper in a private project; no full check recursion.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/project/tests" "$work/bin"
cp "$root/tests/check-strict.sh" "$work/project/tests/"
STRICT_TEE=$(command -v tee) STRICT_AWK=$(command -v awk)
export STRICT_TEE STRICT_AWK
cat >"$work/bin/probe" <<'STUB'
#!/usr/bin/env bash
set -eu
case ${0##*/} in
  python) exit 0 ;; # Only the wrapper's import preflight, no Python execution.
  make)
    case $STRICT_CASE in
      make-failed) exit 19 ;;
      unexpected-skip) printf 'SKIP: synthetic unavailable gate\n' ;;
      allowed-skip) printf 'SKIP: systemd-analyze verification requires Linux\n' ;;
      *) printf 'PASS: synthetic software gate\n' ;;
    esac
    ;;
  tee)
    "$STRICT_TEE" "$@"
    [[ $STRICT_CASE != tee-failed ]] || exit 23
    ;;
  awk)
    [[ $STRICT_CASE != awk-failed ]] || exit 29
    exec "$STRICT_AWK" "$@"
    ;;
esac
STUB
chmod +x "$work/bin/probe"
for tool in python make tee awk; do ln -s probe "$work/bin/$tool"; done
export PATH="$work/bin:$PATH" HOME_LAB_PYTHON="$work/bin/python"
failures=0
for scenario in make-failed unexpected-skip tee-failed awk-failed; do
  if STRICT_CASE=$scenario bash "$work/project/tests/check-strict.sh" >"$work/result.log" 2>&1; then
    printf 'FAIL: strict wrapper accepted %s\n' "$scenario" >&2
    failures=$((failures + 1))
  fi
done
for scenario in valid allowed-skip; do
  STRICT_CASE=$scenario bash "$work/project/tests/check-strict.sh" >"$work/result.log" 2>&1
  grep -q '^PASS: strict software checks;' "$work/result.log"
done
((failures == 0)) || exit 1
printf 'Strict make/log/skip-discovery failures and narrow exceptions passed\n'
