"""Derive the ComfyUI environment's requirements from the pinned sources.

SwarmUI validates its self-started ComfyUI environment on every start and runs
`pip install` for anything missing (ComfyUISelfStartBackend.DoLibFixes). The
image must already satisfy those checks, so this reads both lists from the
pinned SwarmUI source instead of copying them by hand:

  RequiredPythonPackages         (site-packages folder, pip name)
  RequiredVersionPythonPackages  (folder, pip name, relation, version)

plus ComfyUI's own requirements.txt. torch, torchvision and torchaudio are
dropped: the pipeline's torch stage supplies torch and torchvision, and the
ROCm build has no torchaudio.

Usage: requirements.py COMFY_REQUIREMENTS SWARM_SELFSTART_CS REQUIREMENTS_OUT CHECKS_OUT
"""

import json
import re
import sys
from pathlib import Path

PIPELINE_SUPPLIED = {"torch", "torchvision", "torchaudio"}
# Swarm's relations; "force-eq" reinstalls unless the version is exactly equal.
RELATIONS = {">=": ">=", "<=": "<=", "==": "==", "force-eq": "=="}


def name_of(requirement):
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    if not match:
        raise ValueError(f"unparsable requirement: {requirement!r}")
    return re.sub(r"[-_.]+", "-", match[1]).lower()


def comfy_requirements(text):
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and name_of(line) not in PIPELINE_SUPPLIED:
            lines.append(line)
    return lines


def block(source, name):
    match = re.search(rf"\b{name}\s*=\s*\[(.*?)\];", source, re.S)
    if not match:
        raise ValueError(f"SwarmUI source no longer defines {name}")
    # Drop // comments so commented-out entries are not required.
    return "\n".join(line.split("//", 1)[0] for line in match[1].splitlines())


def swarm_requirements(source):
    plain, versioned = [], []
    for folder, pip_name in re.findall(r'\(\s*"([^"]+)"\s*,\s*(\$?"[^"]+")\s*\)', block(source, "RequiredPythonPackages")):
        # An interpolated pip name (the validated frontend) is only installed when
        # its folder is missing; ComfyUI's requirements already pin that package.
        plain.append({"folder": folder, "pip": None if pip_name.startswith("$") else pip_name.strip('"')})
    for folder, pip_name, relation, version in re.findall(
            r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)',
            block(source, "RequiredVersionPythonPackages")):
        if relation not in RELATIONS:
            raise ValueError(f"unknown SwarmUI version relation {relation!r} for {pip_name}")
        versioned.append({"folder": folder, "pip": pip_name, "relation": relation, "version": version})
    if not plain or not versioned:
        raise ValueError("SwarmUI package lists are empty; the source layout changed")
    return plain, versioned


def main(comfy_path, swarm_path, requirements_out, checks_out):
    lines = comfy_requirements(Path(comfy_path).read_text())
    plain, versioned = swarm_requirements(Path(swarm_path).read_text())
    lines += [entry["pip"] for entry in plain if entry["pip"]]
    lines += [f'{entry["pip"]}{RELATIONS[entry["relation"]]}{entry["version"]}' for entry in versioned]
    # SwarmUI updates numpy below 1.25 separately.
    lines.append("numpy>=1.25")
    Path(requirements_out).write_text("".join(f"{line}\n" for line in sorted(set(lines), key=str.lower)))
    Path(checks_out).write_text(json.dumps({"schema": 1, "plain": plain, "versioned": versioned,
                                            "numpy_minimum": "1.25"}, indent=2) + "\n")
    print(f"REQUIREMENTS={len(set(lines))} SWARM_PLAIN={len(plain)} SWARM_VERSIONED={len(versioned)}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
