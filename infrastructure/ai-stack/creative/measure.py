#!/usr/bin/env python3
"""Render the Job that measures SDXL's VRAM peak and render times on one card.

The Job runs measure_vram.py in the creative image against the SDXL checkpoint
the catalog entry staged in ai-models (read-only, by its exact revision). It
gets one card through its own DRA claim, selected by PCI address. DRA never
allocates a device to two claims, so the Job stays Pending while SGLang or
SwarmUI hold that card. Stop them first; the measurement needs an idle card.
Output is a JSON List: ConfigMap, ResourceClaimTemplate and Job.

Usage: measure.py IMAGE_REF [--pci 0000:c3:00.0] [--decode full|tiled] [--miopen] [--hold-seconds N]
                  [--fp8-unet] [--fp8-text-enc] [--text-enc-cpu]
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
    parser.add_argument("--decode", choices=("full", "tiled"), default="full", help="VAEDecode or VAEDecodeTiled (512 px)")
    parser.add_argument("--miopen", action="store_true", help="keep MIOpen on (COMFYUI_ENABLE_MIOPEN=1)")
    parser.add_argument("--hold-seconds", type=int, default=0, help="keep the pod after RESULT so images can be copied")
    parser.add_argument("--fp8-unet", action="store_true", help="store UNet weights in fp8 (--fp8_e4m3fn-unet)")
    parser.add_argument("--fp8-text-enc", action="store_true", help="store text encoder weights in fp8 (--fp8_e4m3fn-text-enc)")
    parser.add_argument("--text-enc-cpu", action="store_true",
                        help="run the text encoders on the CPU (--disable-dynamic-vram --lowvram)")
    args = parser.parse_args()
    if not 0 <= args.hold_seconds <= 900:
        parser.error("--hold-seconds must be 0..900")
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
    comfy_flags = ((["--fp8_e4m3fn-unet"] if args.fp8_unet else []) + (["--fp8_e4m3fn-text-enc"] if args.fp8_text_enc else [])
                   + (["--disable-dynamic-vram", "--lowvram"] if args.text_enc_cpu else []))
    variant = (args.decode + ("-miopen" if args.miopen else "") + ("-fp8u" if args.fp8_unet else "")
               + ("-fp8te" if args.fp8_text_enc else "") + ("-tecpu" if args.text_enc_cpu else ""))
    key = hashlib.sha256(json.dumps([args.image, args.pci, revision_dir, workflow, script, variant,
                                     args.hold_seconds]).encode()).hexdigest()[:10]
    name = f"sdxl-vram-{variant}-{key}"
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
        "env": [{"name": "HOME", "value": "/tmp"}, {"name": "HF_HUB_OFFLINE", "value": "1"},
                {"name": "MEASURE_DECODE", "value": args.decode},
                {"name": "MEASURE_HOLD_SECONDS", "value": str(args.hold_seconds)},
                {"name": "MEASURE_COMFY_FLAGS", "value": " ".join(comfy_flags)}]
               + ([{"name": "COMFYUI_ENABLE_MIOPEN", "value": "1"}] if args.miopen else []),
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
