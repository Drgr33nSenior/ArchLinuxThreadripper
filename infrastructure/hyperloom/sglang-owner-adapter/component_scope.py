"""Closed source surface for automated C4 component candidates.

The host wrapper is unchanged or has the one bounded index-width conversion
below. Kernel expressions may use the declared Triton operations; candidates
cannot introduce Python imports, global writes or arbitrary Python calls.
"""

import ast

INDEX_CAST = '''    if (
        kv_indices.dtype == torch.int64
        and k_buffer.shape[0] * k_buffer.stride(0) < 2**31
        and v_buffer.shape[0] * v_buffer.stride(0) < 2**31
    ):
        kv_indices = kv_indices.to(torch.int32)
'''
HOST_ANCHOR = ("    sm_scale = sm_scale or 1.0 / (Lq**0.5)\n"
               "    batch_size, head_num = qo_indptr.shape[0] - 1, q_extend.shape[1]")
TL_CALLS = frozenset(("arange", "dot", "exp", "exp2", "full", "load", "log", "log2", "max", "maximum",
                     "minimum", "multiple_of", "program_id", "store", "sum", "where", "zeros",
                     "static_range", "static_assert", "assume", "debug_barrier", "num_programs"))
NAME_CALLS = frozenset(("float", "range", "tanh", "SCORE_MOD"))


def validate(baseline, candidate):
    before, after = ast.parse(baseline), ast.parse(candidate)
    host = lambda tree: next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "extend_attention_fwd")
    baseline_text = baseline.decode() if isinstance(baseline, bytes) else baseline
    if baseline_text.count(HOST_ANCHOR) != 1:
        raise ValueError("fixed host insertion anchor changed")
    index_host = host(ast.parse(baseline_text.replace(HOST_ANCHOR, INDEX_CAST + HOST_ANCHOR)))
    if ast.dump(host(after)) not in (ast.dump(host(before)), ast.dump(index_host)):
        raise ValueError("host wrapper must be unchanged or use the declared bounded index cast")
    kernel = next(n for n in after.body if isinstance(n, ast.FunctionDef) and n.name == "_fwd_kernel")
    for node in ast.walk(kernel):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef,
                             ast.AsyncFunctionDef, ast.Lambda, ast.With, ast.AsyncWith)):
            raise ValueError("candidate kernel contains an undeclared Python operation")
        if isinstance(node, ast.FunctionDef) and node is not kernel:
            raise ValueError("nested candidate functions are outside the component surface")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private Python attribute access is outside the component surface")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if any(not isinstance(n, (ast.Name, ast.Tuple, ast.List, ast.Store)) for n in ast.walk(target)):
                    raise ValueError("kernel assignments must bind local names")
        if isinstance(node, ast.Call):
            function = node.func
            valid = isinstance(function, ast.Name) and function.id in NAME_CALLS
            if isinstance(function, ast.Attribute):
                valid = function.attr == "to" or (isinstance(function.value, ast.Name)
                                                  and function.value.id == "tl" and function.attr in TL_CALLS)
            if not valid:
                raise ValueError("kernel call is outside the declared Triton surface: " + ast.unparse(function))
    return dict(host_index_cast=ast.dump(host(after)) == ast.dump(index_host),
                frozen_geometry=True, arbitrary_python_calls=False)
