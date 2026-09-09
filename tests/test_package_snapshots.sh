#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repo_root/lib/common.sh"
source "$repo_root/lib/workstation/runtime.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/bin"
key=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
export TEST_SIGNING_KEY=$key TEST_REPO_LOG="$work/repo.log"
cat >"$work/bin/gpg" <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *' --verify '*) printf '[GNUPG:] VALIDSIG %s date timestamp 0 4 0 1 10 00 %s\n' "$TEST_SIGNING_KEY" "$TEST_SIGNING_KEY" ;;
  *) while (($#)); do if [[ $1 == --output ]]; then printf 'synthetic-signature\n' > "$2"; exit 0; fi; shift; done; exit 1 ;;
esac
STUB
cat >"$work/bin/bsdtar" <<'STUB'
#!/usr/bin/env bash
printf 'pkgname = synthetic-rocm\npkgver = 1-1\n'
STUB
cat >"$work/bin/repo-add" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$TEST_REPO_LOG"
[[ ${TEST_REPO_FAIL:-0} == 0 ]] || exit 8
for arg in "$@"; do [[ $arg != *.sig ]] || exit 9; done
printf 'synthetic-db\n' > workstation-rocm.db.tar.gz
STUB
chmod 0755 "$work/bin/"*
export PATH="$work/bin:$PATH"
ws_require_arch() { :; }
ws_require_user() { :; }
printf 'synthetic package\n' >"$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst"
printf 'synthetic signature\n' >"$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst.sig"
ws_package_snapshot experimental "$work/snapshot" "$key" "$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst" >/dev/null
[[ -s $work/snapshot/manifest.json.sig ]]
[[ $(ws_package_restore_plan "$work/snapshot" "$key") == *'pacman -U'* ]]
if (ws_package_snapshot experimental "$work/snapshot" "$key" "$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst") >/dev/null 2>&1; then
  echo 'existing repository was overwritten' >&2
  exit 1
fi
printf 'tampered\n' >>"$work/snapshot/synthetic-rocm-1-1-x86_64.pkg.tar.zst"
if (ws_package_restore_plan "$work/snapshot" "$key") >/dev/null 2>&1; then
  echo 'tampered rollback package was accepted' >&2
  exit 1
fi
if (ws_package_snapshot stable "$work/wrong-key" BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB "$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst") >/dev/null 2>&1; then
  echo 'unapproved signing key was accepted' >&2
  exit 1
fi
[[ ! -e $work/wrong-key ]]
export TEST_REPO_FAIL=1
if (ws_package_snapshot experimental "$work/repo-failure" "$key" "$work/synthetic-rocm-1-1-x86_64.pkg.tar.zst") >/dev/null 2>&1; then
  echo 'failed repository was published' >&2
  exit 1
fi
[[ ! -e $work/repo-failure ]]
unset TEST_REPO_FAIL
echo 'Local package snapshot boundary tests passed (synthetic signing tools)'
