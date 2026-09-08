# Sunshine streaming candidate

The repository now builds and configures a streaming candidate. It does **not**
yet deploy a qualified gaming session. Replicas remain zero, the image reference
requires promotion, and the existing session command rejects unqualified gaming
before stopping AI. No host kernel, ROCm driver, cluster or disk change is part
of this workflow. Sunshine is the server; Moonlight runs on the clients.

## Implementation record — 8 September 2026

| Change | Source and evidence | Implementation | Remaining validation |
| --- | --- | --- | --- |
| Known image contract | Maintained [Steam-Headless source](https://github.com/Steam-Headless/docker-steam-headless/tree/096fc4b1c09288b105b2eada7c386d41a51efc08); its inspected image contained Sunshine 2026.516.143833 and Mesa 25.0.7 | `infrastructure/gaming/Dockerfile`, `versions.lock`; replace the unverified publisher reference with a build-required placeholder | Rootful startup, extra services and input/display access remain unqualified |
| Current Sunshine | [2026.906.222525 release](https://github.com/LizardByte/Sunshine/releases/tag/v2026.906.222525), commit `cb72dffa3233c5815cd5ba88f09f049dd679ba75`; release asset downloaded and hashed | Pinned Debian package; remove package-installed executable capabilities; preserve upstream session lifecycle through a launcher wrapper | R9700 encode/capture and changed Linux virtual-input implementation |
| Matched graphics userspace | Debian [Mesa 26.1.2 backport](https://packages.debian.org/trixie-backports/mesa-vulkan-drivers) for amd64/i386 | Pin the Mesa package family; install during image build, not session startup; retain host `amdgpu` ownership | This is a supported distro backport, not a claim to contain every latest Mesa commit |
| Explicit encoder profiles | Versioned [configuration](https://github.com/LizardByte/Sunshine/blob/v2026.906.222525/docs/configuration.md) and [CLI parser](https://github.com/LizardByte/Sunshine/blob/v2026.906.222525/src/config.cpp) | `lib/workstation/sunshine.sh`, `config/workstation.conf.example`, gaming ConfigMap | Compare the same display/client/game inputs; no measured gain claimed |
| Allocated GPU and persistent caches | [Vulkan render-node mapping](https://github.com/LizardByte/Sunshine/blob/v2026.906.222525/src/platform/linux/vulkan_encode.cpp), [Mesa environment](https://docs.mesa3d.org/envvars.html) | Discover one accessible render node and PCI BDF; set Mesa rendering preference and Sunshine adapter; `/home/default/.cache` on existing PVC | Device-plugin isolation, actual rendering/capture GPU, permissions, no software-renderer fallback |
| Comparable diagnostics | Existing Bash/jq CLI and test conventions | `sunshine devices`, `diagnose`, `compare`; fixture and optional image smoke tests | Physical performance, power and latency are NOT RUN — target hardware unavailable |

The safe-change and infrastructure checks kept this within the existing CLI,
Kustomize profiles and session gates. No new controller or monitoring service is
introduced. The existing 12-CPU/12-GiB gaming envelope, 2-GiB shared memory,
one-GPU request, AI unload checks and cooperative build inhibition are retained.

## Build and inspect the image

Run from the reviewed full checkout, not the live installer:

```sh
./bin/workstationctl sunshine image-build artifacts/sunshine-image-01
gaming_image_id=$(jq -r .local_image_id artifacts/sunshine-image-01/build-result.json)
HOME_LAB_GAME_IMAGE="$gaming_image_id" bash tests/test_sunshine_image.sh
```

The builder sends only its staged source context and verified release asset to
the existing Docker builder, targeting `linux/amd64` explicitly. It does not
start the upstream rootful entrypoint, publish an image or contact K3s. Expect
roughly 1 GB of compressed base-image downloads plus dependencies and several
GB of builder storage. Failed builds retain evidence; nothing prunes Docker.

The base digest, Sunshine hash and Mesa version are locked. Other Debian
dependencies are resolved through signed repositories during the build, so this
is **not** a bit-for-bit reproducible dependency closure. The complete installed
package list is `/usr/share/workstation/gaming-packages.tsv` inside the image.
Rebuilds need a fresh immutable image digest and qualification. The FFmpeg CLI
package version is not proof of Sunshine's embedded FFmpeg build options.

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

Use the existing workstation config; legacy config files retain defaults:

```sh
./bin/workstationctl --config config/workstation.conf sunshine profile artifacts/sunshine-profile-01
./bin/workstationctl session plan gaming
```

The first command writes a new `sunshine.env`; it does not edit a deployment.
Copy the selected values into the existing `sunshine-profile` ConfigMap generator
in `apps/base/steam-headless/kustomization.yaml`, or merge that generator in a
parent/kids overlay. Render with `kubectl kustomize apps/overlays/family` and
review the resulting per-owner ConfigMap references. Normal Kustomize content
hashes change the Pod template when a profile changes. Follow maintenance/session
shutdown before promotion; do not patch a running game to benchmark it.

| Setting | Baseline | Explicit experiment |
| --- | --- | --- |
| `SUNSHINE_ENCODER` | `vaapi` | `vulkan` |
| `SUNSHINE_CAPTURE` | `x11` for the candidate XFCE/Xorg session | Qualified `kms`, `wlr` or `kwin` DMA-BUF path |
| `SUNSHINE_OUTPUT_NAME` | Empty: existing config/automatic selection | Discovered connector name; required for KMS; no numeric GPU/display guesses |
| `SUNSHINE_HEVC_MODE`, `SUNSHINE_AV1_MODE` | `0`, capability detection | `1` disables; `2` enables advertised 8-bit support; `3` advertises HDR only after qualification |
| `SUNSHINE_VK_TUNE` | `2`, low latency | `3`, ultra-low latency, with quality comparison |
| `SUNSHINE_VAAPI_STRICT_RC_BUFFER` | `disabled` | `enabled` for scene-change network-drop testing |

Vulkan+x11 is rejected: the pinned Vulkan path expects DMA-BUF capture. For a
paired VA-API/Vulkan test, use the **same qualified capture path for both**. For
example, `SUNSHINE_CAPTURE=wlr` requires an actual reviewed wlroots session; the
recipe does not install one. Setting an environment variable cannot create a
Wayland compositor, headless connector or permission to capture another GPU.
KMS requires primary DRM access and privilege which conflicts with the current
baseline policy. No namespace exception or host mount has been introduced.

The runtime launcher uses Sunshine's `key=value` overrides. It preserves the
existing configuration, pairing state, app definitions and credentials. Profile
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

The maintained image uses `/home/default`, not the old placeholder's `/home/user`.
The PVC itself is unchanged. Existing relative files remain on it, but inspect
UID/GID ownership and any absolute paths in app definitions before migrating.
Take a NAS backup first; no automatic chown, copy, pairing reset or deletion is
performed by this change. Upstream first-run initialization must be reviewed too.

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

The rootful Steam-Headless entrypoint, its extra services/default accounts and
input requirements remain incompatible with the current conservative promotion
gate. Resolving that security/display contract is a separate prerequisite; do not
add capabilities or mark the deployment qualified just to start it. Validate
`/dev/uhid`, `/dev/uinput`, audio and display access for the pinned input backend,
and expose only reviewed private streaming ports. Steam downloads need reviewed
egress; they remain denied by the current policy.

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
a successful stream. Reject llvmpipe/software rendering; inspect Sunshine's local
startup logs for the selected encoder and capture path, and verify actual VCN
activity and hardware decoding with the client. Do not publish raw logs without
checking their contents. Record power/temperature separately with existing GPU
telemetry; the diagnostic command does not sample workload power.

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

## Local verification — 8 September 2026

`workstationctl sunshine image-build` completed locally. The final smoke-tested
Docker config ID was
`sha256:a6025d38327649c9d2837c235b9b897d4ddf242c45e65e4074104896d5b02145`;
this is not a deployable registry manifest reference.

`HOME_LAB_PYTHON=/usr/local/bin/python3.11 HOME_LAB_GAME_IMAGE=<that-local-ID> make check`
passed all 36 test files, including the opt-in image test. Shell syntax,
ShellCheck, YAML parsing, Kustomize semantics and the Jinja/TOML checks passed.
The isolated non-root image test checked the pinned Sunshine/Mesa versions,
native profile parsing, source-file permissions, removed executable capabilities,
missing-GPU refusal and unchanged persistent configuration. It did not start a
stream. Hash-mismatch and failed-build fixtures produced no success record.

Optional checks skipped: shfmt, Bats, Ansible, real ccache compilation,
CMake/Ninja integration, operator rendering without its local chart archive,
the Bash-4-only USB signal fixture, and Linux systemd verification. IDE build
reported success with limited build diagnostics. Its weak inspection warnings
concerned dpkg format fields, a generated ConfigMap, deliberately uncommitted
credential Secrets and an indirectly invoked test fixture; no controls were
disabled to suppress those warnings.

R9700 capture/encoding, GPU isolation, input/audio, client compatibility,
frame-time/latency/power measurements and recovery under a real streaming session
remain **NOT RUN — target hardware unavailable**. The rootful image's admission
incompatibility also remains unresolved; hardware arrival alone does not qualify
this deployment.
