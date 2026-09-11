#!/usr/bin/env bash
# All stage execution is synthetic. Never contact Docker, sign or build an ISO.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
repo="$work/project with spaces"
mkdir -p "$repo/infrastructure/iso" "$repo/infrastructure/packages/bootstrap" "$repo/lib"
cp "$root/infrastructure/iso/release.sh" "$repo/infrastructure/iso/"
cp "$root/lib/common.sh" "$repo/lib/"
export ISO_RELEASE_TEST_LOG="$work/stages" ISO_RELEASE_TEST_RUN="$work/run-path" ISO_RELEASE_TEST_SOURCE="$work/current-source"
printf 'first checkout\n' >"$ISO_RELEASE_TEST_SOURCE"

# Fixture source is literal; variables expand only when the child script runs.
# shellcheck disable=SC2016
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
  'context= execute=false mounts=false' \
  'while (($#)); do case $1 in' \
  '  --context) context=$2; shift 2 ;;' \
  '  --execute) execute=true; shift ;;' \
  '  --allow-iso-mounts) mounts=true; shift ;;' \
  '  *) break ;; esac; done' \
  'action=$1; shift' \
  'printf "%s\n" "$action" >> "$ISO_RELEASE_TEST_LOG"' \
  '[[ $context == desktop-linux || $context == alternate-local ]] || exit 1' \
  '[[ ${ISO_RELEASE_TEST_FAIL:-} != "$action" ]] || exit 7' \
  'case $action in' \
  '  image|check) [[ $execute == true ]] || exit 1 ;;' \
  '  bridge-build)' \
  '    [[ $mounts == false && ! -e $5 && $2 =~ ^[a-f0-9]{40}$ && $3 == v0.0.0 && $4 == */source && -f $4/bootstrap-source.tar.gz && -f $4/source.lock ]] || exit 1' \
  '    if [[ $execute == true ]]; then mkdir "$5"; fi ;;' \
  '  packages)' \
  '    [[ $execute == true && $mounts == false && $1 =~ ^[a-z][a-z0-9-]{0,31}$ ]] || exit 1' \
  '    [[ $2 == */source && $3 == "${2%/source}/packages" && $1 == "$(basename -- "${2%/source}")" ]] || exit 1' \
  '    cmp "$2/source.lock" "$ISO_RELEASE_TEST_SOURCE"' \
  '    mkdir "$3"' \
  '    cp "$2/source.lock" "$3/source.lock"' \
  '    printf "%s\n" "${2%/source}" > "$ISO_RELEASE_TEST_RUN" ;;' \
  '  bridge)' \
  '    [[ $execute == true && $mounts == false && $4 == "${2%/packages}/bundled" ]] || exit 1' \
  '    mkdir "$4"; cp "$2/source.lock" "$4/source.lock"; printf "{}\n" > "$4/bridge-bundle.json" ;;' \
  '  iso)' \
  '    base=${2%/packages}; base=${base%/bundled}; [[ $5 == "$base/$1" ]] || exit 1' \
  '    if [[ $execute == true ]]; then [[ $mounts == true ]] || exit 1; mkdir "$5"; fi' \
  '    printf "%s\n" "$5" > "$ISO_RELEASE_TEST_RUN" ;;' \
  '  *) exit 1 ;;' \
  'esac' >"$repo/infrastructure/iso/docker.sh"
# shellcheck disable=SC2016
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' \
  'printf "prepare\n" >> "$ISO_RELEASE_TEST_LOG"' \
  '[[ ${ISO_RELEASE_TEST_FAIL:-} != prepare ]] || exit 7' \
  'mkdir "$1"' 'cp "$ISO_RELEASE_TEST_SOURCE" "$1/source.lock"' 'printf "synthetic sealed source\n" > "$1/bootstrap-source.tar.gz"' \
  >"$repo/infrastructure/packages/bootstrap/prepare-source.sh"
release="$repo/infrastructure/iso/release.sh"

plan=$(bash "$release" packages 2>&1)
[[ ! -e $repo/build && ! -e $ISO_RELEASE_TEST_LOG ]] || exit 1
[[ $plan == *'1/4:'* && $plan == *'4/4:'* && $plan == *'Preview only'* ]] || exit 1

# A failed image or userspace check cannot prepare source or start packages.
for failed_stage in image check; do
  : >"$ISO_RELEASE_TEST_LOG"
  status=0
  ISO_RELEASE_TEST_FAIL=$failed_stage bash "$release" --execute packages >"$work/result" 2>&1 || status=$?
  [[ $status == 7 && ! -e $repo/build ]] || exit 1
  [[ $(tail -n 1 "$ISO_RELEASE_TEST_LOG") == "$failed_stage" ]] || exit 1
  if grep -q '^ISO_RUN=' "$work/result"; then exit 1; fi
done

# Each execution reads the current checkout and couples the job/source/output.
: >"$ISO_RELEASE_TEST_LOG"
bash "$release" --context alternate-local --execute packages >"$work/result" 2>&1
first_run=$(<"$ISO_RELEASE_TEST_RUN")
[[ $(tr '\n' ' ' <"$ISO_RELEASE_TEST_LOG") == 'image check prepare packages ' ]] || exit 1
printf -v assignment 'ISO_RUN=%q' "$first_run"
grep -Fxq "$assignment" "$work/result"
printf 'second checkout\n' >"$ISO_RELEASE_TEST_SOURCE"
bash "$release" --execute packages >"$work/result" 2>&1
second_run=$(<"$ISO_RELEASE_TEST_RUN")
[[ $first_run != "$second_run" && $(<"$first_run/source/source.lock") == 'first checkout' ]] || exit 1
cmp "$second_run/packages/source.lock" "$ISO_RELEASE_TEST_SOURCE"

# Failed source/package stages retain their work but never report success.
for failed_stage in prepare packages; do
  : >"$ISO_RELEASE_TEST_LOG"
  status=0
  ISO_RELEASE_TEST_FAIL=$failed_stage bash "$release" --execute packages >"$work/result" 2>&1 || status=$?
  [[ $status == 7 && $(tail -n 1 "$ISO_RELEASE_TEST_LOG") == "$failed_stage" ]] || exit 1
  [[ -d $first_run && -d $second_run ]] || exit 1
  if grep -q '^ISO_RUN=' "$work/result"; then exit 1; fi
done

before=$(<"$ISO_RELEASE_TEST_LOG")
if bash "$release" packages old-source >/dev/null 2>&1; then exit 1; fi
if bash "$release" --allow-iso-mounts packages >/dev/null 2>&1; then exit 1; fi
if bash "$release" --context bad/context --execute packages >/dev/null 2>&1; then exit 1; fi
if bash "$release" --privileged packages >/dev/null 2>&1; then exit 1; fi
if bash "$release" --execute iso "$first_run" public.asc SYNTHETIC >/dev/null 2>&1; then exit 1; fi
[[ $(<"$ISO_RELEASE_TEST_LOG") == "$before" ]] || exit 1

# Delegation passes the mount opt-in only for an explicit ISO execution. A new
# attempt name permits reuse of signed packages without overwriting prior output.
bash "$release" iso "$first_run" public.asc SYNTHETIC >"$work/result" 2>&1
iso_preview=$(<"$ISO_RELEASE_TEST_RUN")
[[ ! -e $iso_preview ]] || exit 1
bash "$release" --execute --allow-iso-mounts iso "$first_run" public.asc SYNTHETIC >"$work/result" 2>&1
iso_first=$(<"$ISO_RELEASE_TEST_RUN")
bash "$release" --execute --allow-iso-mounts iso "$first_run" public.asc SYNTHETIC >"$work/result" 2>&1
iso_second=$(<"$ISO_RELEASE_TEST_RUN")
[[ $iso_first != "$iso_second" && -d $iso_first && -d $iso_second ]] || exit 1
printf 'wrong run\n' >"$first_run/packages/source.lock"
before=$(<"$ISO_RELEASE_TEST_LOG")
if bash "$release" iso "$first_run" public.asc SYNTHETIC >/dev/null 2>&1; then exit 1; fi
[[ $(<"$ISO_RELEASE_TEST_LOG") == "$before" ]] || exit 1

# The explicit Bridge stage must use this run, retain old inputs and refuse
# ambiguous output. Incomplete bundle state must never select packages instead.
bash "$release" bridge "$second_run" "$work/candidate" >"$work/result" 2>&1
[[ ! -e $second_run/bundled ]]
bash "$release" --execute bridge "$second_run" "$work/candidate" >"$work/result" 2>&1
cmp "$second_run/bundled/source.lock" "$second_run/source/source.lock"
if bash "$release" --execute bridge "$second_run" "$work/candidate" >/dev/null 2>&1; then exit 1; fi
bash "$release" iso "$second_run" public.asc SYNTHETIC >"$work/result" 2>&1
mv "$second_run/bundled/bridge-bundle.json" "$second_run/bundled/retained-manifest.json"
if bash "$release" iso "$second_run" public.asc SYNTHETIC >/dev/null 2>&1; then exit 1; fi

: >"$ISO_RELEASE_TEST_LOG"
revision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
bash "$release" bridge-build "$second_run" "$revision" v0.0.0 "$work/bridge-built" >"$work/result" 2>&1
[[ ! -e $work/bridge-built && $(tr '\n' ' ' <"$ISO_RELEASE_TEST_LOG") == 'bridge-build ' ]]
: >"$ISO_RELEASE_TEST_LOG"
bash "$release" --execute bridge-build "$second_run" "$revision" v0.0.0 "$work/bridge-built" >"$work/result" 2>&1
[[ -d $work/bridge-built && $(tr '\n' ' ' <"$ISO_RELEASE_TEST_LOG") == 'bridge-build image check bridge-build ' ]]
grep -q '^BRIDGE_ARTIFACTS=' "$work/result"
for failed_stage in image check bridge-build; do
  status=0
  ISO_RELEASE_TEST_FAIL=$failed_stage bash "$release" --execute bridge-build "$second_run" "$revision" v0.0.0 "$work/bridge-failed" >"$work/result" 2>&1 || status=$?
  [[ $status == 7 && ! -e $work/bridge-failed ]]
  if grep -q '^BRIDGE_ARTIFACTS=' "$work/result"; then exit 1; fi
done
mkdir "$work/incomplete-run"
if bash "$release" bridge-build "$work/incomplete-run" "$revision" v0.0.0 "$work/bridge-missing-run" >/dev/null 2>&1; then exit 1; fi

# One reviewed command builds installer, fetches/builds Bridge, then bundles it.
: >"$ISO_RELEASE_TEST_LOG"
bash "$release" packages "$revision" v0.0.0 >"$work/result" 2>&1
[[ ! -s $ISO_RELEASE_TEST_LOG ]]
bash "$release" --execute packages "$revision" v0.0.0 >"$work/result" 2>&1
[[ $(tr '\n' ' ' <"$ISO_RELEASE_TEST_LOG") == 'image check prepare packages bridge-build bridge ' ]]
integrated_run=$(<"$ISO_RELEASE_TEST_RUN")
[[ -d $integrated_run/bundled && -d $integrated_run/bridge-artifacts ]]
for failed_stage in bridge-build bridge; do
  status=0
  ISO_RELEASE_TEST_FAIL=$failed_stage bash "$release" --execute packages "$revision" v0.0.0 >"$work/result" 2>&1 || status=$?
  [[ $status == 7 ]]
  if grep -q '^ISO_RUN=' "$work/result"; then exit 1; fi
done
if bash "$release" --execute packages main v0.0.0 >/dev/null 2>&1; then exit 1; fi

# Check the shell examples without executing their signing or privileged steps.
awk -v output="$work" '
  /^```sh$/ {n++; active=1; next}
  /^```$/ {active=0}
  active {print > (output "/example-" n ".sh")}
' "$root/docs/ISO.md"
for example in "$work"/example-*.sh; do bash -n "$example"; done
printf 'ISO release coordinator: dry-run, fresh inputs, stop-on-failure and retry tests passed\n'
