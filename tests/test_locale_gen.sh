#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/bootstrap/common.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

# glibc 2.44 ships entries with two trailing spaces; the old sed missed them.
write_fixture() {
  printf '%s\n' '# Uncomment the needed locales below.' '#' \
    '#en_GB.UTF-8 UTF-8  ' '#en_GB ISO-8859-1  ' '#en_GBXUTF-8 UTF-8  ' '#en_US.UTF-8 UTF-8' >"$work/locale.gen"
}

write_fixture
bootstrap_enable_locale "$work/locale.gen" en_GB.UTF-8 2>/dev/null || fail 'trailing-space entry was not enabled'
[[ $(grep -c '^[^#]' "$work/locale.gen") == 1 ]] || fail 'expected exactly one enabled entry'
grep -qx 'en_GB.UTF-8 UTF-8' "$work/locale.gen" || fail 'en_GB.UTF-8 was not enabled'
grep -qx '#en_GBXUTF-8 UTF-8  ' "$work/locale.gen" || fail 'the dot matched another character'
grep -qx '#en_GB ISO-8859-1  ' "$work/locale.gen" || fail 'a non-UTF-8 entry changed'

# Rerunning is harmless and still succeeds.
bootstrap_enable_locale "$work/locale.gen" en_GB.UTF-8 2>/dev/null || fail 'already-enabled locale was rejected'
[[ $(grep -c '^[^#]' "$work/locale.gen") == 1 ]] || fail 'rerun added an entry'

# Entries without trailing whitespace still work.
write_fixture
bootstrap_enable_locale "$work/locale.gen" en_US.UTF-8 2>/dev/null || fail 'plain entry was not enabled'
grep -qx 'en_US.UTF-8 UTF-8' "$work/locale.gen" || fail 'en_US.UTF-8 was not enabled'

# An unlisted locale fails and leaves the file unchanged.
write_fixture
cp "$work/locale.gen" "$work/before"
if bootstrap_enable_locale "$work/locale.gen" xx_XX.UTF-8 2>/dev/null; then
  fail 'unlisted locale was accepted'
fi
cmp -s "$work/before" "$work/locale.gen" || fail 'failed call changed locale.gen'
[[ -z $(find "$work" -name 'locale.gen.*') ]] || fail 'staging file left behind'

printf 'PASS: locale.gen enabling\n'
