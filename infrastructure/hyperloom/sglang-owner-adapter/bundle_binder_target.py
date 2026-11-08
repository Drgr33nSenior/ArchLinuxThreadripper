"""Device-free real JIT-binder screen; appended to the authenticated bootstrap.

CPU tensors supply real specialization metadata. A metadata device view allows
the unchanged serving wrapper to run. Its launch boundary invokes only Triton's
real Heuristics.run(warmup=True), with a device-free target provider. No binary
may be loaded, no model weights are read, and no acceptance pin is changed.
"""
import ast
from contextlib import ExitStack, contextmanager
import gzip
import linecache
from types import SimpleNamespace
from typing import Tuple
import triton.language as tl
from triton import knobs
from triton.backends.compiler import GPUTarget
from triton.compiler import ASTSource
from triton.runtime.driver import driver

TARGET = GPUTarget('hip', 'gfx1201', 32)
PARTS = ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')
META = ('arch', 'global_scratch_size', 'profile_scratch_size', 'num_stages',
        'num_warps', 'shared', 'warp_size', 'waves_per_eu')


@contextmanager
def historical_module(raw, path='/mnt/review/w4a4_component/baseline/kernels.py'):
    """Original proof's AST-only construction, retained as the calibration arm."""
    name='mxfp4_component.kernels'
    names={'_mxfp4_quant_op','_dynamic_mxfp4_quant_kernel','_gemm_afp4wfp4_kernel','pid_grid','remap_xcd'}
    missing=object();old=sys.modules.get(name,missing);old_line=linecache.cache.get(path,missing)
    tree=ast.parse(raw,filename=path)
    tree.body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert {n.name for n in tree.body}==names
    mod=types.ModuleType(name);mod.__file__=path;mod.__dict__.update(triton=triton,tl=tl,Tuple=Tuple)
    linecache.cache[path]=(len(raw),None,raw.splitlines(True),path);sys.modules[name]=mod
    try:
        exec(compile(tree,path,'exec'),mod.__dict__)
        yield mod
    finally:
        for mapping,key,value in ((sys.modules,name,old),(linecache.cache,path,old_line)):
            if value is missing:mapping.pop(key,None)
            else:mapping[key]=value


def fingerprint(jit):
    return dict(source_sha256=hashlib.sha256(jit.src.encode()).hexdigest(),cache_key=jit.cache_key,
        file_name=jit.file_name,starting_line_number=jit.starting_line_number,
        def_file_line_number=jit.def_file_line_number,def_file_col_number=jit.def_file_col_number,
        code_flags=jit.fn.__code__.co_flags,module=jit.module)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


@contextmanager
def module_source(name, filename, raw):
    missing=object();old=sys.modules.get(name, missing);old_line=linecache.cache.get(filename, missing)
    module=types.ModuleType(name);module.__file__=filename
    module.__package__=name.rpartition('.')[0]
    sys.modules[name]=module
    linecache.cache[filename]=(len(raw),None,raw.splitlines(True),filename)
    try:
        # Entire source, imports, decorators and line numbers are preserved.
        exec(compile(raw,filename,'exec',dont_inherit=True),module.__dict__)
        yield module
    finally:
        for mapping,key,value in ((sys.modules,name,old),(linecache.cache,filename,old_line)):
            if value is missing:mapping.pop(key,None)
            else:mapping[key]=value


def compiler_record(compiled):
    assert compiled.module is None and compiled.function is None and compiled._run is None
    parts={};blobs={}
    for name in PARTS:
        raw=compiled.asm[name];raw=raw.encode() if type(raw) is str else raw
        parts[name]=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        blobs[name]=base64.b64encode(gzip.compress(raw,mtime=0)).decode()
    return dict(parts=parts,metadata={key:getattr(compiled.metadata,key) for key in META}),blobs


class MetadataView:
    """Only wrapper-facing device metadata differs from the CPU backing tensor."""
    def __init__(self, value):self.value=value
    @property
    def shape(self):return self.value.shape
    @property
    def dtype(self):return self.value.dtype
    @property
    def device(self):return SimpleNamespace(type='cuda',index=0)
    def stride(self,*args):return self.value.stride(*args)
    def untyped_storage(self):return self.value.untyped_storage()


def run():
    assert not Path('/dev/kfd').exists() and not list(Path('/dev/dri').glob('renderD*'))
    assert not torch.cuda.is_initialized()
    report=dict(schema=1,status='CPU_BINDER_SCREEN_STARTED',gpu_launches=0,model_starts=0,
                actual_jit_binder=True,cpu_backing_tensors=True,wrapper_device_metadata_fixture=True,
                live_gpu_specialization_observed=False,acceptance_pins_changed=False,
                calibration=[],cases={},artifacts={})
    sources=PAYLOAD['sources'];rows=PAYLOAD['rows'];expected=PAYLOAD['pins']
    # Calibration is the exact original construction. Full-module construction
    # is a separately reported experiment and must not redefine its acceptance.
    with historical_module(sources['kernels']) as mod:
        jit=mod._gemm_afp4wfp4_kernel.fn
        report['historical_fingerprint']=fingerprint(jit)
        for m in (8,16):
            for arm,label in (('control','baseline'),('mtp','candidate')):
                row=rows[str(m)][label]
                constants={ast.literal_eval(k):v for k,v in row['constants'].items()}
                attrs={ast.literal_eval(k):v for k,v in row['attrs'].items()}
                compiled=triton.compile(ASTSource(jit,row['signature'],constants,attrs),target=TARGET,options=row['options'])
                record,_=compiler_record(compiled)
                key='gate-up-m'+str(m)+'-wave'+('1' if arm=='control' else '0')
                assert canonical(record)==canonical(expected[arm][key]),('historical calibration',key,record)
                report['calibration'].append(key)
    # Reproduce the old proof's second compile with an unchanged compiler key.
    # This arm intentionally keeps the cache; its output is diagnostic only.
    with historical_module(sources['kernels'], '/mnt/review/kernels.py') as mod:
        jit=mod._gemm_afp4wfp4_kernel.fn
        report['rebound_fingerprint']=fingerprint(jit)
        for m in (8,16):
            for arm,label in (('control','baseline'),('mtp','candidate')):
                row=rows[str(m)][label]
                constants={ast.literal_eval(k):v for k,v in row['constants'].items()}
                attrs={ast.literal_eval(k):v for k,v in row['attrs'].items()}
                compiled=triton.compile(ASTSource(jit,row['signature'],constants,attrs),target=TARGET,options=row['options'])
                record,blobs=compiler_record(compiled)
                key='gate-up-m'+str(m)+'-wave'+('1' if arm=='control' else '0')
                assert canonical(record)==canonical(expected[arm][key]),('cached rebound',key,record)
                report.setdefault('cached_rebound',{})[key]=record
                report['artifacts']['cached-rebound-'+key]=blobs
    old_driver=driver.__dict__['_active']
    old_always_compile=knobs.compilation.always_compile
    virtual=SimpleNamespace(get_current_target=lambda:TARGET,get_current_device=lambda:0,
                            get_current_stream=lambda device:0)
    # Explicitly avoid lazy HIP-driver construction in this device-free process.
    driver.set_active(virtual)
    package=types.ModuleType('mxfp4_component');package.__path__=['/mnt/review']
    missing=object();old_package=sys.modules.get('mxfp4_component',missing)
    sys.modules['mxfp4_component']=package
    try:
        # Force code generation for the consumed path rather than accepting a
        # same-key artifact left by the historical calibration. CPU scope only.
        knobs.compilation.always_compile=True
        with ExitStack() as stack:
            for name in ('bundle_contract','bundle_kernels','bundle_runtime'):
                stack.enter_context(module_source(name,'/mnt/review/'+name+'.py',sources[name]))
            stack.enter_context(module_source('mxfp4_component.prefill_reference','/mnt/review/prefill_reference.py',sources['prefill_reference']))
            kernels=stack.enter_context(module_source('mxfp4_component.kernels','/mnt/review/kernels.py',sources['kernels']))
            ops=stack.enter_context(module_source('mxfp4_component.ops','/mnt/review/ops.py',sources['ops']))
            runtime=sys.modules['bundle_runtime']
            heuristic=kernels._gemm_afp4wfp4_kernel;jit=heuristic.fn
            report['full_module_fingerprint']=fingerprint(jit)
            # Calibrate the historical explicit ASTSource at the SAME full-module
            # source path before exercising the generated native binder.
            for m in (8,16):
                for arm,label in (('control','baseline'),('mtp','candidate')):
                    row=rows[str(m)][label]
                    constants={ast.literal_eval(k):v for k,v in row['constants'].items()}
                    attrs={ast.literal_eval(k):v for k,v in row['attrs'].items()}
                    compiled=triton.compile(ASTSource(jit,row['signature'],constants,attrs),
                                           target=TARGET,options=row['options'])
                    record,blobs=compiler_record(compiled)
                    key='gate-up-m'+str(m)+'-wave'+('1' if arm=='control' else '0')
                    report.setdefault('full_module_explicit',{})[key]=dict(compiler=record,
                        compiler_matches=canonical(record)==canonical(expected[arm][key]))
                    report['artifacts']['full-explicit-'+key]=blobs
            original_compile=jit._do_compile
            calls=[]
            def observed_compile(key,signature,device,constexprs,options,attrs,warmup):
                assert warmup is True and device==0
                inputs=dict(signature=signature,constants={repr(k):v for k,v in constexprs.items()},
                            attrs={repr(k):v for k,v in attrs.items()},options=options.__dict__)
                compiled=original_compile(key,signature,device,constexprs,options,attrs,warmup)
                assert not torch.cuda.is_initialized()
                calls.append((inputs,compiled))
                return compiled
            jit._do_compile=observed_compile
            class WarmOnly:
                def __getitem__(self,grid):
                    def launch(*args,**kwargs):
                        raw=tuple(x.value if isinstance(x,MetadataView) else x for x in args)
                        return heuristic.run(*raw,grid=grid,warmup=True,**kwargs)
                    return launch
            fixture_kernels=SimpleNamespace(_gemm_afp4wfp4_kernel=WarmOnly())
            original_runtime=ops.runtime
            ops.runtime=lambda device:(torch,triton,fixture_kernels)
            try:
                for m in (8,16):
                    for arm,label in (('control','baseline'),('mtp','candidate')):
                        runtime.STATE=SimpleNamespace(manifest={'arm':arm},attached=False)
                        backing=[torch.empty((m,2560),dtype=torch.uint8,device='cpu'),
                            torch.empty((17408,2560),dtype=torch.uint8,device='cpu'),
                            torch.empty_strided((m,160),(1,m),dtype=torch.uint8,device='cpu'),
                            torch.empty((17408,160),dtype=torch.uint8,device='cpu'),
                            torch.empty((m,17408),dtype=torch.bfloat16,device='cpu')]
                        start=len(calls)
                        result=ops.gemm_preallocated(*(MetadataView(x) for x in backing),expected_device=0)
                        assert len(calls)==start+1,'each row must bind/compile freshly'
                        actual,compiled=calls[-1];record,blobs=compiler_record(result)
                        row=rows[str(m)][label];key='gate-up-m'+str(m)+'-wave'+('1' if arm=='control' else '0')
                        differences={field:dict(frozen=row[field],observed=actual[field])
                            for field in ('signature','constants','attrs','options')
                            if canonical(row[field])!=canonical(actual[field])}
                        report['cases'][key]=dict(inputs=actual,input_differences=differences,
                            compiler=record,expected=expected[arm][key],
                            compiler_matches=canonical(record)==canonical(expected[arm][key]),
                            tensor_metadata=[dict(dtype=str(x.dtype),shape=list(x.shape),stride=list(x.stride()),
                                alignment_mod16=x.data_ptr()%16,storage_bytes=x.untyped_storage().nbytes(),
                                has_ptr_range=hasattr(x,'ptr_range')) for x in backing])
                        report['artifacts'][key]=blobs
                        del backing,result
            finally:
                ops.runtime=original_runtime;jit._do_compile=original_compile;runtime.STATE=None
    finally:
        knobs.compilation.always_compile=old_always_compile
        driver.set_active(old_driver)
        if old_package is missing:sys.modules.pop('mxfp4_component',None)
        else:sys.modules['mxfp4_component']=old_package
    assert selected.consumed_sources(inventory)==consumed
    assert not torch.cuda.is_initialized()
    report.update(status='CPU_BINDER_SCREEN_COMPLETE',compiled_objects_unloaded=True,
                  driver_restored=driver.__dict__['_active'] is old_driver,
                  always_compile_restored=knobs.compilation.always_compile==old_always_compile,
                  original_modules_and_linecache_restored=True)
    return report


try:
    result=run()
except Exception as error:
    import traceback
    result=dict(status='CPU_BINDER_REFUSED',error=repr(error),traceback=traceback.format_exc(),gpu_launches=0,model_starts=0)
assert not torch.cuda.is_initialized()
print(json.dumps(result,sort_keys=True))
if result['status']!='CPU_BINDER_SCREEN_COMPLETE':raise SystemExit(1)
