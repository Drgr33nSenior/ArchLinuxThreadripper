#!/usr/bin/env python3
"""Render the Job that measures SDXL's VRAM peak and render times on one card.

The Job runs measure_vram.py in the creative image against the SDXL checkpoint
the catalog entry staged in ai-models (read-only, by its exact revision). It
gets one card through its own DRA claim, selected by PCI address. DRA never
allocates a device to two claims, so the Job stays Pending while SGLang or
SwarmUI hold that card. Stop them first; the measurement needs an idle card.
Output is a JSON List: ConfigMap, ResourceClaimTemplate and Job.

Usage: measure.py IMAGE_REF [--pci 0000:c3:00.0]
"""
import argparse
import hashlib
import json
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
NAMESPACE = "ai-home-lab"
GPU_GROUPS = [987, 983]  # render, video on gr33nstein (as the SGLang serving contract)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", help="creative image pinned by digest")
    parser.add_argument("--pci", default="0000:c3:00.0", help="card to measure (gpu-1, the card SwarmUI shares)")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[a-f0-9]{64}", args.image):
        parser.error("the image must be pinned by digest")
    if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", args.pci):
        parser.error("--pci must be a PCI address like 0000:c3:00.0")
    entry = json.loads((HERE.parent / "models" / "sdxl-base-1.0.json").read_text())
    model = entry["model"]
    checkpoint = next(f["path"] for f in model["files"] if f["path"].endswith(".safetensors"))
    revision_dir = f'{model["repository"]}/{model["revision"]}'
    workflow = (ROOT / "apps/base/shared-api/sdxl-workflow.json").read_text()
    script = (HERE / "measure_vram.py").read_text()
    key = hashlib.sha256(json.dumps([args.image, args.pci, revision_dir, workflow, script]).encode()).hexdigest()[:10]
    name = f"sdxl-vram-measure-{key}"
    labels = {"app.kubernetes.io/name": "sdxl-vram-measure"}
    meta = {"name": name, "namespace": NAMESPACE, "labels": labels}
    claim = {"apiVersion": "resource.k8s.io/v1", "kind": "ResourceClaimTemplate", "metadata": meta,
             "spec": {"spec": {"devices": {"requests": [{"name": "gpu", "exactly": {
                 "deviceClassName": "gpu.amd.com", "allocationMode": "ExactCount", "count": 1,
                 "selectors": [{"cel": {"expression":
                     f'device.attributes["resource.kubernetes.io"].pciBusID == "{args.pci}"'}}]}}]}}}}
    container = {
        "name": "measure",
        "image": args.image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["/SwarmUI/dlbackend/ComfyUI/venv/bin/python", "-u", "/measure/measure_vram.py",
                    "/measure/workflow.json", "/checkpoint", args.pci],
        "env": [{"name": "HOME", "value": "/tmp"}, {"name": "HF_HUB_OFFLINE", "value": "1"}],
        "resources": {"requests": {"cpu": "8", "memory": "24Gi"}, "limits": {"cpu": "8", "memory": "24Gi"},
                      "claims": [{"name": "gpu"}]},
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "measure", "mountPath": "/measure", "readOnly": True},
                         {"name": "models", "mountPath": "/checkpoint", "subPath": revision_dir, "readOnly": True},
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
        "volumes": [{"name": "measure", "configMap": {"name": name}},
                    {"name": "models", "persistentVolumeClaim": {"claimName": "ai-models", "readOnly": True}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}}],
    }
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "ttlSecondsAfterFinished": 604800,
                    "template": {"metadata": {"labels": labels}, "spec": pod}}}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta,
              "data": {"measure_vram.py": script, "workflow.json": workflow, "checkpoint": checkpoint}}
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [config, claim, job]}, indent=2))


if __name__ == "__main__":
    main()
