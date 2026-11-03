"""Source-bound CPU projection of the frozen verification cases, not GPU evidence."""
import ast
import hashlib
import math
import struct
import types

from gemm_dispatch import require
from verify_dispatch import CASES, SOURCE_SHA256

METADATA_SHA256 = "10d4f1ac194e1fd999b58bbfcb2d1ad09bc25df289128925d2945108173afdc0"
SLOTS, WIDTH, MAX_SPLITS, BLOCK = 120833, 8, 16, 32


def _scalar_language():
    def cast(value, dtype):
        return int(value) if dtype == "int32" else struct.unpack("f", struct.pack("f", value))[0]
    return types.SimpleNamespace(minimum=min, maximum=max, log2=math.log2, cast=cast,
                                 float32="float32", int32="int32", cdiv=lambda a, b: (a + b - 1) // b)


def singleton_splits(metadata, length):
    require(hashlib.sha256(metadata).hexdigest() == METADATA_SHA256, "selected metadata source")
    require(type(length) is int and 1 <= length <= 131048, "positive bounded singleton length")
    fn = next(n for n in ast.parse(metadata).body if isinstance(n, ast.FunctionDef)
              and n.name == "get_num_kv_splits_triton")
    # With one real sequence, the first masked load's max is length and the
    # second load fills every inactive lane with length. Thus min=max=length.
    start = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.If))
    end = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "num_kv_splits" for t in n.targets))
    tree = ast.Module(body=fn.body[start:end + 1], type_ignores=[])
    values = dict(tl=_scalar_language(), seq_lens=length, max_seq_len=length, min_seq_len=length,
                  num_seq=1, num_group=1, num_head=12, num_kv_head=2, max_kv_splits=16, device_core_count=64)
    exec(compile(tree, "pinned-singleton-split-arithmetic", "exec"), values)
    return values["num_kv_splits"]


def split_spans(source, length, count):
    require(hashlib.sha256(source).hexdigest() == SOURCE_SHA256, "selected attention source")
    require(type(length) is int and 1 <= length <= 131048 and type(count) is int and 1 <= count <= 16,
            "bounded split inputs")
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
              and n.name == "_fwd_grouped_kernel_stage1")
    names = ("kv_len_per_split", "split_kv_start", "split_kv_end")
    nodes = [n for n in fn.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    require(len(nodes) == 3, "pinned split arithmetic closure")
    tree = ast.Module(body=nodes, type_ignores=[])
    rows = []
    for split in range(16):
        values = dict(tl=_scalar_language(), cur_batch_seq_len=length, kv_splits=count,
                      MIN_BLOCK_KV=32, split_kv_id=split)
        exec(compile(tree, "pinned-grouped-split-arithmetic", "exec"), values)
        start, end = values["split_kv_start"], values["split_kv_end"]
        iterations = max(0, (end - start + 31) // 32)
        rows.append(dict(split=split, start=start, end=end, active=end > start,
                         iterations=iterations, last_block_tokens=(end - start - 1) % 32 + 1 if end > start else 0))
    require(sum(max(0, row["end"] - row["start"]) for row in rows) == length,
            "split partition covers each singleton token once")
    return rows


def project(source, metadata):
    output = {}
    for name, prefixes in CASES:
        rows, indptr = [], [0]
        # This proposed pool fixture gives requests disjoint physical ranges.
        # Each row consumes its request's full prefix and only its causal tail.
        base = 1
        for request, prefix in enumerate(prefixes):
            for row in range(8):
                length = prefix + row + 1
                count = singleton_splits(metadata, length)
                require(count == 16, "frozen workload singleton split result")
                spans = split_spans(source, length, count)
                rows.append(dict(request=request, causal_row=row, length=length, split_count=count,
                                 prefix_slots=[base, base + prefix], tail_slots=[base + prefix, base + prefix + row + 1],
                                 splits=spans))
                indptr.append(indptr[-1] + length)
            base += prefix + 8
        require(base <= SLOTS and indptr[-1] <= len(rows) * (131040 + 8), "bounded pool and packed index allocation")
        output[name] = dict(prefixes=list(prefixes), rows=rows, indptr=indptr,
                            order="even-odd" if name.endswith("even-odd") else "sequential",
                            highest_used_slot=base - 1, packed_index_bytes=indptr[-1] * 8)
    return dict(schema=1, source_sha256=SOURCE_SHA256, metadata_sha256=METADATA_SHA256,
                cases=output, physical_split_values_observed=False, gpu_work=False,
                limitation="Source arithmetic on CPU; the future component must call the pinned device helper and authenticate actual values.")
