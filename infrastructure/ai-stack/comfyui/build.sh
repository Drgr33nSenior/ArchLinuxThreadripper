#!/usr/bin/env bash
# Runs inside the pinned TheRock CI image. Resolves a complete offline
# wheelhouse for ComfyUI as SwarmUI self-starts it: ComfyUI's requirements plus
# the packages SwarmUI checks on start (requirements.py reads both from the
# pinned sources), held to the ROCm and PyTorch stages' builds. The creative
# image installs exactly these wheels offline; their digests in the receipt are
# the resolved dependency lock. Also exports the pinned ComfyUI source tree.
set -euo pipefail
: "${COMFYUI_REPOSITORY:?}" "${COMFYUI_COMMIT:?}" "${SWARMUI_REPOSITORY:?}" "${SWARMUI_COMMIT:?}"
: "${GPU_TARGET:?}" "${PYTHON_ABI:?}"
rocm=/work/rocm-out
torch=/work/torch-out
run=/work/run
out=$run/out
rm -rf -- "$out/wheelhouse" "$out/ComfyUI"
mkdir -p "$out/wheelhouse" "$out/ComfyUI"
git config --global --add safe.directory '*'
"/opt/python/$PYTHON_ABI/bin/python3" -m venv /work/venv
# shellcheck disable=SC1091
source /work/venv/bin/activate
pip install --quiet --upgrade pip
read -r rocm_version < <(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_version"])' "$rocm/RECEIPT.json")
links=(--find-links "$rocm/packages/dist" --find-links "$torch/wheels")
# Runtime libraries and the target's device kernels; no devel SDK (no JIT here).
extras=libraries
device=$(tar -xOzf "$(compgen -G "$rocm/packages/dist/rocm-[0-9]*.tar.gz")" --wildcards '*/PKG-INFO' |
  awk -v t="device-$GPU_TARGET" '$1 == "Provides-Extra:" && ($2 == t || $2 == "device") {print $2}' | sort -r | head -n 1)
[[ -z $device ]] || extras+=",$device"
echo "ROCM_EXTRAS=$extras"
pip install --quiet "${links[@]}" "rocm[$extras]==$rocm_version" "$torch"/wheels/*.whl
pip list --format=freeze | grep -i -E '^(rocm|rocm[-_]sdk[-_][a-z0-9_-]+|torch|torchvision|triton|pytorch[-_]triton[-_]rocm)==' \
  >"$out/constraints.txt"
grep -q -i '^torch==' "$out/constraints.txt" || {
  echo 'STOP: torch is not installed from the torch stage' >&2
  exit 1
}
checkout() { # repository commit shared-clone run-copy
  [[ -d $3/.git ]] || git clone --quiet "$1" "$3"
  git -C "$3" fetch --quiet origin
  git -C "$3" cat-file -e "$2^{commit}"
  rm -rf -- "$4"
  git clone --quiet --shared --no-checkout "$3" "$4"
  git -C "$4" checkout --quiet --detach "$2"
}
checkout "$COMFYUI_REPOSITORY" "$COMFYUI_COMMIT" /work/src/comfyui "$run/src/ComfyUI"
checkout "$SWARMUI_REPOSITORY" "$SWARMUI_COMMIT" /work/src/swarmui "$run/src/SwarmUI"
python3 -I /pipeline/comfyui/requirements.py "$run/src/ComfyUI/requirements.txt" \
  "$run/src/SwarmUI/src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs" \
  "$out/requirements.in" "$out/swarm-python-checks.json"
# pip itself is one of SwarmUI's checks, so the wheelhouse carries it too.
pip wheel --quiet --wheel-dir "$out/wheelhouse" -c "$out/constraints.txt" "${links[@]}" \
  -r "$out/requirements.in" "rocm[$extras]==$rocm_version" "$torch"/wheels/*.whl
git -C "$run/src/ComfyUI" archive --format=tar HEAD | tar -x -C "$out/ComfyUI"
python3 - "$out" <<'PY'
import hashlib, json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
def sha(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
keys = ["COMFYUI_REPOSITORY", "COMFYUI_COMMIT", "SWARMUI_REPOSITORY", "SWARMUI_COMMIT", "GPU_TARGET", "PYTHON_ABI"]
wheels = sorted((out / "wheelhouse").glob("*.whl"))
if not any(w.name.startswith("pip-") for w in wheels):
    sys.exit("STOP: the wheelhouse has no pip wheel; SwarmUI requires pip in the environment")
receipt = {
    "schema": 1, "stage": "comfyui", **{k.lower(): os.environ[k] for k in keys},
    "rocm_receipt_sha256": sha(pathlib.Path("/work/rocm-out/RECEIPT.json")),
    "torch_receipt_sha256": sha(pathlib.Path("/work/torch-out/RECEIPT.json")),
    "requirements_in_sha256": sha(out / "requirements.in"),
    "swarm_checks_sha256": sha(out / "swarm-python-checks.json"),
    "wheelhouse": [{"file": w.name, "size": w.stat().st_size, "sha256": sha(w)} for w in wheels],
}
(out / "RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(f"COMFYUI_STAGE=built; wheelhouse={len(wheels)}")
PY
