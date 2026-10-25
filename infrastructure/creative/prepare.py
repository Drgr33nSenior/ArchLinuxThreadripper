"""Stage reviewed inputs for an offline creative image; never build or deploy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def prepare(swarm, comfy, nuget, wheels, requirements, destination):
    lock = json.loads((ROOT / "infrastructure/creative/runtime.json").read_text())
    for source, name in ((swarm, "swarm"), (comfy, "comfy")):
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        if result.stdout.strip() != lock[name]["commit"]:
            raise ValueError("source commit differs from creative runtime contract")
        if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]).strip():
            raise ValueError("source must be clean; stage NuGet lock separately")
    # Lock is supplied by the owner-reviewed restore closure. It cannot be
    # guessed from a tag, and the build never regenerates it.
    if not (nuget / "packages.lock.json").is_file() or not requirements.is_file():
        raise ValueError("reviewed NuGet and Python locks are required")
    if not all("==" in line and "--hash=sha256:" in line for line in
               requirements.read_text().replace("\\\n", "").splitlines() if line.strip() and not line.startswith("#")):
        raise ValueError("Python closure must contain exact versions and hashes")
    destination.mkdir(mode=0o700, exist_ok=False)
    for source, name in ((swarm, "SwarmUI"), (comfy, "ComfyUI"), (nuget, "nuget"), (wheels, "wheels")):
        shutil.copytree(source, destination / name, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    shutil.copyfile(nuget / "packages.lock.json", destination / "SwarmUI/src/packages.lock.json")
    # Swarm's AutoUpdate=false does NOT disable DoLibFixes. Make attempted pip
    # repair fail explicitly in this offline package instead of changing it.
    backend = destination / "SwarmUI/src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs"
    text = backend.read_text()
    old = 'Process p = DoPythonCall($"-s -m pip {call}");'
    if text.count(old) != 1:
        raise ValueError("upstream offline-installer guard anchor changed")
    text = text.replace(old, 'throw new InvalidOperationException("Offline package needs rebuilt dependencies; runtime pip is prohibited.");\n                Process p = null;')
    backend.write_text(text)
    shutil.copyfile(requirements, destination / "requirements.lock")
    for name in ("Dockerfile", "launch.sh", "runtime.json"):
        shutil.copyfile(ROOT / "infrastructure/creative" / name, destination / name)
    manifest = {}
    for file in sorted(destination.rglob("*")):
        if file.is_symlink():
            raise ValueError("staging symlinks must be replaced by reviewed regular files")
        if file.is_file():
            manifest[str(file.relative_to(destination))] = hashlib.sha256(file.read_bytes()).hexdigest()
    (destination / "staged-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Offline build context staged; build, package closure and GPU acceptance NOT RUN.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("swarm", "comfy", "nuget", "wheels", "requirements", "destination"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    prepare(**vars(args))
