"""Compile the pinned grouped stage-1 function without loading or launching it."""
import ast
import base64
import gzip
import hashlib
import linecache
import sys
import time
import types


def compile_verify(raw, launch):
    import torch
    import triton
    import triton.language as tl
    from triton.backends.compiler import GPUTarget
    from triton.compiler import ASTSource, make_backend
    from triton.runtime.jit import MockTensor, create_function_from_signature

    filename = "/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py"
    tree = ast.parse(raw, filename=filename)
    names = {"tanh", "_fwd_grouped_kernel_stage1"}
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in tree.body} == names
    linecache.cache[filename] = (len(raw), None, raw.splitlines(True), filename)
    module = types.ModuleType("sglang.kernels.ops.attention.decode_attention")
    module.__file__ = filename
    module.__dict__.update(triton=triton, tl=tl)
    sys.modules[module.__name__] = module
    exec(compile(tree, filename, "exec"), module.__dict__)
    fn = module._fwd_grouped_kernel_stage1
    target = GPUTarget("hip", "gfx1201", 32)
    backend = make_backend(target)
    args = [MockTensor(getattr(torch, value["dtype"]), value["shape"])
            if isinstance(value, dict) else value for value in launch["args"]]
    kwargs = dict(launch["kwargs"], debug=False, instrumentation_mode="")
    binder = create_function_from_signature(fn.signature, fn.params, backend)
    bound, specialization, options = binder(*args, **kwargs)
    options, signature, constants, attrs = fn._pack_args(backend, kwargs, bound, specialization, options)
    started = time.monotonic()
    compiled = triton.compile(ASTSource(fn, signature, constants, attrs), target=target, options=options.__dict__)
    assert compiled.module is None and compiled.function is None and compiled._run is None
    assert not torch.cuda.is_initialized()
    artifacts = {}
    for part in ("ttir", "ttgir", "llir", "amdgcn", "hsaco"):
        data = compiled.asm[part]
        data = data.encode() if isinstance(data, str) else data
        assert 0 < len(data) <= 8 << 20
        artifacts[part] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                              gzip_base64=base64.b64encode(gzip.compress(data, mtime=0)).decode())
    return dict(source_sha256=hashlib.sha256(raw.encode()).hexdigest(), source_path=filename,
                elapsed_seconds=time.monotonic() - started, signature=signature,
                constants={str(k): v for k, v in constants.items()}, attrs={str(k): v for k, v in attrs.items()},
                options=options.__dict__, metadata=compiled.metadata._asdict(), artifacts=artifacts)
