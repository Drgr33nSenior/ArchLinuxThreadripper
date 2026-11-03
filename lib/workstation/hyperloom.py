"""Install and inspect an isolated, hash-locked Hyperloom developer environment.

Offline commands never launch workloads. The explicit sglang-owner action
delegates a registered native packet to its existing protected owner.
"""

import argparse
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import types
import urllib.request
import zipfile


DEPENDENCIES = Path("infrastructure/hyperloom/runtime-linux-py314.lock")
TRACELENS_DEPENDENCIES = Path("infrastructure/hyperloom/tracelens-linux-py314.lock")
CONTRACTS = {
    "HYPERLOOM": ({"HYPERLOOM_VERSION", "HYPERLOOM_COMMIT", "HYPERLOOM_WHEEL_URL",
                   "HYPERLOOM_WHEEL_SHA256", "HYPERLOOM_RUNTIME_LOCK_SHA256"}, DEPENDENCIES),
    "TRACELENS": ({"TRACELENS_COMMIT", "TRACELENS_ARCHIVE_URL", "TRACELENS_ARCHIVE_SHA256",
                   "TRACELENS_RUNTIME_LOCK_SHA256"}, TRACELENS_DEPENDENCIES),
}
SITE_PACKAGES = Path("venv/lib/python3.14/site-packages")
TRACELENS_PATH_FILE = "workstation-tracelens.pth"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_contract(root, family="HYPERLOOM"):
    keys, lock = CONTRACTS[family]
    values = {}
    for line in (root / "infrastructure/hyperloom/versions.lock").read_text().splitlines():
        if line.startswith(family + "_"):
            key, value = line.split("=", 1)
            require(key not in values, "duplicate " + family + " identity")
            values[key] = value
    require(set(values) == keys, "incomplete " + family + " identity")
    require(digest(root / lock) == values[family + "_RUNTIME_LOCK_SHA256"], "runtime lock changed")
    return values


def report_writer(wheel, contract):
    """Load only the pure report writer from the authenticated release wheel."""
    from hyperloom_sglang import checked_bytes
    raw = checked_bytes(wheel, contract["HYPERLOOM_WHEEL_SHA256"], 8 * 1024 * 1024)
    name = "hyperloom/orchestrator/actions/executors/bypass_report.py"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist().count(name) == 1 and archive.getinfo(name).file_size <= 128 * 1024,
                "bounded unique pure report module required")
        source = archive.read(name)
    module = types.ModuleType("_workstation_hyperloom_owner_report")
    module.__file__ = str(wheel) + "/" + name
    # dataclasses resolves the module through sys.modules during construction.
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def requirements(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        row = re.fullmatch(r"([a-z0-9-]+)(\[runtime\])?==([A-Za-z0-9.+!_-]+) --hash=sha256:([a-f0-9]{64})", line)
        require(row is not None and row[1] not in result, "invalid or duplicate locked requirement")
        result[row[1]] = (row[3], row[4])
    require(result and "hyperloom-inference-optimizer" in result, "missing Hyperloom requirement")
    return result


def verify_wheels(wheelhouse, pins):
    require(wheelhouse.is_dir() and not wheelhouse.is_symlink(), "wheelhouse must be a real directory")
    paths = list(wheelhouse.iterdir())
    require(len(paths) == len(pins), "wheelhouse must contain exactly the locked wheels")
    total, found = 0, set()
    for path in paths:
        require(path.is_file() and not path.is_symlink() and path.suffix == ".whl", "unsafe wheelhouse member")
        total += path.stat().st_size
        require(total <= 512 * 1024 * 1024, "wheelhouse exceeds 512 MiB")
        with zipfile.ZipFile(path) as wheel:
            names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            require(len(names) == 1 and wheel.getinfo(names[0]).file_size <= 2 * 1024 * 1024, "invalid wheel metadata")
            metadata = BytesParser().parsebytes(wheel.read(names[0]))
        name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
        require(name in pins and name not in found, "unlocked or duplicate wheel")
        require((metadata.get("Version"), digest(path)) == pins[name], "wheel version or hash changed")
        found.add(name)
    require(found == set(pins), "incomplete wheelhouse")
    return total


def private_environment(prefix):
    # No inherited provider credentials, proxy configuration or user agent files.
    home = prefix / "private-home"
    home.mkdir(mode=0o700, exist_ok=True)
    require(not home.is_symlink() and home.stat().st_uid == os.geteuid() and not home.stat().st_mode & 0o077,
            "private tool home must remain caller-owned and private")
    return {"HOME": str(home), "PATH": os.defpath, "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "HYPERLOOM_REPORT_LLM_BACKEND": "none"}


def installed_python(prefix, contract):
    record = prefix / "INSTALLATION.json"
    require(record.is_file() and not record.is_symlink(), "missing installation receipt")
    receipt = json.loads(record.read_text())
    require(receipt.get("status") == "installed" and receipt.get("contract") == contract,
            "installation incomplete or belongs to a different lock")
    release = prefix / "release.whl"
    require(release.is_file() and not release.is_symlink() and digest(release) == contract["HYPERLOOM_WHEEL_SHA256"],
            "installed release identity changed")
    with zipfile.ZipFile(release) as wheel:
        for name in wheel.namelist():
            if name.startswith(("hyperloom/", "kernelforge/")) and not name.endswith("/"):
                path = prefix / "venv/lib/python3.14/site-packages" / name
                require(path.is_file() and not path.is_symlink()
                        and digest(path) == hashlib.sha256(wheel.read(name)).hexdigest(),
                        "installed Hyperloom source or data changed")
    executable = prefix / "venv/bin/python3"
    require(executable.is_file(), "installed Python missing")
    return executable


def unpack_tracelens(archive, prefix, contract):
    """Expose the authenticated source tree as upstream's editable TRACELENS_ROOT checkout.

    A path entry replaces ``pip install -e``: setup.py, its build backend and
    the unused SharePoint/plotting requirements are never executed or installed.
    """
    retained = prefix / "tracelens.tar.gz"
    shutil.copyfile(archive, retained)
    require(digest(retained) == contract["TRACELENS_ARCHIVE_SHA256"], "TraceLens archive changed")
    top = "TraceLens-" + contract["TRACELENS_COMMIT"]
    staging = prefix / "tracelens-staging"
    staging.mkdir(mode=0o700)
    with tarfile.open(retained, "r:gz") as source:
        members = source.getmembers()
        require(len(members) <= 4096 and sum(member.size for member in members) <= 512 * 1024 * 1024,
                "TraceLens archive exceeds its bounds")
        require(all(member.name == top or member.name.startswith(top + "/") for member in members),
                "TraceLens archive must contain one commit directory")
        source.extractall(staging, filter="data")
    root = prefix / "TraceLens"
    (staging / top).rename(root)
    staging.rmdir()
    (prefix / SITE_PACKAGES / TRACELENS_PATH_FILE).write_text(str(root) + "\n")
    # Read-only directories keep bytecode and planted modules out of the checked tree.
    for directory, _, _ in os.walk(root, topdown=False):
        os.chmod(directory, 0o500)
    return root


def tracelens_root(prefix, contract):
    """Return TRACELENS_ROOT after checking every package file against the retained archive."""
    archive = prefix / "tracelens.tar.gz"
    require(archive.is_file() and not archive.is_symlink() and digest(archive) == contract["TRACELENS_ARCHIVE_SHA256"],
            "installed TraceLens archive identity changed")
    root = prefix / "TraceLens"
    path_file = prefix / SITE_PACKAGES / TRACELENS_PATH_FILE
    require(path_file.is_file() and not path_file.is_symlink() and path_file.read_text() == str(root) + "\n",
            "TraceLens import path changed")
    top = "TraceLens-" + contract["TRACELENS_COMMIT"] + "/"
    expected = {}
    with tarfile.open(archive, "r|gz") as source:
        for member in source:
            if member.name.startswith(top + "TraceLens/") and member.isfile():
                expected[member.name[len(top):]] = hashlib.file_digest(source.extractfile(member), "sha256").hexdigest()
    package = root / "TraceLens"
    require(package.is_dir() and not package.is_symlink(), "TraceLens package missing")
    found = {path.relative_to(root).as_posix() for path in package.rglob("*") if not path.is_dir() or path.is_symlink()}
    require(found == set(expected), "TraceLens package files added or removed")
    for name, sha in expected.items():
        path = root / name
        require(path.is_file() and not path.is_symlink() and digest(path) == sha, "installed TraceLens source changed")
    return root


# Offline tools are deliberately denied network access, process spawning and GPU
# device opens. Hyperloom's provider clients are not invoked by these commands.
OFFLINE = r'''
import json, pathlib, runpy, sys
# Upstream trace provenance probes (git, rocminfo) catch OSError and degrade.
refused = PermissionError if sys.argv[1:2] == ["trace-analysis"] else RuntimeError
def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise refused("offline Hyperloom command refused external execution/network")
    if event == "open" and isinstance(args[0], str) and (args[0] == "/dev/kfd" or args[0].startswith("/dev/dri/")):
        raise refused("offline Hyperloom command refused GPU access")
sys.addaudithook(guard)
if sys.argv[1] == "status":
    from importlib.metadata import version
    from hyperloom.inference_optimizer.gpu_types import gpu_runner_type, _runner_framework_error, targeted_build_validated
    from hyperloom.common.gpu_identity import AMD_GPU_DISPATCH_IDENTITIES
    print(json.dumps({"version":version("hyperloom-inference-optimizer"),
        "r9700_identity":AMD_GPU_DISPATCH_IDENTITIES["r9700"],
        "runner":gpu_runner_type("r9700"),
        "framework_gates":{f:_runner_framework_error("r9700",f) for f in ("sglang","vllm","custom")},
        "targeted_build_validated":targeted_build_validated("r9700"),
        "gpu_execution":"NOT RUN", "provider_calls":"DISABLED"}))
elif sys.argv[1] in {"sglang-import", "sglang-bind"}:
    # executors.__init__ constructs a baseline executor and probes Magpie in a
    # subprocess. Load this already wheel-verified, pure report module directly.
    import importlib.util
    package = importlib.util.find_spec("hyperloom")
    report_path = pathlib.Path(package.origin).parent / "orchestrator/actions/executors/bypass_report.py"
    spec = importlib.util.spec_from_file_location("_workstation_hyperloom_report", report_path)
    bypass_report = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bypass_report
    spec.loader.exec_module(bypass_report)
    adapter, profile = sys.argv[2:4]
    bridge = runpy.run_path(adapter)
    selected = json.loads(pathlib.Path(profile).read_text())
    if sys.argv[1] == "sglang-bind":
        owner, seal_sha, arm, output, quality, quality_sha = sys.argv[4:]
        result = bridge["export_bound"](selected, owner, seal_sha, arm, output, bypass_report,
                                        quality_path=quality or None, quality_sha=quality_sha or None)
    else:
        proof, proof_sha, summary, summary_sha, output = sys.argv[4:]
        result = bridge["export"](selected, proof, proof_sha, summary, summary_sha, output, bypass_report)
    print(json.dumps({"status":result["status"], "groups":len(result["groups"]),
                      "output":output, "automatic_optimization":False}))
elif sys.argv[1] == "trace-analysis":
    import functools, os
    paths = json.loads(os.environ.get("WORKSTATION_TRACELENS_SOURCE_PATHS", "[]"))
    if paths:
        # The pinned bypass CLI has no source-path argument. Supply TraceLens's
        # public search_paths parameter in this process, before its caller imports it.
        from TraceLens.TraceUtils import kernel_source
        kernel_source.resolve_kernel_source = functools.partial(kernel_source.resolve_kernel_source, search_paths=paths)
    sys.argv = ["bypass_trace_analysis", *sys.argv[2:]]
    runpy.run_module("hyperloom.orchestrator.trace_analysis.bypass_trace_analysis", run_name="__main__")
else:
    sys.argv = ["hyperloom", *sys.argv[1:]]
    runpy.run_module("hyperloom", run_name="__main__")
'''


def install(root, wheelhouse, prefix, contract, lock=DEPENDENCIES, archive=None):
    require(os.geteuid() != 0, "install Hyperloom as the ordinary developer user")
    require(sys.platform == "linux" and platform.machine() == "x86_64" and sys.version_info[:2] == (3, 14),
            "runtime lock requires Linux x86_64 CPython 3.14")
    pins = requirements(root / lock)
    require(pins["hyperloom-inference-optimizer"] == (contract["HYPERLOOM_VERSION"], contract["HYPERLOOM_WHEEL_SHA256"]),
            "release and dependency identities disagree")
    total = verify_wheels(wheelhouse, pins)
    if archive is not None:
        require(archive.is_file() and not archive.is_symlink() and digest(archive) == contract["TRACELENS_ARCHIVE_SHA256"],
                "TraceLens archive missing or changed")
    require(prefix.is_absolute() and not prefix.exists() and not prefix.is_symlink(), "choose a fresh absolute installation prefix")
    require(prefix.parent.is_dir() and prefix.parent.stat().st_uid == os.geteuid() and not prefix.parent.stat().st_mode & 0o022,
            "installation parent must be caller-owned and not writable by others")
    prefix.mkdir(mode=0o700)
    receipt = {"status": "installing", "contract": contract, "wheel_bytes": total,
               "python": sys.version.split()[0], "optimization_qualified": False}
    record = prefix / "INSTALLATION.json"
    record.write_text(json.dumps(receipt, indent=2) + "\n")
    env = private_environment(prefix)
    try:
        subprocess.run([sys.executable, "-I", "-m", "venv", str(prefix / "venv")], check=True, timeout=60, env=env)
        python = prefix / "venv/bin/python3"
        subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir",
                        "--only-binary=:all:", "--require-hashes", "--no-compile", "--find-links", str(wheelhouse),
                        "-r", str(root / lock)], check=True, timeout=180, env=env)
        subprocess.run([str(python), "-I", "-m", "pip", "--isolated", "check"], check=True, timeout=30, env=env)
        release = next(p for p in wheelhouse.iterdir() if digest(p) == contract["HYPERLOOM_WHEEL_SHA256"])
        shutil.copyfile(release, prefix / "release.whl")
        if archive is not None:
            receipt["tracelens_root"] = str(unpack_tracelens(archive, prefix, contract))
        receipt["status"] = "installed"
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        receipt["status"] = "failed"
        raise
    finally:
        record.write_text(json.dumps(receipt, indent=2) + "\n")
    return {"installed": str(prefix), "version": contract["HYPERLOOM_VERSION"], "optimization_qualified": False}


def fetch_archive(contract, destination):
    """Download the commit-addressed TraceLens archive over HTTPS and require its pinned SHA256."""
    url = contract["TRACELENS_ARCHIVE_URL"]
    require(url == "https://codeload.github.com/AMD-AGI/TraceLens/tar.gz/" + contract["TRACELENS_COMMIT"],
            "unexpected TraceLens origin")
    destination.parent.mkdir(mode=0o700)
    hasher, total = hashlib.sha256(), 0
    # No inherited proxy configuration, as for the private pip environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response, destination.open("xb") as stream:
        require(response.geturl() == url, "TraceLens archive redirected")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            require(total <= 400 * 1024 * 1024, "TraceLens archive exceeds 400 MiB")
            hasher.update(chunk)
            stream.write(chunk)
    require(hasher.hexdigest() == contract["TRACELENS_ARCHIVE_SHA256"], "TraceLens archive hash changed")
    return total


def fetch(root, output, contract, lock=DEPENDENCIES, archive=False):
    require(os.geteuid() != 0, "fetch Hyperloom as the ordinary developer user")
    require(sys.platform == "linux" and platform.machine() == "x86_64" and sys.version_info[:2] == (3, 14),
            "runtime lock requires Linux x86_64 CPython 3.14")
    require(output.is_absolute() and not output.exists() and not output.is_symlink(), "choose a fresh absolute fetch directory")
    output.mkdir(mode=0o700)
    env = private_environment(output)
    bootstrap = output / "bootstrap-env"
    subprocess.run([sys.executable, "-I", "-m", "venv", str(bootstrap)], check=True, timeout=60, env=env)
    # pip's normal direct-reference/hash interface selects the authenticated
    # upstream asset while every transitive dependency remains exactly pinned.
    text = (root / lock).read_text()
    original = re.compile(r"^(hyperloom-inference-optimizer(?:\[runtime\])?)==" + re.escape(contract["HYPERLOOM_VERSION"]) + " ",
                          re.MULTILINE)
    require(len(original.findall(text)) == 1, "release requirement changed")
    require(contract["HYPERLOOM_WHEEL_URL"].startswith("https://github.com/AMD-AGI/Hyperloom/releases/download/"),
            "unexpected release origin")
    download_lock = output / "download.lock"
    download_lock.write_text(original.sub(lambda row: row[1] + " @ " + contract["HYPERLOOM_WHEEL_URL"] + " ", text))
    wheels = output / "wheelhouse"
    wheels.mkdir(mode=0o700)
    subprocess.run([str(bootstrap / "bin/python3"), "-I", "-m", "pip", "--isolated", "download",
                    "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--no-cache-dir",
                    "--require-hashes", "--dest", str(wheels), "-r", str(download_lock)],
                   check=True, timeout=180, env=env)
    total = verify_wheels(wheels, requirements(root / lock))
    result = {"wheelhouse": str(wheels), "bytes": total, "version": contract["HYPERLOOM_VERSION"]}
    if archive:
        source = output / "source" / ("TraceLens-" + contract["TRACELENS_COMMIT"] + ".tar.gz")
        result.update(archive=str(source), archive_bytes=fetch_archive(contract, source))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    evidence = actions.add_parser("sglang-evidence", help="prepare authenticated research and historical evidence for the optimizer")
    evidence.add_argument("manifest", type=Path)
    evidence.add_argument("output", type=Path)
    evidence.add_argument("--manifest-sha256", required=True)
    evidence.add_argument("--runtime-root", type=Path, help="absolute bundle path on the optimizer host")
    download = actions.add_parser("fetch", help="fetch the exact binary runtime into a new private directory")
    download.add_argument("output", type=Path)
    setup = actions.add_parser("install", help="offline installation from the exact locked wheelhouse")
    setup.add_argument("wheelhouse", type=Path)
    setup.add_argument("prefix", type=Path)
    for name in ("status", "help", "report"):
        command = actions.add_parser(name)
        command.add_argument("prefix", type=Path)
        if name == "report":
            command.add_argument("input", type=Path)
            command.add_argument("output", type=Path)
    native = actions.add_parser("sglang-import", help="offline ROCm 10.2 native timing import; never launches or qualifies")
    native.add_argument("prefix", type=Path)
    native.add_argument("stack_proof", type=Path)
    native.add_argument("summary", type=Path)
    native.add_argument("output", type=Path)
    native.add_argument("--stack-sha256", required=True)
    native.add_argument("--summary-sha256", required=True)
    bound = actions.add_parser("sglang-bind", help="offline binding of one sealed native owner run; never launches or qualifies")
    bound.add_argument("prefix", type=Path)
    bound.add_argument("owner", type=Path)
    bound.add_argument("output", type=Path)
    bound.add_argument("--seal-sha256", required=True)
    bound.add_argument("--arm", choices=("bf16", "kv4"), required=True)
    bound.add_argument("--quality", type=Path, help="reviewed retained native CPU quality join")
    bound.add_argument("--quality-sha256", help="SHA256 of that reviewed quality join")
    tracelens_fetch = actions.add_parser("tracelens-fetch", help="fetch pinned TraceLens source and exact binary wheels")
    tracelens_fetch.add_argument("output", type=Path)
    tracelens_setup = actions.add_parser("tracelens-install", help="offline trace-analysis installation from the exact fetch")
    tracelens_setup.add_argument("wheelhouse", type=Path)
    tracelens_setup.add_argument("archive", type=Path)
    tracelens_setup.add_argument("prefix", type=Path)
    trace = actions.add_parser("trace-analysis", help="offline bypass analysis of one Kineto trace; never launches or qualifies")
    trace.add_argument("prefix", type=Path)
    trace.add_argument("trace", type=Path)
    trace.add_argument("output", type=Path)
    trace.add_argument("--source-path", type=Path, action="append", default=[],
                       help="kernel source directory for TraceLens; repeat for multiple roots")
    owner = actions.add_parser("sglang-owner", help="check a fresh native owner packet; launch only with --execute-owner")
    owner.add_argument("session", type=Path)
    owner.add_argument("--authorization-sha256", required=True)
    owner.add_argument("--selection", type=Path, required=True)
    owner.add_argument("--boot-id", required=True)
    owner.add_argument("--execute-owner", action="store_true")
    owner.add_argument("--output", type=Path)
    owner.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        contract = read_contract(args.root)
        if args.action == "sglang-evidence":
            from hyperloom_evidence import bundle
            result = bundle(args.manifest, args.manifest_sha256, args.output, runtime_root=args.runtime_root)
            print(json.dumps(result, indent=2))
            return 0
        if args.action in {"tracelens-fetch", "tracelens-install", "trace-analysis"}:
            contract = {**contract, **read_contract(args.root, "TRACELENS")}
        if args.action == "sglang-owner":
            from hyperloom_owner import controller_runtime_ready, prepare, run
            plan = prepare(args.session, args.authorization_sha256, args.selection, args.boot_id)
            if not args.execute_owner:
                print(json.dumps({"status": "CONTROLLER_HANDOFF_CHECKED", "plan": plan,
                                  "controller_runtime_ready": controller_runtime_ready(),
                                  "gpu_execution": "NOT RUN"}, indent=2))
                return 0
            require(args.output is not None and args.wheel is not None,
                    "execution requires a fresh report output and authenticated release wheel")
            profile = json.loads((args.root / "infrastructure/hyperloom/sglang-rocm102.json").read_text())
            result = run(plan, args.output, profile, report_writer(args.wheel, contract))
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "OWNER_COMPLETED_REPORTS_IMPORTED" else 1
        if args.action == "fetch":
            print(json.dumps(fetch(args.root.resolve(), args.output, contract), indent=2))
            return 0
        if args.action == "install":
            print(json.dumps(install(args.root.resolve(), args.wheelhouse.absolute(), args.prefix, contract), indent=2))
            return 0
        if args.action == "tracelens-fetch":
            print(json.dumps(fetch(args.root.resolve(), args.output, contract, TRACELENS_DEPENDENCIES, archive=True), indent=2))
            return 0
        if args.action == "tracelens-install":
            print(json.dumps(install(args.root.resolve(), args.wheelhouse.absolute(), args.prefix, contract,
                                     TRACELENS_DEPENDENCIES, args.archive.absolute()), indent=2))
            return 0
        python = installed_python(args.prefix, contract)
        forwarded = ["status"] if args.action == "status" else ["--help"]
        env, timeout = (None if args.action == "trace-analysis" else private_environment(args.prefix)), 60
        if args.action == "trace-analysis":
            source = tracelens_root(args.prefix, contract)
            require(args.trace.is_file() and not args.trace.is_symlink() and args.trace.stat().st_size <= 4 * 1024 ** 3,
                    "trace input missing, linked or too large")
            output = args.output
            require(output.is_absolute() and not output.exists() and not output.is_symlink(), "choose a fresh absolute analysis output")
            require(all(path.is_dir() for path in args.source_path), "source paths must be existing directories")
            source_paths = list(dict.fromkeys(str(path.resolve()) for path in args.source_path))
            output.mkdir(mode=0o700)
            env = private_environment(output)
            for private in ("cache", "tmp"):
                (output / private).mkdir(mode=0o700)
            env.update(TRACELENS_ROOT=str(source), TRACELENS_KSI_CACHE_DIR=str(output / "cache"),
                       TMPDIR=str(output / "tmp"), HYPERLOOM_ROCPROF_ROOFLINE_ENRICH="0",
                       WORKSTATION_TRACELENS_SOURCE_PATHS=json.dumps(source_paths))
            (output / "SOURCE-PATHS.json").write_text(json.dumps({"source_paths": source_paths}, indent=2) + "\n")
            forwarded = ["trace-analysis", "--trace-input", str(args.trace.resolve()), "--workspace-path", str(output),
                         "--session-id", "offline", "--framework", "sglang", "--target-platform", "r9700"]
            timeout = 1800
        elif args.action == "report":
            require(args.input.is_file() and args.input.stat().st_size <= 8 * 1024 * 1024, "report input missing or too large")
            require(not args.output.exists() and not args.output.is_symlink(), "choose a fresh report output")
            content = json.loads(args.input.read_text())
            metadata = content.get("metadata") if isinstance(content, dict) else None
            session = metadata.get("session") if isinstance(metadata, dict) else None
            require(isinstance(session, dict) and isinstance(session.get("session_id"), str) and session["session_id"],
                    "report requires a Hyperloom metadata.session.session_id")
            forwarded = ["session", "report", "--no-llm", "--input", str(args.input.resolve()), "--output", str(args.output.absolute())]
        elif args.action == "sglang-import":
            forwarded = ["sglang-import", str(args.root.resolve() / "lib/workstation/hyperloom_sglang.py"),
                         str(args.root.resolve() / "infrastructure/hyperloom/sglang-rocm102.json"),
                         str(args.stack_proof.absolute()), args.stack_sha256,
                         str(args.summary.absolute()), args.summary_sha256, str(args.output.absolute())]
        elif args.action == "sglang-bind":
            require((args.quality is None) == (args.quality_sha256 is None),
                    "quality file and reviewed SHA256 required together")
            forwarded = ["sglang-bind", str(args.root.resolve() / "lib/workstation/hyperloom_sglang.py"),
                         str(args.root.resolve() / "infrastructure/hyperloom/sglang-rocm102.json"),
                         str(args.owner.absolute()), args.seal_sha256, args.arm, str(args.output.absolute()),
                         str(args.quality.absolute()) if args.quality else "", args.quality_sha256 or ""]
        subprocess.run([str(python), "-I", "-B", "-c", OFFLINE, *forwarded], check=True, timeout=timeout, env=env)
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError, subprocess.SubprocessError) as error:
        if args.action == "sglang-owner":
            print("FAILED: Hyperloom owner handoff: " + str(error), file=sys.stderr)
        else:
            print("FAILED: Hyperloom identity, platform, installation or offline command; retained prefix is not deleted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
