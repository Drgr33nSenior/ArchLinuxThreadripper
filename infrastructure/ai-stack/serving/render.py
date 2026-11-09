#!/usr/bin/env python3
"""Render Kubernetes objects for one serving profile arm.

The profile JSON is the single record of the server's flags and environment;
model and draft revisions come from the model catalog it names. `argv` prints
the server command line. `job` renders a qualification Job (plus the probe's
ConfigMap) that starts the server beside qualify.py on the ai-models volume
and exits with the probe's result. `bench` renders the same pod running
bench.py, which replays the BetterBench profile vendored in the serving
repository (benchmarks/betterbench/native-v2), so results compare with the
op233/op234 measurements. Output is JSON that `kubectl apply -f -` accepts;
this script never contacts a cluster.

Usage: render.py argv PROFILE [--arm ARM]
       render.py job PROFILE IMAGE [--arm ARM]
       render.py bench PROFILE IMAGE --serving SERVING_REPOSITORY_ROOT [--arm ARM] [--greedy] [--top-p P]
"""
import argparse
import base64
import hashlib
import io
import json
import pathlib
import re
import tarfile

HERE = pathlib.Path(__file__).resolve().parent
REPOSITORY = HERE.parents[2]
# The assistant acceptance runner and its menu, bundled unchanged with their
# repository-relative paths (the runner locates its menu from its own path).
QUALITY_FILES = ["lib/workstation/assistant_acceptance.py", "lib/workstation/assistant_api.py",
                 "lib/workstation/coding_eval.py", "infrastructure/sglang/assistant-quality.json"]
QUALITY_TASKS = "infrastructure/sglang/assistant-tasks"
NAMESPACE = "ai-home-lab"
PORT = 30000
SERVER_LABEL = "ai-stack/sglang-server"
# Host groups of /dev/kfd and /dev/dri/renderD* (render) and /dev/dri/card* (video).
GPU_GROUPS = [987, 983]
IMAGE_RE = r"registry\.lan\.thespry\.family/local/[a-z0-9-]+@sha256:[a-f0-9]{64}"


def load(profile_path):
    profile = json.loads(pathlib.Path(profile_path).read_text())
    if profile.get("schema") != 1:
        raise SystemExit("STOP: unsupported profile schema")
    catalog = json.loads((HERE.parent / "models" / f"{profile['model']}.json").read_text())
    return profile, catalog


def arm_of(profile, arm):
    arms = profile["arms"]
    if arm is None:
        arm = next(name for name, spec in arms.items() if spec.get("default"))
    if arm not in arms:
        raise SystemExit(f"STOP: unknown arm {arm}")
    return arm, arms[arm]


def server_argv(profile, catalog, arm):
    _, spec = arm_of(profile, arm)
    model, draft = catalog["model"], catalog["draft"]
    argv = ["--model-path", f"/models/{model['repository']}/{model['revision']}",
            "--revision", model["revision"],
            "--served-model-name", profile["served_model_name"],
            "--speculative-draft-model-path", f"/models/{draft['repository']}/{draft['revision']}",
            "--speculative-draft-model-revision", draft["revision"]]
    # An arm may drop base flags (e.g. a disable switch) before adding its own.
    drop = set(spec.get("drop", []))
    unknown = drop - {entry["flag"] for entry in profile["args"]}
    if unknown:
        raise SystemExit(f"STOP: arm {arm} drops flags the profile does not set: {sorted(unknown)}")
    for entry in [e for e in profile["args"] if e["flag"] not in drop] + spec["args"]:
        argv += [entry["flag"], *entry["value"]]
    return argv + ["--host", "0.0.0.0", "--port", str(PORT)]


def environment(profile, arm=None):
    _, spec = arm_of(profile, arm)
    env = [{"name": e["name"], "value": e["value"]} for e in profile["env"] + spec.get("env", [])]
    # Writable locations: the root filesystem is read-only.
    env += [{"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "XDG_CACHE_HOME", "value": "/cache/xdg"}, {"name": "HF_HOME", "value": "/cache/huggingface"},
            {"name": "TRITON_CACHE_DIR", "value": "/cache/triton"},
            {"name": "TORCHINDUCTOR_CACHE_DIR", "value": "/cache/torchinductor"},
            {"name": "SGLANG_JIT_CACHE_DIR", "value": "/cache/sglang-jit"}]
    return env


def betterbench_bundle(serving, greedy, top_p=None):
    """The vendored BetterBench client, corpus and profile, plus the assistant
    acceptance runner and menu under quality/, as one tar.gz."""
    root = pathlib.Path(serving) / "benchmarks/betterbench/native-v2"
    profile = json.loads((root / "profile.json").read_text())
    if profile.get("name") != "qwen38-selected-thinking-low-native-terminal-v2":
        raise SystemExit("STOP: unexpected BetterBench profile")
    if greedy:
        profile["config"]["greedy"] = True
        profile["name"] += "-greedy"
    if top_p is not None:
        if not 0 < top_p <= 1:
            raise SystemExit("STOP: --top-p must be in (0, 1]")
        profile["config"]["top_p"] = top_p
        profile["name"] += f"-top-p-{top_p:g}"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        def add(path, data):
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), 0o444, 0
            archive.addfile(info, io.BytesIO(data))
        for source in sorted((root / "upstream").rglob("*")):
            relative = source.relative_to(root / "upstream")
            if source.is_file() and relative.parts[0] in ("betterbench", "corpus") and source.suffix in (".py", ".jsonl"):
                add(str(relative), source.read_bytes())
        add("profile.json", json.dumps(profile, sort_keys=True).encode())
        tasks = sorted(p for p in (REPOSITORY / QUALITY_TASKS).rglob("*") if p.is_file())
        for source in [REPOSITORY / name for name in QUALITY_FILES] + tasks:
            add("quality/" + str(source.relative_to(REPOSITORY)), source.read_bytes())
    return buffer.getvalue()


def job(profile, catalog, image, arm, bench=None):
    if not re.fullmatch(IMAGE_RE, image):
        raise SystemExit("STOP: IMAGE must be a lab registry image pinned by digest")
    arm, _ = arm_of(profile, arm)
    argv = server_argv(profile, catalog, arm)
    kind = "qualify" if bench is None else "bench"
    probe = (HERE / f"{kind}.py").read_text()
    data, binary = {f"{kind}.py": probe}, {}
    if bench is None:
        command = f'python3 /qualify/qualify.py {PORT} "$SERVED_MODEL_NAME" "$server" 2400'
    else:
        binary["betterbench.tar.gz"] = base64.b64encode(bench).decode()
        command = f'python3 /qualify/bench.py {PORT} "$SERVED_MODEL_NAME" "$server" 2400 /qualify/betterbench.tar.gz'
    key = hashlib.sha256(json.dumps([argv, image, probe] + ([binary] if binary else [])).encode()).hexdigest()[:10]
    name = f"sglang-{kind}-{arm.lower()}-{key}"
    labels = {"app.kubernetes.io/name": f"sglang-{kind}", "ai-stack/profile": profile["profile"], "ai-stack/arm": arm}
    script = ('python3 -m sglang.launch_server "$@" & server=$!\n'
              f'{command}; rc=$?\n'
              'kill -TERM "$server" 2>/dev/null; wait "$server"; exit "$rc"\n')
    container = {
        "name": "sglang",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["bash", "-c", script, f"sglang-{kind}"],
        "args": argv,
        "env": environment(profile, arm) + [{"name": "SERVED_MODEL_NAME", "value": profile["served_model_name"]}],
        "resources": {"requests": {"cpu": "16", "memory": "38Gi", "amd.com/gpu": "2"},
                      "limits": {"cpu": "16", "memory": "38Gi", "amd.com/gpu": "2"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "models", "mountPath": "/models", "readOnly": True},
                         {"name": "qualify", "mountPath": "/qualify", "readOnly": True},
                         {"name": "cache", "mountPath": "/cache"}, {"name": "tmp", "mountPath": "/tmp"},
                         {"name": "shm", "mountPath": "/dev/shm"}],
    }
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "nodeSelector": {"ai-home-lab.local/gfx": "gfx1201"},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "supplementalGroups": GPU_GROUPS, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "ai-models", "readOnly": True}},
                    {"name": "qualify", "configMap": {"name": name}},
                    {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}},
                    {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}}],
    }
    meta = {"name": name, "namespace": NAMESPACE, "labels": labels}
    # Telemetry scrapes sglang.ai-home-lab.svc:30000 (job "sglang"); this Service
    # points it at whichever qualification or bench server is running. Only one
    # Job (or the serving Deployment) can hold the GPUs, so only one matches.
    service = {"apiVersion": "v1", "kind": "Service",
               "metadata": {"name": "sglang", "namespace": NAMESPACE, "labels": labels},
               "spec": {"type": "ClusterIP", "selector": {SERVER_LABEL: "true"},
                        "ports": [{"name": "http", "port": PORT, "targetPort": PORT}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": data, **({"binaryData": binary} if binary else {})},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 4500, "ttlSecondsAfterFinished": 172800,
                  "template": {"metadata": {"labels": {**labels, SERVER_LABEL: "true"}}, "spec": pod}}},
        service,
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["argv", "job", "bench"])
    parser.add_argument("profile")
    parser.add_argument("image", nargs="?")
    parser.add_argument("--arm")
    parser.add_argument("--serving", help="serving repository root (bench)")
    parser.add_argument("--greedy", action="store_true", help="bench at temperature 0 (acceptance bisection)")
    parser.add_argument("--top-p", type=float, help="bench with this top-p instead of the profile's 0.95")
    args = parser.parse_args()
    profile, catalog = load(args.profile)
    if args.mode == "argv":
        print(json.dumps(server_argv(profile, catalog, args.arm)))
        return
    if not args.image:
        raise SystemExit(f"STOP: {args.mode} needs IMAGE")
    bench = None
    if args.mode == "bench":
        if not args.serving:
            raise SystemExit("STOP: bench needs --serving SERVING_REPOSITORY_ROOT")
        bench = betterbench_bundle(args.serving, args.greedy, args.top_p)
    print(json.dumps(job(profile, catalog, args.image, args.arm, bench), indent=2))


if __name__ == "__main__":
    main()
