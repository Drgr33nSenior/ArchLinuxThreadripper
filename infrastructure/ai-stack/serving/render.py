#!/usr/bin/env python3
"""Render Kubernetes objects for one serving profile arm.

The profile JSON is the single record of the server's flags and environment;
model and draft revisions come from the model catalog it names. `argv` prints
the server command line. `job` renders a qualification Job (plus the probe's
ConfigMap) that starts the server beside qualify.py on the ai-models volume
and exits with the probe's result. Output is JSON that `kubectl apply -f -`
accepts; this script never contacts a cluster.

Usage: render.py argv PROFILE [--arm A|B]
       render.py job PROFILE IMAGE [--arm A|B]
"""
import argparse
import hashlib
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
NAMESPACE = "ai-home-lab"
PORT = 30000
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
    for entry in profile["args"] + spec["args"]:
        argv += [entry["flag"], *entry["value"]]
    return argv + ["--host", "0.0.0.0", "--port", str(PORT)]


def environment(profile):
    env = [{"name": e["name"], "value": e["value"]} for e in profile["env"]]
    # Writable locations: the root filesystem is read-only.
    env += [{"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "XDG_CACHE_HOME", "value": "/cache/xdg"}, {"name": "HF_HOME", "value": "/cache/huggingface"},
            {"name": "TRITON_CACHE_DIR", "value": "/cache/triton"},
            {"name": "TORCHINDUCTOR_CACHE_DIR", "value": "/cache/torchinductor"},
            {"name": "SGLANG_JIT_CACHE_DIR", "value": "/cache/sglang-jit"}]
    return env


def job(profile, catalog, image, arm):
    if not re.fullmatch(IMAGE_RE, image):
        raise SystemExit("STOP: IMAGE must be a lab registry image pinned by digest")
    arm, _ = arm_of(profile, arm)
    argv = server_argv(profile, catalog, arm)
    probe = (HERE / "qualify.py").read_text()
    key = hashlib.sha256(json.dumps([argv, image, probe]).encode()).hexdigest()[:10]
    name = f"sglang-qualify-{arm.lower()}-{key}"
    labels = {"app.kubernetes.io/name": "sglang-qualify", "ai-stack/profile": profile["profile"], "ai-stack/arm": arm}
    script = ('python3 -m sglang.launch_server "$@" & server=$!\n'
              f'python3 /qualify/qualify.py {PORT} "$SERVED_MODEL_NAME" "$server" 2400; rc=$?\n'
              'kill -TERM "$server" 2>/dev/null; wait "$server"; exit "$rc"\n')
    container = {
        "name": "sglang",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["bash", "-c", script, "sglang-qualify"],
        "args": argv,
        "env": environment(profile) + [{"name": "SERVED_MODEL_NAME", "value": profile["served_model_name"]}],
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
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": {"qualify.py": probe}},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 4500, "ttlSecondsAfterFinished": 172800,
                  "template": {"metadata": {"labels": labels}, "spec": pod}}},
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["argv", "job"])
    parser.add_argument("profile")
    parser.add_argument("image", nargs="?")
    parser.add_argument("--arm")
    args = parser.parse_args()
    profile, catalog = load(args.profile)
    if args.mode == "argv":
        print(json.dumps(server_argv(profile, catalog, args.arm)))
    else:
        if not args.image:
            raise SystemExit("STOP: job needs IMAGE")
        print(json.dumps(job(profile, catalog, args.image, args.arm), indent=2))


if __name__ == "__main__":
    main()
