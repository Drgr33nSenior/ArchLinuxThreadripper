#!/usr/bin/env bash
# Runs inside the pinned TheRock CI image. Builds PyTorch, Triton and
# TorchVision from their pinned commits against the ROCm stage's SDK wheels,
# using TheRock's own build helpers at the pinned TheRock commit.
set -euo pipefail
: "${THEROCK_COMMIT:?}" "${GPU_TARGET:?}" "${PYTHON_ABI:?}" "${JOBS:?}"
: "${PYTORCH_REPOSITORY:?}" "${PYTORCH_COMMIT:?}" "${TORCHVISION_REPOSITORY:?}" "${TORCHVISION_COMMIT:?}"
: "${TRITON_REPOSITORY:?}" "${TRITON_COMMIT:?}"
helpers=/work/src/TheRock/external-builds/pytorch
rocm=/work/rocm-out
out=/work/run/out
mkdir -p "$out"
git config --global --add safe.directory '*'
[[ $(git -C /work/src/TheRock rev-parse HEAD) == "$THEROCK_COMMIT" ]]
rocm_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_version"])' "$rocm/RECEIPT.json")

"/opt/python/$PYTHON_ABI/bin/python3" -m venv /work/venv
# shellcheck disable=SC1091
source /work/venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r "$helpers/requirements-ci.txt"
cd "$helpers"

# Pinned checkouts (TheRock applies its ROCm patches during checkout).
python pytorch_torch_repo.py checkout --checkout-dir /work/run/src/pytorch \
  --gitrepo-origin "$PYTORCH_REPOSITORY" --repo-hashtag "$PYTORCH_COMMIT"
python pytorch_vision_repo.py checkout --checkout-dir /work/run/src/vision \
  --gitrepo-origin "$TORCHVISION_REPOSITORY" --repo-hashtag "$TORCHVISION_COMMIT"
python pytorch_triton_repo.py checkout --checkout-dir /work/run/src/triton --torch-dir /work/run/src/pytorch \
  --gitrepo-origin "$TRITON_REPOSITORY"
[[ $(tr -d '[:space:]' </work/run/src/pytorch/.ci/docker/ci_commit_pins/triton.txt) == "$TRITON_COMMIT" ]] ||
  {
    echo 'STOP: PyTorch now pins a different Triton; refresh stack.lock' >&2
    exit 1
  }

# Install the SDK from the ROCm stage only. A per-target device wheel exists
# only when TheRock splits kernel packs, so ask for that extra only if built.
extras=()
compgen -G "$rocm/packages/dist/rocm_sdk_device_${GPU_TARGET}-*.whl" >/dev/null && extras=(--rocm-extras "device-$GPU_TARGET")
export MAX_JOBS=$JOBS CMAKE_BUILD_PARALLEL_LEVEL=$JOBS CCACHE_DIR=/work/ccache
! command -v ccache >/dev/null || ccache --zero-stats >/dev/null
python build_prod_wheels.py build \
  --install-rocm --find-links "$rocm/packages/dist" --rocm-sdk-version "==$rocm_version" "${extras[@]}" \
  --pytorch-rocm-arch "$GPU_TARGET" \
  --pytorch-dir /work/run/src/pytorch --pytorch-vision-dir /work/run/src/vision --triton-dir /work/run/src/triton \
  --build-triton --build-pytorch-vision --no-build-pytorch-audio --no-build-apex \
  --use-ccache --output-dir "$out/wheels" --pip-cache-dir /work/run/pip-cache

# Compiler cache effectiveness for this stage, read by stage-metrics.py.
if command -v ccache >/dev/null; then
  ccache --print-stats >"$out/ccache-stats.tsv" || rm -f -- "$out/ccache-stats.tsv"
fi

python3 - "$out" <<'PY'
import hashlib, json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
wheels = sorted((out / "wheels").glob("*.whl"))
keys = ["THEROCK_COMMIT", "GPU_TARGET", "PYTHON_ABI", "PYTORCH_COMMIT", "TORCHVISION_COMMIT", "TRITON_COMMIT", "JOBS"]
rocm = json.loads(pathlib.Path("/work/rocm-out/RECEIPT.json").read_text())
receipt = {"schema": 1, "stage": "torch", **{k.lower(): os.environ[k] for k in keys},
           "rocm_package_version": rocm["package_version"],
           "wheels": [{"file": w.name, "size": w.stat().st_size, "sha256": sha(w)} for w in wheels]}
(out / "RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(f"TORCH_STAGE=built; wheels={len(wheels)}")
PY
