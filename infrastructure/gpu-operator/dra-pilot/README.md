# DRA pilot: GPUs through ResourceClaims

Goal: prove that AMD's DRA driver allocates this host's two Radeon AI PRO R9700
(gfx1201) cards, so that a later change can let SGLang (TP2, both cards) and
SwarmUI share the second card through one ResourceClaim. Upstream documents the
driver for Instinct GPUs; Radeon support is what this pilot establishes.

## What changes

- `deviceconfig.yaml`: the device plugin is disabled and the DRA driver is
  enabled at `rocm/k8s-gpu-dra-driver:v1.0.1`, pinned by digest. The operator
  refuses to run both. v1.0.1 is required because the in-kernel amdgpu module
  reports no version on this host; v1.0.0 publishes no GPUs in that case.
- `values.yaml`: the operator chart renders the `gpu.amd.com` DeviceClass. The
  driver's RBAC was already part of the render.
- `../claims.yaml` (canonical, outside the pilot): standalone claims `gpu-0` (`0000:23:00.0`) and `gpu-1`
  (`0000:c3:00.0`), selected by PCI address.
- `probe.yaml`: a restricted pod that shares `gpu-1` and only lists devices.

Prerequisites already met: Kubernetes v1.36.6 with `resource.k8s.io/v1`, CDI on
in K3s containerd (`/var/run/cdi`), Node Feature Discovery labels the node, and
ROCm supports gfx1201 without overrides.

## Effect on running workloads

The node stops advertising `amd.com/gpu`. The running SGLang pod keeps its
cards, but **any pod that still requests `amd.com/gpu` and (re)starts stays
Pending**, including SGLang after a crash or node reboot, until it is moved to
the claims in the follow-up change. `session switch` also stops working until
then, because it counts `amd.com/gpu`. Plan the pilot for a window where losing
the model until the follow-up merges is acceptable, or roll back.

DRA does not isolate VRAM or compute. Sharing a card is cooperative.

## Run (owner)

1. Merge, then re-render the operator from the pinned chart and apply it, which
   adds the DeviceClass (`infrastructure/gpu-operator/render.sh <chart.tgz>`).
2. Apply `deviceconfig.yaml`. Wait until the device plugin DaemonSet is gone and
   the DRA driver DaemonSet is ready.
3. Apply `../claims.yaml`, then `probe.yaml`.
4. Run `verify.sh --probe`. Expected: every line PASS, two devices with the PCI
   addresses above, the probe sees one render node and one gfx1201 GPU, and the
   SGLang pod is still Running.
5. Delete the probe pod. Keep the claims for the follow-up change.

## Rollback

Set `devicePlugin.enableDevicePlugin: true` and `draDriver.enable: false` in
`deviceconfig.yaml` and apply it. Delete the probe and claims. The DeviceClass is
harmless and can stay until the values change is reverted.

## Also check

- The AMD metrics exporter attributes metrics to pods through the kubelet
  pod-resources API. Confirm its POD/NAMESPACE labels still appear for
  DRA-allocated cards; dashboards that join on them may lose pod names.
