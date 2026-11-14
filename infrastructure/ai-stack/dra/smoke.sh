#!/usr/bin/env bash
# Offline smoke test for the DRA driver image, run inside it as a non-root user with no
# network and a read-only root. It checks what the GPU operator relies on without a GPU or
# a cluster: the binaries are on PATH, they load (glibc and the vendored AMD SMI libraries
# resolve), the flags the operator's DaemonSet sets exist, and every driver feature gate
# defaults to off, so the operator's draDriver fields need no new arguments.
set -euo pipefail
fail() {
  printf 'SMOKE FAIL: %s\n' "$*" >&2
  exit 1
}
[[ $(id -u) != 0 ]] || fail 'smoke must run as a non-root user'
for binary in gpu-kubeletplugin webhook; do
  [[ $(command -v "$binary") == /usr/bin/$binary && -x /usr/bin/$binary ]] || fail "$binary is not /usr/bin/$binary"
done
! ldd /usr/bin/gpu-kubeletplugin | grep -q 'not found' || fail 'gpu-kubeletplugin has unresolved libraries'
ldd /usr/bin/gpu-kubeletplugin | grep -Fq 'libamd_smi.so.27 => /opt/amd-smi/lib/' ||
  fail 'gpu-kubeletplugin does not load the vendored AMD SMI library'
help=$(gpu-kubeletplugin --help) || fail 'gpu-kubeletplugin --help failed'
# The operator sets these through the environment (CDI_ROOT, KUBELET_*_PATH, NODE_NAME).
for flag in --node-name --cdi-root --kubelet-registrar-directory-path --kubelet-plugins-directory-path --feature-gates; do
  grep -Fq -- "$flag" <<<"$help" || fail "gpu-kubeletplugin lacks $flag"
done
for gate in AutoPartition DeviceMetadata VFIOPassthrough; do
  grep -Eq "$gate=true\|false \(ALPHA - default=false\)" <<<"$help" || fail "feature gate $gate is not an alpha gate defaulting to off"
done
webhook --help >/dev/null || fail 'webhook --help failed'
modprobe --version >/dev/null || fail 'modprobe (kmod) is missing'
printf 'DRA_SMOKE=passed uid=%s\n' "$(id -u)"
