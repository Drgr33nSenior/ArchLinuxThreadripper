"""Creative image smoke test, run by build-stage.sh on the build host's GPUs before push.

Run with the image's ComfyUI venv python. It needs /dev/kfd and /dev/dri.
1. PyTorch sees a gfx1201 GPU and computes FP16 matmul and conv2d there
   within rounding of the CPU result.
2. ComfyUI initialises on the GPU and imports all of its built-in nodes
   (--quick-test-for-ci), with no node import failures.
3. SwarmUI starts with the repository's Backends.fds, self-starts that ComfyUI
   (no runtime pip: the image refuses it) and reports the backend up (idle or
   running) through its API.
No model is loaded; generation is qualified separately on the cluster.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

import torch

COMFY = "/SwarmUI/dlbackend/ComfyUI"
PYTHON = f"{COMFY}/venv/bin/python"


def gpu():
    assert torch.cuda.is_available(), "no GPU visible"
    arch = torch.cuda.get_device_properties(0).gcnArchName.split(":")[0]
    assert arch == "gfx1201", f"unexpected GPU {arch}"
    torch.manual_seed(0)
    a, b = torch.randn(256, 256), torch.randn(256, 256)
    got = (a.half().cuda() @ b.half().cuda()).float().cpu()
    assert torch.allclose(got, a.half().float() @ b.half().float(), atol=0.5, rtol=2e-2), "GPU matmul differs"
    x, w = torch.randn(1, 4, 32, 32), torch.randn(8, 4, 3, 3)
    got = torch.nn.functional.conv2d(x.half().cuda(), w.half().cuda(), padding=1).float().cpu()
    want = torch.nn.functional.conv2d(x.half().float(), w.half().float(), padding=1)
    assert torch.allclose(got, want, atol=0.5, rtol=2e-2), "GPU conv2d differs"
    print(f"SMOKE gpu={arch} torch={torch.__version__} hip={torch.version.hip}")


def comfy():
    dirs = {name: f"/tmp/comfy-{name}" for name in ("output", "temp", "input", "user")}
    for path in dirs.values():
        os.makedirs(path, exist_ok=True)
    result = subprocess.run([PYTHON, f"{COMFY}/main.py", "--quick-test-for-ci", "--dont-print-server",
                             "--output-directory", dirs["output"], "--temp-directory", dirs["temp"],
                             "--input-directory", dirs["input"], "--user-directory", dirs["user"]],
                            cwd=COMFY, capture_output=True, text=True, timeout=600)
    log = result.stdout + result.stderr
    failed = [line for line in log.splitlines()
              if "IMPORT FAILED" in line or "did not import correctly" in line or "Cannot import" in line]
    if result.returncode != 0 or failed:
        print(log[-6000:], file=sys.stderr)
        raise SystemExit(f"ComfyUI quick test failed (exit {result.returncode}): {failed[:5]}")
    print("SMOKE comfy=quick-test-ok")


def call(path, body):
    request = urllib.request.Request(f"http://127.0.0.1:7801{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def swarm(backends):
    os.makedirs("/SwarmUI/Data", exist_ok=True)
    shutil.copyfile(backends, "/SwarmUI/Data/Backends.fds")
    log = open("/tmp/swarm.log", "w")
    process = subprocess.Popen(["/usr/local/bin/workstation-creative"], stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    deadline, status = time.monotonic() + 600, None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SystemExit(f"SwarmUI exited with {process.returncode}")
            try:
                session = call("/API/GetNewSession", {})["session_id"]
                listed = call("/API/ListBackends", {"session_id": session, "full_data": True})
                status = [entry.get("status") for entry in listed.values()]
                if "errored" in status:
                    raise SystemExit(f"SwarmUI backend errored: {listed}")
                # BackendStatus: disabled, errored, waiting, loading, idle, running.
                if status and all(s in ("running", "idle") for s in status):
                    print(f"SMOKE swarm=backends-up {status}")
                    return
            except (OSError, KeyError, ValueError):
                pass
            time.sleep(3)
        raise SystemExit(f"SwarmUI backend not running in time (last status {status})")
    except SystemExit:
        log.flush()
        print(open("/tmp/swarm.log").read()[-8000:], file=sys.stderr)
        raise
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke.py BACKENDS_FDS")
    gpu()
    comfy()
    swarm(sys.argv[1])
    print("SMOKE=pass")
