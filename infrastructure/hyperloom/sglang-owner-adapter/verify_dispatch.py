"""Metadata-only capture of the pinned grouped verification stage-1 launcher."""
import ast
import hashlib
import types

from gemm_dispatch import Kernel, Tensor, require

SOURCE_SHA256 = "34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a"
SOURCE_PATH = "/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py"
CASES = (("p119990-rows8-sequential", (119990,)),
         ("p119990-rows8-even-odd", (119990,)),
         ("p59993-59980-rows16", (59993, 59980)), ("p724-rows8", (724,)))


def derive_prefetch(raw):
    require(hashlib.sha256(raw).hexdigest() == SOURCE_SHA256, "active BF16 decode source pin")
    text = raw.decode()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)
                and n.name == "_fwd_grouped_kernel_stage1")
    lines = text.splitlines(keepends=True)
    body = "".join(lines[node.lineno - 1:node.end_lineno])
    old = """        for start_n in tl.range(split_kv_start, split_kv_end, BLOCK_N):
            offs_n = start_n + tl.arange(0, BLOCK_N)
            kv_loc = tl.load(
                kv_indices + cur_batch_kv_start_idx + offs_n,
                mask=offs_n < split_kv_end,
                other=0,
            )
"""
    new = """        first_n = split_kv_start + tl.arange(0, BLOCK_N)
        kv_loc = tl.load(
            kv_indices + cur_batch_kv_start_idx + first_n,
            mask=first_n < split_kv_end,
            other=0,
        )
        for start_n in tl.range(split_kv_start, split_kv_end, BLOCK_N):
            offs_n = start_n + tl.arange(0, BLOCK_N)
            next_n = start_n + BLOCK_N + tl.arange(0, BLOCK_N)
            next_kv_loc = tl.load(
                kv_indices + cur_batch_kv_start_idx + next_n,
                mask=next_n < split_kv_end,
                other=0,
            )
"""
    tail = "            e_sum = e_sum * re_scale + tl.sum(p, 1)\n            e_max = n_e_max\n"
    require(body.count(old) == body.count(tail) == 1, "single grouped prefix loop and recurrence anchors")
    body = body.replace(old, new).replace(tail, tail + "            kv_loc = next_kv_loc\n")
    lines[node.lineno - 1:node.end_lineno] = [body]
    candidate = "".join(lines).encode()
    # Everything outside the one kernel is structurally identical.
    a, b = ast.parse(raw), ast.parse(candidate)
    keep = lambda tree: [ast.dump(n) for n in tree.body
                         if not (isinstance(n, ast.FunctionDef) and n.name == "_fwd_grouped_kernel_stage1")]
    require(keep(a) == keep(b), "prefetch must not alter host dispatch or another kernel")
    return candidate


def capture(raw, prefixes, card=0):
    require(hashlib.sha256(raw).hexdigest() == SOURCE_SHA256, "active BF16 decode source pin")
    require(prefixes in [r[1] for r in CASES] and type(card) is int and card in (0, 1), "frozen case/card")
    names = {"_grouped_head_tiles", "_extract_kv_strides", "_decode_grouped_att_m_fwd"}
    tree = ast.parse(raw, filename=SOURCE_PATH)
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    require({node.name for node in tree.body} == names, "pinned grouped host function closure")
    kernel = Kernel()
    def unpack(score_mod, auxiliary):
        require(score_mod is auxiliary is None, "no score modifier or auxiliary")
        return None, 0, 0, 0
    namespace = dict(_is_hip=True, _is_gfx1250=False, _GROUPED_BLOCK_H=16, _MIN_BLOCK_KV=32,
                     triton=types.SimpleNamespace(cdiv=lambda a, b: (a + b - 1) // b,
                                                  next_power_of_2=lambda n: 1 << (n - 1).bit_length()),
                     _fwd_grouped_kernel_stage1=kernel, unpack_aux_tensors=unpack)
    exec(compile(tree, SOURCE_PATH, "exec"), namespace)
    rows = 8 * len(prefixes)
    def tensor(shape, dtype, pointer):
        stride, total = [], 1
        for size in reversed(shape):
            stride.insert(0, total)
            total *= size
        value = Tensor(shape, dtype, stride, card, pointer)
        value.ndim = len(shape)
        return value
    q = tensor((rows, 12, 256), "bfloat16", 16)
    k = tensor((262144, 2, 256), "bfloat16", 32)
    v = tensor((262144, 2, 256), "bfloat16", 48)
    partial = tensor((rows, 12, 16, 256), "float32", 64)
    lse = tensor((rows, 12, 16), "float32", 80)
    indptr = tensor((rows + 1,), "int32", 96)
    indices = tensor((rows * (131040 + 8),), "int64", 112)
    splits = tensor((rows,), "int32", 128)
    namespace["_decode_grouped_att_m_fwd"](q, k, v, partial, lse, indptr, indices, splits,
                                          16, 0.0625, 0.0)
    require(len(kernel.calls) == 1, "one stage-1 dispatch")
    launch = kernel.calls[0]
    require(launch["grid"] == [rows, 2, 16], "selected grouped launch geometry")
    require(launch["kwargs"]["USE_FORCED"] is False and launch["kwargs"]["BLOCK_N"] == 32,
            "native singleton split selection and N32")
    return dict(prefixes=list(prefixes), rows=rows, launch=launch,
                metadata_values_observed=False, split_values_observed=False,
                note="Prefix and index order do not specialize this kernel; tensor contents need a later GPU oracle.")


def capture_cases(raw):
    result = {}
    for name, prefixes in CASES:
        b, c = capture(raw, prefixes, 0), capture(raw, prefixes, 1)
        require(b == c, "same stage-1 metadata on both cards")
        result[name] = b
    return result


def capture_full(raw, prefixes, card=0):
    """Execute the pinned public wrapper with inert tensors and two kernel recorders."""
    from typing import Tuple

    expected = capture(raw, prefixes, card)
    tree = ast.parse(raw, filename=SOURCE_PATH)
    names = {"decode_attention_fwd", "decode_attention_fwd_grouped", "_decode_grouped_att_m_fwd",
             "_decode_softmax_reducev_fwd", "_grouped_head_tiles", "_extract_kv_strides",
             "_mla_launch_plan", "_mla_tuning_applies", "_lean_head_dim_ok"}
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    require({n.name for n in tree.body} == names, "public grouped decode host closure")
    stage1, stage2 = Kernel(), Kernel()

    def unpack(score_mod, auxiliary):
        require(score_mod is auxiliary is None, "frozen auxiliary arguments")
        return None, 0, 0, 0

    namespace = dict(_is_hip=True, _is_gfx1250=False, _GROUPED_BLOCK_H=16, _MIN_BLOCK_KV=32,
                     Tuple=Tuple, unpack_aux_tensors=unpack,
                     triton=types.SimpleNamespace(cdiv=lambda a, b: (a + b - 1) // b,
                                                  next_power_of_2=lambda n: 1 << (n - 1).bit_length()),
                     _fwd_grouped_kernel_stage1=stage1, _fwd_kernel_stage2=stage2)
    exec(compile(tree, SOURCE_PATH, "exec"), namespace)
    args = []
    for index, value in enumerate(expected["launch"]["args"]):
        if isinstance(value, dict):
            value = Tensor(value["shape"], value["dtype"], value["stride"], card, 16 * (index + 1))
            value.ndim = len(value.shape)
        args.append(value)
    q, k, v, scale, indptr, indices, partial, lse, splits = args[:9]
    out = Tensor(q.shape, q.dtype, q.stride(), card, 4096)
    namespace["decode_attention_fwd"](q, k, v, out, indptr, indices, partial, lse, splits,
                                       16, scale, 1.0, 1.0, enable_lean=False, page_size=1)
    require(stage1.calls == [expected["launch"]] and len(stage2.calls) == 1,
            "public wrapper reaches the exact census stage-1 and one reduction")
    return dict(stage1=stage1.calls[0], stage2=stage2.calls[0], physical_split_values_observed=False,
                note="The actual public wrapper ran against metadata fixtures; no device execution occurred.")
