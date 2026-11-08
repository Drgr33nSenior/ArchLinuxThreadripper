#!/usr/bin/env python3
"""Render the SGLang Deployment and Service for one catalog entry and image.

The server reads the verified model revision from the ai-models volume and
requires the API key from the `sglang-api-key` Secret. SGLang reads the key only
as an argument, so Kubernetes expands it from the Secret at container start. Clients and telemetry reach it
through the stable `sglang` Service; switching models or images is a re-render
and apply. Output is a JSON List.

Usage: render-serve.py CATALOG_ENTRY IMAGE
"""
import json
import pathlib
import re
import sys

NAMESPACE = "ai-home-lab"
PORT = 30000
# Host render and video groups for /dev/dri and /dev/kfd.
GPU_GROUPS = [987, 983]


def server_args(entry: dict) -> list[str]:
    serving = entry["serving"]
    model = entry["model"]
    args = [
        "--model-path", f"/models/{model['repository']}/{model['revision']}",
        "--served-model-name", entry["served_model_name"],
        "--host", "0.0.0.0", "--port", str(PORT),
        "--api-key", "$(SGLANG_API_KEY)",
        # Prometheus metrics for workstation telemetry; /metrics needs no key.
        "--enable-metrics",
    ]
    for key in ("tp_size", "context_length", "max_total_tokens", "max_running_requests", "kv_cache_dtype",
                "speculative_algorithm", "speculative_num_draft_tokens", "reasoning_parser", "tool_call_parser"):
        if key in serving:
            args += ["--" + key.replace("_", "-"), str(serving[key])]
    if "draft" in entry:
        draft = entry["draft"]
        args += ["--speculative-draft-model-path", f"/models/{draft['repository']}/{draft['revision']}"]
    return args


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    entry = json.loads(pathlib.Path(sys.argv[1]).read_text())
    image = sys.argv[2]
    if not re.fullmatch(r"registry\.lan\.thespry\.family/local/[a-z0-9-]+(:[A-Za-z0-9._-]+|@sha256:[a-f0-9]{64})", image):
        raise SystemExit("STOP: IMAGE must be a lab registry image")
    name = "sglang-" + entry["alias"].replace(".", "-")
    labels = {"app.kubernetes.io/name": "sglang", "ai-stack/model": entry["alias"].replace(".", "-")}
    hardened = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    container = {
        "name": "sglang",
        "image": image,
        "args": server_args(entry),
        "env": [
            {"name": "SGLANG_API_KEY", "valueFrom": {"secretKeyRef": {"name": "sglang-api-key", "key": "api-key"}}},
            {"name": "HOME", "value": "/scratch/home"},
            {"name": "XDG_CACHE_HOME", "value": "/scratch/cache"},
            {"name": "TRITON_CACHE_DIR", "value": "/scratch/cache/triton"},
            {"name": "TMPDIR", "value": "/scratch/tmp"},
        ],
        "ports": [{"name": "http", "containerPort": PORT}],
        "securityContext": hardened,
        "resources": {
            "requests": {"cpu": "4", "memory": "16Gi", "amd.com/gpu": str(entry["serving"]["tp_size"])},
            "limits": {"memory": "32Gi", "amd.com/gpu": str(entry["serving"]["tp_size"])},
        },
        # Weight load and kernel compilation take minutes on a cold start.
        "startupProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10, "failureThreshold": 180},
        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}, "periodSeconds": 10},
        "volumeMounts": [
            {"name": "models", "mountPath": "/models", "readOnly": True},
            {"name": "scratch", "mountPath": "/scratch"},
            {"name": "shm", "mountPath": "/dev/shm"},
        ],
    }
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "replicas": 1,
            # Both GPUs belong to one server; never run two at once.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True, "runAsUser": 2000, "runAsGroup": 2000,
                        "supplementalGroups": GPU_GROUPS, "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [
                        {"name": "models", "persistentVolumeClaim": {"claimName": "ai-models", "readOnly": True}},
                        {"name": "scratch", "emptyDir": {}},
                        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"}},
                    ],
                },
            },
        },
    }
    # One stable Service, whichever model is loaded: clients and the telemetry
    # collector (sglang.ai-home-lab.svc:30000) never change address on a swap.
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "sglang", "namespace": NAMESPACE, "labels": {"app.kubernetes.io/name": "sglang"}},
        "spec": {"selector": {"app.kubernetes.io/name": "sglang"},
                 "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]},
    }
    json.dump({"apiVersion": "v1", "kind": "List", "items": [deployment, service]}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
