"""Image smoke test, run by build-stage.sh on the build host's GPUs before push.

A CPU-only import cannot see HIP-only imports (SGLang's is_hip() is false
without a GPU), so this needs /dev/kfd and /dev/dri. It checks that the server
modules import, that dense MXFP4 takes the gfx1201 route (serving patch 0004),
and that Triton can compile and run the quantizer and FP4 GEMM, which also
proves the image has the C compiler Triton needs for its launcher. Finally it
compiles a HIP C++ kernel the way SGLang's JIT kernels are built (hipcc,
C++20, standard headers), which needs g++ and the device bitcode path. It also
calls the opt-in verify-path ports directly: split-K down/out within the op234
limits, the packed GDN projection bitwise equal to the separate projections,
and the FP8 LM head route on a small synthetic head.
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

# Verify-path ports (serving patches 0006, 0007, 0009), called directly so the
# smoke test covers them although the image leaves them off by default.
from sglang.srt.layers import gfx1201_fp8_lm_head as fp8_head
from sglang.srt.layers.quantization.quark.schemes import gfx1201_mxfp4 as g

down = torch.randn(5120, 8704, device="cuda", dtype=torch.bfloat16) * 0.05
dq, ds = mxfp4.dynamic_mxfp4_quant(down)
ds = ds.contiguous()
x8 = torch.randn(8, 8704, device="cuda", dtype=torch.bfloat16)
xq8, xs8 = mxfp4.dynamic_mxfp4_quant(x8)
one, split = (torch.empty(8, 5120, device="cuda", dtype=torch.bfloat16) for _ in range(2))
g.gemm_afp4wfp4(xq8, dq, xs8, ds, torch.bfloat16, one, splitk_downout=False)
g.gemm_afp4wfp4(xq8, dq, xs8, ds, torch.bfloat16, split, splitk_downout=True)
diff = (split.float() - one.float()).abs()
if not (diff.max() <= one.float().abs().max() * 2**-7 and (diff > 0).float().mean() <= 0.05):
    raise SystemExit("IMAGE_SMOKE=fail: split-K down/out GEMM outside the op234 limits")

qkvz = torch.randn(256, 5120, device="cuda", dtype=torch.bfloat16) * 0.05
ba = torch.randn(48, 5120, device="cuda", dtype=torch.bfloat16) * 0.05
(qw, qs), (bw, bs) = (mxfp4.dynamic_mxfp4_quant(w) for w in (qkvz, ba))
qs, bs = qs.contiguous(), bs.contiguous()
xg = torch.randn(8, 5120, device="cuda", dtype=torch.bfloat16)
xq, xs = mxfp4.dynamic_mxfp4_quant(xg)
a, b, packed = (torch.empty(8, n, device="cuda", dtype=torch.bfloat16) for n in (256, 48, 304))
g.gemm_afp4wfp4(xq, qw, xs, qs, torch.bfloat16, a)
g.gemm_afp4wfp4(xq, bw, xs, bs, torch.bfloat16, b)
g.gemm_afp4wfp4(xq, torch.cat([qw, bw]).contiguous(), xs, torch.cat([qs, bs]).contiguous(), torch.bfloat16, packed)
if not (torch.equal(packed[:, :256], a) and torch.equal(packed[:, 256:], b)):
    raise SystemExit("IMAGE_SMOKE=fail: packed GDN projection differs from the separate projections")

head_w = torch.randn(1024, 5120, device="cuda", dtype=torch.bfloat16) * 0.02
b8, b8_scales = fp8_head._quantize_weight(head_w)
head = type("Head", (), {})()
head.weight = head_w
setattr(head, fp8_head._ATTR, (head_w, head_w.data_ptr(), b8, b8_scales))
xh = torch.randn(8, 5120, device="cuda", dtype=torch.bfloat16)
logits = fp8_head.project(xh, head, torch.float32)
ref = torch.mm(xh, head_w.T, out_dtype=torch.float32)
if logits is None or not torch.isfinite(logits).all() or (
        (logits - ref).abs().max() > 0.05 * (ref.max() - ref.min())):
    raise SystemExit("IMAGE_SMOKE=fail: FP8 LM head route missing or outside 5% of the logit range")
if fp8_head.project(torch.randn(9, 5120, device="cuda", dtype=torch.bfloat16), head, torch.float32) is not None:
    raise SystemExit("IMAGE_SMOKE=fail: FP8 LM head route took rows it does not cover")

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
