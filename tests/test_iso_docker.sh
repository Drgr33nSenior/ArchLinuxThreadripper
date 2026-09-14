#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$root/lib/common.sh"
wrapper="$root/infrastructure/iso/docker.sh"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export ISO_DOCKER_TEST_LOG="$work/docker-calls"
# Synthetic Docker responses only. This suite never contacts a Docker daemon.
docker() {
  printf '%s\n' "$*" >>"$ISO_DOCKER_TEST_LOG"
  case "$*" in
    *'context inspect'*) printf '%s\n' "${ISO_DOCKER_TEST_ENDPOINT:-unix:///synthetic/docker.sock}" ;;
    *' info '*) printf '%s\n' 'Docker Desktop' ;;
    *'image inspect'*'.Os'*) printf 'linux/amd64\n' ;;
    *'image inspect'*'io.arch-workstation.recipe'*) printf '%s\n' "$ISO_DOCKER_TEST_RECIPE" ;;
    *'image inspect'*'.Id'*) printf 'sha256:%064d\n' 0 ;;
    *'container ls'* | *'volume ls'*)
      [[ ${ISO_DOCKER_TEST_STATE_FAIL:-false} == false ]] || return 1
      if [[ ${ISO_DOCKER_TEST_EXISTS:-false} == true ]]; then printf 'arch-workstation-iso-packages-candidate-01\n'; fi
      ;;
    *' build '*) [[ ${ISO_DOCKER_TEST_BUILD_FAIL:-false} != true ]] ;;
    *'volume create'* | *' run '* | *' cp '*) return 0 ;;
    *) return 1 ;;
  esac
}
export -f docker

plan=$(bash "$wrapper" image)
[[ ! -e $ISO_DOCKER_TEST_LOG && $plan == *'--platform linux/amd64'* ]]
[[ $plan != *'--push'* && $plan == *'/infrastructure/iso/docker/Dockerfile'* ]]
ISO_DOCKER_TEST_RECIPE=$(sed -n 's/.*io.arch-workstation.recipe=\([a-f0-9]*\).*/\1/p' <<<"$plan")
export ISO_DOCKER_TEST_RECIPE
plan=$(bash "$wrapper" check)
[[ $plan == *'--read-only'* && $plan == *'--network none'* && $plan == *'--cap-drop ALL'* ]]
[[ ! -e $ISO_DOCKER_TEST_LOG ]]

mkdir "$work/source" "$work/signed"
for file in bootstrap-source.tar.gz source.lock PKGBUILD; do
  printf 'synthetic input\n' >"$work/source/$file"
done
plan=$(bash "$wrapper" packages candidate-01 "$work/source" "$work/packages")
[[ $plan == *'--user 1000:1000'* && $plan == *'--network none'* && $plan == *'type=volume'* ]]
[[ $plan == *'--memory-swap 6g'* && $plan == *readonly* && $plan == *' cp '* ]]
[[ $plan != *'/input/launch-bootstrap'* && $plan != *'/input/launch-live'* ]]
[[ ! -e $work/packages && ! -e $ISO_DOCKER_TEST_LOG ]]
if bash "$wrapper" packages '../bad-job' "$work/source" "$work/packages" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" packages '' "$work/source" "$work/packages" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" packages candidate-01 "$work/source" "$work/source" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" packages candidate-01 "$work/source" "$work/missing-parent/out" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" --privileged image >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" --allow-iso-mounts packages candidate-01 "$work/source" "$work/packages" >/dev/null 2>&1; then exit 1; fi

revision=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
plan=$(bash "$wrapper" bridge-build candidate-01 "$revision" v0.0.0 "$work/source" "$work/bridge-package")
[[ $plan == *'--platform linux/amd64'* && $plan == *'--user 1000:1000'* && $plan == *'--cap-drop ALL'* ]]
[[ $plan == *'--memory-swap 6g'* && $plan == *"bridge-build $revision v0.0.0"* && $plan != *'--network none'* ]]
[[ $plan == *'/installer/bootstrap-source.tar.gz'* && $plan == *'/installer/source.lock'* ]]
[[ $plan == *'type=bind'* && $plan != *SYS_ADMIN* && ! -e $work/bridge-package && ! -e $ISO_DOCKER_TEST_LOG ]]
if bash "$wrapper" --allow-iso-mounts bridge-build candidate-01 "$revision" v0.0.0 "$work/source" "$work/bridge-package" >/dev/null 2>&1; then exit 1; fi
for bad_revision in main v0.0.0 abc '../bad'; do
  if bash "$wrapper" bridge-build candidate-01 "$bad_revision" v0.0.0 "$work/source" "$work/bridge-package" >/dev/null 2>&1; then exit 1; fi
done
if bash "$wrapper" bridge-build candidate-01 "$revision" latest "$work/source" "$work/bridge-package" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" bridge-build candidate-01 "$revision" v0.0.0 "$work/missing" "$work/bridge-package" >/dev/null 2>&1; then exit 1; fi

# The local path receives only three sealed files, not a checkout or credentials.
# Full archive validation is exercised by test_bridge_build.sh, inside the
# installed builder boundary. This preflight must reject drift before Docker.
mkdir "$work/local-source"
local_source=$(cd -- "$work/local-source" && pwd -P)
printf 'tiny fixture archive\n' >"$work/local-source/spry-bridge-0.0.0-src.tar.gz"
printf 'tiny fixture recipe\n' >"$work/local-source/PKGBUILD"
printf 'never mount\n' >"$work/local-source/unrelated-private-file"
source_sha=$(common::sha256_file "$work/local-source/spry-bridge-0.0.0-src.tar.gz")
recipe_sha=$(common::sha256_file "$work/local-source/PKGBUILD")
printf '%s  spry-bridge-0.0.0-src.tar.gz\n%s  PKGBUILD\n' "$source_sha" "$recipe_sha" >"$work/local-source/SHA256SUMS"
plan=$(bash "$wrapper" bridge-build-local candidate-01 "$source_sha" v0.0.0 "$work/source" "$work/local-source" "$work/local-package")
[[ $plan == *"bridge-build-local $source_sha v0.0.0"* && $plan == *'--user 1000:1000'* && $plan == *'--cap-drop ALL'* ]]
[[ $plan == *'--memory-swap 6g'* && $plan != *SYS_ADMIN* && $plan != *unrelated-private-file* && $plan != *"source=$local_source\\,target="* ]]
for file in spry-bridge-0.0.0-src.tar.gz PKGBUILD SHA256SUMS; do
  [[ $plan == *"target=/candidate/$file\\,readonly"* ]]
done
[[ ! -e $work/local-package && ! -e $ISO_DOCKER_TEST_LOG ]]
for bad_identity in "$revision" main "${source_sha/a/b}"; do
  [[ $bad_identity != "$source_sha" ]] || continue
  if bash "$wrapper" bridge-build-local candidate-01 "$bad_identity" v0.0.0 "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
done
if bash "$wrapper" --allow-iso-mounts bridge-build-local candidate-01 "$source_sha" v0.0.0 "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
if bash "$wrapper" bridge-build-local candidate-01 "$source_sha" latest "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
printf 'tampered\n' >>"$work/local-source/spry-bridge-0.0.0-src.tar.gz"
if bash "$wrapper" bridge-build-local candidate-01 "$source_sha" v0.0.0 "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
printf 'tiny fixture archive\n' >"$work/local-source/spry-bridge-0.0.0-src.tar.gz"
printf 'unlisted manifest record\n' >>"$work/local-source/SHA256SUMS"
if bash "$wrapper" bridge-build-local candidate-01 "$source_sha" v0.0.0 "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
printf '%s  spry-bridge-0.0.0-src.tar.gz\n%s  PKGBUILD\n' "$source_sha" "$recipe_sha" >"$work/local-source/SHA256SUMS"
mv "$work/local-source/PKGBUILD" "$work/local-source/retained-recipe"
ln -s retained-recipe "$work/local-source/PKGBUILD"
if bash "$wrapper" bridge-build-local candidate-01 "$source_sha" v0.0.0 "$work/source" "$work/local-source" "$work/local-package" >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/local-package && ! -e $ISO_DOCKER_TEST_LOG ]]

mkdir "$work/bundle-input" "$work/candidate"
for name in bootstrap boot backup bridge-runtime; do
  printf 'fixture\n' >"$work/bundle-input/arch-workstation-$name-0.1.0-1-any.pkg.tar.zst"
done
printf 'fixture\n' >"$work/bundle-input/source.lock"
for file in spry-ai-workstation-bridge-0.0.0-1-x86_64.pkg.tar.zst spry-bridge-0.0.0-src.tar.gz PKGBUILD builder.lock; do
  printf 'fixture\n' >"$work/candidate/$file"
done
printf 'must not mount\n' >"$work/candidate/unrelated-private-file"
plan=$(bash "$wrapper" bridge candidate-01 "$work/bundle-input" "$work/candidate" "$work/bundled")
[[ $plan == *'--user 1000:1000'* && $plan == *'--cap-drop ALL'* && $plan == *'/candidate/PKGBUILD'* ]]
[[ $plan != *unrelated-private-file* && $plan != *"src=$work/candidate,dst="* && ! -e $work/bundled ]]
printf 'ambiguous\n' >"$work/candidate/spry-ai-workstation-bridge-0.0.1-1-x86_64.pkg.tar.zst"
if bash "$wrapper" bridge candidate-01 "$work/bundle-input" "$work/candidate" "$work/bundled" >/dev/null 2>&1; then exit 1; fi

for file in arch-workstation-bootstrap-0.1.0-1-any.pkg.tar.zst arch-workstation-boot-0.1.0-1-any.pkg.tar.zst arch-workstation.db.tar.gz; do
  printf 'synthetic signed artifact\n' >"$work/signed/$file"
  printf 'synthetic signature\n' >"$work/signed/$file.sig"
done
printf '%s\n' '-----BEGIN PGP PUBLIC KEY BLOCK-----' 'SYNTHETIC, NOT A REAL KEY' >"$work/public.asc"
fingerprint=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
plan=$(bash "$wrapper" iso candidate-01 "$work/signed" "$work/public.asc" "$fingerprint" "$work/iso" 2>/dev/null)
[[ $plan == *'--cap-add SYS_ADMIN'* && $plan == *'--user 0:0'* && $plan == *'--security-opt no-new-privileges'* ]]
[[ $plan == *'--pids-limit 512'* ]]
[[ $plan == *'/project/infrastructure/iso/mkarchiso.sh\,readonly'* ]]
[[ $plan != *'--privileged'* && $plan != *unconfined* && $plan != *docker.sock* && $plan != *'--device'* ]]
[[ ! -e $work/iso && ! -e $ISO_DOCKER_TEST_LOG ]]
if bash "$wrapper" --execute iso candidate-01 "$work/signed" "$work/public.asc" "$fingerprint" "$work/iso" >/dev/null 2>&1; then exit 1; fi
[[ ! -e $ISO_DOCKER_TEST_LOG ]]
printf '%s\n' 'PRIVATE KEY' >>"$work/public.asc"
if bash "$wrapper" iso candidate-01 "$work/signed" "$work/public.asc" "$fingerprint" "$work/iso" >/dev/null 2>&1; then exit 1; fi

# A remote endpoint is rejected before build, volume creation or execution.
if ISO_DOCKER_TEST_ENDPOINT=ssh://synthetic-remote bash "$wrapper" --execute image >/dev/null 2>&1; then exit 1; fi
if grep -Eq ' (build|run|create) ' "$ISO_DOCKER_TEST_LOG"; then exit 1; fi
bash "$wrapper" --execute check >/dev/null
grep -q ' run ' "$ISO_DOCKER_TEST_LOG"
if ISO_DOCKER_TEST_BUILD_FAIL=true bash "$wrapper" --execute image >/dev/null 2>&1; then exit 1; fi
if ISO_DOCKER_TEST_EXISTS=true bash "$wrapper" --execute packages candidate-01 "$work/source" "$work/packages" >/dev/null 2>&1; then exit 1; fi
if ISO_DOCKER_TEST_STATE_FAIL=true bash "$wrapper" --execute packages candidate-01 "$work/source" "$work/packages" >/dev/null 2>&1; then exit 1; fi
[[ ! -e $work/packages ]]

grep -Eq '^FROM docker.io/library/archlinux@sha256:[a-f0-9]{64}$' "$root/infrastructure/iso/docker/Dockerfile"
grep -Fq -- '--mount=type=tmpfs,target=/etc/pacman.d/gnupg' "$root/infrastructure/iso/docker/Dockerfile"
grep -Fxq '**' "$root/infrastructure/iso/.dockerignore"
[[ $(sed -n '/^!/p' "$root/infrastructure/iso/.dockerignore" | wc -l | tr -d ' ') == 8 ]]
grep -Fxq '!docker/bridge-build.sh' "$root/infrastructure/iso/.dockerignore"
grep -Fxq '!docker/bridge-source.py' "$root/infrastructure/iso/.dockerignore"
grep -Fq 'COPY docker/bridge-build.sh docker/bridge-source.py /opt/arch-workstation-builder/' "$root/infrastructure/iso/docker/Dockerfile"
grep -Fxq '!docker/pacstrap.sh' "$root/infrastructure/iso/.dockerignore"
grep -Fq 'COPY --chmod=0755 docker/pacstrap.sh /opt/arch-workstation-builder/bin/pacstrap' "$root/infrastructure/iso/docker/Dockerfile"

# Exercise the real directory/install/check fragment with synthetic pacman only.
# Never execute the trust setup, user creation or absolute-path writes on macOS.
setup_fragment=$(sed -n '/^# Restore shared documentation directories/,/^useradd /{ /^useradd /!p; }' "$root/infrastructure/iso/docker/setup.sh")
[[ $setup_fragment == *'pacman -Qkk archiso'* ]] || exit 1
(
  export ISO_DOCKER_TEST_ROOT="$work/builder-root"
  expected=$(sed -n 's/^ARCHISO_PACKAGE_VERSION=//p' "$root/infrastructure/iso/versions.lock")
  lock="$root/infrastructure/iso/versions.lock"
  export expected lock
  # Exported for the child Bash process that executes the setup fragment.
  # shellcheck disable=SC2329
  install() {
    [[ $* == '-d -m0755 /usr/share/doc /usr/share/man' ]] || return 1
    command install -d -m0755 "$ISO_DOCKER_TEST_ROOT/usr/share/doc" "$ISO_DOCKER_TEST_ROOT/usr/share/man"
  }
  # shellcheck disable=SC2329
  pacman() {
    case $1 in
      -Syu)
        [[ -d $ISO_DOCKER_TEST_ROOT/usr/share/doc && -d $ISO_DOCKER_TEST_ROOT/usr/share/man ]] || return 1
        [[ " $* " == *" openai-codex=$(sed -n 's/^CODEX_PACKAGE_VERSION=//p' "$lock") "* ]] || return 1
        return "${ISO_DOCKER_TEST_INSTALL_STATUS:-0}"
        ;;
      -Q)
        if [[ $2 == openai-codex ]]; then
          printf 'openai-codex %s\n' "${ISO_DOCKER_TEST_CODEX_VERSION:-$(sed -n 's/^CODEX_PACKAGE_VERSION=//p' "$lock")}"
          return 0
        fi
        [[ $* == '-Q archiso' ]] || return 1
        printf 'archiso %s\n' "${ISO_DOCKER_TEST_VERSION:-$expected}"
        ;;
      -Qkk)
        [[ $* == '-Qkk archiso' ]] || return 1
        return "${ISO_DOCKER_TEST_INTEGRITY_STATUS:-0}"
        ;;
      *) return 1 ;;
    esac
  }
  # No real package cache or executable is touched by this fragment regression.
  # shellcheck disable=SC2329 # Exported into the setup-fragment child shell.
  codex() {
    [[ $* == --version ]] || return 1
    printf 'codex-cli %s\n' "$(sed -n 's/^CODEX_CLI_VERSION=//p' "$lock")"
  }
  # shellcheck disable=SC2329 # Exported into the setup-fragment child shell.
  sha256sum() {
    local line
    [[ $* == '--check --strict' ]] || return 1
    IFS= read -r line
    [[ $line == "$(sed -n 's/^CODEX_PACKAGE_SHA256=//p' "$lock")  /var/cache/pacman/pkg/openai-codex-$(sed -n 's/^CODEX_PACKAGE_VERSION=//p' "$lock")-x86_64.pkg.tar.zst" ]] || return 1
    return "${ISO_DOCKER_TEST_CODEX_HASH_STATUS:-0}"
  }
  export -f install pacman codex sha256sum
  bash -euo pipefail -c "$setup_fragment"
  # Directory restoration is also safe when both directories already exist.
  bash -euo pipefail -c "$setup_fragment"
  if ISO_DOCKER_TEST_INSTALL_STATUS=1 bash -euo pipefail -c "$setup_fragment"; then exit 1; fi
  if ISO_DOCKER_TEST_VERSION=unexpected bash -euo pipefail -c "$setup_fragment" >/dev/null 2>&1; then exit 1; fi
  if ISO_DOCKER_TEST_INTEGRITY_STATUS=1 bash -euo pipefail -c "$setup_fragment"; then exit 1; fi
  if ISO_DOCKER_TEST_CODEX_VERSION=unexpected bash -euo pipefail -c "$setup_fragment"; then exit 1; fi
  if ISO_DOCKER_TEST_CODEX_HASH_STATUS=1 bash -euo pipefail -c "$setup_fragment"; then exit 1; fi
)
printf 'Docker ISO wrapper, target, privilege and source-context boundary tests passed\n'
