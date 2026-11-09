#!/usr/bin/env bash
# Runs inside the pinned TheRock CI image. Applies the pinned patch series to
# SGLang, builds the gfx1201 AOT kernel wheel and the SGLang wheel against the
# ROCm and PyTorch stages, then resolves a complete offline wheelhouse for the
# runtime image, which installs exactly those wheels offline. The wheelhouse
# digests in the receipt are the resolved dependency lock.
set -euo pipefail
: "${SGLANG_REPOSITORY:?}" "${SGLANG_COMMIT:?}" "${SGLANG_PATCHSET_SHA256:?}" "${GPU_TARGET:?}" "${PYTHON_ABI:?}" "${JOBS:?}"
rocm=/work/rocm-out
torch=/work/torch-out
run=/work/run
out=$run/out
src=$run/src/sglang
mkdir -p "$out/wheels" "$out/wheelhouse"
git config --global --add safe.directory '*'

# The patch series must be exactly the one stack.lock names.
[[ $(/pipeline/patchset-hash.sh /patches) == "$SGLANG_PATCHSET_SHA256" ]] ||
  {
    echo 'STOP: patch series does not match SGLANG_PATCHSET_SHA256' >&2
    exit 1
  }

"/opt/python/$PYTHON_ABI/bin/python3" -m venv /work/venv
# shellcheck disable=SC1091
source /work/venv/bin/activate
pip install --quiet --upgrade pip
read -r rocm_version < <(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_version"])' "$rocm/RECEIPT.json")
links=(--find-links "$rocm/packages/dist" --find-links "$torch/wheels")
extras=libraries,devel
# The device extra's name depends on how TheRock split the SDK ("device" for
# one target, "device-<target>" otherwise); use the one the package declares.
device=$(tar -xOzf "$(compgen -G "$rocm/packages/dist/rocm-[0-9]*.tar.gz")" --wildcards '*/PKG-INFO' |
  awk -v t="device-$GPU_TARGET" '$1 == "Provides-Extra:" && ($2 == t || $2 == "device") {print $2}' | sort -r | head -n 1)
[[ -z $device ]] || extras+=",$device"
echo "ROCM_EXTRAS=$extras"
pip install --quiet "${links[@]}" "rocm[$extras]==$rocm_version" "$torch"/wheels/*.whl

# Hold the ROCm and PyTorch builds fixed for everything resolved after this.
pip list --format=freeze | grep -i -E '^(rocm|rocm[-_]sdk[-_][a-z0-9_-]+|torch|torchvision|triton|pytorch[-_]triton[-_]rocm)==' \
  >"$out/constraints.txt"
grep -q -i '^torch==' "$out/constraints.txt" || {
  echo 'STOP: torch is not installed from the torch stage' >&2
  exit 1
}
pip install --quiet -c "$out/constraints.txt" build wheel 'setuptools>=61' 'setuptools-rust>=1.10' 'setuptools-scm>=8'

# A shared clone keeps tags for versioning; each run checks out a fresh copy.
[[ -d /work/src/sglang/.git ]] || git clone --quiet "$SGLANG_REPOSITORY" /work/src/sglang
git -C /work/src/sglang fetch --quiet --tags origin
git -C /work/src/sglang cat-file -e "$SGLANG_COMMIT^{commit}"
rm -rf -- "$src"
git clone --quiet --shared --no-checkout /work/src/sglang "$src"
git -C "$src" checkout --quiet --detach "$SGLANG_COMMIT"
for patch in /patches/*.patch; do
  git -C "$src" apply --check "$patch"
  git -C "$src" apply "$patch"
  echo "APPLIED=${patch##*/}"
done

# Version names the upstream base, the commit and the patch series. Upstream
# tags releases on release branches, so `git describe` from main finds a stale
# tag; its own describe script (pyproject's git_describe_command) finds the
# newest release. It prints a bare tag on a release commit.
sglang_version() {
  local describe=$1 commit=$2 series=$3 base count label
  [[ $describe =~ ^v([0-9][0-9a-z.]*)(-([0-9]+)-g[0-9a-f]+)?$ ]] || return 1
  base=${BASH_REMATCH[1]} count=${BASH_REMATCH[3]:-0}
  label=g${commit:0:8}.p${series:0:8}
  # A post-release base cannot take a second .post; its count joins the label.
  if [[ $base == *post* ]]; then
    printf '%s+n%s.%s\n' "$base" "$count" "$label"
  else
    printf '%s.post%s+%s\n' "$base" "$count" "$label"
  fi
}
describe=$(cd "$src" && python3 scripts/release/get_version_tag.py)
SETUPTOOLS_SCM_PRETEND_VERSION=$(sglang_version "$describe" "$SGLANG_COMMIT" "$SGLANG_PATCHSET_SHA256") || {
  echo "STOP: unexpected SGLang version tag $describe" >&2
  exit 1
}
python3 -c 'import sys; from packaging.version import Version; Version(sys.argv[1])' "$SETUPTOOLS_SCM_PRETEND_VERSION" || {
  echo "STOP: $SETUPTOOLS_SCM_PRETEND_VERSION is not a valid Python package version" >&2
  exit 1
}
export SETUPTOOLS_SCM_PRETEND_VERSION
echo "SGLANG_VERSION=$SETUPTOOLS_SCM_PRETEND_VERSION"

# The packaged SDK, not /opt/rocm, is the toolchain for kernels and runtime JIT.
ROCM_HOME=$(rocm-sdk path --root)
export ROCM_HOME ROCM_PATH=$ROCM_HOME PATH=$ROCM_HOME/bin:$PATH
export MAX_JOBS=$JOBS CMAKE_BUILD_PARALLEL_LEVEL=$JOBS CCACHE_DIR=/work/ccache AMDGPU_TARGET=$GPU_TARGET
! command -v ccache >/dev/null || ccache --zero-stats >/dev/null

(
  cd "$src/python/sglang/kernels/aot"
  rm -f pyproject.toml
  mv pyproject_rocm.toml pyproject.toml
  python setup_rocm.py bdist_wheel --dist-dir "$out/wheels"
  # The AOT build tree sits inside the SGLang package; keep it out of that wheel.
  rm -rf build
)
(
  cd "$src/python"
  cp pyproject_other.toml pyproject.toml
  python -m build --wheel --no-isolation --outdir "$out/wheels" .
)

# Python 3.14 has no wheels for srt_hip's petit_kernel or wave-lang; the
# gfx1201 runtime uses runtime_common plus the AOT kernels, as before.
sglang_wheel=$(compgen -G "$out/wheels/sglang-*.whl")
pip wheel --quiet --wheel-dir "$out/wheelhouse" -c "$out/constraints.txt" "${links[@]}" --find-links "$out/wheels" \
  "rocm[$extras]==$rocm_version" "${sglang_wheel}[runtime_common]" "$out"/wheels/sglang_kernel-*.whl "$torch"/wheels/*.whl

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
patches = sorted(pathlib.Path("/patches").glob("*.patch"))
keys = ["SGLANG_REPOSITORY", "SGLANG_COMMIT", "SGLANG_PATCHSET_SHA256", "GPU_TARGET", "PYTHON_ABI", "JOBS"]
receipt = {
    "schema": 1, "stage": "sglang", **{k.lower(): os.environ[k] for k in keys},
    "sglang_version": os.environ["SETUPTOOLS_SCM_PRETEND_VERSION"],
    "rocm_receipt_sha256": sha(pathlib.Path("/work/rocm-out/RECEIPT.json")),
    "torch_receipt_sha256": sha(pathlib.Path("/work/torch-out/RECEIPT.json")),
    "patches": [{"file": p.name, "sha256": sha(p)} for p in patches],
    "wheelhouse": [{"file": w.name, "size": w.stat().st_size, "sha256": sha(w)}
                   for w in sorted((out / "wheelhouse").glob("*.whl"))],
}
(out / "RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(f"SGLANG_STAGE=built; wheelhouse={len(receipt['wheelhouse'])}")
PY
