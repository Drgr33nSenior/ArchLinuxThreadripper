"""Identity of trusted local llama builds; not a loader for untrusted binaries."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


EXECUTABLES = ("llama-cli", "llama-bench", "llama-perplexity", "test-backend-ops")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def loader_environment():
    # Reject even empty overrides: defaults differ between loaders/vendors.
    prefixes = ("LD_", "DYLD_", "GGML_BACKEND_", "VK_", "LLAMA_ARG_")
    forbidden = {"VULKAN_SDK", "HIP_PATH", "ROCM_PATH", "ROCM_HOME"}
    if any(key.startswith(prefixes) or key in forbidden for key in os.environ):
        raise ValueError("unset loader, Vulkan, GGML and llama CLI environment overrides")


def command(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=60).stdout


def dependencies(path):
    # Only called on outputs of the explicitly reviewed local build. ldd must
    # never be used to inspect an arbitrary downloaded/untrusted executable.
    text = command("ldd", str(path))
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("linux-vdso.so."):
            continue
        match = re.fullmatch(r"(?:\S+ => )?(/.+) \(0x[0-9a-fA-F]+\)", line)
        if not match:
            raise ValueError("unresolved or unsupported ldd output")
        paths.append(Path(match[1]))
    if not paths:
        raise ValueError("expected a dynamically linked native build")
    return paths


def file_record(path):
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("runtime entry is not a regular file")
    return {"path": str(path), "resolved": str(resolved),
            "link": os.readlink(path) if path.is_symlink() else None,
            "sha256": digest(resolved)}


def icd_paths():
    # Include the loader's user configuration locations too. XDG overrides
    # otherwise permit a different ICD to escape an identical system inventory.
    directories = [Path("/usr/share/vulkan/icd.d"), Path("/etc/vulkan/icd.d"),
                   Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "vulkan/icd.d",
                   Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "vulkan/icd.d"]
    for value, default in (("XDG_CONFIG_DIRS", "/etc/xdg"), ("XDG_DATA_DIRS", "/usr/local/share:/usr/share")):
        directories.extend(Path(item) / "vulkan/icd.d" for item in os.environ.get(value, default).split(":") if item)
    return sorted({path for directory in directories for path in directory.glob("*.json")})


def collect(candidate):
    loader_environment()
    candidate = Path(candidate).resolve(strict=True)
    build = candidate / "build"
    paths = {build / "bin" / name for name in EXECUTABLES}
    # Include plugins even when not in DT_NEEDED, and every versioned symlink.
    paths.update(path for path in build.rglob("*")
                 if re.search(r"\.so(?:\.[^/]+)*$", path.name))
    if not all(path.is_file() for path in paths):
        raise ValueError("missing executable or dangling runtime symlink; rebuild candidate")
    built = sorted(paths)
    for path in built:
        paths.update(dependencies(path))
    # Vulkan ICDs are dlopened, not necessarily in the executable's ldd closure.
    # Relative names require the host ldconfig cache, not a card-order guess.
    ldconfig = command("ldconfig", "-p")
    for icd in icd_paths():
        paths.add(icd)
        library = json.loads(icd.read_text())["ICD"]["library_path"]
        if library.startswith("/"):
            library = Path(library)
        elif "/" in library:
            library = icd.parent / library
        else:
            matches = re.findall(r"^\s*" + re.escape(library) +
                                 r" \([^\n]*x86-64[^\n]*\) => (/.+)$", ldconfig, re.M)
            if len(set(matches)) != 1:
                raise ValueError("cannot uniquely resolve installed Vulkan ICD")
            library = Path(matches[0])
        paths.add(library)
        paths.update(dependencies(library))
    return {"schema": 1, "built_paths": [str(path) for path in built],
            "files": [file_record(path) for path in sorted(paths)],
            "packages": command("pacman", "-Q").splitlines(),
            "kernel": command("uname", "-r").strip(),
            "amdgpu_module": command("modinfo", "amdgpu"),
            "scope": "built outputs, linked closure, installed Vulkan ICDs and host package/driver identity; not a trace of every dlopen or GPU code object"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument("candidate")
    parser.add_argument("manifest")
    args = parser.parse_args()
    observed = collect(args.candidate)
    path = Path(args.manifest)
    if args.mode == "capture":
        with path.open("x") as stream:
            json.dump(observed, stream, indent=2)
            stream.write("\n")
    else:
        verify(path, observed)


def verify(path, observed):
    if json.loads(path.read_text()) != observed:
        raise ValueError("llama runtime changed; rebuild/requalify in a new directory")


if __name__ == "__main__":
    main()
