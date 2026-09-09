#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/bin"
bash_path=$(command -v bash)
for tool in bash dirname sort; do ln -s "$(command -v "$tool")" "$work/bin/$tool"; done
printf ':\n' >"$work/pass.sh"
for script in run syntax; do
  if PATH="$work/bin" "$bash_path" "$root/tests/$script.sh" >"$work/missing.log" 2>&1; then
    printf 'missing rg accepted by %s\n' "$script" >&2
    exit 1
  fi
  grep -Fq 'rg is required' "$work/missing.log"
done
cat >"$work/bin/rg" <<'STUB'
#!/usr/bin/env bash
case $DISCOVERY_CASE in
  failed) exit 2 ;;
  partial) printf '%s\n' "$DISCOVERY_FILE"; exit 2 ;;
  empty) exit 0 ;;
  valid) printf '%s\n' "$DISCOVERY_FILE" ;;
esac
STUB
chmod +x "$work/bin/rg"
for script in run syntax; do
  for scenario in failed partial empty; do
    if PATH="$work/bin" DISCOVERY_CASE="$scenario" DISCOVERY_FILE="$work/pass.sh" \
      "$bash_path" "$root/tests/$script.sh" >"$work/$script-$scenario.log" 2>&1; then
      printf '%s discovery accepted by %s\n' "$scenario" "$script" >&2
      exit 1
    fi
    grep -Fq 'FAIL:' "$work/$script-$scenario.log"
  done
  PATH="$work/bin" DISCOVERY_CASE=valid DISCOVERY_FILE="$work/pass.sh" \
    "$bash_path" "$root/tests/$script.sh"
done
printf 'Discovery failure/empty/partial/missing-tool regressions passed\n'
