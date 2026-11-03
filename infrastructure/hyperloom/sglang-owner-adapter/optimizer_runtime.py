"""Finite, device-free Linux runtime for the owner-backed Hyperloom client.

The Mac controller owns GPU operations independently. This process can write
requests and candidate source, read replies, and use the dedicated Codex login.
It cannot reach GPU devices, SSH keys, the controller, or serving runtimes.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import resource
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time


PREFIX = Path("/home/developer/.local/share/workstation/hyperloom/1.1.5")
SITE = PREFIX / "venv/lib/python3.14/site-packages"
CODEX = SITE / "codex_cli_bin/bin/codex"
CODEX_SHA = "399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215"
CODE_HOST_SHA = "8724d73ec190536233ab4628d24a071bfbcf27e5124f08fe35129f26fe48b916"
WHEEL_SHA = "b6e155758652f5c9e32fdfd4a057a58398988080e89a8240f95e12fb3105ebc2"
AUTH = Path("/home/developer/.local/share/workstation/hyperloom-chatgpt-20261102-v1/auth")
BOOT = "ff396f8f-e5c3-4ae5-86c1-b986bc483185"
MODEL = "/home/developer/model-serving.izR2m1O9/models/Qwen3.8-27B-MXFP4-acquisition.c1eb6630b6849d21/verified"
MODEL_CONFIG_SHA = "4a139d3e01df039e17b4c8b8362f7944674d76051a8cd96f213c6bc332d69f55"
TOKENIZER_CONFIG_SHA = "b11349aafa7cdc6a320767cf7ceb29ed82f7eda5d65e8e0819e76f0ce947bf27"
QUALIFICATION_CANDIDATE_SHA = "e945ad72296777556ed93859263442c077e0f1d1d7082268c1f669e157a324e9"
MODES = {"qualification", "optimizer"}
CLOSEOUT_SECONDS = 300
OUTPUT_FILE_CAP = 256 << 20
WRITABLE = ("requests", "candidate", "work", "results")
ENV_KEYS = {
    "USER_DATA_PATH", "HYPERLOOM_CUSTOM_SPECIALIST_ONLY",
    "INFERENCE_OPTIMIZER_RUN_LOG", "HYPERLOOM_RESEARCH_BUNDLE",
    "HYPERLOOM_CODEX_SANDBOX_MODE", "HYPERLOOM_CODEX_EXECUTABLE",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def checked(path, pin, maximum=4 << 20):
    path = Path(path)
    info = path.lstat()
    require(path.resolve() == path and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_size <= maximum, "canonical bounded regular input required")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == pin, "runtime input identity changed")
    return raw


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def publish(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pending-', delete=False) as output:
        pending = Path(output.name)
        output.write(canonical(value))
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(pending, path)
    finally:
        pending.unlink()


def budget(settings, *, now=None):
    now = time.time() if now is None else now
    start, end = settings["started_unix_ms"], settings["deadline_unix_ms"]
    require(type(start) is int and type(end) is int and 0 < end - start <= 14_400_000,
            "one campaign deadline of at most four hours required")
    require(start / 1000 <= now + 5, "campaign start is in the future")
    # Leave shutdown/reap inside the global deadline, never add it afterwards.
    seconds = int(end / 1000 - now - 15)
    require(seconds > 0, "campaign deadline has no remaining runtime budget")
    return seconds


def optimizer_argv(settings, *, now=None):
    """Give Hyperloom the remaining campaign time, including a CLOSE reserve."""
    args = list(settings["argv"])
    positions = [i for i, value in enumerate(args) if value == "--max-hours" or value.startswith("--max-hours=")]
    require(len(positions) == 1, "one explicit optimizer hours budget required")
    index = positions[0]
    require(args[index] == "--max-hours" and index + 1 < len(args), "use --max-hours followed by hours")
    hours = float(args[index + 1])
    require(math.isfinite(hours) and 0 < hours <= 4, "bounded finite optimizer hours required")
    remaining = budget(settings, now=now) - CLOSEOUT_SECONDS
    require(remaining > 0, "campaign has no optimizer time after its closeout reserve")
    # Round down so parsing the CLI value cannot extend the shared deadline.
    effective = math.floor(min(hours, remaining / 3600) * 1_000_000) / 1_000_000
    require(effective > 0, "optimizer budget exhausted")
    args[index + 1] = str(effective)
    return args


def validate(config, pin, *, native=True):
    config = Path(config)
    settings = json.loads(checked(config, pin))
    require(settings.get("schema") == 1 and settings.get("mode") in MODES, "runtime mode/schema")
    root = Path(settings["root"])
    require(root.is_absolute() and root.resolve() == root and root.is_dir()
            and config.parent == root / "inputs", "canonical private campaign root required")
    require(stat.S_IMODE(root.stat().st_mode) == 0o700 and root.stat().st_uid == os.getuid(),
            "private user-owned campaign root required")
    if native:
        require(os.getuid() == os.geteuid() == 1000 and socket.gethostname() == "threadripper-ai",
                "native workstation identity changed")
        require(Path("/proc/sys/kernel/random/boot_id").read_text().strip() == BOOT, "native boot changed")
        require(digest(PREFIX / "release.whl") == WHEEL_SHA and digest(CODEX) == CODEX_SHA
                and digest(CODEX.parent / "codex-code-mode-host") == CODE_HOST_SHA,
                "installed optimizer or Codex identity changed")
    for name in (*WRITABLE, "replies", "sdk", "evidence"):
        directory = root / name
        require(directory.is_dir() and directory.resolve() == directory, "runtime directory missing or linked")
    pins = settings.get("source_pins")
    require(isinstance(pins, dict) and 1 <= len(pins) <= 4096, "closed runtime source inventory required")
    require("optimizer_runtime.py" in pins, "runtime helper must be pinned")
    total = 0
    for name, source_pin in pins.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and ".." not in relative.parts
                and relative.parts[0] not in (*WRITABLE, "replies"), "invalid immutable runtime member")
        total += len(checked(root / name, source_pin, 32 << 20))
    require(total <= 128 << 20, "runtime source inventory exceeds 128 MiB")
    members = {p.relative_to(root).as_posix() for p in (root / "sdk").rglob("*") if p.is_file()}
    require(members == {name for name in pins if name.startswith("sdk/")},
            "optimizer SDK inventory is not closed")
    bound = settings["binding"]
    binding_path = Path(bound["path"])
    require(binding_path.parent == root / "inputs", "binding outside read-only runtime inputs")
    binding = json.loads(checked(binding_path, bound["sha256"]))
    require(binding["benchmark"]["model"] == MODEL, "selected model path changed")
    require(pins.get("inputs/model-config.json") == MODEL_CONFIG_SHA, "selected model metadata pin required")
    require(pins.get("inputs/tokenizer-config.json") == TOKENIZER_CONFIG_SHA,
            "selected tokenizer metadata pin required")
    require(binding["campaign_deadline_unix_ms"] == settings["deadline_unix_ms"], "binding deadline differs")
    require(binding["requests_root"] == str(root / "requests")
            and binding["replies_root"] == str(root / "replies")
            and binding["sources"] == {"extend_attention_fwd_candidate.py":
                                     str(root / "candidate/extend_attention_fwd_candidate.py")},
            "runtime exchange or source mapping differs")
    require(settings.get("environment", {}).keys() <= ENV_KEYS, "unexpected runtime environment override")
    require(settings.get("automatic_retry") is False and settings.get("adoption_authorized") is False,
            "runtime cannot retry or adopt")
    if settings["mode"] == "qualification":
        require(pins.get("inputs/qualification-candidate.py") == QUALIFICATION_CANDIDATE_SHA,
                "reviewed component qualification candidate required")
        completed=settings.get('completed_component')
        if completed is not None:
            require(completed==dict(path=str(root/'inputs/completed-component.json'),
                                    sha256=pins.get('inputs/completed-component.json')),
                    'completed component must be a pinned read-only input')
    if settings["mode"] == "optimizer":
        require(settings.get("model") == "gpt-6.1-sol", "explicit authenticated model required")
        args = settings.get("argv")
        require(isinstance(args, list) and args and args[0] == "optimize" and len(args) <= 100
                and all(isinstance(v, str) and len(v) < 16384 for v in args), "bounded optimizer arguments required")
        require("--no-kernel" in args and "--degraded-kb" in args and "--degraded-pr" in args
                and "--no-enable-roofline" in args, "owner-only campaign options required")
        require("--resume-from" not in args and "--quantize" not in args, "fresh fixed-precision campaign required")
    return root, settings, binding, budget(settings)


def command(root, config, pin, settings):
    argv = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-all"]
    if settings["mode"] == "optimizer":
        argv.append("--share-net")  # Codex inference only; agent tools have network-disabled profiles.
    argv += ["--new-session", "--clearenv", "--ro-bind", "/usr", "/usr",
             "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
             "--symlink", "usr/lib", "/lib64", "--dir", "/etc"]
    for name in ("ld.so.cache", "resolv.conf", "hosts", "nsswitch.conf", "ssl", "ca-certificates"):
        path = Path("/etc") / name
        if path.exists():
            argv += ["--ro-bind", str(path.resolve()), str(path)]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--ro-bind", str(PREFIX), str(PREFIX), "--ro-bind", str(root), str(root),
             "--ro-bind", str(root / "evidence"), "/mnt/hyperloom-evidence",
             "--ro-bind", str(root / "inputs/model-config.json"), MODEL + "/config.json",
             "--ro-bind", str(root / "inputs/tokenizer-config.json"), MODEL + "/tokenizer_config.json"]
    for name in WRITABLE:
        argv += ["--bind", str(root / name), str(root / name)]
    argv += ["--ro-bind", str(root / "replies"), str(root / "replies")]
    env = {
        "HOME": str(root / "work/home"), "TMPDIR": "/tmp",
        "PATH": str(PREFIX / "venv/bin") + ":/usr/bin", "PYTHONDONTWRITEBYTECODE": "1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "ROCR_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": "", "CUDA_VISIBLE_DEVICES": "",
        "HYPERLOOM_BENCHMARK_BACKEND": "workstation_owner",
        "HYPERLOOM_WORKSTATION_BINDING": settings["binding"]["path"],
        "HYPERLOOM_WORKSTATION_BINDING_SHA256": settings["binding"]["sha256"],
    }
    env.update(settings.get("environment", {}))
    if settings["mode"] == "optimizer":
        require(AUTH.resolve() == AUTH and AUTH.is_dir(), "dedicated ChatGPT sign-in missing")
        argv += ["--bind", str(AUTH), str(AUTH)]
        env.update(HYPERLOOM_AUTH_MODE="chatgpt", HYPERLOOM_CHATGPT_AUTH_HOME=str(AUTH))
    for key, value in sorted(env.items()):
        require(isinstance(value, str), "runtime environment values must be text")
        argv += ["--setenv", key, value]
    argv += ["--chdir", str(root / "work"), str(PREFIX / "venv/bin/python"), "-I", "-B", "-S",
             str(root / "optimizer_runtime.py"), "inside", str(config), "--sha256", pin]
    return argv


def run_group(argv, *, seconds, output, error):
    """Term/reap a fresh process group, including deadline and signal paths."""
    require(type(seconds) is int and seconds > 0, "finite runtime required")
    started = time.monotonic()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                             start_new_session=True)
    signals = []
    failure = None
    try:
        child.wait(timeout=seconds)
    except BaseException as exception:
        failure = type(exception).__name__
    finally:
        # Kill the owned group even if its leader exited with a remaining child.
        # Bubblewrap's PID namespace also dies when its init process exits.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                break
            signals.append(int(sig))
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if child.poll() is None:
            child.wait(timeout=5)
    try:
        os.killpg(child.pid, 0)
    except ProcessLookupError:
        absent = True
    else:
        absent = False
    return dict(exit_code=child.returncode, elapsed_seconds=time.monotonic()-started,
                child_reaped=child.poll() is not None, owned_group_absent=absent,
                failure=failure, signals=signals)


def limits():
    os.setpriority(os.PRIO_PROCESS, 0, 10)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (14_400, 14_400))
    # V8's sparse address-space reservation is larger than its resident heap.
    # A 4/64-GiB address cap crashes the pinned code-mode host. The real tool
    # proof passed at 2 TiB virtual with a separate 4-GiB data limit (410 MiB RSS).
    resource.setrlimit(resource.RLIMIT_AS, (2 << 40, 2 << 40))
    resource.setrlimit(resource.RLIMIT_DATA, (4 << 30, 4 << 30))
    # Per-file ceiling for a four-hour coordinator transcript, not the short
    # native startup proof's 64-MiB cap. A cap hit remains a retained failure.
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_FILE_CAP, OUTPUT_FILE_CAP))


def interrupted(signum, frame):
    raise InterruptedError("runtime received signal " + str(signum))


def completed_component(root,settings,binding):
    """Authenticate the original same-campaign component; never invent a new run."""
    reference=settings.get('completed_component')
    if reference is None:return None
    import hyperloom_exchange as exchange
    from hyperloom_owner_backend import validate_component
    value=json.loads(checked(reference['path'],reference['sha256']))
    require(value.get('schema')==1 and value.get('new_measurement') is False,'retained component descriptor')
    request,result=value['request'],value['result']
    require(request['kind']=='component' and request['campaign_sha256']==binding['campaign_sha256']
            and request['selection_sha256']==binding['selection_sha256']
            and len(request['sources'])==1 and request['sources'][0]['sha256']==QUALIFICATION_CANDIDATE_SHA,
            'retained component must be this campaign and reviewed oracle candidate')
    exchange.validate_result(result,request,controller_sha256=binding['controller_sha256'])
    report=validate_component(result,request,binding)
    require(report['correct_both_cards'] is True and report['eligible_for_serving'] is False,
            'retained component must pass correctness without serving eligibility')
    seal='inputs/completed-component-seal.sha256'
    require(settings['source_pins'].get(seal)==result['owner_seal_sha256'],'retained owner seal must be pinned')
    checked(root/seal,result['owner_seal_sha256'])
    return dict(report=report,index=result['index'],request_sha256=result['request_sha256'],
                owner_seal_sha256=result['owner_seal_sha256'],new_measurement=False)


def qualify(root, binding, *, benchmark, source_set, completed=None):
    """Qualify the component path, then C2 profiling and unchanged-C4 serving."""
    require(source_set(binding) == binding["selected_source_set_sha256"], "qualification requires selected C4")
    candidate = root / "candidate/extend_attention_fwd_candidate.py"
    selected = candidate.read_bytes()
    reviewed = checked(root / "inputs/qualification-candidate.py", QUALIFICATION_CANDIDATE_SHA)
    reports = {}
    if completed is None:
        try:
            candidate.write_bytes(reviewed)
            output = root / "results/qualification-component"
            output.mkdir(mode=0o700)
            config = {"benchmark": dict(binding["benchmark"], timeout_seconds=2910,
                                        workstation_component={"contract":"c4-fixed-geometry-v1"})}
            reports["component"] = benchmark(config, output, binding)
            require(reports["component"].get("correct_both_cards") is True,
                    "component qualification did not pass both numerical oracles")
        finally:
            candidate.write_bytes(selected)
    else:
        require(completed['new_measurement'] is False and completed['report']['correct_both_cards'] is True,
                'authenticated completed component required')
        reports['component']=completed['report']
        publish(root/'results/COMPONENT-REUSED.json',completed)
    require(source_set(binding) == binding["selected_source_set_sha256"], "selected source restoration failed")
    # Both jobs need the same full admission envelope. Run the shorter profile
    # first so the longer A/A can still be admitted under the shared deadline.
    for kind in ("profile", "serving"):
        config = {"benchmark": dict(binding["benchmark"], timeout_seconds=7660)}
        if kind == "profile":
            config["benchmark"]["profiler"] = {"torch_profiler": {"enabled": True}}
        output = root / "results" / ("qualification-" + kind)
        output.mkdir(mode=0o700)
        reports[kind] = benchmark(config, output, binding)
    publish(root / "results/qualification-reports.json", reports)
    return 0


def inside(root, settings, binding):
    require(not Path("/dev/kfd").exists() and not Path("/dev/dri").exists(), "optimizer GPU devices exposed")
    require(os.statvfs(root / "replies").f_flag & os.ST_RDONLY, "optimizer replies are writable")
    require(os.statvfs(root / "inputs").f_flag & os.ST_RDONLY, "optimizer binding is writable")
    sys.path[:0] = [str(root / "sdk"), str(SITE)]
    from hyperloom_owner_backend import read_binding, source_set, benchmark
    binding = read_binding(settings["binding"]["path"], settings["binding"]["sha256"])
    publish(root / "results" / (settings["mode"] + "-inside.json"),
            dict(gpu_devices=False, replies_read_only=True, inputs_read_only=True,
                 deadline_unix_ms=settings["deadline_unix_ms"], pid=os.getpid()))
    if settings["mode"] == "qualification":
        require(not (root / "results/qualification-cancelled.json").exists(), "qualification was cancelled")
        return qualify(root, binding, benchmark=benchmark, source_set=source_set,
                       completed=completed_component(root,settings,binding))
    require(not (root / "results/optimizer-cancelled.json").exists(), "optimizer was cancelled")
    from hyperloom.inference_optimizer.cli import main as optimize
    args = optimizer_argv(settings)
    publish(root / "results/optimizer-effective-budget.json", dict(argv=args,
        deadline_unix_ms=settings["deadline_unix_ms"], closeout_seconds=CLOSEOUT_SECONDS,
        output_file_cap=OUTPUT_FILE_CAP))
    return optimize(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("launch", "inside"))
    parser.add_argument("config", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if args.action == 'launch':
        # The SSH command uses exec, so its server process is this supervisor's
        # parent. A lost transport must terminate the owned CPU sandbox too.
        import ctypes
        parent=os.getppid()
        signal.signal(signal.SIGTERM, interrupted)
        require(ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) == 0,
                'cannot bind CPU supervisor lifetime to its SSH parent')
        require(os.getppid() == parent and parent > 1, 'SSH parent exited during runtime start')
    root, settings, binding, seconds = validate(args.config, args.sha256, native=args.action == "launch")
    require(not (root / 'results' / (settings['mode'] + '-cancelled.json')).exists(), "runtime was cancelled before start")
    limits()
    if args.action == "inside":
        return inside(root, settings, binding)
    signal.signal(signal.SIGTERM, interrupted)
    result_path = root / "results" / (settings["mode"] + "-runtime-result.json")
    started_path = root / "results" / (settings["mode"] + "-runtime-started.json")
    publish(started_path, dict(settings_sha256=args.sha256, started_unix_ms=int(time.time()*1000),
                              seconds=seconds, automatic_retry=False, gpu_devices=False,
                              pid=os.getpid(), boot_id=BOOT, config=str(args.config),
                              process_start_ticks=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]))
    with (root / "results" / (settings["mode"] + ".stdout")).open("xb") as output, \
         (root / "results" / (settings["mode"] + ".stderr")).open("xb") as error:
        require(not (root / 'results' / (settings['mode'] + '-cancelled.json')).exists(), "runtime was cancelled before spawn")
        observed = run_group(command(root, args.config, args.sha256, settings), seconds=seconds,
                             output=output, error=error)
    observed.update(schema=1, settings_sha256=args.sha256, mode=settings["mode"],
                    automatic_retry=False, adoption_authorized=False, deadline_unix_ms=settings["deadline_unix_ms"])
    publish(result_path, observed)
    print(json.dumps(observed, sort_keys=True), flush=True)
    return 0 if observed["exit_code"] == 0 and not observed["failure"] and observed["owned_group_absent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
