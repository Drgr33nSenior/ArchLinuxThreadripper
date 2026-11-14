# DRA pilot: GPUs through ResourceClaims

> **Historical.** The pilot succeeded and DRA is how the GPUs are shared today: SGLang
> holds both claims, and SwarmUI and `comfyui-gpu0` each share one. The current setup is in
> [home lab](../../../docs/HOME-LAB.md#4-gpu-operator-and-allocation) and
> [service modes](../../../docs/MODES.md); the notes below record the pilot.
>
> **Driver image.** The DeviceConfig now pins a driver built by the AI-stack
> [`dra` stage](../../ai-stack/README.md#stages) from a pinned upstream `main` commit. The
> pilot's `v1.0.1` image is the documented rollback. To change images, follow
> [switching the driver image](#switching-the-driver-image).

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

## Switching the driver image

Run this in a maintenance window. Every step except the apply and the mode commands is read-only.

1. **Check the image before the window.** On the workstation, run
   `infrastructure/ai-stack/signing/verify-image.sh <draDriver.image>`. Then run `verify.sh` and
   keep its output: device names, PCI addresses and memory.
2. **Drain the GPUs.** Run `sudo workstationctl mode maintenance` and read its plan, then run it
   again with `--execute`. Both claims must show no consumer:
   `kubectl -n ai-home-lab get resourceclaims gpu-0 gpu-1 -o 'custom-columns=NAME:.metadata.name,USERS:.status.reservedFor[*].name'`.
   The new driver reads the old checkpoint, but draining means no claim is prepared during the
   switch. The DaemonSet replaces its pod with `maxUnavailable: 1` and no surge, so a pod that
   starts during the gap cannot get its card.
3. **Apply the DeviceConfig:** `kubectl apply -f infrastructure/gpu-operator/deviceconfig.yaml`.
4. **Wait for the driver.** Run
   `kubectl -n kube-amd-gpu rollout status ds/ai-home-lab-dra-driver --timeout=5m`, then
   `kubectl -n kube-amd-gpu logs ds/ai-home-lab-dra-driver | grep 'Discovered 2 AMD GPU devices'`.
5. **Verify.** Run `verify.sh`. It must print the new image, every line PASS, and the same device
   names, PCI addresses and memory as in step 1. The `published PCI addresses match the claims`
   line checks `0000:23:00.0` and `0000:c3:00.0`.
6. **Bring the service back.** Run `sudo workstationctl mode family`, then add `--execute`. SGLang
   and the picture generators must reach Running, and chat must answer.

**Rollback.** In `deviceconfig.yaml`, set `draDriver.image` to the `# Rollback image:` line
(`v1.0.1`). Then repeat steps 2 to 6. A checkpoint the new driver wrote without partitioning
has the same format as `v1.0.1`'s, so the old driver reads it.

After any reboot, the [post-reboot CDI fix](../../../docs/HOME-LAB.md#4-gpu-operator-and-allocation)
still applies: the pinned commit, like v1.0.1, does not rewrite CDI specs for claims that its
checkpoint lists as prepared.

## Also check

- The AMD metrics exporter attributes metrics to pods through the kubelet
  pod-resources API. Confirm its POD/NAMESPACE labels still appear for
  DRA-allocated cards; dashboards that join on them may lose pod names.
