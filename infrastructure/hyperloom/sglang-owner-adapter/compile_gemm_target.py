"""Explicit gfx1201 target compilation; imported only inside the CPU sandbox."""
import base64
import gzip
import hashlib
import linecache
import sys
import time
import types


def compile_gemm(raw, launch):
    import torch
    import triton
    from triton.backends.compiler import GPUTarget
    from triton.compiler import ASTSource, make_backend
    from triton.runtime.jit import MockTensor, create_function_from_signature

    filename = "/mnt/review/w4a4_component/baseline/kernels.py"
    linecache.cache[filename] = (len(raw), None, raw.splitlines(True), filename)
    module = types.ModuleType("mxfp4_component.kernels")
    module.__file__ = filename
    sys.modules[module.__name__] = module
    exec(compile(raw, filename, "exec"), module.__dict__)
    wrapped = module._gemm_afp4wfp4_kernel
    fn = wrapped.fn
    assert set(wrapped.values) == {"EVEN_K"}
    target = GPUTarget("hip", "gfx1201", 32)
    backend = make_backend(target)
    args = [MockTensor(getattr(torch, value["dtype"]), value["shape"])
            if isinstance(value, dict) else value for value in launch["args"]]
    kwargs = dict(launch["kwargs"], debug=False, instrumentation_mode="")
    heuristic_args = dict(zip(fn.arg_names, args), **kwargs)
    kwargs["EVEN_K"] = wrapped.values["EVEN_K"](heuristic_args)
    assert kwargs["EVEN_K"] is True
    binder = create_function_from_signature(fn.signature, fn.params, backend)
    bound, specialization, options = binder(*args, **kwargs)
    options, signature, constants, attrs = fn._pack_args(backend, kwargs, bound, specialization, options)
    started = time.monotonic()
    compiled = triton.compile(ASTSource(fn, signature, constants, attrs), target=target, options=options.__dict__)
    assert compiled.module is None and compiled.function is None and compiled._run is None
    assert not torch.cuda.is_initialized()
    artifacts = {}
    for part in ("ttir", "ttgir", "llir", "amdgcn", "hsaco"):
        raw_part = compiled.asm[part]
        raw_part = raw_part.encode() if isinstance(raw_part, str) else raw_part
        assert 0 < len(raw_part) <= 8 << 20
        artifacts[part] = dict(bytes=len(raw_part), sha256=hashlib.sha256(raw_part).hexdigest(),
                              gzip_base64=base64.b64encode(gzip.compress(raw_part, mtime=0)).decode())
    return dict(source_sha256=hashlib.sha256(raw.encode()).hexdigest(), source_path=filename,
                elapsed_seconds=time.monotonic() - started, signature=signature,
                constants={str(k): v for k, v in constants.items()}, attrs={str(k): v for k, v in attrs.items()},
                options=options.__dict__, metadata=compiled.metadata._asdict(), artifacts=artifacts)
