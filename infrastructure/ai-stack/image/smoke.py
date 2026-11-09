"""Image smoke test, run by build-stage.sh on the build host's GPUs before push.

A CPU-only import cannot see HIP-only imports (SGLang's is_hip() is false
without a GPU), so this needs /dev/kfd and /dev/dri. It checks that the server
modules import, that dense MXFP4 takes the gfx1201 route (serving patch 0004),
and that Triton can compile and run the quantizer and FP4 GEMM, which also
proves the image has the C compiler Triton needs for its launcher. Finally it
compiles a HIP C++ kernel the way SGLang's JIT kernels are built (hipcc,
C++20, standard headers), which needs g++ and the device bitcode path.
"""

import os
import shutil
import subprocess
import tempfile

import torch

import sglang.srt.entrypoints.http_server  # noqa: F401
import sglang.srt.managers.scheduler  # noqa: F401
from sglang.srt.layers.quantization.quark.schemes import quark_w4a4_mxfp4 as mxfp4

if not torch.cuda.is_available():
    raise SystemExit("IMAGE_SMOKE=fail: no GPU visible")
arch = torch.cuda.get_device_properties(0).gcnArchName.split(":")[0]
if arch == "gfx1201" and not mxfp4._use_gfx1201_mxfp4:
    raise SystemExit("IMAGE_SMOKE=fail: gfx1201 is not using the MXFP4 route of patch 0004")

E2M1 = torch.tensor([0, 0.5, 1, 1.5, 2, 3, 4, 6, -0.0, -0.5, -1, -1.5, -2, -3, -4, -6])


def dequant(packed, scales):
    p = packed.to(torch.int64)
    table = E2M1.to(p.device)
    vals = torch.stack([table[p & 0xF], table[p >> 4]], dim=-1).reshape(p.shape[0], -1)
    return vals * torch.exp2(scales.float() - 127).repeat_interleave(32, dim=1)


torch.manual_seed(0)
x = torch.randn(16, 5120, device="cuda", dtype=torch.bfloat16)
w = torch.randn(256, 5120, device="cuda", dtype=torch.bfloat16) * 0.05
xq, xs = mxfp4.dynamic_mxfp4_quant(x)
wq, ws = mxfp4.dynamic_mxfp4_quant(w)
ws = ws.contiguous()
y = torch.empty(16, 256, device="cuda", dtype=torch.bfloat16)
mxfp4.gemm_afp4wfp4(xq, wq, xs, ws, torch.bfloat16, y)
ref = dequant(xq, xs) @ dequant(wq, ws).T
rel = ((y.float() - ref).abs().max() / ref.abs().max()).item()
if not rel < 1e-2:
    raise SystemExit(f"IMAGE_SMOKE=fail: FP4 GEMM relative error {rel:.2e}")

if shutil.which("c++") is None:
    raise SystemExit("IMAGE_SMOKE=fail: no c++ for SGLang JIT compiler fingerprinting")
with tempfile.TemporaryDirectory() as work:
    source = os.path.join(work, "jit.hip")
    with open(source, "w") as handle:
        handle.write("#include <hip/hip_runtime.h>\n#include <cmath>\n"
                     "__global__ void k(float* x) { x[0] = std::sqrt(2.0f); }\n")
    hipcc = os.path.join(os.environ["ROCM_PATH"], "bin", "hipcc")
    built = subprocess.run([hipcc, f"--offload-arch={arch}", "-std=c++20", "-O3", "-fPIC", "-c", source,
                            "-o", os.path.join(work, "jit.o")], capture_output=True, text=True)
    if built.returncode != 0:
        raise SystemExit("IMAGE_SMOKE=fail: HIP C++ JIT compile failed: " + built.stderr.strip()[-400:])
print(f"IMAGE_SMOKE=ok {arch} torch {torch.__version__} gemm_rel_err {rel:.1e}")
