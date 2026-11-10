"""Fail the image build if SwarmUI would run `pip install` on start.

Mirrors ComfyUISelfStartBackend.DoLibFixes for the checks recorded by the
comfyui stage (requirements.py): a required package is present when a
site-packages entry's name before the first '-' matches its folder, and a
versioned package is updated when its installed version fails the relation.
Versions are read as SwarmUI reads them: the part before '+', then before
'.dev', parsed like .NET's Version (2 to 4 numeric parts). A version that does
not parse (for example 2.4.0rc1) counts as missing, so SwarmUI would reinstall.
The image's offline guard turns any attempted runtime pip call into an error,
so a gap here would otherwise surface only when the backend starts.

Usage: python check_swarm_deps.py CHECKS_JSON   (run with the ComfyUI venv's python)
"""

import importlib.metadata
import json
import re
import sys
import sysconfig
from pathlib import Path


def dotnet_version(text):
    """System.Version.Parse after SwarmUI's trimming; None when it would not parse."""
    text = text.split("+", 1)[0].split(".dev", 1)[0]
    if not re.fullmatch(r"\d+(\.\d+){1,3}", text):
        return None
    parts = [int(p) for p in text.split(".")]
    # .NET compares missing build/revision components as -1 (1.2 < 1.2.0).
    return tuple(parts + [-1] * (4 - len(parts)))


def installed_folders(site):
    return {entry.name.split("-", 1)[0].lower() for entry in Path(site).iterdir() if entry.is_dir()}


def version_of(folder):
    # SwarmUI reads the version from "<folder>-<version>.dist-info".
    for dist in importlib.metadata.distributions():
        if dist.metadata["Name"] and dist.metadata["Name"].replace("-", "_").lower() == folder.lower():
            return dotnet_version(dist.version)
    return None


def violations(checks, site):
    folders = installed_folders(site)
    problems = [f"missing {entry['folder']} (SwarmUI would pip install {entry['pip'] or entry['folder']})"
                for entry in checks["plain"] if entry["folder"].lower() not in folders]
    numpy = version_of("numpy")
    if numpy is None or numpy < dotnet_version(checks["numpy_minimum"]):
        problems.append(f"numpy {numpy} is below {checks['numpy_minimum']}")
    for entry in checks["versioned"]:
        current, wanted = version_of(entry["folder"]), dotnet_version(entry["version"])
        relation = entry["relation"]
        stale = current is None or {">=": current < wanted, "<=": current > wanted,
                                    "==": current < wanted, "force-eq": current != wanted}[relation]
        if stale:
            problems.append(f"{entry['pip']} {current} fails SwarmUI's {relation} {wanted}")
    return problems


def main(path):
    checks = json.loads(Path(path).read_text())
    if checks.get("schema") != 1:
        raise SystemExit("unknown SwarmUI checks schema")
    problems = violations(checks, sysconfig.get_paths()["purelib"])
    if problems:
        raise SystemExit("SwarmUI start-up dependency checks would fail:\n  " + "\n  ".join(problems))
    print(f"SWARM_DEPS=ok plain={len(checks['plain'])} versioned={len(checks['versioned'])}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
