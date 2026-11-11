#!/usr/bin/env python3
"""Render the Job that runs the serving repository's fp8 KV kernel check on one card.

The Job runs tools/check_fp8_kv_dequant.py (serving patch 0026, "UQ2") in the SGLang
image built with the series: kernel error on an fp8 cache against an fp32 reference, and
target-verify time against a bf16 cache. It needs no model. It gets one card through its
own DRA claim, selected by PCI address. DRA never allocates a device to two claims, so the
Job stays Pending while SGLang or SwarmUI hold that card; pause them first.
Output is a JSON List: ConfigMap, ResourceClaimTemplate and Job.

Usage: kernel_check.py IMAGE_REF --serving SERVING_REPOSITORY_ROOT [--pci 0000:c3:00.0] [--quick]
"""
import argparse
import hashlib
import json
import pathlib
import re

NAMESPACE = "ai-home-lab"
GPU_GROUPS = [987, 983]  # render, video on gr33nstein (as the SGLang serving contract)
TOOL = "tools/check_fp8_kv_dequant.py"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="SGLang image built with patch 0026, pinned by digest")
    parser.add_argument("--serving", required=True, help="serving repository root (holds the check tool)")
    parser.add_argument("--pci", default="0000:c3:00.0", help="card to use (default gpu-1)")
    parser.add_argument("--quick", action="store_true", help="1k and 8k prefixes only")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[a-f0-9]{64}", args.image):
        parser.error("the image must be pinned by digest")
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", args.pci):
        parser.error("--pci must be a PCI address like 0000:c3:00.0")
    tool = (pathlib.Path(args.serving) / TOOL).read_text()
    key = hashlib.sha256(json.dumps([args.image, args.pci, tool, args.quick]).encode()).hexdigest()[:10]
    name = f"fp8-kv-check-{key}"
    labels = {"app.kubernetes.io/name": "fp8-kv-check"}
    meta = {"name": name, "namespace": NAMESPACE, "labels": labels}
    claim = {"apiVersion": "resource.k8s.io/v1", "kind": "ResourceClaimTemplate", "metadata": meta,
             "spec": {"spec": {"devices": {"requests": [{"name": "gpu", "exactly": {
                 "deviceClassName": "gpu.amd.com", "allocationMode": "ExactCount", "count": 1,
                 "selectors": [{"cel": {"expression":
                     f'device.attributes["resource.kubernetes.io"].pciBusID == "{args.pci}"'}}]}}]}}}}
    env = [{"name": "SGLANG_GFX1201_VERIFY_ATTENTION", "value": "1"},
           {"name": "SGLANG_GFX1201_FP8_KV_DEQUANT", "value": "1"},
           # Writable locations: the root filesystem is read-only.
           {"name": "HOME", "value": "/tmp"}, {"name": "TMPDIR", "value": "/tmp"},
           {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
           {"name": "XDG_CACHE_HOME", "value": "/tmp/xdg"}, {"name": "TRITON_CACHE_DIR", "value": "/tmp/triton"}]
    container = {
        "name": "check",
        "image": args.image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python3", "-u", "/check/check_fp8_kv_dequant.py", *(["--quick"] if args.quick else [])],
        "env": env,
        "resources": {"requests": {"cpu": "8", "memory": "16Gi"}, "limits": {"cpu": "8", "memory": "16Gi"},
                      "claims": [{"name": "gpu"}]},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "check", "mountPath": "/check", "readOnly": True},
                         {"name": "tmp", "mountPath": "/tmp"}],
    }
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "nodeSelector": {"ai-home-lab.local/gfx": "gfx1201"},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "supplementalGroups": GPU_GROUPS, "seccompProfile": {"type": "RuntimeDefault"}},
        "resourceClaims": [{"name": "gpu", "resourceClaimTemplateName": name}],
        "containers": [container],
        "volumes": [{"name": "check", "configMap": {"name": name}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}}],
    }
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "ttlSecondsAfterFinished": 604800,
                    "template": {"metadata": {"labels": labels}, "spec": pod}}}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta,
              "data": {"check_fp8_kv_dequant.py": tool}}
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [config, claim, job]}, indent=2))


if __name__ == "__main__":
    main()
