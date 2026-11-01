"""Check the standalone creative environment without requiring a GPU."""
import importlib.metadata
import hashlib
import json
from pathlib import Path
import sys

import torch
import torchvision


def verify(contract):
    expected = json.loads(Path(contract).read_text())["python_runtime"]
    observed = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "torch": torch.__version__,
        "hip": torch.version.hip,
        "rocm": importlib.metadata.version("rocm"),
        "torchvision": importlib.metadata.version("torchvision"),
    }
    if any(observed[name] != expected[name] for name in observed):
        raise RuntimeError(f"creative runtime identity mismatch: {observed}")
    # Import alone does not establish that the matching native extension loads.
    boxes = torch.tensor([[0., 0., 2., 2.], [0., 0., 2., 2.]])
    selected = torchvision.ops.nms(boxes, torch.tensor([1., 0.5]), 0.5)
    if selected.tolist() != [0]:
        raise RuntimeError("torchvision CPU operator check failed")
    libraries = {}
    maps = Path("/proc/self/maps").read_text().splitlines()
    for name, field in (("libamdhip64.so", "hip_library_sha256"), ("librccl.so", "rccl_library_sha256")):
        paths = {Path(line.split(maxsplit=5)[-1]).resolve(strict=True) for line in maps
                 if len(line.split(maxsplit=5)) == 6 and Path(line.split(maxsplit=5)[-1]).name.startswith(name)}
        if len(paths) != 1:
            raise RuntimeError(f"expected one loaded {name}: {paths}")
        path = paths.pop()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected[field]:
            raise RuntimeError(f"unexpected loaded {name}: {path} ({digest})")
        libraries[name] = {"path": str(path), "sha256": digest}
    print(json.dumps({"runtime": observed, "torchvision_cpu_nms": "pass",
                      "libraries": libraries, "gpu_qualification": "NOT RUN"}, sort_keys=True))


if __name__ == "__main__":
    verify(sys.argv[1])
