"""Image smoke test, run by build-stage.sh on the build host's GPUs before push.

A CPU-only import cannot see HIP-only imports (SGLang's is_hip() is false
without a GPU), so this needs /dev/kfd and /dev/dri. It checks that the server
modules import, that dense MXFP4 takes the gfx1201 route (serving patch 0004),
and that Triton can compile and run the quantizer and FP4 GEMM, which also
proves the image has the C compiler Triton needs for its launcher. Finally it
compiles a HIP C++ kernel the way SGLang's JIT kernels are built (hipcc,
C++20, standard headers), which needs g++ and the device bitcode path. It also
calls the opt-in verify-path ports directly: split-K down/out and the wide
split-K (QKV) within the op234 limits, the packed GDN projection bitwise equal to the separate projections,
the FP8 LM head route on a small synthetic head, the fused GDN gated-norm
MXFP4 quantization bitwise equal to norm then quantize, and the split-KV verify
attention within BF16 rounding of extend_attention_fwd.
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

# Wide split-K (serving patch 0013) on the attention QKV shard at 8 rows.
qkv = torch.randn(7168, 5120, device="cuda", dtype=torch.bfloat16) * 0.05
qq, qs = mxfp4.dynamic_mxfp4_quant(qkv)
qs = qs.contiguous()
xq5, xs5 = mxfp4.dynamic_mxfp4_quant(torch.randn(8, 5120, device="cuda", dtype=torch.bfloat16))
one, split = (torch.empty(8, 7168, device="cuda", dtype=torch.bfloat16) for _ in range(2))
g.gemm_afp4wfp4(xq5, qq, xs5, qs, torch.bfloat16, one, splitk_wide=False)
g.gemm_afp4wfp4(xq5, qq, xs5, qs, torch.bfloat16, split, splitk_wide=True)
diff = (split.float() - one.float()).abs()
if not (g._splitk_parts(7168, 5120, None, True) == 4
        and diff.max() <= one.float().abs().max() * 2**-7 and (diff > 0).float().mean() <= 0.05):
    raise SystemExit("IMAGE_SMOKE=fail: wide split-K QKV GEMM outside the op234 limits")

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

from sglang.kernels.ops.attention.fla.layernorm_gated import RMSNorm as RMSNormGated
from sglang.srt.layers.quantization.quark.schemes import gfx1201_gdn_norm_quant as f1

gnorm = RMSNormGated(128, eps=1e-6, group_size=None, norm_before_gate=True, dtype=torch.bfloat16).cuda()
with torch.no_grad():
    gnorm.weight.copy_(torch.rand(128, device="cuda", dtype=torch.bfloat16) + 0.5)
xn = torch.randn(8 * 24, 128, device="cuda", dtype=torch.bfloat16)
zn = torch.randn(8 * 24, 128, device="cuda", dtype=torch.bfloat16)
p0, s0 = mxfp4.dynamic_mxfp4_quant(gnorm(xn, zn).reshape(8, 24 * 128))
p1, s1 = f1.gated_norm_mxfp4(xn, zn, gnorm.weight, gnorm.eps, 24)
if not (torch.equal(p0, p1) and torch.equal(s0, s1) and s0.stride() == s1.stride()):
    raise SystemExit("IMAGE_SMOKE=fail: fused GDN norm quantization differs from norm then quantize")

# Split-KV verify attention (serving patch 0012) against extend_attention_fwd:
# TP2 Qwen3.8 shapes, 8 draft tokens whose K/V are already in the cache.
import math

from sglang.kernels.ops.attention import verify_attention_gfx1151 as va
from sglang.kernels.ops.attention.extend_attention import extend_attention_fwd

hq, hkv, hd, tv, pv = 12, 2, 256, 8, 2048
kc = torch.randn(pv + tv + 1, hkv, hd, device="cuda", dtype=torch.bfloat16)
vc = torch.randn_like(kc)
r2t = torch.zeros(2, pv + tv, dtype=torch.int32, device="cuda")
r2t[1] = torch.randperm(pv + tv, device="cuda").to(torch.int32) + 1
qv = torch.randn(tv, hq, hd, device="cuda", dtype=torch.bfloat16)
ov, oe = torch.empty_like(qv), torch.empty_like(qv)
va.verify_attention_gfx1151(qv, ov, kc, vc, r2t, torch.tensor([1], device="cuda"), torch.tensor([pv], device="cuda"),
                            tv, 1 / math.sqrt(hd), nsplit=va.NSPLIT_GFX1201)
blk = r2t[1, pv:].long()
extend_attention_fwd(qv, kc[blk].contiguous(), vc[blk].contiguous(), oe, kc, vc,
                     torch.tensor([0, tv], device="cuda", dtype=torch.int32),
                     torch.tensor([0, pv], device="cuda", dtype=torch.int32), r2t[1, :pv].contiguous(), None, True,
                     torch.zeros(2, device="cuda", dtype=torch.int64), tv, 1.0, 1.0, sm_scale=1 / math.sqrt(hd))
if not (ov.float() - oe.float()).abs().max().item() < 1e-2:
    raise SystemExit("IMAGE_SMOKE=fail: split-KV verify attention differs from extend_attention_fwd")

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
