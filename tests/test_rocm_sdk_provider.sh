#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
lock="$work/versions.lock"
printf '%s\n' \
  'ROCM_AUR_REPOSITORY=https://aur.archlinux.org/rocm-gfx120x-bin.git' \
  'ROCM_AUR_PACKAGE=rocm-gfx120x-bin' \
  'ROCM_AUR_PACKAGE_VERSION=10.0.0-2' \
  'ROCM_AUR_COMMIT=ccac18259575a393b402ea90cd9ef3552081721e' \
  'ROCM_AUR_PKGBUILD_SHA256=578d394a3f82f4006fca1707a2bed85ffc9774b4ca24e75e723399ae52dbb4d8' \
  'ROCM_AUR_SOURCE_URL=https://stable.repo.amd.com/rocm/core/tarball/therock-dist-linux-gfx120X-all-10.0.0.tar.gz' \
  'ROCM_AUR_SOURCE_SHA256=eb99db434a1738fd83b0c3b933146cdb76418f35fcf4647743fbdfef76e8c71f' > "$lock"

unset ROCM_SDK_PROVIDER
[[ $(ws_rocm_sdk_provider) == arch ]]
[[ $(ws_rocm_sdk_prefix) == /opt/rocm ]]
ws_load_config "$repo_root/config/workstation.conf.example"
[[ $ROCM_SDK_PROVIDER == arch ]]

sed 's/^ROCM_SDK_PROVIDER=arch$/ROCM_SDK_PROVIDER=unsupported/' "$repo_root/config/workstation.conf.example" > "$work/unsupported.conf"
if (ws_load_config "$work/unsupported.conf") >/dev/null 2>&1; then
  printf 'unsupported ROCm SDK provider configuration was accepted\n' >&2
  exit 1
fi

ROCM_SDK_PROVIDER=aur-gfx120x-bin
[[ $(ws_rocm_sdk_provider) == aur-gfx120x-bin ]]
[[ $(ws_rocm_sdk_prefix) == /opt/rocm/core ]]
ws_rocm_sdk_aur_lock_validate "$lock" >/dev/null

make_aur_layout() {
  local root=$1 core="$1/core"
  mkdir -p -- "$core/bin" "$core/include" "$core/lib/cmake/hip" "$core/lib/cmake/hipblas" "$core/lib/cmake/rocblas"
  : > "$core/bin/amdclang++"
  : > "$core/bin/hipcc"
  chmod +x "$core/bin/amdclang++" "$core/bin/hipcc"
  : > "$core/lib/cmake/hip/hip-config.cmake"
  : > "$core/lib/cmake/hipblas/hipblas-config.cmake"
  : > "$core/lib/cmake/rocblas/rocblas-config.cmake"
  ln -s "$core/bin" "$root/bin"
  ln -s "$core/lib" "$root/lib"
  ln -s "$core/include" "$root/include"
}

layout_root="$work/rocm"
make_aur_layout "$layout_root"
ws_rocm_sdk_aur_layout_validate "$layout_root" "$layout_root/core"

escaped_layout_root="$work/escaped-rocm"
make_aur_layout "$escaped_layout_root"
rm -- "$escaped_layout_root/bin"
ln -s /tmp "$escaped_layout_root/bin"
if (ws_rocm_sdk_aur_layout_validate "$escaped_layout_root" "$escaped_layout_root/core") >/dev/null 2>&1; then
  printf 'ROCm AUR compatibility symlink escape was accepted\n' >&2
  exit 1
fi

ROCM_SDK_PROVIDER=unsupported
if (ws_rocm_sdk_provider) >/dev/null 2>&1; then
  printf 'unsupported ROCm SDK provider was accepted\n' >&2
  exit 1
fi
ROCM_SDK_PROVIDER=aur-gfx120x-bin

sed 's/ROCM_AUR_PACKAGE_VERSION=10.0.0-2/ROCM_AUR_PACKAGE_VERSION=10.0.1-1/' "$lock" > "$work/unreviewed.lock"
if (ws_rocm_sdk_aur_lock_validate "$work/unreviewed.lock") >/dev/null 2>&1; then
  printf 'unreviewed ROCm AUR lock was accepted\n' >&2
  exit 1
fi

ws_rocm_llama_rocm_owner_output() {
  local path
  for path in "$@"; do
    if [[ ${TEST_ROCM_FOREIGN_TARGET:-} == "$path" ]]; then
      printf 'hip-runtime-amd %s\n' "$path"
    else
      printf '%s %s\n' "${TEST_ROCM_OWNER:-rocm-gfx120x-bin}" "$path"
    fi
  done
}
pacman() {
  case $1 in
    # A package installed from the reviewed local repository is not foreign.
    -Qm) return 1 ;;
    -Q) printf 'rocm-gfx120x-bin %s\n' "${TEST_ROCM_VERSION:-10.0.0-2}" ;;
    *) return 1 ;;
  esac
}

ws_rocm_llama_aur_owner_output rocm-gfx120x-bin 10.0.0-2 \
  "$layout_root/core/bin/hipcc" "$layout_root/core/lib/cmake/hip/hip-config.cmake" >/dev/null

: > "$layout_root/core/bin/hipcc.target"
ln -s "$layout_root/core/bin/hipcc.target" "$layout_root/core/bin/hipcc.symlink"
TEST_ROCM_FOREIGN_TARGET=$(ws_rocm_realpath_existing "$layout_root/core/bin/hipcc.target")
if (ws_rocm_llama_aur_owner_output rocm-gfx120x-bin 10.0.0-2 "$layout_root/core/bin/hipcc.symlink") >/dev/null 2>&1; then
  printf 'ROCm AUR symlink target with a different package owner was accepted\n' >&2
  exit 1
fi
unset TEST_ROCM_FOREIGN_TARGET

TEST_ROCM_OWNER='hip-runtime-amd'
if (ws_rocm_llama_aur_owner_output rocm-gfx120x-bin 10.0.0-2 "$layout_root/core/bin/hipcc") >/dev/null 2>&1; then
  printf 'mixed ROCm package owner was accepted\n' >&2
  exit 1
fi
unset TEST_ROCM_OWNER

TEST_ROCM_VERSION='10.0.1-1'
if (ws_rocm_llama_aur_owner_output rocm-gfx120x-bin 10.0.0-2 "$layout_root/core/bin/hipcc") >/dev/null 2>&1; then
  printf 'mismatched ROCm AUR package version was accepted\n' >&2
  exit 1
fi

printf 'ROCm SDK provider tests passed\n'
