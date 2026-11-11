"""Measure SDXL's VRAM peak and render times on one card (runs inside the creative image).

Run by the Job that measure.py renders, with the ComfyUI venv's python:
  measure_vram.py WORKFLOW_JSON CHECKPOINT_DIR PCI_ADDRESS

Starts the image's ComfyUI on the claimed card, then runs the fixed SDXL
workflow (apps/base/shared-api/sdxl-workflow.json) with SwarmUI's output node
swapped for ComfyUI's SaveImage: one cold 1024x1024 render (including model
load), three warm 1024x1024 renders and one 512x512 render. A thread samples the
card's own VRAM counter (sysfs mem_info_vram_used, the whole card, which is what
matters when SDXL shares it with SGLang) every 50 ms; the result also records
the idle baseline before ComfyUI starts, so the card must be otherwise unused.

ComfyUI keeps models resident when VRAM is plentiful, so this peak is an upper
bound; with less free VRAM it offloads and tiles VAE decode, using less memory
but running slower. Prints one JSON line prefixed RESULT=.

Variant (environment, set by measure.py):
  MEASURE_DECODE=full|tiled  ComfyUI's VAEDecode, or VAEDecodeTiled (512 px tiles,
                             64 px overlap) to cap the decode spike
  COMFYUI_ENABLE_MIOPEN=1    keep MIOpen (cuDNN) on; ComfyUI turns it off on RDNA3+
  MEASURE_HOLD_SECONDS=N     stay alive N seconds after RESULT so the images in
                             /tmp/comfy-output can be copied out (seam review)
  MEASURE_COMFY_FLAGS="..."  extra ComfyUI memory flags, each one of COMFY_FLAGS
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

COMFY = "/SwarmUI/dlbackend/ComfyUI"
PORT = 8188
PROMPT = "a red wooden cabin beside a calm mountain lake at sunrise, pine trees, photograph"
NEGATIVE = "blurry, low quality, text, watermark"
# ComfyUI flags a variant may add: fp8 weight storage, and text encoders on the CPU
# (--lowvram only moves them when dynamic VRAM is off).
COMFY_FLAGS = ("--fp8_e4m3fn-unet", "--fp8_e4m3fn-text-enc", "--lowvram", "--disable-dynamic-vram")
# ComfyUI startup lines that say which memory and attention mode it chose.
LOG_FACTS = ("vram state", "dynamic", "aimdo", "attention", "fp8", "weight dtype", "cudnn", "miopen", "text encoder")


class Sampler(threading.Thread):
    def __init__(self, counter):
        super().__init__(daemon=True)
        self.counter, self.peak, self.running = counter, 0, True

    def read(self):
        with open(self.counter) as f:
            return int(f.read())

    def run(self):
        while self.running:
            self.peak = max(self.peak, self.read())
            time.sleep(0.05)


def api(path, body=None):
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def workflow(template, size, seed, decode="full"):
    graph = json.loads(json.dumps(template))
    if decode == "tiled":
        graph["6"] = {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["5", 0], "vae": ["1", 2], "tile_size": 512,
                                                                 "overlap": 64, "temporal_size": 64, "temporal_overlap": 8}}
    elif decode != "full":
        raise SystemExit(f"unknown decode variant {decode!r}")
    graph["1"]["inputs"]["ckpt_name"] = "sd_xl_base_1.0.safetensors"
    graph["2"]["inputs"]["text"], graph["3"]["inputs"]["text"] = PROMPT, NEGATIVE
    graph["4"]["inputs"]["width"] = graph["4"]["inputs"]["height"] = size
    graph["5"]["inputs"]["seed"] = seed
    # SwarmSaveImageWS is SwarmUI's node; plain ComfyUI saves with SaveImage.
    graph["7"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": f"measure-{size}"}}
    return graph


def render(graph, sampler):
    sampler.peak = sampler.read()
    started = time.monotonic()
    prompt_id = api("/prompt", {"prompt": graph})["prompt_id"]
    while True:
        history = api(f"/history/{prompt_id}")
        if prompt_id in history:
            status = history[prompt_id].get("status", {})
            if status.get("status_str") != "success":
                raise SystemExit(f"render failed: {status}")
            return time.monotonic() - started, sampler.peak
        time.sleep(0.2)


def main(workflow_path, checkpoint_dir, pci):
    decode = os.environ.get("MEASURE_DECODE", "full")
    miopen = os.environ.get("COMFYUI_ENABLE_MIOPEN") == "1"
    flags = os.environ.get("MEASURE_COMFY_FLAGS", "").split()
    unknown = [f for f in flags if f not in COMFY_FLAGS]
    if unknown:
        raise SystemExit(f"unknown ComfyUI flags {unknown}")
    counter = f"/sys/bus/pci/devices/{pci}/mem_info_vram_used"
    total = int(open(f"/sys/bus/pci/devices/{pci}/mem_info_vram_total").read())
    template = json.load(open(workflow_path))
    sampler = Sampler(counter)
    baseline = sampler.read()
    paths = "/tmp/extra_model_paths.yaml"
    with open(paths, "w") as f:
        f.write(f"measure:\n  checkpoints: {checkpoint_dir}\n")
    for name in ("output", "temp", "input", "user"):
        os.makedirs(f"/tmp/comfy-{name}", exist_ok=True)
    log = open("/tmp/comfy.log", "w")
    comfy = subprocess.Popen([sys.executable, f"{COMFY}/main.py", "--listen", "127.0.0.1", "--port", str(PORT),
                              "--extra-model-paths-config", paths, "--output-directory", "/tmp/comfy-output",
                              "--temp-directory", "/tmp/comfy-temp", "--input-directory", "/tmp/comfy-input",
                              "--user-directory", "/tmp/comfy-user", "--disable-auto-launch", *flags],
                             cwd=COMFY, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 300
        while True:
            try:
                api("/system_stats")
                break
            except OSError:
                if comfy.poll() is not None or time.monotonic() > deadline:
                    log.flush()
                    print(open("/tmp/comfy.log").read()[-6000:], file=sys.stderr)
                    raise SystemExit("ComfyUI did not start")
                time.sleep(1)
        sampler.start()
        runs = []
        for label, size, seed in (("cold-1024", 1024, 1), ("warm-1024", 1024, 2), ("warm-1024", 1024, 3),
                                  ("warm-1024", 1024, 4), ("warm-512", 512, 5)):
            seconds, peak = render(workflow(template, size, seed, decode), sampler)
            runs.append({"run": label, "size": size, "seconds": round(seconds, 2), "card_peak_mib": peak >> 20})
            print(f"RUN {label} {size}px {seconds:.2f}s card_peak={peak >> 20} MiB", flush=True)
        sampler.running = False
        overall = max(r["card_peak_mib"] for r in runs)
        log.flush()
        for line in open("/tmp/comfy.log", errors="replace"):
            if any(fact in line.lower() for fact in LOG_FACTS):
                print("COMFY " + line.rstrip()[:200], flush=True)
        result = {"schema": 1, "variant": {"decode": decode, "miopen": miopen, "comfy_flags": flags},
                  "pci": pci, "card_total_mib": total >> 20, "baseline_mib": baseline >> 20,
                  "peak_mib": overall, "sdxl_peak_above_baseline_mib": overall - (baseline >> 20),
                  "runs": runs, "steps": template["5"]["inputs"]["steps"],
                  "sampler": template["5"]["inputs"]["sampler_name"], "note": "upper bound: ComfyUI keeps models resident when VRAM is free"}
        print("RESULT=" + json.dumps(result), flush=True)
        hold = int(os.environ.get("MEASURE_HOLD_SECONDS", "0"))
        if hold > 0:
            print(f"HOLD {hold}s: images in /tmp/comfy-output", flush=True)
            time.sleep(hold)
    finally:
        comfy.terminate()
        try:
            comfy.wait(timeout=30)
        except subprocess.TimeoutExpired:
            comfy.kill()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
