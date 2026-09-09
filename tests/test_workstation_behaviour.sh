#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT

ws_load_config "$repo_root/config/workstation.conf.example"
# Build environment uses measured memory on Linux; deterministic policy tests
# live in test_amd_build.sh instead of assuming this host has 48 usable threads.
ws_build_jobs() { [[ $1 == normal ]] && printf '12\n' || printf '6\n'; }
[[ "$(ws_build_environment normal)" == *'MAKEFLAGS=-j12'* ]]
[[ "$(ws_build_environment memory-heavy)" == *'MAX_JOBS=6'* ]]
[[ "$(ws_build_environment memory-heavy)" == *'WORKSTATION_BUILD_JOBS=6'* ]]

incomplete_config="$tmp_dir/incomplete-workstation.conf"
grep -v '^MAKE_JOBS=' "$repo_root/config/workstation.conf.example" >"$incomplete_config"
export MAKE_JOBS=99
if (ws_load_config "$incomplete_config") >/dev/null 2>&1; then
  printf 'missing workstation config value was inherited from the environment\n' >&2
  exit 1
fi
unset MAKE_JOBS
ws_load_config "$repo_root/config/workstation.conf.example"

git_dir="$tmp_dir/aur"
git init -q "$git_dir"
printf 'pkgname=test\n' >"$git_dir/PKGBUILD"
git -C "$git_dir" add PKGBUILD
git -C "$git_dir" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$git_dir" remote add origin https://example.invalid/test.git
commit=$(git -C "$git_dir" rev-parse HEAD)
ws_assert_clean_locked_checkout "$git_dir" https://example.invalid/test.git "$commit"

if (ws_validate_clean_chroot /) >/dev/null 2>&1; then
  printf 'filesystem root was accepted as a clean-chroot directory\n' >&2
  exit 1
fi

printf '# dirty\n' >>"$git_dir/PKGBUILD"
if (ws_assert_clean_locked_checkout "$git_dir" https://example.invalid/test.git "$commit") >/dev/null 2>&1; then
  printf 'dirty AUR checkout was accepted\n' >&2
  exit 1
fi
git -C "$git_dir" checkout -q -- PKGBUILD
printf 'untracked\n' >"$git_dir/untracked"
if (ws_assert_clean_locked_checkout "$git_dir" https://example.invalid/test.git "$commit") >/dev/null 2>&1; then
  printf 'untracked AUR content was accepted\n' >&2
  exit 1
fi

fake_bin="$tmp_dir/bin"
mkdir -p "$fake_bin" "$tmp_dir/model"
cat >"$fake_bin/podman" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PODMAN_LOG"
if [[ "$1 $2" == 'container exists' ]]; then exit "${PODMAN_EXISTS_RC:-0}"; fi
if [[ "$1" == stop ]]; then exit "${PODMAN_STOP_RC:-0}"; fi
exit 0
STUB
chmod +x "$fake_bin/podman"

export PATH="$fake_bin:$PATH" PODMAN_LOG="$tmp_dir/podman.log"
ws_require_arch() { :; }
ws_require_user() { :; }
ws_gpu_validate() { :; }
ws_llm_devices() { printf '%s\n%s\n' /dev/dri/card0 /dev/dri/renderD128; }
ws_llm_up 0000:01:00.0 "$tmp_dir/model" "$repo_root/versions.lock" >/dev/null
grep -Fq -- '--workdir /llm --entrypoint /bin/bash' "$PODMAN_LOG"
grep -Fq -- 'source /opt/intel/oneapi/setvars.sh --force' "$PODMAN_LOG"
grep -Fq -- '--userns=keep-id' "$PODMAN_LOG"
grep -Fq -- '-p 127.0.0.1:8000:8000' "$PODMAN_LOG"

: >"$PODMAN_LOG"
export PODMAN_EXISTS_RC=1
ws_llm_down >/dev/null
if grep -q '^stop ' "$PODMAN_LOG"; then
  printf 'LLM stop was attempted for an absent container\n' >&2
  exit 1
fi

export PODMAN_EXISTS_RC=0 PODMAN_STOP_RC=7
if ws_llm_down >/dev/null 2>&1; then
  printf 'LLM stop failure was suppressed\n' >&2
  exit 1
fi

printf 'workstation behavior tests passed\n'
