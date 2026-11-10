#!/usr/bin/env bash
# Build one AI-stack stage on this host with rootless Podman. Inputs come only
# from stack.lock. Each run directory is keyed by the pins that affect it, so a
# refresh rebuilds only stages whose inputs changed; ccache and source clones
# persist under $AI_STACK_ROOT across runs.
# Usage: build-stage.sh rocm|torch|sglang|image|comfyui|swarmui|creative
set -euo pipefail
stop() {
  printf 'STOP: %s\n' "$*" >&2
  exit 1
}
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
stage=${1:-}
[[ $stage =~ ^(rocm|torch|sglang|image|comfyui|swarmui|creative)$ ]] ||
  stop 'usage: build-stage.sh rocm|torch|sglang|image|comfyui|swarmui|creative'
lock=$here/stack.lock
read_lock() {
  awk -F= -v key="$1" '$1 == key {print substr($0, index($0, "=") + 1); n++} END {exit n != 1}' "$lock" ||
    stop "stack.lock needs exactly one $1"
}
commit() {
  local value
  value=$(read_lock "$1")
  [[ $value =~ ^[a-f0-9]{40}$ ]] || stop "$1 must be a full commit"
  printf '%s\n' "$value"
}
digest_image() {
  local value
  value=$(read_lock "$1")
  [[ $value =~ @sha256:[a-f0-9]{64}$ ]] || stop "$1 must be pinned by digest"
  printf '%s\n' "$value"
}
key_of() { printf '%s\n' "$@" | sha256sum | cut -c1-12; }

export THEROCK_REPOSITORY THEROCK_COMMIT GPU_TARGET BUILD_IMAGE JOBS BUILD_DATE PYTHON_ABI
THEROCK_REPOSITORY=$(read_lock THEROCK_REPOSITORY)
THEROCK_COMMIT=$(commit THEROCK_COMMIT)
GPU_TARGET=$(read_lock GPU_TARGET)
BUILD_IMAGE=$(digest_image THEROCK_BUILD_IMAGE)
PYTHON_ABI=$(read_lock PYTHON_ABI)
[[ $GPU_TARGET =~ ^gfx[0-9a-f]+$ ]] || stop 'GPU_TARGET must be a gfx target'
[[ $PYTHON_ABI =~ ^cp[0-9]+-cp[0-9]+t?$ ]] || stop 'PYTHON_ABI must name a /opt/python interpreter'
export PYTORCH_REPOSITORY PYTORCH_COMMIT TORCHVISION_REPOSITORY TORCHVISION_COMMIT TRITON_REPOSITORY TRITON_COMMIT
PYTORCH_REPOSITORY=$(read_lock PYTORCH_REPOSITORY)
PYTORCH_COMMIT=$(commit PYTORCH_COMMIT)
TORCHVISION_REPOSITORY=$(read_lock TORCHVISION_REPOSITORY)
TORCHVISION_COMMIT=$(commit TORCHVISION_COMMIT)
TRITON_REPOSITORY=$(read_lock TRITON_REPOSITORY)
TRITON_COMMIT=$(commit TRITON_COMMIT)
export SGLANG_REPOSITORY SGLANG_COMMIT SGLANG_PATCHSET_SHA256
SGLANG_REPOSITORY=$(read_lock SGLANG_REPOSITORY)
SGLANG_COMMIT=$(commit SGLANG_COMMIT)
SGLANG_PATCHSET_SHA256=$(read_lock SGLANG_PATCHSET_SHA256)
[[ $SGLANG_PATCHSET_SHA256 =~ ^[a-f0-9]{64}$ ]] || stop 'SGLANG_PATCHSET_SHA256 must be a sha256'

# Parallelism follows the repository's measured build budget (no swap here).
ctl=(/usr/lib/bridge/workstation-runtime/bin/workstationctl)
[[ ! -f $HOME/workstation/workstation.conf ]] || ctl+=(--config "$HOME/workstation/workstation.conf")
JOBS=${AI_STACK_JOBS:-$("${ctl[@]}" build environment normal | awk -F= '/CMAKE_BUILD_PARALLEL_LEVEL/ {print $2}')}
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || stop 'could not determine build parallelism'
BUILD_DATE=$(date -u +%Y%m%d)

# Stage keys: a stage's run directory changes whenever any upstream pin does.
root=${AI_STACK_ROOT:-$HOME/ai-stack}
rocm_run=$root/builds/rocm-${THEROCK_COMMIT:0:12}
torch_run=$root/builds/torch-$(key_of "$THEROCK_COMMIT" "$PYTHON_ABI" "$PYTORCH_COMMIT" "$TORCHVISION_COMMIT" "$TRITON_COMMIT")
sglang_key=$(key_of "${torch_run##*/}" "$SGLANG_COMMIT" "$SGLANG_PATCHSET_SHA256")
sglang_run=$root/builds/sglang-$sglang_key
# The image also changes with the files it is built from (stack.lock is copied
# into it) and with the smoke test that gates it.
image_key=$(key_of "$sglang_key" "$(cat -- "$lock" "$here/image/Containerfile" "$here/image/smoke.py" | sha256sum | cut -c1-64)")
image_run=$root/builds/image-$image_key
# Image generation: the ComfyUI wheelhouse on the same ROCm/PyTorch builds,
# SwarmUI built with the pinned .NET SDK, and the creative image from both.
export COMFYUI_REPOSITORY COMFYUI_COMMIT SWARMUI_REPOSITORY SWARMUI_COMMIT
COMFYUI_REPOSITORY=$(read_lock COMFYUI_REPOSITORY)
COMFYUI_COMMIT=$(commit COMFYUI_COMMIT)
SWARMUI_REPOSITORY=$(read_lock SWARMUI_REPOSITORY)
SWARMUI_COMMIT=$(commit SWARMUI_COMMIT)
DOTNET_IMAGE=$(digest_image DOTNET_SDK_IMAGE)
content() { cat -- "$@" | sha256sum | cut -c1-64; }
comfyui_key=$(key_of "${torch_run##*/}" "$COMFYUI_COMMIT" "$SWARMUI_COMMIT" \
  "$(content "$here/comfyui/build.sh" "$here/comfyui/requirements.py")")
comfyui_run=$root/builds/comfyui-$comfyui_key
swarmui_key=$(key_of "$SWARMUI_COMMIT" "$DOTNET_IMAGE" "$(content "$here/swarmui/build.sh" "$here"/swarmui/*.patch)")
swarmui_run=$root/builds/swarmui-$swarmui_key
creative_files=("$here"/creative/{Containerfile,smoke.py,launch.sh,check_swarm_deps.py,Backends.fds})
creative_key=$(key_of "$comfyui_key" "$swarmui_key" "$(content "$lock" "${creative_files[@]}")")
creative_run=$root/builds/creative-$creative_key
patches=$root/patches/$SGLANG_PATCHSET_SHA256
need() { [[ -s $1/out/RECEIPT.json ]] || stop "build the $2 stage first ($1)"; }

# Stage progress for the Workloads dashboard and build alerts, read by
# node-exporter's textfile collector. stage-metrics.py never fails a build.
metrics_dir=${AI_STACK_METRICS_DIR:-/var/lib/workstation-ai-stack}
track() {
  local stats=$1
  python3 "$here/stage-metrics.py" "$metrics_dir" "$stage" start
  # shellcheck disable=SC2064 # Expand the stats path now; $? at exit time.
  trap "python3 '$here/stage-metrics.py' '$metrics_dir' '$stage' finish --status \$? --ccache-stats '$stats'" EXIT
}

# Scan before push, then sign and attest; image.digest is written only once
# the pushed image verifies, so a failed step is retried on the next run.
# Usage: publish TAG RUN_DIR SCAN_ALLOWLIST
publish() {
  local tag=$1 run=$2 allowlist=$3 keys authfile cosign_image trivy_image scan ref
  keys=${AI_STACK_SIGNING_DIR:-$HOME/.config/ai-stack/signing}
  [[ -s $here/signing/cosign.pub && -s $keys/cosign.key && -s $keys/cosign_password.cred ]] ||
    stop 'image signing is not set up: run signing/init-key.sh and commit signing/cosign.pub'
  authfile=${REGISTRY_AUTH_FILE:-${XDG_RUNTIME_DIR:-/run/user/$UID}/containers/auth.json}
  [[ -s $authfile ]] || stop "no registry login at $authfile; run podman login first"
  cosign_image=$(digest_image COSIGN_IMAGE)
  trivy_image=$(digest_image TRIVY_IMAGE)
  scan=$run/out/scan
  mkdir -p "$scan" "$root/scan" "$root/trivy-cache"
  rm -f -- "$scan/pushed.digest"
  podman save --format docker-archive -o "$root/scan/image.tar" "$tag"
  trivy() {
    podman run --rm --pull=missing --network host --user 0:0 -v "$root/scan:/scan:ro" \
      -v "$root/trivy-cache:/cache" -v "$scan:/out" "$trivy_image" image --cache-dir /cache \
      --input /scan/image.tar --scanners vuln --quiet "$@"
  }
  if ! { trivy --format json --output /out/vulns.json &&
    trivy --format cyclonedx --output /out/sbom.cdx.json &&
    trivy --format cosign-vuln --output /out/vuln.cosign.json; }; then
    rm -f -- "$root/scan/image.tar"
    stop 'vulnerability scan failed'
  fi
  rm -f -- "$root/scan/image.tar"
  # Record the scanner and vulnerability-database versions the reports used.
  podman run --rm --pull=never --network none --user 0:0 -v "$root/trivy-cache:/cache:ro" "$trivy_image" \
    version --cache-dir /cache --format json >"$scan/trivy-version.json"
  python3 "$here/image/scan-policy.py" "$scan/vulns.json" "$allowlist" >"$scan/policy.json" ||
    stop "vulnerability policy blocks this image; see $scan/policy.json"
  podman push --digestfile "$scan/pushed.digest" "$tag"
  ref=${tag%:*}@$(<"$scan/pushed.digest")
  COSIGN_PASSWORD=$(systemd-creds --user decrypt --name=cosign_password "$keys/cosign_password.cred" -)
  export COSIGN_PASSWORD
  # signing-config.json lists no transparency log or timestamp service, so
  # nothing is uploaded to Rekor; signatures live beside the image in Zot.
  cosign() {
    podman run --rm --pull=missing --network host --user 0:0 -e COSIGN_PASSWORD -e DOCKER_CONFIG=/auth \
      -v "$keys:/keys:ro" -v "$authfile:/auth/config.json:ro" -v "$scan:/scan:ro" -v "$here/signing:/signing:ro" \
      "$cosign_image" "$@"
  }
  cosign sign --key /keys/cosign.key --signing-config /signing/signing-config.json --yes "$ref" >/dev/null
  cosign attest --key /keys/cosign.key --signing-config /signing/signing-config.json --yes \
    --type cyclonedx --predicate /scan/sbom.cdx.json "$ref" >/dev/null
  cosign attest --key /keys/cosign.key --signing-config /signing/signing-config.json --yes \
    --type vuln --predicate /scan/vuln.cosign.json "$ref" >/dev/null
  unset COSIGN_PASSWORD
  bash "$here/signing/verify-image.sh" "$ref"
  mv -- "$scan/pushed.digest" "$run/out/image.digest"
  printf 'PUSHED=%s\nSIGNED=%s\n' "$ref" "$ref"
}
if [[ $stage == creative ]]; then
  need "$comfyui_run" comfyui
  need "$swarmui_run" swarmui
  base=$(digest_image RUNTIME_BASE_IMAGE)
  tag=$(read_lock CREATIVE_IMAGE_REPOSITORY):$creative_key
  if podman image exists "$tag" && [[ ${AI_STACK_PUSH:-0} != 1 || -s $creative_run/out/image.digest ]]; then
    printf 'BUILT=creative %s\n' "$tag"
    exit 0
  fi
  track /dev/null
  mkdir -p "$creative_run/out"
  podman build --pull=missing --build-arg RUNTIME_BASE_IMAGE="$base" --build-arg STACK_ID="$creative_key" \
    --build-context swarmui="$swarmui_run/out" --build-context comfyui="$comfyui_run/out" \
    -f "$here/creative/Containerfile" -t "$tag" "$here/creative"
  # GPU, ComfyUI and SwarmUI start-up smoke on the build host's GPUs before push.
  # Container root is the invoking (render-group) user under rootless Podman.
  podman run --rm --pull=never --network none --user 0:0 --tmpfs /tmp --device /dev/kfd --device /dev/dri \
    --group-add keep-groups --security-opt label=disable -e HOME=/tmp \
    -v "$here/creative/smoke.py:/smoke.py:ro" -v "$here/creative/Backends.fds:/smoke/Backends.fds:ro" \
    --entrypoint /SwarmUI/dlbackend/ComfyUI/venv/bin/python "$tag" /smoke.py /smoke/Backends.fds
  printf 'IMAGE=%s\n' "$tag"
  [[ ${AI_STACK_PUSH:-0} != 1 ]] || publish "$tag" "$creative_run" "$here/creative/scan-allowlist.json"
  exit 0
fi
if [[ $stage == image ]]; then
  need "$sglang_run" sglang
  base=$(digest_image RUNTIME_BASE_IMAGE)
  tag=$(read_lock IMAGE_REPOSITORY):$image_key
  if podman image exists "$tag" && [[ ${AI_STACK_PUSH:-0} != 1 || -s $image_run/out/image.digest ]]; then
    printf 'BUILT=image %s\n' "$tag"
    exit 0
  fi
  track /dev/null
  mkdir -p "$image_run/out"
  cp -- "$lock" "$sglang_run/out/stack.lock"
  podman build --pull=missing --build-arg RUNTIME_BASE_IMAGE="$base" --build-arg STACK_ID="$image_key" \
    -f "$here/image/Containerfile" -t "$tag" "$sglang_run/out"
  # HIP-only imports, the MXFP4 route and Triton's runtime compiler show only
  # with a GPU, so smoke-test on the build host's GPUs before pushing.
  # Read-only root and a /tmp tmpfs, as in the pod.
  podman run --rm --pull=never --network none --read-only --tmpfs /tmp --device /dev/kfd --device /dev/dri \
    --group-add keep-groups --security-opt label=disable -e HOME=/tmp \
    -e SGLANG_USE_AITER=false -e SGLANG_USE_AITER_MHA=false -e SGLANG_USE_AITER_MOE=false \
    -v "$here/image/smoke.py:/smoke.py:ro" --entrypoint python3 "$tag" /smoke.py
  printf 'IMAGE=%s\n' "$tag"
  [[ ${AI_STACK_PUSH:-0} != 1 ]] || publish "$tag" "$image_run" "$here/image/scan-allowlist.json"
  exit 0
fi

mkdir -p "$root/src" "$root/ccache"
mounts=(-v "$root/src:/work/src" -v "$root/ccache:/work/ccache" -v "$here:/pipeline:ro")
envs=(-e THEROCK_REPOSITORY -e THEROCK_COMMIT -e GPU_TARGET -e JOBS -e BUILD_DATE -e BUILD_IMAGE -e PYTHON_ABI)
case $stage in
  rocm)
    run=$rocm_run
    mkdir -p "$run/build" "$run/out"
    mounts+=(-v "$run/build:/work/build" -v "$run/out:/work/out")
    ;;
  torch)
    need "$rocm_run" rocm
    run=$torch_run
    mounts+=(-v "$run:/work/run" -v "$rocm_run/out:/work/rocm-out:ro")
    envs+=(-e PYTORCH_REPOSITORY -e PYTORCH_COMMIT -e TORCHVISION_REPOSITORY -e TORCHVISION_COMMIT
      -e TRITON_REPOSITORY -e TRITON_COMMIT)
    ;;
  sglang)
    need "$rocm_run" rocm
    need "$torch_run" torch
    [[ -d $patches ]] || stop "patch series $SGLANG_PATCHSET_SHA256 is not on this host; run sync.sh"
    run=$sglang_run
    mounts+=(-v "$run:/work/run" -v "$rocm_run/out:/work/rocm-out:ro" -v "$torch_run/out:/work/torch-out:ro"
      -v "$patches:/patches:ro")
    envs+=(-e SGLANG_REPOSITORY -e SGLANG_COMMIT -e SGLANG_PATCHSET_SHA256)
    ;;
  comfyui)
    need "$rocm_run" rocm
    need "$torch_run" torch
    run=$comfyui_run
    mounts+=(-v "$run:/work/run" -v "$rocm_run/out:/work/rocm-out:ro" -v "$torch_run/out:/work/torch-out:ro")
    envs+=(-e COMFYUI_REPOSITORY -e COMFYUI_COMMIT -e SWARMUI_REPOSITORY -e SWARMUI_COMMIT)
    ;;
  swarmui)
    # The .NET SDK image, not TheRock's: no ROCm or compiler cache involved.
    run=$swarmui_run
    container_image=$DOTNET_IMAGE
    mounts=(-v "$root/src:/work/src" -v "$here:/pipeline:ro" -v "$run:/work/run")
    envs=(-e SWARMUI_REPOSITORY -e SWARMUI_COMMIT)
    ;;
esac
mkdir -p "$run/out"
if [[ -s $run/out/RECEIPT.json ]]; then
  printf 'BUILT=%s %s\n' "$stage" "$run/out/RECEIPT.json"
  exit 0
fi
printf 'STAGE=%s THEROCK=%s TARGET=%s JOBS=%s RUN=%s\n' "$stage" "${THEROCK_COMMIT:0:12}" "$GPU_TARGET" "$JOBS" "$run"
track "$run/out/ccache-stats.tsv"

# Root inside the rootless container maps to the invoking user on the host.
podman run --rm --pull=missing --name "ai-stack-$stage-${run##*-}" \
  --network host --memory 48g --memory-swap 48g --pids-limit 32768 \
  "${mounts[@]}" "${envs[@]}" "${container_image:-$BUILD_IMAGE}" "/pipeline/$stage/build.sh" 2>&1 | tee "$run/out/build.log"
