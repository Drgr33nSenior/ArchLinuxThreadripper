# Sunshine and Moonlight

Sunshine streams the container-local desktop to Moonlight. The non-root gaming
candidate uses KWin Wayland, native KWin/PipeWire capture, XWayland for X11 apps
and Vulkan Video encoding. The default image recipe is Debian; `arch-wayland`
is the opt-in candidate that passed the latest physical checks.

## Current result: 1 November 2026

The pinned Arch candidate reached an AMD-rendered 1920×1080/60.000-Hz virtual
output and started H.264, HEVC and AV1 Vulkan encoders. The owner confirmed mouse
movement, clicks and typing in both X11 and native Wayland windows after the
name-specific KWin grant below. The temporary session/network exception was
cleaned up. Exact identities and failed predecessors remain in the private
`test-results/sunshine-input-bootstrap.ReskhlIA/HANDOFF.md` evidence chain.

This is basic capture/encoder startup and manual input evidence. Audio,
controllers, games, delivered frame cadence/quality/latency, repeated startup,
persistent-home recovery and full handover remain unqualified. Gaming replicas
stay zero. The latest profile/template integration has source/package checks;
a fresh image rebuild from that final source is still required.

## Design and qualification boundary

| Area | Implementation and limit |
| --- | --- |
| Desktop | One container-local non-root KWin session with PipeWire, WirePlumber, Steam and Sunshine; no host display/socket or inherited rootful supervisor |
| Capture/encode | `capture=kwin`, `encoder=vulkan`; compare VA-API separately on the same accepted capture path |
| Graphics | Sunshine and base inputs are locked; each build records installed packages and needs a fresh immutable image identity |
| State | Per-owner gaming-home PVC retains pairing, config, Steam data and shader caches; no automatic state migration/reset |
| GPU | One `amd.com/gpu`; verify actual render/capture/encode placement by BDF, not an environment index |

The existing 12-CPU/12-GiB gaming envelope, 2-GiB shared-memory limit, one-GPU
request, AI-unload checks and cooperative build inhibition are retained. This
change adds no controller, monitoring service, host mount or live-cluster action.

## Build and inspect the image

Run from the reviewed full checkout, not the live installer:

```sh
# Default: non-root KWin Wayland session.
./bin/workstationctl sunshine image-build artifacts/sunshine-wayland-01

# Separate Arch candidate, same Sunshine release and existing session/profile.
./bin/workstationctl sunshine image-build artifacts/sunshine-arch-01 arch-wayland

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

The opt-in `arch-wayland` target uses `Dockerfile.arch`, the locked official Arch
base and `SUNSHINE_ARCH_SNAPSHOT`. It upgrades the complete base against that
dated signed repository set and installs the hash-pinned upstream Arch Sunshine
package. It uses native Arch Steam packages and catatonit; it reuses the existing
non-root session, profile, device checks and persistent-home contract. The
Debian targets and default selection remain available. Its receipt records the
repository snapshot and package hash; Debian-specific version fields are null.

The tested Arch stack used Sunshine `2026.906.222525-1`, KWin `6.8.2-1`, Mesa
`1:26.2.6-1` and XWayland `24.1.14-1`. Its initial startup exposed a missing
`env` object in the default apps template, now corrected. The session checks
JSON syntax and `env`/`apps` types before launch and preserves invalid existing
files for owner repair; this is not a complete app-schema validator.

The older Debian capture trials failed even after matching `kwin-common`
plugins were added; its XWayland also lacked an option considered by the older
input hypothesis. Preserve those failures. The Arch success does not establish
one cause for the Debian capture failure, nor qualify that candidate's input.

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

`sunshine profile` creates a new `sunshine.env` and an inactive
`kwinrc.input-example`; it does not edit or deploy a workload or grant input
permission. The example is for the owner-approved KWin 6.8.2 setup described
below. Copy reviewed environment values to the `sunshine-profile` ConfigMap generator in
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

For a custom HTTPS web UI address, add that exact trusted origin to the owner's
`sunshine.conf` before starting Sunshine. For example, the temporary LAN test at
`https://192.168.2.15:31990` requires:

```ini
csrf_allowed_origins = https://192.168.2.15:31990
```

Use the actual UI address and port; omit `/welcome` or other paths. This setting
allows state-changing requests from the trusted origin without a CSRF token;
authentication remains required where applicable. Other browser origins still
require a valid token. Keep the list narrow and do not disable CSRF protection.
Sunshine also trusts its built-in localhost origins, so a loopback-only SSH
forward can use those defaults. This setting does not open a firewall or expose
a Service. See the [Sunshine configuration reference](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2configuration.html#csrf_allowed_origins).

The Wayland image runs as UID/GID `1000`, with all Linux capabilities dropped.
KWin starts the virtual Wayland socket and provides its actual XWayland
`DISPLAY` and `XAUTHORITY` to its session hook. The hook starts PipeWire,
WirePlumber and a container-local Pulse sink before Steam and Sunshine. It
serializes use of each persistent home and stops child process groups on exit.
It never inherits an upstream `DISPLAY`, host D-Bus or host Pulse socket.

The maintained image uses `/home/default`, not the old placeholder's `/home/user`.
The PVC itself is unchanged. Existing relative files remain on it, but inspect
UID/GID ownership and any absolute paths in app definitions before migrating.
Take an independent backup first; no automatic chown, copy, pairing reset or deletion is
performed on existing state. New homes receive minimal default app definitions.
Inspect old app definitions for Xrandr commands and Xorg connector names; remove
or replace those commands explicitly. The migration does not rewrite user apps.

Include every bound `gaming-home`, `parent-gaming-home` and `kids-gaming-home`
claim in the [application-consistent backup](SERVICE-RECOVERY.md). Container
`/home/default` is a PVC mount, not the host's `/home`; the host Restic include
list does not capture it. Stop games gracefully and verify gaming writers have
exited before capture. Preserve pairing/configuration/app definitions, Steam
user data, local saves, Proton compatibility prefixes and optional portal state.
The initial capture includes the complete home to avoid excluding hidden saves.
Matching account Secrets are recovered with the K3s state. Restore ownership,
Moonlight pairing and a saved game from both the backup SSD and S3.

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

## Input on the tested Arch candidate

KWin 6.8.2 passes `LIBEI_SOCKET` to Xwayland and uses its own input permission
dialog. This route does not use `-enable-ei-portal`. See the pinned
[EIS backend](https://github.com/KDE/kwin/blob/f130ab33c8e6f3b9d728aade87836086ae9a8ed4/src/plugins/eis/eisbackend.cpp)
and [authorization check](https://github.com/KDE/kwin/blob/f130ab33c8e6f3b9d728aade87836086ae9a8ed4/src/plugins/eis/xwaylandeiscontext.cpp).
An unapproved client can display a consent dialog that the blocked input cannot
click. Option presence or a device mount alone does not prove a working route.

Do not add `/dev/uinput` as a guessed fix: the virtual KWin backend does not
consume ordinary libinput devices, and changing Sunshine's fallback can leave
input undelivered. Keep the tested direct-EIS route separate from the older
Debian portal hypothesis.

For an owner-approved headless trial on that version, the isolated container's
`$XDG_CONFIG_HOME/kwinrc` can authorize the expected client before KWin starts:

```ini
[Xwayland]
XwaylandEisNoPrompt=false
XwaylandEisNoPromptApps=sunshine.real
```

This is opt-in. The expected name follows from the installed executable
`/usr/lib/workstation/sunshine.real` and Xwayland's executable-basename rule.
Confirm the actual requester during qualification. KDE matches this name without
verifying the X11 application's identity; another X11 app can claim the same
name. Keep the grant inside the isolated gaming home and keep the global
no-prompt setting false. Preserve other owner entries. Revoke only this entry
through Legacy X11 App Support or the same config, then restart the session.
Do not add an empty-app portal grant for this different input mechanism.

Native KDE configuration parsing and read-back passed. In the bounded Moonlight
test on 1 November, the owner confirmed mouse movement, clicking and typing in
both X11 and native Wayland test windows after this grant was applied. The live
Pod's config and Xwayland input socket were also checked. This is a manual basic
input pass on the pinned image, not gaming latency, audio, controller or restart
qualification. See `test-results/sunshine-input-bootstrap.ReskhlIA/HANDOFF.md`.

The generated `kwinrc.input-example` preserves these exact tested settings.
After owner approval and a version check, merge its two entries into the
isolated gaming home's `.config/kwinrc` before session startup. Preserve other
owner entries; the profile generator never installs the example automatically.
Native KWin capture avoids capture consent, not all input consent. Keep gaming
deployment disabled until its remaining acceptance checks pass.

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

## Image and target qualification

Build a fresh candidate image in a new output directory, then run its bounded
nonroot smoke test before target testing:

```sh
./bin/workstationctl sunshine image-build artifacts/sunshine-wayland-candidate
gaming_image_id=$(jq -r .local_image_id artifacts/sunshine-wayland-candidate/build-result.json)
HOME_LAB_GAME_IMAGE="$gaming_image_id" bash tests/test_sunshine_image.sh
```

A bounded non-root image smoke test can check packaging and startup prerequisites,
but cannot qualify GPU rendering, capture, encoding, portal/input consent,
controller input, audio, real clients, latency, frame times or power.

Run the target diagnostics and paired measurements above before marking the
deployment qualified. A local image build or rendered manifest is not target
qualification.
