# Sunshine streaming candidate

The default gaming-image candidate uses a container-local KWin Wayland virtual
session, native KWin/PipeWire capture, XWayland for Steam and older games, and
Sunshine Vulkan Video encoding. It does **not** start Xorg, XFCE, noVNC, a host
display server, or the inherited rootful Steam-Headless supervisor. XWayland is
present only as a compatibility server for applications that need X11.

This remains a candidate, not a qualified gaming deployment. All gaming replicas
remain zero. Image promotion, device permissions, controller input, audio,
private streaming exposure, egress, capture placement and encode quality must be
accepted on the target workstation before a session command can enable gaming.
Sunshine is the server; Moonlight is its client.

## Implementation record — 8 September 2026

| Change | Source and evidence | Implementation | Remaining validation |
| --- | --- | --- | --- |
| Headless Wayland session | [KWin Wayland](https://invent.kde.org/plasma/kwin), [Sunshine capture configuration](https://docs.lizardbyte.dev/projects/sunshine/master/md_docs_2configuration.html#capture) | `infrastructure/gaming/wayland-session.sh` starts KWin's virtual backend, then PipeWire, WirePlumber, Steam and Sunshine in one non-root session | R9700 rendering, virtual output, capture, shutdown and recovery |
| Native capture and encoder | [Sunshine Linux compatibility](https://docs.lizardbyte.dev/projects/sunshine/latest/) | Default `capture=kwin`, `encoder=vulkan`; the CLI rejects Vulkan with X11 or wlroots capture | Compare Vulkan and VA-API on the same qualified KWin capture path; no gain is claimed |
| Pinned graphics userspace | [Sunshine 2026.906.222525](https://github.com/LizardByte/Sunshine/releases/tag/v2026.906.222525), [Mesa backport](https://packages.debian.org/trixie-backports/mesa-vulkan-drivers) | `versions.lock` pins Sunshine commit `cb72dffa3233c5815cd5ba88f09f049dd679ba75`, Mesa `26.1.2-1~bpo13+1`, and KWin `4:6.3.6-1` | KWin is a reviewed compatible lock, not a claim to be the newest release or to qualify the stack |
| Persistent state and caches | Existing per-owner `gaming-home` PVC | Pairing, Sunshine configuration, Steam files and shader caches remain below `/home/default`; no reset, copy, chown or deletion occurs | Inspect ownership and absolute app paths; take a NAS backup before changing existing state |
| Device isolation | [AMD device-plugin allocation](https://instinct.docs.amd.com/projects/k8s-device-plugin/en/latest/user-guide/resource-allocation.html) | The launcher requires one accessible AMD/amdgpu render node and records its BDF; the Deployment requests one `amd.com/gpu` | Confirm rendering, capture and encoding all use the allocated GPU; `DRI_PRIME` and `adapter_name` are preferences, not isolation |

The existing 12-CPU/12-GiB gaming envelope, 2-GiB shared-memory limit, one-GPU
request, AI-unload checks and cooperative build inhibition are retained. This
change adds no controller, monitoring service, host mount or live-cluster action.

## Build and inspect the image

Run from the reviewed full checkout, not the live installer:

```sh
# Default: non-root KWin Wayland session.
./bin/workstationctl sunshine image-build artifacts/sunshine-wayland-01

# Explicit rollback candidate only: inherited rootful Xorg/XFCE session.
./bin/workstationctl sunshine image-build artifacts/sunshine-x11-rollback-01 x11
SUNSHINE_ENCODER=vaapi SUNSHINE_CAPTURE=x11 \
  ./bin/workstationctl sunshine profile artifacts/sunshine-profile-x11-01
```

The builder stages repository sources and the hash-verified Sunshine release,
uses the existing Docker builder for `linux/amd64`, and does not publish, deploy
or contact K3s. The Wayland build is the default. The `x11` target is an explicit
rollback image, not a fallback selected at runtime; it retains the inherited
rootful session and needs its own security, input and display review before use.

The base digest, Sunshine hash and Mesa version are locked. The Wayland result
also records the KWin package lock. Other Debian dependencies are resolved
through signed repositories during the build, so this is **not** a bit-for-bit
reproducible dependency closure. The complete installed package list is
`/usr/share/workstation/gaming-packages.tsv` inside the image. Rebuilds need a
fresh immutable image digest and qualification. The FFmpeg CLI package version
is not proof of Sunshine's embedded FFmpeg build options.

Sunshine's Debian maintainer scripts try udev/module operations during package
installation. An ordinary isolated Docker build can report unavailable udev or
kernel-module warnings. Do not enable privileged builds to eliminate them.
The recipe removes the package's file capabilities before producing the image.

`build-result.json` contains a local Docker **config ID**, not a registry manifest
digest. After security and target acceptance, use the established registry/import
process and replace `localhost/workstation/steam-headless:UNQUALIFIED` with the
resulting immutable image reference. Publication and deployment are deliberately
not automated here. Never mark the upstream base image as the finished recipe.

## Configure a profile

The default profile is KWin capture, Vulkan Video, and a 1920x1080 virtual
output:

```sh
./bin/workstationctl --config config/workstation.conf \
  sunshine profile artifacts/sunshine-profile-wayland-01
kubectl kustomize apps/overlays/family
./bin/workstationctl session plan gaming
```

`sunshine profile` creates a new `sunshine.env`; it does not edit or deploy a
workload. Copy reviewed values to the `sunshine-profile` ConfigMap generator in
`apps/base/steam-headless/kustomization.yaml`, or make the equivalent per-owner
overlay change. A ConfigMap hash causes a Pod rollout. Change `GAMING_WIDTH` or
`GAMING_HEIGHT` only with that profile/image rollout; KWin creates its virtual
output at process startup. `SUNSHINE_OUTPUT_NAME` is observed output metadata,
not a card number or scheduler selector, and must not be hard-coded.

| Setting | Baseline | Explicit experiment |
| --- | --- | --- |
| `SUNSHINE_ENCODER` | `vulkan` | `vaapi` on the same qualified capture path |
| `SUNSHINE_CAPTURE` | `kwin` native KWin/PipeWire capture | `portal` only after explicit owner consent; `x11` only in the separately built rollback image |
| `GAMING_WIDTH`, `GAMING_HEIGHT` | `1920`, `1080` | A reviewed even resolution followed by a rollout |
| `SUNSHINE_OUTPUT_NAME` | Empty | Observed output name after qualification; never a numeric ordinal guess |
| `SUNSHINE_HEVC_MODE`, `SUNSHINE_AV1_MODE` | `0`, capability detection | `1` disables; `2` enables advertised 8-bit support; `3` advertises HDR only after qualification |
| `SUNSHINE_VK_TUNE` | `2`, low latency | `3`, ultra-low latency, with quality comparison |
| `SUNSHINE_VAAPI_STRICT_RC_BUFFER` | `disabled` | `enabled` for scene-change network-drop testing |

Portal capture is optional, not a transparent fallback. Its first request may
need an owner to approve the desktop-portal consent prompt in the KWin session.
Retain and protect the portal's persistent restore state when it is issued; do
not automate consent, log a restore token, or switch to X11 when consent or a
portal request fails. Use the native KWin capture default while establishing the
headless session.

KMS and wlroots capture are not part of this image. KMS needs DRM-primary-node
access and capabilities that conflict with this deployment's conservative
profile. The pinned Sunshine compatibility matrix does not support Vulkan Video
with X11 or wlroots capture, so the configuration validator rejects both pairs.

The runtime launcher uses Sunshine's `key=value` overrides. It preserves the
existing configuration, pairing state and app definitions. The session entrypoint
retains the existing Secret-backed web-account credential update at startup;
it withholds that command's output and removes the credential variables before
starting the desktop processes. Profile
values override saved settings for that process; an empty output-name setting
does not erase a saved connector. Restore baseline by reverting the profile
values above through the same maintenance path. No clocks, voltage, ASPM, EPP
or TuneD settings are changed, so no power-policy restoration is needed.

Do not add `AMD_DEBUG=lowlatencyenc`: this Sunshine release already sets it for
AMD, as described in its [troubleshooting guide](https://github.com/LizardByte/Sunshine/blob/v2026.906.222525/docs/troubleshooting.md).
Keep informational logging for measured runs. Debug logging and new shader
compilation can distort latency results. Codec, stream resolution, FPS, HDR and
bitrate must also be selected and recorded on each actual Moonlight client;
Sunshine capability advertisements do not force the client's decoder choice.

## Persistence and session boundaries

The Wayland image runs as UID/GID `1000`, with all Linux capabilities dropped.
KWin starts the virtual Wayland socket and provides its actual XWayland
`DISPLAY` and `XAUTHORITY` to its session hook. The hook starts PipeWire,
WirePlumber and a container-local Pulse sink before Steam and Sunshine. It
serializes use of each persistent home and stops child process groups on exit.
It never inherits an upstream `DISPLAY`, host D-Bus or host Pulse socket.

The maintained image uses `/home/default`, not the old placeholder's `/home/user`.
The PVC itself is unchanged. Existing relative files remain on it, but inspect
UID/GID ownership and any absolute paths in app definitions before migrating.
Take a NAS backup first; no automatic chown, copy, pairing reset or deletion is
performed on existing state. New homes receive minimal default app definitions.
Inspect old app definitions for Xrandr commands and Xorg connector names; remove
or replace those commands explicitly. The migration does not rewrite user apps.

Mesa shader caches use the persistent home with a `2G` cache limit per applicable
architecture/cache implementation. This is not a limit on Steam's separate
shader caches or the whole PVC. The existing 500-GiB local-path request is
advisory, not a filesystem quota. Monitor free space and use Steam's library/cache
management; no destructive retention job is added.

The device plugin must allocate one exclusive GPU. The launcher refuses zero or
multiple accessible render nodes, checks the AMD vendor/host driver and resolves
the BDF at each start. `DRI_PRIME` and `adapter_name` are preferences, not isolation.
Upstream Vulkan can fall back to its default device, so actual runtime placement
still needs checking. Do not add blanket `/dev`, host IPC/network, or environment
filters as substitutes for device allocation. Rendering, display/capture and
encoding must all be verified on the allocated card.

The K3s source now uses `runAsNonRoot`, UID/GID `1000`, RuntimeDefault seccomp
and `drop: [ALL]`; it no longer requests `SYS_ADMIN` or `SYS_NICE`. It still is
not admitted or enabled. Verify the AMD device-plugin device nodes and
permissions, the selected input mechanism, audio/capture
permissions, private streaming ports and Steam egress before accepting a
manifest. Do not add blanket `/dev`, Docker socket, host IPC/network, host D-Bus
or host display mounts to make it start.

Native Wayland input is a separate unresolved acceptance requirement. KWin's
[virtual backend does not consume libinput devices](https://github.com/KDE/kwin/blob/v6.3.6/src/input.cpp).
Sunshine's [pinned Linux input implementation](https://github.com/LizardByte/libvirtualhid/blob/6fdb8bd4de3b68d96c30e5303ac2ebb333c09746/src/platform/linux/uhid_backend.cpp)
uses uinput or XTest fallback. Adding `/dev/uinput` can suppress that fallback
without delivering input to this compositor. Do not add it as a guessed fix.
The candidate path for keyboard/mouse is XTest through Xwayland's EIS portal
support into KWin; it depends on the package build and separate input consent.
Check `Xwayland -help` for `-enable-ei-portal`, then demonstrate owner bootstrap,
keyboard/mouse in both an XWayland game and a native Wayland window, and each
gamepad type. Native KWin capture avoids **capture** consent, not all **input**
consent. Selecting portal capture alone does not establish input injection.
Keep gaming disabled until that workflow is demonstrated; a visible stream is
not acceptance of an interactive session.

The local Workstation Bridge repository still duplicates an older X11
`internal/worker/sunshine.Containerfile` recipe. This repository change does not
modify or build that recipe. Coordinate a Bridge follow-up before asking the
Bridge worker to build or promote the Wayland image.

Use the existing [session switch/restore procedure](AI-PERFORMANCE.md#workload-and-session-selection).
The current handover stops the selected AI deployment before gaming; it does not
promise simultaneous AI on the other card. For Steam Remote Play, select
`SESSION_STREAMING_PATH=steam` and a reviewed manifest with Sunshine disabled.
Moonlight/Sunshine and Steam Remote Play are alternative session paths. Disconnect
recovery remains explicit `session restore`, not a new always-running controller.

## Target diagnostics and paired measurements

Inside the **qualified one-GPU container**, as the session user:

```sh
/opt/workstation/bin/workstationctl sunshine devices
/opt/workstation/bin/workstationctl sunshine diagnose /home/default/diagnostics/stream-01
```

Expect the allocated BDF and current render path; inspect `commands.jsonl` and
`optional-commands.jsonl` for failures/timeouts. Diagnostics collect version,
VA-API/Vulkan capability, renderer, display and audio metadata without copying
Sunshine logs, credentials or paired-client state. A successful collection is not
a successful stream. Reject llvmpipe/software rendering; confirm KWin, capture
and encoder placement on the allocated card, then verify VCN activity and the
actual Moonlight decoder choice. Do not publish raw logs without checking their
contents. Record power/temperature separately with existing GPU telemetry; the
diagnostic command does not sample workload power.

Copy `templates/sunshine/measurement.example.json` for each profile. Replace all
provenance placeholders and record at least three timed runs after warm-up using
the same deterministic game/replay, settings, client, codec, bitrate, capture,
image and background workload. Alternate A/B order. Keep cold and warm caches
separate. Measure frame-time tails with the game's existing telemetry or a
reviewed overlay, and record client/network drops and encode/decode latency.
Do not put an overlay's mean latency into a percentile field; leave unavailable
metrics `null`. No performance numbers are supplied by the template.

```sh
./bin/workstationctl sunshine compare artifacts/vaapi.json artifacts/vulkan.json artifacts/stream-comparison-01
```

The command rejects mismatched provenance, fewer than three runs, negative
metrics and changes to more than one profile setting. It reports the median,
minimum, maximum and count of available per-run metrics; these are not pooled
frame-time percentiles. It does not launch games or manufacture measurements.

Next tests, in order: confirm hardware rendering/encoding and placement; compare
VA-API/Vulkan on one DMA-BUF capture path; compare AV1/HEVC on each real client;
then test strict VA-API rate control or Vulkan tune 3 independently. Keep a change
only if repeatable latency/drop or bitrate-quality results justify its trade-off.
AMF forks, blanket maximum clocks, voltage changes and unrelated ROCm tuning are
outside this change.

## Verification status — 8 September 2026

The following repository check completed successfully (41 test files reported,
including explicit optional skips):

```sh
HOME_LAB_PYTHON=/usr/local/bin/python3.11 \
HOME_LAB_WAYLAND_RUNTIME_IMAGE=sha256:a6025d38327649c9d2837c235b9b897d4ddf242c45e65e4074104896d5b02145 \
  make check
```

Shell syntax, ShellCheck, YAML parsing, offline Kustomize semantics and the
configured Python Jinja/TOML checks passed. Focused fixtures cover dimensions,
capture/encoder compatibility, device mapping, existing paired state, renderer
refusal, missing session prerequisites and build-target selection. The Linux
process test used the **previous local image only as a Bash/setsid runtime**,
with one read-only source-file mount. Repeated cleanup and TERM-resistant child
shutdown passed without network, devices or privileges. This is not a test of
the new compositor or image. Documentation audits and `git diff --check` passed.

The new image build was attempted at
`artifacts/sunshine-wayland-20260908-01`. APT resolved the Wayland package set,
then stopped because Docker Desktop's 59-GB Linux disk had no free space. The
failed context is retained; there is no new image ID or success receipt. No
Docker images, containers, volumes or caches were pruned. Increase the Docker
disk limit, then retry with a new output directory:

```sh
./bin/workstationctl sunshine image-build artifacts/sunshine-wayland-20260908-02
gaming_image_id=$(jq -r .local_image_id artifacts/sunshine-wayland-20260908-02/build-result.json)
HOME_LAB_GAME_IMAGE="$gaming_image_id" bash tests/test_sunshine_image.sh
```

New-image smoke tests remain **BLOCKED — Docker disk full**. Other skipped
checks: shfmt, Bats, Ansible, real ccache compilation, CMake/Ninja integration,
operator rendering without its local chart archive, the Bash-4-only USB signal
fixture, and Linux systemd verification. No tools were installed to bypass skips.

A bounded non-root image smoke test can check packaging and startup prerequisites,
but cannot qualify GPU rendering, capture, encoding, portal/input consent,
controller input, audio, real clients, latency, frame times or power.

Those physical checks remain **NOT RUN — target hardware unavailable**. Do not
mark the deployment qualified merely because a local image build or rendered
manifest succeeds.
