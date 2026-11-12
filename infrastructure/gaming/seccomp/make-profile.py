#!/usr/bin/env python3
"""Derive the gaming container's seccomp profile from the pinned upstream default.

Usage: make-profile.py BASE_JSON OUTPUT_JSON

The base is moby/profiles seccomp/v0.3.0 default.json (the profile containerd's
RuntimeDefault mirrors), vendored next to this script with its SHA-256 checked.

Kubernetes Localhost profiles reach the runtime in plain OCI form: Docker's
archMap, includes/excludes and comment fields are ignored. So the profile is
written already resolved, as containerd resolves its own default, for this
container: amd64 with its x86/x32 sub-architectures (Steam is 32-bit), NO Linux
capabilities (every capability-gated rule is dropped), and a kernel newer than
any upstream minKernel.

The only addition: Steam's bubblewrap sandbox may create its own user, mount,
PID, IPC, network and UTS namespaces and build its root inside them. Those calls
are allowed; the kernel still permits mounts, pivot_root, sethostname and setns
only inside a user namespace the caller owns, and the container keeps
no_new_privs. bpf, perf_event_open, syslog, quotactl and the rest of the
capability-gated calls stay out, so they keep the default deny.
"""
import hashlib
import json
import sys
from pathlib import Path

BASE_SHA256 = "6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6"
ARCH = "amd64"
NATIVE = "SCMP_ARCH_X86_64"
NAMESPACE_SYSCALLS = sorted({
    "clone", "clone3", "unshare", "setns",
    "mount", "umount", "umount2", "pivot_root",
    "mount_setattr", "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
    "sethostname", "setdomainname",
})


def applies(rule):
    """Moby's setupSeccomp filter for an amd64 container with no capabilities."""
    includes, excludes = rule.get("includes") or {}, rule.get("excludes") or {}
    if includes.get("arches") and ARCH not in includes["arches"]:
        return False
    if includes.get("caps"):
        return False  # no capabilities are granted
    if ARCH in (excludes.get("arches") or []):
        return False
    # excludes.caps: none are held, so never excluded; includes.minKernel: host is newer.
    return True


def derive(base):
    (arch,) = [a for a in base["archMap"] if a["architecture"] == NATIVE]
    syscalls = []
    for rule in base["syscalls"]:
        if not applies(rule):
            continue
        names = [n for n in rule["names"] if n not in NAMESPACE_SYSCALLS]
        if not names:
            continue  # every name in this rule is allowed unconditionally below
        out = {"names": names, "action": rule["action"]}
        for key in ("errnoRet", "args"):
            if rule.get(key):
                out[key] = rule[key]
        syscalls.append(out)
    syscalls.append({"names": NAMESPACE_SYSCALLS, "action": "SCMP_ACT_ALLOW"})
    return {"defaultAction": base["defaultAction"], "defaultErrnoRet": base["defaultErrnoRet"],
            "architectures": [NATIVE] + arch["subArchitectures"], "syscalls": syscalls}


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: make-profile.py BASE_JSON OUTPUT_JSON")
    data = Path(argv[1]).read_bytes()
    if hashlib.sha256(data).hexdigest() != BASE_SHA256:
        raise SystemExit("base profile does not match the pinned moby/profiles seccomp/v0.3.0 digest")
    profile = derive(json.loads(data))
    Path(argv[2]).write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(sys.argv)
