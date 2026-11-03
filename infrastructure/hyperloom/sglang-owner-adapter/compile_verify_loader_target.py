"""Target-explicit compilation of the actual harness loader, without device use."""
def compile_loaded(fn, raw, launch):
    import torch
    import triton
    from triton.backends.compiler import GPUTarget
    from triton.compiler import ASTSource, make_backend
    from triton.runtime.jit import MockTensor, create_function_from_signature
    target=GPUTarget('hip','gfx1201',32);backend=make_backend(target)
    args=[MockTensor(getattr(torch,v['dtype']),v['shape']) if isinstance(v,dict) else v for v in launch['args']]
    kwargs=dict(launch['kwargs'],debug=False,instrumentation_mode='')
    binder=create_function_from_signature(fn.signature,fn.params,backend)
    bound,specialization,options=binder(*args,**kwargs)
    options,signature,constants,attrs=fn._pack_args(backend,kwargs,bound,specialization,options)
    started=time.monotonic()
    compiled=triton.compile(ASTSource(fn,signature,constants,attrs),target=target,options=options.__dict__)
    assert compiled.module is None and compiled.function is None and compiled._run is None
    assert not torch.cuda.is_initialized()
    artifacts={}
    for part in ('ttir','ttgir','llir','amdgcn','hsaco'):
        data=compiled.asm[part];data=data.encode() if isinstance(data,str) else data
        assert 0<len(data)<=8<<20
        artifacts[part]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
            gzip_base64=base64.b64encode(gzip.compress(data,mtime=0)).decode())
    return dict(source_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        source_path='/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py',
        elapsed_seconds=time.monotonic()-started,signature=signature,
        constants={str(k):v for k,v in constants.items()},attrs={str(k):v for k,v in attrs.items()},
        options=options.__dict__,metadata=compiled.metadata._asdict(),artifacts=artifacts)
