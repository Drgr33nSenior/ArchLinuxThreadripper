#!/usr/bin/env bash
# Synthetic clean-chroot build: validates wrapper arguments and staged artifacts;
# it never compiles, installs, signs, or promotes a kernel.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=lib/common.sh
source "$repo_root/lib/common.sh"
# shellcheck source=lib/workstation/runtime.sh
source "$repo_root/lib/workstation/runtime.sh"

work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
source_dir="$work/linux-git"
chroot_dir="$work/chroot"
output_dir="$work/new-artifacts/candidate"
lock_file="$work/versions.lock"
fake_bin="$work/bin"
mkdir -p -- "$source_dir" "$chroot_dir" "$fake_bin"

git init -q "$source_dir"
printf 'pkgname=linux-git\n' > "$source_dir/PKGBUILD"
printf 'CONFIG_SMP=y\nCONFIG_CPU_MITIGATIONS=y\nCONFIG_MODULE_SIG=y\nCONFIG_DRM_AMDGPU=m\nCONFIG_HSA_AMD=y\nCONFIG_IOMMU_SUPPORT=y\nCONFIG_AMD_IOMMU=y\n' > "$source_dir/config"
printf '# synthetic upstream extra configuration\n' > "$source_dir/config.extra"
printf '# upstream config.user is intentionally replaced after a collision check\n' > "$source_dir/config.user"
printf '# synthetic remote\n' > "$source_dir/remote"
printf 'PATCHES=()\n' > "$source_dir/patches"
git -C "$source_dir" add PKGBUILD config config.extra config.user remote patches
git -C "$source_dir" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$source_dir" remote add origin https://example.invalid/linux-git.git
aur_commit="$(git -C "$source_dir" rev-parse HEAD)"
kernel_commit=73e3f0710014fe6d4ed98cfc02292f6121db7558
cat > "$lock_file" <<EOF
LINUX_GIT_AUR_REPOSITORY=https://example.invalid/linux-git.git
LINUX_GIT_AUR_COMMIT=$aur_commit
LINUX_GIT_SOURCE_REPOSITORY=torvalds/linux
LINUX_GIT_SOURCE_COMMIT=$kernel_commit
EOF

cat > "$fake_bin/makechrootpkg" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'MAKEFLAGS=%s\nARGS=%s\nCFLAGS=%s\nCXXFLAGS=%s\nRUSTFLAGS=%s\nCCACHE_DIR=%s\n' \
  "${MAKEFLAGS-}" "$*" "${CFLAGS-unset}" "${CXXFLAGS-unset}" "${RUSTFLAGS-unset}" "${CCACHE_DIR-unset}" > "$KERNEL_BUILD_LOG"
payload="$(mktemp -d)"
trap 'rm -rf -- "$payload"' EXIT
headers="$payload/usr/lib/modules/synthetic/build"
mkdir -p -- "$headers/arch/x86"
cat > "$headers/.config" <<'EOF'
CONFIG_X86_NATIVE_CPU=y
CONFIG_SMP=y
CONFIG_CPU_MITIGATIONS=y
CONFIG_MODULE_SIG=y
CONFIG_DRM_AMDGPU=m
CONFIG_HSA_AMD=y
CONFIG_IOMMU_SUPPORT=y
CONFIG_AMD_IOMMU=y
EOF
cat > "$headers/arch/x86/Makefile" <<'EOF'
ifdef CONFIG_X86_NATIVE_CPU
        KBUILD_CFLAGS += -march=native
        KBUILD_RUSTFLAGS += -Ctarget-cpu=native
endif
EOF
printf 'installed = gcc-15.1.0-1\ninstalled = rust-1.90.0-1\ninstalled = binutils-2.45-1\ninstalled = make-4.4.1-1\n' > "$payload/.BUILDINFO"
tar -cf linux-git-headers-synthetic-x86_64.pkg.tar.zst -C "$payload" .
tar -cf linux-git-synthetic-x86_64.pkg.tar.zst -C "$payload" .
STUB
cat > "$fake_bin/namcap" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [[ ${KERNEL_NAMCAP_MODE:-warning} == error ]]; then
  printf 'E: synthetic audit error\n'
else
  printf 'W: synthetic warning retained for review\n'
fi
printf '%s\n' "$*" > "$KERNEL_NAMCAP_LOG"
STUB
chmod 0755 "$fake_bin/makechrootpkg" "$fake_bin/namcap"

export PATH="$fake_bin:$PATH" KERNEL_BUILD_LOG="$work/makechrootpkg.log" KERNEL_NAMCAP_LOG="$work/namcap.args"
ws_require_arch() { :; }
ws_require_user() { :; }
ws_validate_clean_chroot() { printf '%s\n' "$1"; }
ws_build_jobs() { [[ $1 == memory-heavy ]] || exit 1; printf '7\n'; }
uname() {
  if [[ ${1:-} == -m ]]; then
    printf 'x86_64\n'
  else
    command uname "$@"
  fi
}

# A changed copied config.user is a collision and must be rejected before staging.
stage_dir="$work/stage"
mkdir "$stage_dir"
git -C "$source_dir" archive --format=tar "$aur_commit" | tar -xf - -C "$stage_dir"
printf '# collision\n' >> "$stage_dir/config.user"
if (ws_kernel_stage_native_config "$source_dir" "$aur_commit" "$stage_dir" "$repo_root/templates/workstation/linux-git.config") >/dev/null 2>&1; then
  printf 'changed copied config.user was accepted\n' >&2
  exit 1
fi

for override in etc/linux-git/config etc/linux-git/remote etc/linux-git/patches/patches; do
  mkdir -p "$(dirname -- "$chroot_dir/root/$override")"
  printf 'synthetic override\n' > "$chroot_dir/root/$override"
  if (ws_kernel_reject_chroot_overrides "$chroot_dir") >/dev/null 2>&1; then
    printf 'clean-chroot linux-git override was accepted: %s\n' "$override" >&2
    exit 1
  fi
  rm -- "$chroot_dir/root/$override"
done
printf 'REMOTE=example.invalid/override\n' > "$chroot_dir/root/etc/linux-git/remote"
if (ws_kernel_build "$source_dir" "$chroot_dir" "$output_dir" "$lock_file") >/dev/null 2>&1; then
  printf 'clean-chroot linux-git override was accepted\n' >&2
  exit 1
fi
[[ ! -e $KERNEL_BUILD_LOG ]]
rm -- "$chroot_dir/root/etc/linux-git/remote"

CCACHE_DIR="$work/shared-ccache" ws_kernel_build "$source_dir" "$chroot_dir" "$output_dir" "$lock_file" >/dev/null
grep -Fxq 'MAKEFLAGS=-j7' "$KERNEL_BUILD_LOG"
grep -Fxq "ARGS=-c -n -r $chroot_dir -- --syncdeps" "$KERNEL_BUILD_LOG"
grep -Fxq 'CFLAGS=unset' "$KERNEL_BUILD_LOG"
grep -Fxq 'CXXFLAGS=unset' "$KERNEL_BUILD_LOG"
grep -Fxq 'RUSTFLAGS=unset' "$KERNEL_BUILD_LOG"
grep -Fxq 'CCACHE_DIR=unset' "$KERNEL_BUILD_LOG"
grep -Fq -- 'linux-git-headers-synthetic-x86_64.pkg.tar.zst' "$KERNEL_NAMCAP_LOG"
grep -Fxq 'CONFIG_X86_NATIVE_CPU=y' "$output_dir/linux-git.effective.config"
grep -Fq -- 'CONFIG_X86_NATIVE_CPU=y' "$output_dir/linux-git.config.delta"
grep -Fxq 'gcc=15.1.0-1' "$output_dir/linux-git.chroot-toolchain.txt"
grep -Fxq 'clang=absent-from-package-buildinfo' "$output_dir/linux-git.chroot-toolchain.txt"
grep -Fxq 'installed = rust-1.90.0-1' "$output_dir/linux-git.headers.BUILDINFO"
grep -Fxq 'W: synthetic warning retained for review' "$output_dir/linux-git.namcap.txt"
grep -Fxq "AUR_COMMIT=$aur_commit" "$output_dir/linux-git.build-lock"
grep -Eq '^EFFECTIVE_CONFIG_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
grep -Fxq 'MEASURED_BUILD_JOBS=7' "$output_dir/linux-git.build-lock"
grep -Fxq 'MAKEFLAGS=-j7' "$output_dir/linux-git.build-lock"
grep -Eq '^[a-f0-9]{64}  linux-git-headers-synthetic-x86_64\.pkg\.tar\.zst$' "$output_dir/linux-git.package-sha256"

if (KERNEL_NAMCAP_MODE=error ws_kernel_namcap_audit "$work/namcap-error.txt" "$output_dir/linux-git-synthetic-x86_64.pkg.tar.zst") >/dev/null 2>&1; then
  printf 'namcap errors did not stop the candidate flow\n' >&2
  exit 1
fi

before="$(wc -l < "$KERNEL_BUILD_LOG")"
if (ws_kernel_build "$source_dir" "$chroot_dir" "$output_dir" "$lock_file") >/dev/null 2>&1; then
  printf 'existing kernel output candidate was replaced\n' >&2
  exit 1
fi
[[ "$(wc -l < "$KERNEL_BUILD_LOG")" == "$before" ]]

printf 'native kernel staging and audit tests passed\n'
