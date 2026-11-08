#!/usr/bin/env python3
"""Render the Kubernetes Job that fetches and verifies one catalog entry.

The Job runs fetch.py in the stack's pinned Python base image, writes to the
ai-models volume, and is named after the entry's content hash, so a changed
entry gets a new Job and an unchanged one is a no-op. Output is a JSON List.

Usage: render-fetch.py CATALOG_ENTRY STACK_LOCK
"""
import hashlib
import json
import pathlib
import re
import sys

NAMESPACE = "ai-home-lab"


def lock_value(lock: str, key: str) -> str:
    values = [line.split("=", 1)[1] for line in lock.splitlines() if line.startswith(key + "=")]
    if len(values) != 1:
        raise SystemExit(f"STOP: stack.lock needs exactly one {key}")
    return values[0]


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    entry_path = pathlib.Path(sys.argv[1])
    entry_text = entry_path.read_text()
    entry = json.loads(entry_text)
    image = lock_value(pathlib.Path(sys.argv[2]).read_text(), "RUNTIME_BASE_IMAGE")
    if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[a-f0-9]{64}", image):
        raise SystemExit("STOP: RUNTIME_BASE_IMAGE must be pinned by digest")
    fetcher = (pathlib.Path(__file__).parent / "fetch.py").read_text()
    key = hashlib.sha256((entry_text + fetcher).encode()).hexdigest()[:10]
    name = f"model-fetch-{entry['alias'].replace('.', '-')}-{key}"[:63]
    labels = {"app.kubernetes.io/name": "model-fetch", "ai-stack/model": entry["alias"].replace(".", "-")}
    config = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "data": {"fetch.py": fetcher, "entry.json": entry_text},
    }
    hardened = {
        "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "backoffLimit": 4,
            "ttlSecondsAfterFinished": 604800,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    # The NAS export maps every client to 2000:2000.
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 2000, "runAsGroup": 2000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [{
                        "name": "fetch",
                        "image": image,
                        "command": ["python3", "-I", "-u", "/fetch/fetch.py", "/fetch/entry.json", "/models"],
                        "securityContext": hardened,
                        "resources": {
                            "requests": {"cpu": "500m", "memory": "256Mi"},
                            "limits": {"memory": "2Gi"},
                        },
                        "volumeMounts": [
                            {"name": "fetch", "mountPath": "/fetch", "readOnly": True},
                            {"name": "models", "mountPath": "/models"},
                        ],
                    }],
                    "volumes": [
                        {"name": "fetch", "configMap": {"name": name}},
                        {"name": "models", "persistentVolumeClaim": {"claimName": "ai-models"}},
                    ],
                },
            },
        },
    }
    json.dump({"apiVersion": "v1", "kind": "List", "items": [config, job]}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
