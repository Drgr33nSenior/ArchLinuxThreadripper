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
profile_dir="$work/profile"
lock_file="$work/versions.lock"
fake_bin="$work/bin"
fixture_config="$work/effective.config"
required_modules="$work/required-modules.txt"
policy="$repo_root/infrastructure/kernel/threadripper-9960x-dual-r9700.json"
mkdir -p -- "$source_dir" "$chroot_dir" "$fake_bin" "$profile_dir"

git init -q "$source_dir"
cat >"$source_dir/PKGBUILD" <<'RECIPE'
# Synthetic executable metadata; exact patch context from the locked recipe.

pkgbase=linux-git
pkgver=7.0.r11782.c1f49dea2b8f
pkgrel=1
pkgdesc="Linus Torvalds' Mainline Linux"
url="https://www.kernel.org"
arch=(x86_64)
backup=(etc/linux-git/config etc/linux-git/remote etc/linux-git/patches/patches)
_package() {
  :
}

_package-headers() {
  pkgdesc="Headers and scripts for building modules for the $pkgdesc kernel"
  depends=(
    binutils
    glibc
    libelf
    libgcc
    openssl
    pahole
    xxhash
    zlib
    zstd
  )
}
RECIPE
printf 'CONFIG_SMP=y\nCONFIG_CPU_MITIGATIONS=y\nCONFIG_MODULE_SIG=y\nCONFIG_DRM_AMDGPU=m\nCONFIG_HSA_AMD=y\nCONFIG_IOMMU_SUPPORT=y\nCONFIG_AMD_IOMMU=y\n' >"$source_dir/config"
printf '# synthetic upstream extra configuration\n' >"$source_dir/config.extra"
printf '# upstream config.user is intentionally replaced after a collision check\n' >"$source_dir/config.user"
printf '# synthetic remote\n' >"$source_dir/remote"
printf 'PATCHES=()\n' >"$source_dir/patches"
git -C "$source_dir" add PKGBUILD config config.extra config.user remote patches
git -C "$source_dir" -c user.name=Test -c user.email=test@example.invalid commit -qm initial
git -C "$source_dir" remote add origin https://example.invalid/linux-git.git
aur_commit="$(git -C "$source_dir" rev-parse HEAD)"
kernel_commit=a90ee4305c4a5df72c11b31dacfdc76e00fcf78a
cat >"$lock_file" <<EOF
LINUX_GIT_AUR_REPOSITORY=https://example.invalid/linux-git.git
LINUX_GIT_AUR_COMMIT=$aur_commit
LINUX_GIT_SOURCE_REPOSITORY=torvalds/linux
LINUX_GIT_SOURCE_TAG=v7.3-rc6
LINUX_GIT_SOURCE_TAG_OBJECT=4eeccbed21e50c19f97be9d325511f3de6343f2d
LINUX_GIT_SOURCE_COMMIT=$kernel_commit
EOF

jq -r '.required_config | to_entries[] | "\(.key)=\(.value[0])"' "$policy" >"$fixture_config"
jq -r '.required_modules[]' "$policy" >"$required_modules"
config_sha="$(common::sha256_file "$fixture_config")"
policy_sha="$(common::sha256_file "$policy")"
cp "$fixture_config" "$profile_dir/current.config"
jq -n --arg policy "$policy_sha" --arg config "$config_sha" \
  --argjson platform "$(jq -c '.platform' "$policy")" \
  --argjson cpu "$(jq -c '.cpu' "$policy")" \
  --argjson modules "$(jq -c '.required_modules' "$policy")" \
  --argjson required "$(jq -c '.required_config | with_entries(.value = .value[0])' "$policy")" \
  --argjson pci "$(jq -c '[.pci[] as $item | range(0; $item.count) |
    {vendor:$item.vendor,
     device:$item.device, class:($item.class_prefix + "00"),
     driver:(if $item.allow_unbound then null else $item.drivers[0] end),
     module:(if $item.allow_unbound then null else $item.modules[0] end)}] |
    to_entries | map(.value + {bdf:("0000:" + ((.key + 16) | tostring) + ":00.0")})' "$policy")" \
  '{schema:1,status:"observed",policy_sha256:$policy,platform:$platform,cpu:$cpu,pci:$pci,
    kernel_release:"fixture",loaded_modules:["amdgpu","nvme"],required_modules:$modules,
    candidate_config_failures:[],failures:[],
    current_config:{source:"synthetic",sha256:$config,required:$required},scope:"synthetic fixture"}' \
  >"$profile_dir/profile.json"
jq -n --arg config "$(common::sha256_file "$profile_dir/current.config")" \
  --arg profile "$(common::sha256_file "$profile_dir/profile.json")" \
  '{"current.config":$config,"profile.json":$profile}' >"$profile_dir/MANIFEST.json"
chmod 0700 "$profile_dir"
chmod 0600 "$profile_dir"/*

cat >"$fake_bin/makechrootpkg" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf 'MAKEFLAGS=%s\nNPROC=%s\nARGS=%s\nCFLAGS=%s\nCXXFLAGS=%s\nRUSTFLAGS=%s\nCCACHE_DIR=%s\n' \
  "${MAKEFLAGS-}" "${NPROC-}" "$*" "${CFLAGS-unset}" "${CXXFLAGS-unset}" "${RUSTFLAGS-unset}" "${CCACHE_DIR-unset}" > "$KERNEL_BUILD_LOG"
payload="$(mktemp -d)"
trap 'rm -rf -- "$payload"' EXIT
headers="$payload/usr/lib/modules/synthetic/build"
mkdir -p -- "$headers/arch/x86"
cp "$KERNEL_FIXTURE_CONFIG" "$headers/.config"
cat > "$headers/arch/x86/Makefile" <<'EOF'
ifdef CONFIG_X86_NATIVE_CPU
        KBUILD_CFLAGS += -march=native
        KBUILD_RUSTFLAGS += -Ctarget-cpu=native
endif
EOF
printf 'installed = gcc-15.1.0-1\ninstalled = rust-1.90.0-1\ninstalled = binutils-2.45-1\ninstalled = make-4.4.1-1\n' > "$payload/.BUILDINFO"
while IFS= read -r module; do
  [[ $module != "${KERNEL_DROP_MODULE:-}" ]] || continue
  mkdir -p "$payload/usr/lib/modules/synthetic/kernel"
  : >"$payload/usr/lib/modules/synthetic/kernel/$module.ko.zst"
done <"$KERNEL_REQUIRED_MODULES"
printf 'kernel/drivers/usb/host/xhci-pci.ko\n' >"$payload/usr/lib/modules/synthetic/modules.builtin"
tar -cf linux-git-headers-synthetic-x86_64.pkg.tar.zst -C "$payload" .
tar -cf linux-git-synthetic-x86_64.pkg.tar.zst -C "$payload" .
# Real devtools puts these alongside packages; they must never become packages.
printf 'W: retained audit\n' >linux-git-headers-synthetic-x86_64.pkg.tar.zst-namcap.log
printf 'not a package\n' >linux-git-headers-synthetic-x86_64.pkg.tar.zst.sig
STUB
cat >"$fake_bin/namcap" <<'STUB'
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
export KERNEL_FIXTURE_CONFIG="$fixture_config" KERNEL_REQUIRED_MODULES="$required_modules"
export WORKSTATION_PYTHON="${HOME_LAB_PYTHON:?HOME_LAB_PYTHON is required}"
ws_require_arch() { :; }
ws_require_user() { :; }
ws_validate_clean_chroot() { printf '%s\n' "$1"; }
ws_build_jobs() {
  [[ $1 == kernel ]] || exit 1
  printf '7\n'
}
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
printf '# collision\n' >>"$stage_dir/config.user"
if (ws_kernel_stage_native_config "$source_dir" "$aur_commit" "$stage_dir" "$repo_root/templates/workstation/linux-git.config") >/dev/null 2>&1; then
  printf 'changed copied config.user was accepted\n' >&2
  exit 1
fi

for override in etc/linux-git/config etc/linux-git/remote etc/linux-git/patches/patches; do
  mkdir -p "$(dirname -- "$chroot_dir/root/$override")"
  printf 'synthetic override\n' >"$chroot_dir/root/$override"
  if (ws_kernel_reject_chroot_overrides "$chroot_dir") >/dev/null 2>&1; then
    printf 'clean-chroot linux-git override was accepted: %s\n' "$override" >&2
    exit 1
  fi
  rm -- "$chroot_dir/root/$override"
done

ws_kernel_stage_packaging "$source_dir" "$aur_commit" "$stage_dir" "$repo_root/templates/workstation/linux-git-packaging.patch" >/dev/null
(
  # Only the synthetic, local fixture is sourced here.
  pkgrel=''
  pkgver=''
  backup=()
  depends=()
  # shellcheck disable=SC1091
  source "$stage_dir/PKGBUILD"
  [[ $pkgrel == 3 && ${#backup[@]} == 3 ]]
  [[ $pkgver == 7.3.rc6.r0.a90ee4305c4a.torvalds.linux ]]
  _package-headers
  [[ ${#backup[@]} == 0 ]]
  for dependency in python python-cryptography perl sed make gawk; do
    [[ " ${depends[*]} " == *" $dependency "* ]]
  done
)
if (ws_kernel_stage_packaging "$source_dir" "$aur_commit" "$stage_dir" "$repo_root/templates/workstation/linux-git-packaging.patch") >/dev/null 2>&1; then
  printf 'changed recipe was accepted for a second patch\n' >&2
  exit 1
fi
printf 'REMOTE=example.invalid/override\n' >"$chroot_dir/root/etc/linux-git/remote"
if (ws_kernel_build "$source_dir" "$chroot_dir" "$profile_dir" "$output_dir" "$lock_file") >/dev/null 2>&1; then
  printf 'clean-chroot linux-git override was accepted\n' >&2
  exit 1
fi
[[ ! -e $KERNEL_BUILD_LOG ]]
rm -- "$chroot_dir/root/etc/linux-git/remote"

missing_output="$work/new-artifacts/missing-module"
if (KERNEL_DROP_MODULE=amdgpu ws_kernel_build "$source_dir" "$chroot_dir" "$profile_dir" "$missing_output" "$lock_file") >/dev/null 2>&1; then
  printf 'kernel build accepted a package without the profiled amdgpu module\n' >&2
  exit 1
fi
[[ ! -e $missing_output ]]

printf 'not an archive\n' >"$work/broken.pkg.tar.zst"
if (ws_kernel_module_inventory "$work/broken-inventory.txt" "$work/broken.pkg.tar.zst") >/dev/null 2>&1; then
  printf 'kernel module discovery accepted an unreadable package archive\n' >&2
  exit 1
fi

CCACHE_DIR="$work/shared-ccache" ws_kernel_build "$source_dir" "$chroot_dir" "$profile_dir" "$output_dir" "$lock_file" >/dev/null
grep -Fxq 'MAKEFLAGS=-j7' "$KERNEL_BUILD_LOG"
grep -Fxq 'NPROC=7' "$KERNEL_BUILD_LOG"
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
grep -Fxq 'KERNEL_SOURCE_TAG=v7.3-rc6' "$output_dir/linux-git.build-lock"
grep -Fxq 'KERNEL_SOURCE_TAG_OBJECT=4eeccbed21e50c19f97be9d325511f3de6343f2d' "$output_dir/linux-git.build-lock"
grep -Eq '^HARDWARE_POLICY_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
grep -Eq '^PACKAGING_PATCH_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
grep -Eq '^EFFECTIVE_PKGBUILD_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
[[ ! -e $output_dir/linux-git-headers-synthetic-x86_64.pkg.tar.zst-namcap.log ]]
[[ ! -e $output_dir/linux-git-headers-synthetic-x86_64.pkg.tar.zst.sig ]]
[[ $(wc -l <"$output_dir/linux-git.package-sha256") -eq 2 ]]
grep -Eq '^HARDWARE_PROFILE_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
grep -Fxq 'amdgpu' "$output_dir/linux-git.module-inventory.txt"
grep -Fxq 'ath12k_wifi7' "$output_dir/linux-git.module-inventory.txt"
grep -Fxq 'xhci_pci' "$output_dir/linux-git.module-inventory.txt"
jq -e '.status == "pass" and .missing_modules == []' "$output_dir/linux-git.profile-validation.json" >/dev/null
grep -Eq '^EFFECTIVE_CONFIG_SHA256=[a-f0-9]{64}$' "$output_dir/linux-git.build-lock"
grep -Fxq 'MEASURED_BUILD_JOBS=7' "$output_dir/linux-git.build-lock"
grep -Fxq 'MAKEFLAGS=-j7' "$output_dir/linux-git.build-lock"

sed '/^CONFIG_HSA_AMD_P2P=/d' "$fixture_config" >"$work/missing-p2p.config"
if (ws_kernel_validate_effective_config "$work/missing-p2p.config") >/dev/null 2>&1; then
  printf 'generic HSA and PCI P2PDMA incorrectly qualified AMD peer access\n' >&2
  exit 1
fi
grep -Eq '^[a-f0-9]{64}  linux-git-headers-synthetic-x86_64\.pkg\.tar\.zst$' "$output_dir/linux-git.package-sha256"

if (KERNEL_NAMCAP_MODE=error ws_kernel_namcap_audit "$work/namcap-error.txt" "$output_dir/linux-git-synthetic-x86_64.pkg.tar.zst") >/dev/null 2>&1; then
  printf 'namcap errors did not stop the candidate flow\n' >&2
  exit 1
fi

before="$(wc -l <"$KERNEL_BUILD_LOG")"
if (ws_kernel_build "$source_dir" "$chroot_dir" "$profile_dir" "$output_dir" "$lock_file") >/dev/null 2>&1; then
  printf 'existing kernel output candidate was replaced\n' >&2
  exit 1
fi
[[ "$(wc -l <"$KERNEL_BUILD_LOG")" == "$before" ]]

printf 'native kernel staging and audit tests passed\n'
