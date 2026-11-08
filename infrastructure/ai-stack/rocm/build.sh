#!/usr/bin/env bash
# Runs inside the pinned TheRock CI image. Builds ROCm for one GPU target and
# its Python SDK packages, then writes a receipt. All inputs come from the
# environment set by build-stage.sh; nothing here is host-specific.
set -euo pipefail
: "${THEROCK_REPOSITORY:?}" "${THEROCK_COMMIT:?}" "${GPU_TARGET:?}" "${JOBS:?}" "${BUILD_DATE:?}"
src=/work/src/TheRock
build=/work/build
out=/work/out
mkdir -p "$out"
git config --global --add safe.directory '*'

# The source checkout is a persistent cache; move it to the pinned commit.
[[ -d $src/.git ]] || git clone --quiet --no-checkout "$THEROCK_REPOSITORY" "$src"
git -C "$src" fetch --quiet origin "$THEROCK_COMMIT"
git -C "$src" checkout --quiet --detach "$THEROCK_COMMIT"
[[ $(git -C "$src" rev-parse HEAD) == "$THEROCK_COMMIT" ]]
cd "$src"
# Self-describing version, e.g. 10.3.0.dev20261108+git4c7aaf53.
rocm_version=$(python3 -c 'import json; print(json.load(open("version.json"))["rocm-version"])')
export PACKAGE_VERSION="${rocm_version}.dev${BUILD_DATE}+git${THEROCK_COMMIT:0:8}"
echo "PACKAGE_VERSION=$PACKAGE_VERSION"

python3 -m venv /work/venv
# shellcheck disable=SC1091
source /work/venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
python3 build_tools/fetch_sources.py

# Persistent ccache: unchanged components rebuild from cache on later refreshes.
eval "$(python3 build_tools/setup_ccache.py --dir /work/ccache)"
# Bound compiler memory to the JOBS budget. TheRock builds each sub-project
# with its own `cmake --build`, which otherwise runs Ninja at full machine width
# for the foreground sub-project and every background one at once; Flang
# sources and LLVM links are the largest jobs. Unbounded, the 2026-11-08 build
# thrashed at the container memory limit. These settings change scheduling,
# not compiler flags, so an interrupted build resumes from its objects.
export CMAKE_BUILD_PARALLEL_LEVEL=$((JOBS / 2 > 0 ? JOBS / 2 : 1))
flang_jobs=$((JOBS / 4 > 0 ? JOBS / 4 : 1))
cmake -B "$build" -GNinja . \
  -DTHEROCK_AMDGPU_TARGETS="$GPU_TARGET" \
  -DTHEROCK_DIST_AMDGPU_TARGETS="$GPU_TARGET" \
  -DTHEROCK_BACKGROUND_BUILD_JOBS=1 \
  -DFLANG_PARALLEL_COMPILE_JOBS="$flang_jobs" \
  -DLLVM_PARALLEL_LINK_JOBS=2 \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  -DCMAKE_HIP_COMPILER_LAUNCHER=ccache
cmake --build "$build" --parallel "$JOBS"

# Compiler cache effectiveness for this stage, read by stage-metrics.py.
if command -v ccache >/dev/null; then
  ccache --print-stats >"$out/ccache-stats.tsv" || rm -f -- "$out/ccache-stats.tsv"
fi

python3 build_tools/build_python_packages.py \
  --artifact-dir "$build/artifacts" --dest-dir "$out/packages" --version "$PACKAGE_VERSION"

# Receipt: what was built, from what, and the resulting package digests.
python3 - "$out" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
out = pathlib.Path(sys.argv[1])
packages = sorted(p for p in (out / "packages").rglob("*") if p.is_file() and p.suffix in {".whl", ".gz"})
def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
receipt = {
    "schema": 1, "stage": "rocm",
    "therock_repository": os.environ["THEROCK_REPOSITORY"], "therock_commit": os.environ["THEROCK_COMMIT"],
    "gpu_target": os.environ["GPU_TARGET"], "package_version": os.environ["PACKAGE_VERSION"],
    "build_image": os.environ.get("BUILD_IMAGE", ""), "jobs": int(os.environ["JOBS"]),
    "submodules": subprocess.run(["git", "-C", "/work/src/TheRock", "submodule", "status", "--recursive"],
                                 capture_output=True, text=True).stdout.splitlines(),
    "packages": [{"file": str(p.relative_to(out)), "size": p.stat().st_size, "sha256": sha(p)} for p in packages],
}
(out / "RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(f"ROCM_STAGE=built; packages={len(packages)}; receipt={out / 'RECEIPT.json'}")
PY
