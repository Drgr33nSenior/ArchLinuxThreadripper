#!/usr/bin/env bash
# Commands below are intentionally resolved indirectly by the sourced libraries.
# shellcheck disable=SC2329
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
source "$root/lib/common.sh"

if command -v bsdtar >/dev/null; then
  bash "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$work/source-a" >/dev/null
  bash "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$work/source-b" >/dev/null
  cmp "$work/source-a/bootstrap-source.tar.gz" "$work/source-b/bootstrap-source.tar.gz"
  cmp "$work/source-a/PKGBUILD" "$work/source-b/PKGBUILD"
  [[ ! -e $work/source-a/launch-bootstrap && ! -e $work/source-a/launch-live ]]
  if bash "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$work/source-a" >/dev/null 2>&1; then exit 1; fi
  contents=$(bsdtar -tf "$work/source-a/bootstrap-source.tar.gz")
  [[ $contents == *project/bin/bootstrap-arch* && $contents == *SOURCE-MANIFEST.sha256* ]]
  if grep -E '(^|/)(\.git|\.idea|artifacts|cache|build)/|\.key$|config/install\.conf$' <<< "$contents"; then exit 1; fi
  [[ $(common::sha256_file "$work/source-a/bootstrap-source.tar.gz") == "$(common::lock_get "$work/source-a/source.lock" SOURCE_SHA256)" ]]
  # Run the actual package() functions into scratch roots. The portable install
  # shim implements GNU install -D on macOS; this is not a makepkg build.
  (
    startdir="$work/source-a"
    srcdir=$startdir
    pkgdir="$work/installer-package"
    source=() sha256sums=() backup=()
    export startdir srcdir pkgdir
    install() { local mode=${1#-Dm}; shift; mkdir -p -- "$(dirname -- "$2")"; command install -m "$mode" "$1" "$2"; }
    # shellcheck disable=SC1091
    source "$startdir/PKGBUILD"
    # GNU coreutils is available on Arch. Keep the offline staging test portable
    # on macOS hosts that only provide shasum, without invoking makepkg.
    if ! command -v sha256sum >/dev/null; then
      sha256sum() {
        if (($# == 1)); then shasum -a 256 "$1"; return; fi
        [[ $* == '--check --strict SOURCE-MANIFEST.sha256' ]] || return 1
        local digest path
        while read -r digest path; do
          [[ $(shasum -a 256 "$path" | awk '{print $1}') == "$digest" ]] || return 1
        done < SOURCE-MANIFEST.sha256
      }
    fi
    verify
    prepare >/dev/null
    # Undeclared adjacent launchers cannot affect the package. The only consumed
    # launchers are inside the checked archive and per-file source manifest.
    printf 'SYNTHETIC NOT EXECUTABLE\n' > "$startdir/launch-bootstrap"
    package_arch-workstation-bootstrap
    [[ -x $pkgdir/usr/bin/bootstrap-arch && -x $pkgdir/usr/bin/arch-workstation-live ]]
    cmp "$pkgdir/usr/bin/bootstrap-arch" "$srcdir/project/infrastructure/packages/bootstrap/launch-bootstrap"
    bash "$pkgdir/usr/lib/arch-workstation-bootstrap/bin/bootstrap-arch" --help >/dev/null
    pkgdir="$work/runtime-package"
    package_arch-workstation-boot
    [[ -x $pkgdir/usr/lib/arch-workstation-bootstrap/uki-sync && ! -e $pkgdir/usr/local ]]
    cmp "$pkgdir/usr/lib/arch-workstation-bootstrap/common.sh" "$srcdir/project/lib/common.sh"
    grep -Fqx 'Exec = /usr/lib/arch-workstation-bootstrap/uki-sync --sign' "$pkgdir/usr/share/libalpm/hooks/zzz-bootstrap-uki-sync.hook"
    pkgdir="$work/backup-package"
    package_arch-workstation-backup
    [[ -x $pkgdir/usr/lib/arch-workstation-backup/run && ! -e $pkgdir/usr/local && ! -e $pkgdir/etc/systemd ]]
    [[ -f $pkgdir/etc/restic/workstation.include && -f $pkgdir/etc/restic/workstation.exclude ]]
    [[ ${backup[*]} == 'etc/restic/workstation.include etc/restic/workstation.exclude' ]]
    grep -Fqx 'ExecStart=/usr/lib/arch-workstation-backup/run %i' "$pkgdir/usr/lib/systemd/system/workstation-restic@.service"

    # Incidental edits to the consumed recipe or exported lock must be rejected.
    mkdir "$work/drift"
    cp "$startdir/bootstrap-source.tar.gz" "$startdir/PKGBUILD" "$startdir/source.lock" "$work/drift/"
    printf '# synthetic recipe drift\n' >> "$work/drift/PKGBUILD"
    verify_at() { local startdir=$1; : "$startdir"; verify; }
    if verify_at "$work/drift"; then exit 1; fi
    cp "$startdir/PKGBUILD" "$work/drift/PKGBUILD"
    for key in SOURCE_SHA256 PACKAGE_VERSION SOURCE_DATE_EPOCH; do
      sed "s/^$key=.*/$key=SYNTHETIC_INVALID_METADATA/" "$startdir/source.lock" > "$work/drift/source.lock"
      if verify_at "$work/drift"; then exit 1; fi
    done
    if (_source_version=9.9.9; prepare >/dev/null 2>&1); then exit 1; fi
    if (_source_epoch=1000000000; prepare >/dev/null 2>&1); then exit 1; fi

    # Reconstruct solely from PKGBUILD plus declared sources, the payload that
    # makepkg --source carries. No external lock/launchers or working tree survive.
    (cd "$startdir"; bsdtar -cf "$work/source-package.tar" -- PKGBUILD "${source[@]}")
    mkdir "$work/reconstructed" "$work/reconstructed/src"
    bsdtar -xf "$work/source-package.tar" -C "$work/reconstructed"
    (
      startdir="$work/reconstructed" srcdir="$work/reconstructed/src" pkgdir="$work/rebuilt-package"
      [[ ! -e $startdir/source.lock && ! -e $startdir/launch-bootstrap ]]
      # shellcheck disable=SC1091
      source "$startdir/PKGBUILD"
      [[ $(common::sha256_file "$startdir/${source[0]}") == "${sha256sums[0]}" ]]
      verify
      bsdtar -xf "$startdir/bootstrap-source.tar.gz" -C "$srcdir"
      prepare >/dev/null
      package_arch-workstation-bootstrap
      cmp "$pkgdir/usr/bin/bootstrap-arch" "$work/installer-package/usr/bin/bootstrap-arch"
      printf '# synthetic archived launcher drift\n' >> "$srcdir/project/infrastructure/packages/bootstrap/launch-bootstrap"
      if prepare >/dev/null 2>&1; then exit 1; fi
    )
  )
  # An allowlisted file cannot resolve through an unexpected symlink. Use only
  # this scratch copy, never edit or replace the user's source file.
  mkdir -p "$work/synthetic/infrastructure/packages/bootstrap" "$work/synthetic/lib" "$work/synthetic/infrastructure/iso"
  cp "$root/infrastructure/packages/bootstrap/prepare-source.sh" "$work/synthetic/infrastructure/packages/bootstrap/"
  cp "$root/lib/common.sh" "$work/synthetic/lib/"
  cp "$root/infrastructure/iso/versions.lock" "$work/synthetic/infrastructure/iso/"
  printf 'bin/unsafe\n' > "$work/synthetic/infrastructure/packages/bootstrap/source.files"
  ln -s "$root/bin" "$work/synthetic/bin"
  if bash "$work/synthetic/infrastructure/packages/bootstrap/prepare-source.sh" "$work/refused" >/dev/null 2>&1; then exit 1; fi
else
  echo 'SKIP: ISO source archive and package staging tests require libarchive bsdtar'
fi

# Every VM command here is a dry run with tiny regular fixtures, never QEMU.
printf 'synthetic firmware\n' > "$work/firmware.fd"
printf 'synthetic ISO\n' > "$work/live.iso"
vm="$root/infrastructure/iso/vm/test-vm.sh"
plan=$(bash "$vm" create "$work/new-vm" "$work/firmware.fd")
[[ ! -e $work/new-vm && $plan == *'-f raw'* ]]
mkdir "$work/vm"
printf 'ARCHLAB_VM_V1\n' > "$work/vm/fixture.marker"
cp "$work/firmware.fd" "$work/vm/OVMF_VARS.fd"
plan=$(bash "$vm" boot "$work/vm" "$work/firmware.fd" "$work/live.iso")
[[ $plan == *'-nic none'* && $plan != *hostfwd* && $plan != *vfio* && $plan == *'serial=ARCHLAB_TEST_A'* ]]
plan=$(bash "$vm" recover "$work/vm" "$work/firmware.fd" "$work/live.iso")
[[ $plan == *snapshot=on* && $plan == *'-nic none'* ]]
if bash "$vm" recover "$work/vm" "$work/firmware.fd" "$work/live.iso" --network >/dev/null 2>&1; then exit 1; fi
if bash "$vm" create /dev/synthetic-test "$work/firmware.fd" >/dev/null 2>&1; then exit 1; fi
if bash "$vm" boot "$work/vm" "$work/firmware.fd" "$work/live.iso" --vfio >/dev/null 2>&1; then exit 1; fi
if bash "$vm" create "$work/vm" "$work/firmware.fd" >/dev/null 2>&1; then exit 1; fi
if bash "$root/bin/arch-workstation-live" < /dev/null >/dev/null 2>&1; then exit 1; fi

# A build preview cannot start mkarchiso or reuse incomplete/dangling state.
mkdir -p "$work/prepared/profile"
cp "$root/infrastructure/iso/versions.lock" "$work/prepared/release.lock"
printf 'synthetic artifact inventory\n' > "$work/prepared/artifacts.lock"
cp "$root/infrastructure/iso/profile/profiledef.sh" "$work/prepared/profile/profiledef.sh"
plan=$(bash "$root/infrastructure/iso/build.sh" "$work/prepared")
[[ $plan == *mkarchiso* && ! -e $work/prepared/work && ! -e $work/prepared/out ]]
ln -s "$work/nonexistent-out" "$work/prepared/out"
if bash "$root/infrastructure/iso/build.sh" "$work/prepared" >/dev/null 2>&1; then exit 1; fi

(
  source "$root/bin/bootstrap-arch"
  # Synthetic signature metadata only; no signing key or real package install.
  BOOTSTRAP_BOOT_PACKAGE="$work/synthetic.pkg.tar.zst"
  printf 'synthetic package\n' > "$BOOTSTRAP_BOOT_PACKAGE"
  printf 'synthetic signature\n' > "$BOOTSTRAP_BOOT_PACKAGE.sig"
  gpg() { printf '[GNUPG:] VALIDSIG SYNTHETIC\n[GNUPG:] TRUST_UNDEFINED\n'; }
  if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  gpg() { printf '[GNUPG:] VALIDSIG SYNTHETIC\n[GNUPG:] TRUST_FULLY\n'; }
  bsdtar() {
    if [[ $1 == -xOf ]]; then printf 'pkgname = arch-workstation-boot\n'; else printf '.PKGINFO\nusr/lib/arch-workstation-bootstrap/uki-sync\n'; fi
  }
  bootstrap_verify_boot_package
  (
    bsdtar() {
      if [[ $1 == -xOf ]]; then printf 'pkgname = arch-workstation-boot\n'; else printf '.INSTALL\n'; fi
    }
    if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  )
  (
    bsdtar() { [[ $1 == -xOf ]] || return 1; printf 'pkgname = arch-workstation-boot\n'; }
    if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  )
  (
    bsdtar() { printf 'pkgname = unrelated-package\n'; }
    if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  )
  (
    common::sha256_file() { printf 'invalid checksum\n'; }
    if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  )
  printf 'changed\n' >> "$BOOTSTRAP_BOOT_PACKAGE"
  pacman() { printf 'unexpected install\n' > "$work/package-installed"; }
  if bootstrap_install_boot_package >/dev/null 2>&1; then exit 1; fi
  [[ ! -e $work/package-installed ]]
  mv "$BOOTSTRAP_BOOT_PACKAGE.sig" "$work/real-signature"
  ln -s "$work/real-signature" "$BOOTSTRAP_BOOT_PACKAGE.sig"
  if bootstrap_verify_boot_package >/dev/null 2>&1; then exit 1; fi
  bootstrap_load_config "$root/config/install.conf.example"
  [[ $VM_TEST_MODE == false ]]
  bootstrap_load_config "$root/infrastructure/iso/vm/install.conf.example"
  [[ $VM_TEST_MODE == true ]]
  systemd-detect-virt() { return 1; }
  if bootstrap_select_gpu_packages >/dev/null 2>&1; then exit 1; fi
  # Only the unit-test host probe is synthetic; no real PCI identity is changed.
  bootstrap_require_test_vm() { :; }
  bootstrap_select_gpu_packages
  [[ ${#BOOTSTRAP_GPU_PACKAGES[@]} == 0 && $BOOTSTRAP_GPU_DESCRIPTION == *'qualification is pending'* ]]
  bootstrap_require_root() { :; }; bootstrap_require_tty() { :; }
  bootstrap_preflight() { :; }; bootstrap_assert_safe_target() { :; }
  bootstrap_assert_boot_labels_absent() { :; }; efibootmgr() { :; }
  bootstrap_verify_boot_package() { return 1; }
  bootstrap_create_partitions() { printf 'unsafe\n' > "$work/erased"; }
  BOOTSTRAP_DRY_RUN=0
  if bootstrap_install >/dev/null 2>&1; then exit 1; fi
  [[ ! -e $work/erased ]]
)
printf 'ISO source, package staging, VM dry-run and install-boundary tests passed\n'
