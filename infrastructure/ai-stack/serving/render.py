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

`standalone` writes the same server and benchmark as a folder anyone with two
gfx1201 GPUs can run with podman or docker (run.sh), for a published image.

Usage: render.py argv PROFILE [--arm ARM]
       render.py job PROFILE IMAGE [--arm ARM]
       render.py bench PROFILE IMAGE --serving SERVING_REPOSITORY_ROOT [--arm ARM] [--greedy] [--top-p P]
       render.py standalone PROFILE IMAGE --serving SERVING_REPOSITORY_ROOT --out DIR [--arm ARM]
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
# A published copy (infrastructure/ai-stack/publish/publish.sh keeps the digest).
PUBLIC_IMAGE_RE = r"ghcr\.io/[a-z0-9-]+/[a-z0-9._-]+@sha256:[a-f0-9]{64}"


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
        # The serving `sglang` Service targets the port named "api"; without it the Service
        # has no endpoint for this pod and telemetry cannot scrape the running server.
        "ports": [{"name": "api", "containerPort": PORT}],
        "env": environment(profile, arm) + [{"name": "SERVED_MODEL_NAME", "value": profile["served_model_name"]}],
        # The serving Deployment's 24Gi plus room for the bench client. 38Gi stopped fitting on
        # gr33nstein (44.1 GiB allocatable) once the assistant services requested 12.4 GiB, and
        # the Job then stays Pending with "Insufficient memory".
        "resources": {"requests": {"cpu": "16", "memory": "30Gi"},
                      "limits": {"cpu": "16", "memory": "30Gi"},
                      "claims": [{"name": "gpus"}]},
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
        # Its own claim (from the template below), not the shared gpu-0/gpu-1: DRA
        # never allocates a device to two different claims, so the Job stays
        # Pending while the serving Deployment or SwarmUI hold the cards.
        "resourceClaims": [{"name": "gpus", "resourceClaimTemplateName": name}],
        "volumes": [{"name": "models", "persistentVolumeClaim": {"claimName": "ai-models", "readOnly": True}},
                    {"name": "qualify", "configMap": {"name": name}},
                    {"name": "cache", "emptyDir": {"sizeLimit": "32Gi"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}},
                    {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}}],
    }
    meta = {"name": name, "namespace": NAMESPACE, "labels": labels}
    # Both R9700s, exclusively, for the life of the Job's pod.
    claims = {"apiVersion": "resource.k8s.io/v1", "kind": "ResourceClaimTemplate", "metadata": meta,
              "spec": {"spec": {"devices": {"requests": [{"name": "gpus", "exactly": {
                  "deviceClassName": "gpu.amd.com", "allocationMode": "ExactCount", "count": 2}}]}}}}
    # No Service here. The serving `sglang` Service (apps/base/sglang/service.yaml)
    # selects SERVER_LABEL, which this pod carries, so telemetry's fixed target
    # sglang.ai-home-lab.svc:30000 follows whichever server holds the GPUs. A Service
    # in this List replaced the family service's own on every bench (2026-11-11).
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": data, **({"binaryData": binary} if binary else {})},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
         "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 4500, "ttlSecondsAfterFinished": 172800,
                  "template": {"metadata": {"labels": {**labels, SERVER_LABEL: "true"}}, "spec": pod}}},
        claims,
    ]}


STANDALONE_RUN = r"""#!/usr/bin/env bash
# Generated by render.py standalone: profile @PROFILE@, arm @ARM@. Do not edit; re-render.
# Usage: MODELS_DIR=/path/to/models ./run.sh serve   (OpenAI-compatible API on port @PORT@)
#        MODELS_DIR=/path/to/models ./run.sh bench   (server + BetterBench + quality menu, then exit)
# MODELS_DIR holds <org>/<name>/<revision>/ folders (models.txt). ENGINE=podman|docker
# overrides the detected engine; CACHE_DIR (default ./cache) keeps compiled kernels.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
mode=${1:-}
[[ $mode == serve || $mode == bench ]] || { echo 'usage: run.sh serve|bench' >&2; exit 2; }
: "${MODELS_DIR:?set MODELS_DIR to the folder holding the models listed in models.txt}"
engine=${ENGINE:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}
image=@IMAGE@
cache=${CACHE_DIR:-$here/cache}
mkdir -p "$cache"
while read -r repo revision; do
  [[ -f $MODELS_DIR/$repo/$revision/config.json ]] || { echo "missing $MODELS_DIR/$repo/$revision (models.txt)" >&2; exit 1; }
done < <(grep -v '^#' "$here/models.txt")
mapfile -t args < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))))' "$here/server-args.json")
run=(run --rm --device /dev/kfd --device /dev/dri --read-only --tmpfs '/tmp:rw,exec,size=8g' --shm-size 16g
     --cap-drop ALL --security-opt no-new-privileges --env-file "$here/server.env"
     -v "$MODELS_DIR:/models:ro" -v "$cache:/cache")
if [[ $engine == podman ]]; then
  run+=(--group-add keep-groups --security-opt label=disable)
else
  for group in video render; do
    gid=$(getent group "$group" | cut -d: -f3) && [[ -n $gid ]] && run+=(--group-add "$gid")
  done
  run+=(--user "$(id -u):$(id -g)")
fi
if [[ $mode == serve ]]; then
  exec "$engine" "${run[@]}" -p @PORT@:@PORT@ --entrypoint python3 "$image" -m sglang.launch_server "${args[@]}"
fi
script='python3 -m sglang.launch_server "$@" & server=$!
python3 /qualify/bench.py @PORT@ "$SERVED_MODEL_NAME" "$server" 2400 /qualify/betterbench.tar.gz; rc=$?
kill -TERM "$server" 2>/dev/null; wait "$server"; exit "$rc"'
exec "$engine" "${run[@]}" --network none -v "$here/bench:/qualify:ro" --entrypoint bash "$image" \
  -c "$script" sglang-bench "${args[@]}"
"""


STANDALONE_README = """# @TITLE@: SGLang on two AMD RDNA4 GPUs

A self-contained recipe for the image `@IMAGE@`: SGLang (main, with the gfx1201 patches of
the serving repository) serving `@MODEL@` with the `@DRAFT@` DFlash draft over two GPUs
(tensor parallel 2), plus the BetterBench profile and quality menu used to measure it.
Generated from serving profile `@PROFILE@`, arm `@ARM@`; do not edit, re-render.

## Requirements

- Linux with two RDNA4 GPUs (gfx1201: Radeon AI PRO R9700, RX 9070 / 9070 XT) and a current
  `amdgpu` kernel driver; `/dev/kfd` and `/dev/dri` accessible to your user (groups `video`, `render`).
- podman or docker, `python3`, and about 20 GB of disk for the image.
- The image is built for gfx1201 only. The settings assume 32 GB per GPU (R9700); 16 GB cards
  need a shorter `--context-length` and are untested.

## Models

Download both revisions in `models.txt` into `$MODELS_DIR/<repository>/<revision>/`, e.g.

    hf download @MODEL@ --revision @MODEL_REV@ --local-dir "$MODELS_DIR/@MODEL@/@MODEL_REV@"
    hf download @DRAFT@ --revision @DRAFT_REV@ --local-dir "$MODELS_DIR/@DRAFT@/@DRAFT_REV@"

The models are not part of the image and come under their own licences.

## Run

    MODELS_DIR=/path/to/models ./run.sh serve   # OpenAI-compatible API on http://localhost:@PORT@/v1
    MODELS_DIR=/path/to/models ./run.sh bench   # server, BetterBench, quality menu; prints BENCH_RESULT / QUALITY_RESULT

The first start compiles kernels into `./cache` (reused afterwards). `ENGINE=docker` forces docker.

## Verify the image

The image is signed (no transparency log) and carries CycloneDX SBOM and vulnerability attestations:

    cosign verify --key cosign.pub --insecure-ignore-tlog=true @IMAGE@
    cosign verify-attestation --key cosign.pub --insecure-ignore-tlog=true --type cyclonedx @IMAGE@

## Licences

The recipe and the serving patches are MIT. The image contains SGLang (Apache-2.0), PyTorch
(BSD-3-Clause), Triton (MIT), ROCm / TheRock components (mostly MIT and Apache-2.0) and vendored
AITER kernels (MIT); the CycloneDX SBOM attestation lists every package and licence. The bench
bundle includes BetterBench (Apache-2.0).
"""


def standalone(profile, catalog, image, arm, bench, out):
    if not (re.fullmatch(IMAGE_RE, image) or re.fullmatch(PUBLIC_IMAGE_RE, image)):
        raise SystemExit("STOP: IMAGE must be the lab image or its ghcr.io copy, pinned by digest")
    arm, _ = arm_of(profile, arm)
    out = pathlib.Path(out)
    (out / "bench").mkdir(parents=True, exist_ok=False)
    (out / "server-args.json").write_text(json.dumps(server_argv(profile, catalog, arm), indent=1) + "\n")
    env = environment(profile, arm) + [{"name": "SERVED_MODEL_NAME", "value": profile["served_model_name"]}]
    for e in env:
        if "\n" in e["value"]:
            raise SystemExit(f"STOP: {e['name']} cannot go in an env file")
    (out / "server.env").write_text("".join(f"{e['name']}={e['value']}\n" for e in env))
    model, draft = catalog["model"], catalog["draft"]
    (out / "models.txt").write_text("# <repository> <revision>: download each to $MODELS_DIR/<repository>/<revision>, e.g.\n"
                                    "# hf download <repository> --revision <revision> --local-dir \"$MODELS_DIR/<repository>/<revision>\"\n"
                                    f"{model['repository']} {model['revision']}\n{draft['repository']} {draft['revision']}\n")
    (out / "bench" / "bench.py").write_text((HERE / "bench.py").read_text())
    (out / "bench" / "betterbench.tar.gz").write_bytes(bench)
    script = (STANDALONE_RUN.replace("@PROFILE@", profile["profile"]).replace("@ARM@", arm)
              .replace("@IMAGE@", image).replace("@PORT@", str(PORT)))
    (out / "run.sh").write_text(script)
    (out / "run.sh").chmod(0o755)
    (out / "cosign.pub").write_text((HERE.parent / "signing" / "cosign.pub").read_text())
    readme = STANDALONE_README
    for key, value in {"@TITLE@": profile["served_model_name"], "@IMAGE@": image, "@MODEL@": model["repository"],
                       "@MODEL_REV@": model["revision"], "@DRAFT@": draft["repository"],
                       "@DRAFT_REV@": draft["revision"], "@PROFILE@": profile["profile"], "@ARM@": arm,
                       "@PORT@": str(PORT)}.items():
        readme = readme.replace(key, value)
    (out / "README.md").write_text(readme)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["argv", "job", "bench", "standalone"])
    parser.add_argument("profile")
    parser.add_argument("image", nargs="?")
    parser.add_argument("--arm")
    parser.add_argument("--serving", help="serving repository root (bench)")
    parser.add_argument("--greedy", action="store_true", help="bench at temperature 0 (acceptance bisection)")
    parser.add_argument("--top-p", type=float, help="bench with this top-p instead of the profile's 0.95")
    parser.add_argument("--out", help="output folder (standalone; must not exist)")
    args = parser.parse_args()
    profile, catalog = load(args.profile)
    if args.mode == "argv":
        print(json.dumps(server_argv(profile, catalog, args.arm)))
        return
    if not args.image:
        raise SystemExit(f"STOP: {args.mode} needs IMAGE")
    bench = None
    if args.mode in ("bench", "standalone"):
        if not args.serving:
            raise SystemExit(f"STOP: {args.mode} needs --serving SERVING_REPOSITORY_ROOT")
        bench = betterbench_bundle(args.serving, args.greedy, args.top_p)
    if args.mode == "standalone":
        if not args.out:
            raise SystemExit("STOP: standalone needs --out DIR")
        standalone(profile, catalog, args.image, args.arm, bench, args.out)
        return
    print(json.dumps(job(profile, catalog, args.image, args.arm, bench), indent=2))


if __name__ == "__main__":
    main()
