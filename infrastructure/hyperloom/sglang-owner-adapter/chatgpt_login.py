"""User-operated ChatGPT sign-in in a device-free, private Codex sandbox.

Run on the workstation. Login instructions go to the user's terminal only.
The status action returns a boolean and never prints the Codex credential store.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import stat
import subprocess


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def argv(root, settings, action):
    assert action in {"help", "login", "status"}
    assert os.getuid() == os.geteuid() == 1000
    assert Path("/proc/sys/kernel/random/boot_id").read_text().strip() == settings["boot_id"]
    root = Path(root)
    assert root.is_absolute() and root.resolve() == root
    for path in (root, root / "auth"):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
    assert digest(root / "auth/config.toml") == settings["config_sha256"]
    prefix = Path(settings["prefix"])
    binary = Path(settings["codex"])
    assert prefix.resolve() == prefix and binary.resolve() == binary and binary.is_relative_to(prefix)
    assert digest(binary) == settings["codex_sha256"]
    command = ["/usr/bin/timeout", "--signal=TERM", "--kill-after=5s", "600s" if action == "login" else "30s",
        "/usr/bin/bwrap", "--die-with-parent", "--unshare-all", "--share-net", "--new-session", "--clearenv",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64", "--dir", "/etc"]
    for name in ("ld.so.cache", "resolv.conf", "hosts", "nsswitch.conf", "ssl", "ca-certificates"):
        path = Path("/etc") / name
        if path.exists():
            command.extend(["--ro-bind", str(path.resolve()), str(path)])
    command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--dir", "/mnt/home", "--ro-bind", str(prefix), str(prefix),
        "--bind", str(root / "auth"), "/mnt/auth", "--chdir", "/mnt/home",
        "--setenv", "HOME", "/mnt/home", "--setenv", "CODEX_HOME", "/mnt/auth",
        "--setenv", "PATH", "/usr/bin", "--setenv", "LANG", "C.UTF-8",
        str(binary), "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
        "login", "--device-auth" if action == "login" else "--help" if action == "help" else "status"])
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("help", "login", "status"))
    parser.add_argument("--settings-sha256", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    assert digest(root / "SETTINGS.json") == args.settings_sha256
    settings = json.loads((root / "SETTINGS.json").read_bytes())
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 << 20, 32 << 20))
    os.setpriority(os.PRIO_PROCESS, 0, 10)
    command = argv(root, settings, args.action)
    if args.action == "login":
        # Deliberately no capture, receipt or logging of device-login instructions.
        return subprocess.call(command, timeout=610)
    done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=40)
    assert len(done.stdout) + len(done.stderr) <= 65536
    if args.action == "help":
        assert done.returncode == 0 and b"--device-auth" in done.stdout + done.stderr
        print(json.dumps({"device_login_supported": True, "gpu_work": False}))
    else:
        authenticated = done.returncode == 0 and b"logged in using chatgpt" in (done.stdout + done.stderr).lower()
        print(json.dumps({"authenticated_with_chatgpt": authenticated, "gpu_work": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
