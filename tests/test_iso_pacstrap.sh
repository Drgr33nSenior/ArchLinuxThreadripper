#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
adapter="$root/infrastructure/iso/docker/pacstrap.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
source "$root/infrastructure/iso/docker/pacstrap.sh"

marker='pid_unshare="unshare --fork --pid"'
printf '%s\n' '# synthetic upstream fixture' "$marker" 'printf "%s\n" "$@"' > "$work/upstream"
program=$(pacstrap_program "$work/upstream" /opt/synthetic/pacstrap)
expected='pid_unshare="unshare --fork --pid /bin/bash /opt/synthetic/pacstrap --reap-child"'
grep -Fxq "$expected" <<< "$program"
diff -u <(grep -Fvx "$marker" "$work/upstream") <(grep -Fvx "$expected" <<< "$program")
actual=$(bash -c "$program" /usr/bin/pacstrap -C 'config with spaces' /synthetic/root base)
[[ $actual == $'-C\nconfig with spaces\n/synthetic/root\nbase' ]] || exit 1
if pacstrap_program "$work/upstream" '/unsafe path/pacstrap' >/dev/null 2>&1; then exit 1; fi
printf '%s\n' "$marker" >> "$work/upstream"
if pacstrap_program "$work/upstream" /opt/synthetic/pacstrap >/dev/null 2>&1; then exit 1; fi
printf 'pid_unshare="changed upstream"\n' > "$work/upstream"
if pacstrap_program "$work/upstream" /opt/synthetic/pacstrap >/dev/null 2>&1; then exit 1; fi

# Test the real reaper's argument/status handling without namespaces or Docker.
actual=$(bash "$adapter" --reap-child printf '%s\n' 'argument with spaces' '*')
[[ $actual == $'argument with spaces\n*' ]] || exit 1
actual=$(printf 'synthetic input\n' | bash "$adapter" --reap-child bash -c 'IFS= read -r line; printf "%s\n" "$line"')
[[ $actual == 'synthetic input' ]] || exit 1
status=0
bash "$adapter" --reap-child bash -c 'exit 7' || status=$?
[[ $status == 7 ]] || exit 1
if bash "$adapter" --reap-child >/dev/null 2>&1; then exit 1; fi
printf 'Pacstrap adapter drift, argument and child-status tests passed\n'
