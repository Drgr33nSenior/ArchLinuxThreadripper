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
attention within BF16 rounding of extend_attention_fwd. It runs the P1 prefill
route on one shard (exact FP8 expansion, error against an FP32 reference) and
8-warp prefill attention against the 4-warp kernel, the FP8-draft fused KV
weight against the FP8 layer, and the fused split-K reduce against the
two-kernel path (bitwise, with its compiled-code ordering check). With two GPUs
it runs the PCIe custom all-reduce in two processes against the exact sum.
"""

import os
import shutil
import subprocess
import sys
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
# The one-request tile (serving patch 0021: BT 64, 8 warps, 64-column q chunks).
ow = torch.empty_like(qv)
va.verify_attention_gfx1151(qv, ow, kc, vc, r2t, torch.tensor([1], device="cuda"), torch.tensor([pv], device="cuda"),
                            tv, 1 / math.sqrt(hd), nsplit=va.NSPLIT_GFX1201, bt=va.GFX1201_B1_TILE[0],
                            num_warps=va.GFX1201_B1_TILE[1], qc=va.GFX1201_B1_TILE[2])
if not (ow.float() - oe.float()).abs().max().item() < 1e-2:
    raise SystemExit("IMAGE_SMOKE=fail: one-request verify attention tile differs from extend_attention_fwd")
# The 32 x 32 prefill-attention tile (serving patch 0023), switched on for one call:
# it must take the route and stay within BF16 rounding of the default tile.
from sglang.kernels.ops.attention import extend_attention as ea

os.environ["SGLANG_GFX1201_EXTEND_TILE32"] = "1"
o32 = torch.empty_like(qv)
extend_attention_fwd(qv, kc[blk].contiguous(), vc[blk].contiguous(), o32, kc, vc,
                     torch.tensor([0, tv], device="cuda", dtype=torch.int32),
                     torch.tensor([0, pv], device="cuda", dtype=torch.int32), r2t[1, :pv].contiguous(), None, True,
                     torch.zeros(2, device="cuda", dtype=torch.int64), tv, 1.0, 1.0, sm_scale=1 / math.sqrt(hd))
del os.environ["SGLANG_GFX1201_EXTEND_TILE32"]
if not ea._EXTEND_TILE32_LOGGED or not (o32.float() - oe.float()).abs().max().item() < 1e-2:
    raise SystemExit("IMAGE_SMOKE=fail: 32 x 32 extend attention tile missing or differs from the default tile")

# P1 prefill route (serving patch 0015) on one 5120x3072 shard at 1024 rows: the
# FP8 expansion must equal the dequantized MXFP4 weight exactly, and the output
# must be within FP8 error of an FP32 reference (MXFP4 activations give ~11%).
from sglang.srt.layers.quantization.quark.schemes import gfx1201_p1_prefill as p1

pn, pk = 5120, 3072
pw, ps = g.dynamic_mxfp4_quant(torch.randn(pn, pk, device="cuda", dtype=torch.bfloat16) * 0.02)
player = torch.nn.Module()
player.weight = torch.nn.Parameter(pw.contiguous(), requires_grad=False)
player.weight_scale = torch.nn.Parameter(ps.contiguous(), requires_grad=False)
route = p1.PrefillRoute(torch, p1.SETTING)
route.validate_layer(player, pn, pk)
px = torch.randn(1024, pk, device="cuda", dtype=torch.bfloat16)
py = route.apply(player, px, pn, pk).float()
pref_w = mxfp4._dequant_mxfp4_to_bf16(player.weight, player.weight_scale).float()
pexp, pbs = route._views(torch.cuda.current_device(), 1024, pn, pk)[:2]
if not torch.equal(pexp.float() * pbs.repeat_interleave(128, 0).repeat_interleave(128, 1), pref_w):
    raise SystemExit("IMAGE_SMOKE=fail: P1 FP8 weight expansion is not exact")
pref = px.float() @ pref_w.T
if not (py - pref).norm().item() / pref.norm().item() < 0.05:
    raise SystemExit("IMAGE_SMOKE=fail: P1 prefill GEMM differs from the FP32 reference")

# 8-warp prefill attention (serving patch 0016) against the 4-warp kernel:
# 256 new tokens after a 2048-token prefix, TP2 Qwen3.8 shapes.
ep, ee = 2048, 256
ekc = torch.randn(ep + ee + 1, hkv, hd, device="cuda", dtype=torch.bfloat16)
evc = torch.randn_like(ekc)
eq = torch.randn(ee, hq, hd, device="cuda", dtype=torch.bfloat16)
eouts = []
for warps8 in ("0", "1"):
    os.environ["SGLANG_GFX1201_EXTEND_WARPS8"] = warps8
    eo = torch.empty_like(eq)
    extend_attention_fwd(eq, ekc[ep + 1:].contiguous(), evc[ep + 1:].contiguous(), eo, ekc, evc,
                         torch.tensor([0, ee], device="cuda", dtype=torch.int32),
                         torch.tensor([0, ep], device="cuda", dtype=torch.int32),
                         torch.arange(1, ep + 1, device="cuda", dtype=torch.int64), None, True,
                         torch.zeros(2, device="cuda", dtype=torch.int64), ee, 1.0, 1.0, sm_scale=1 / math.sqrt(hd))
    eouts.append(eo.float())
del os.environ["SGLANG_GFX1201_EXTEND_WARPS8"]
if not (eouts[0] - eouts[1]).abs().max().item() < 1e-2:
    raise SystemExit("IMAGE_SMOKE=fail: 8-warp prefill attention differs from the 4-warp kernel")

# DFLASH fused KV for an FP8 draft (serving patch 0017): an online-FP8 QKV layer
# (non-square, so the (K, N) storage cannot pass by accident) is admitted only
# with the switch, and its dequantized K/V rows match the FP8 layer's K/V output.
from sglang.kernels.ops.speculative.fused_kv_materialize import _dense_kv_rows
from sglang.srt.layers.quantization.fp8 import Fp8Config, Fp8LinearMethod
from sglang.srt.speculative import dflash_utils

fq, fkv, fk = 1024, 256, 2048
fmethod = Fp8LinearMethod(Fp8Config(is_checkpoint_fp8_serialized=False))
flayer = torch.nn.Module()
fmethod.create_weights(flayer, input_size_per_partition=fk, output_partition_sizes=[fq, fkv, fkv], input_size=fk,
                       output_size=fq + 2 * fkv, params_dtype=torch.bfloat16, weight_loader=None)
flayer.weight.data = (torch.randn(fq + 2 * fkv, fk, device="cuda", dtype=torch.bfloat16) * 0.02)
flayer = flayer.cuda()
flayer.quant_method, flayer.output_size_per_partition, flayer.bias = fmethod, fq + 2 * fkv, None
fmethod.process_weights_after_loading(flayer)
os.environ["SGLANG_DFLASH_FUSED_KV_FP8"] = "0"
fkv_off = dflash_utils.can_dflash_use_fused_qkv_proj(flayer)[0]
os.environ["SGLANG_DFLASH_FUSED_KV_FP8"] = "1"
fkv_on = dflash_utils.can_dflash_use_fused_qkv_proj(flayer)[0]
del os.environ["SGLANG_DFLASH_FUSED_KV_FP8"]
if fkv_off or not fkv_on:
    raise SystemExit(f"IMAGE_SMOKE=fail: FP8 draft fused KV eligibility off={fkv_off} on={fkv_on}")
fx = torch.randn(32, fk, device="cuda", dtype=torch.bfloat16)
fdense = (fx @ _dense_kv_rows(flayer, fq, fq + 2 * fkv, torch.bfloat16).T).float()
flayer_kv = fmethod.apply(flayer, fx)[:, fq:].float()
if not (fdense - flayer_kv).norm().item() / flayer_kv.norm().item() < 0.08:
    raise SystemExit("IMAGE_SMOKE=fail: FP8 draft fused KV weight differs from the FP8 layer")

# Fused split-K reduce (serving patch 0018): bitwise equal to GEMM + reduce kernel on
# an out-projection shard at M8, and the compiled variant passes the ordering check
# (store drain before the barrier, device-scope atomic and reloads).
sn, sk = 5120, 3072
swq, sws = g.dynamic_mxfp4_quant(torch.randn(sn, sk, device="cuda", dtype=torch.bfloat16) * 0.05)
sxq, sxs = g.dynamic_mxfp4_quant(torch.randn(8, sk, device="cuda", dtype=torch.bfloat16))
souts = []
for fused in (False, True):
    g.SPLITK_FUSED_REDUCE = fused
    sy = torch.empty(8, sn, device="cuda", dtype=torch.bfloat16)
    g.gemm_afp4wfp4(sxq, swq, sxs, sws.contiguous(), torch.bfloat16, sy, splitk_downout=True)
    souts.append(sy)
g.SPLITK_FUSED_REDUCE = False
if not g._fused_asm_checked.get((torch.cuda.current_device(), 8, sn, sk, 4)):
    raise SystemExit("IMAGE_SMOKE=fail: fused split-K reduce failed its compiled-code ordering check")
if not torch.equal(souts[0], souts[1]):
    raise SystemExit("IMAGE_SMOKE=fail: fused split-K reduce differs from the two-kernel path")

# kq3 verify GEMM (serving patch 0022) on the same out-projection shard: the records
# unpack to the weight exactly, and the kernel is bitwise equal to the split-K path at
# 8 and 16 rows. The split-K switch is set here because the image leaves it off.
import types
from sglang.srt.layers.quantization.quark.schemes import gfx1201_mxfp4_kq3 as kq3
downout = g.SPLITK_DOWNOUT
g.SPLITK_DOWNOUT = True
kholder = types.SimpleNamespace()
krec = kq3.pack_weight(swq, sws.contiguous(), ((kholder, 0),))
if krec is None:
    raise SystemExit("IMAGE_SMOKE=fail: kq3 did not admit the out-projection shard")
kc = krec.canonical()
if not (torch.equal(kc.weight, swq) and torch.equal(kc.weight_scale, sws.contiguous())):
    raise SystemExit("IMAGE_SMOKE=fail: kq3 records do not unpack to the weight")
for km in (8, 16):
    kxq, kxs = g.dynamic_mxfp4_quant(torch.randn(km, sk, device="cuda", dtype=torch.bfloat16))
    kref, kout = (torch.empty(km, sn, device="cuda", dtype=torch.bfloat16) for _ in range(2))
    g.gemm_afp4wfp4(kxq, swq, kxs, sws.contiguous(), torch.bfloat16, kref)
    if not (kq3.admits(krec, kxq, kxs, torch.bfloat16) and torch.equal(kq3.gemm(krec, kxq, kxs, kout), kref)):
        raise SystemExit(f"IMAGE_SMOKE=fail: kq3 differs from the split-K GEMM at {km} rows")
g.SPLITK_DOWNOUT = downout

# P1 expansion from kq3 records (serving patch 0024) on the P1 shard above: the
# records of its weight must expand, bit for bit, to P1's validated expansion of the
# canonical pair (validate_records raises otherwise), and P1's 1024-row output must
# equal the canonical route's.
g.SPLITK_DOWNOUT = True
pholder = types.SimpleNamespace()
precs = kq3.pack_weight(pw.contiguous(), ps.contiguous(), ((pholder, 0),))
g.SPLITK_DOWNOUT = downout
if precs is None:
    raise SystemExit("IMAGE_SMOKE=fail: kq3 did not admit the P1 shard")
route2 = p1.PrefillRoute(torch, p1.SETTING)
route2.validate_layer(precs.canonical(), pn, pk)
route2.validate_records(precs, pn, pk)
py2 = route2.apply(precs.canon, px, pn, pk, records=precs).float()
if not (route2.has_records(precs) and torch.equal(py2, py)):
    raise SystemExit("IMAGE_SMOKE=fail: P1 expansion from kq3 records differs from the canonical route")

# Fused add + Gemma RMSNorm + MXFP4 (serving patch 0019): bitwise equal to the add+norm
# kernel followed by the MXFP4 quantizer, residual included.
from sglang.kernels.ops.layernorm.minimax_m3_rmsnorm import gemma_fused_add_rmsnorm
from sglang.srt.layers.quantization.quark.schemes.gfx1201_add_norm_mxfp4 import gemma_add_rmsnorm_mxfp4

nw = (torch.randn(5120, device="cuda") * 0.1).to(torch.bfloat16)
nh = torch.randn(16, 5120, device="cuda").to(torch.bfloat16)
nr = torch.randn(16, 5120, device="cuda").to(torch.bfloat16)
nout, nres = gemma_fused_add_rmsnorm(nh, nr, nw, 1e-6, round_sum=True)
nq, ns = g.dynamic_mxfp4_quant(nout)
(fq, fs), fres = gemma_add_rmsnorm_mxfp4(nh, nr, nw, 1e-6)
if not (torch.equal(nq, fq) and torch.equal(ns, fs) and torch.equal(nres, fres) and ns.stride() == fs.stride()):
    raise SystemExit("IMAGE_SMOKE=fail: fused add+norm+MXFP4 differs from add+norm then quantize")

# Fused SiLU*mul + MXFP4 (serving patch 0020): bitwise equal to act_and_mul followed by the
# MXFP4 quantizer at a TP2 down-projection width.
from sglang.srt.layers.activation import SiluAndMul
from sglang.srt.layers.quantization.quark.schemes.gfx1201_silu_mul_mxfp4 import silu_mul_mxfp4

sgu = (torch.randn(16, 2 * 8704, device="cuda") * 3).to(torch.bfloat16)
sq, ss = g.dynamic_mxfp4_quant(SiluAndMul()(sgu))
fq2, fs2 = silu_mul_mxfp4(sgu)
if not (torch.equal(sq, fq2) and torch.equal(ss, fs2) and ss.stride() == fs2.stride()):
    raise SystemExit("IMAGE_SMOKE=fail: fused SiLU*mul+MXFP4 differs from act_and_mul then quantize")

# PCIe custom all-reduce (serving patch 0014) on both GPUs: the AOT-built kernel
# must give the exact fixed-order sum on both ranks. The stock kernel gave wrong,
# rank-divergent sums here.
_CAR_RANK = """
import os, sys, torch, torch.distributed as dist
rank = int(sys.argv[1])
os.environ.update(MASTER_ADDR="127.0.0.1", MASTER_PORT="29599", SGLANG_GFX1201_PCIE_CUSTOM_AR="1")
torch.cuda.set_device(rank)
dist.init_process_group("gloo", rank=rank, world_size=2)
from sglang.srt.distributed.device_communicators.custom_all_reduce import CustomAllreduce
ca = CustomAllreduce(group=dist.group.WORLD, device=rank)
if ca.disabled or not ca.pcie_two_gpu:
    sys.exit(3)
gens = [torch.Generator(device=f"cuda:{rank}").manual_seed(41 + r) for r in (0, 1)]
base = [torch.randn(16, 5120, device=f"cuda:{rank}", generator=g) for g in gens]
bad = 0
for it in range(500):
    s = 0.5 + it * 0.003
    out = ca.custom_all_reduce((base[rank] * s).to(torch.bfloat16))
    want = ((base[0] * s).to(torch.bfloat16).float() + (base[1] * s).to(torch.bfloat16).float()).to(torch.bfloat16)
    bad += int((out != want).sum())
ca.close()
dist.destroy_process_group()
sys.exit(0 if bad == 0 else 4)
"""
if torch.cuda.device_count() == 2:
    # stderr goes to files: SGLang's import logging could fill a pipe and block a rank.
    logs = [tempfile.TemporaryFile(mode="w+") for _ in (0, 1)]
    ranks = [subprocess.Popen([sys.executable, "-c", _CAR_RANK, str(r)], stdout=subprocess.DEVNULL, stderr=logs[r])
             for r in (0, 1)]
    codes = []
    for proc in ranks:
        try:
            proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        codes.append(proc.returncode)
    if codes != [0, 0]:
        for log in logs:
            log.seek(0)
        tail = " | ".join(log.read().strip()[-300:] for log in logs)
        raise SystemExit(f"IMAGE_SMOKE=fail: PCIe custom all-reduce exit codes {codes} (3 not enabled, 4 wrong sums): {tail}")

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
