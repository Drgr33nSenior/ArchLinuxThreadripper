"""Appended to the pinned device-free compiler bootstrap; no kernel is loaded."""
import ast
from contextlib import contextmanager
import gzip
import linecache
from typing import Tuple
from triton.backends.compiler import GPUTarget
from triton.compiler import ASTSource
from triton import knobs
import triton.language as tl

# PAYLOAD is inserted as decoded immutable input by bundle_compile. The complete
# historical recipes authenticate Torch, Triton, compiler helpers and toolchain
# before this body runs. All AST nodes keep their original paths and lines.
TARGET = GPUTarget('hip', 'gfx1201', 32)
PARTS = ('ttir','ttgir','llir','amdgcn','hsaco')
META = ('arch','global_scratch_size','profile_scratch_size','num_stages','num_warps','shared','warp_size','waves_per_eu')
GEMM_NAMES = {'_mxfp4_quant_op','_dynamic_mxfp4_quant_kernel','_gemm_afp4wfp4_kernel','pid_grid','remap_xcd'}
ATT_NAMES = {'tanh','_fwd_grouped_kernel_stage1','_fwd_kernel_stage2'}


@contextmanager
def source_module(raw, name, path, names, globals_=None):
    absent = object(); old = sys.modules.get(name, absent); old_line = linecache.cache.get(path, absent)
    tree = ast.parse(raw, filename=path)
    tree.body = [node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    assert {node.name for node in tree.body} == set(names)
    mod = types.ModuleType(name); mod.__file__ = path
    mod.__dict__.update(triton=triton,tl=tl,Tuple=Tuple)
    if globals_: mod.__dict__.update(globals_)
    linecache.cache[path] = (len(raw),None,raw.splitlines(True),path)
    sys.modules[name] = mod
    try:
        exec(compile(tree,path,'exec'),mod.__dict__)
        yield mod
    finally:
        for mapping,key,value in ((sys.modules,name,old),(linecache.cache,path,old_line)):
            if value is absent: mapping.pop(key,None)
            else: mapping[key] = value


def compile_record(function, row):
    # The reviewed reports bind the real binder's exact signature, constants,
    # specialization attributes and options. No approximated signature is used.
    if not hasattr(function,'signature'): function = function.fn
    assert hasattr(function,'signature')
    constants = {ast.literal_eval(key): value for key,value in row['constants'].items()}
    attrs = {ast.literal_eval(key): value for key,value in row['attrs'].items()}
    # Triton's key omits the source filename. A component calibration must not
    # supply its debug-path artifact to the subsequent serving-context proof.
    previous = knobs.compilation.always_compile
    try:
        knobs.compilation.always_compile = True
        compiled = triton.compile(ASTSource(function,row['signature'],constants,attrs),
                                  target=TARGET,options=row['options'])
    finally:
        knobs.compilation.always_compile = previous
    assert compiled.module is None and compiled.function is None and compiled._run is None
    assert not torch.cuda.is_initialized()
    location = ('loc('+json.dumps(function.file_name)+':'+str(function.def_file_line_number)+
                ':'+str(function.def_file_col_number)+')')
    assert location in compiled.asm['ttir'], ('compiled source origin', location)
    parts = {}
    blobs = {}
    for part in PARTS:
        raw = compiled.asm[part]; raw = raw.encode() if type(raw) is str else raw
        parts[part] = dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        blobs[part] = base64.b64encode(gzip.compress(raw,mtime=0)).decode()
    return dict(parts=parts,metadata={key:getattr(compiled.metadata,key) for key in META}), blobs


def historical(function,row):
    value, _ = compile_record(function,row)
    mismatches={p:dict(observed=value['parts'][p],expected={k:row['artifacts'][p][k] for k in ('bytes','sha256')})
                for p in PARTS if value['parts'][p]['sha256']!=row['artifacts'][p]['sha256'] or
                                  value['parts'][p]['bytes']!=row['artifacts'][p]['bytes']}
    if mismatches:raise ValueError('historical calibration failed: '+json.dumps(mismatches,sort_keys=True))


def run():
    report = dict(schema=1,status='STARTED',gpu_launches=0,model_starts=0,device_nodes=False,
                  calibration=[],serving={},artifacts={})
    source = PAYLOAD['sources']; rows = PAYLOAD['rows']
    # First reproduce each previously reviewed component context. A mismatch
    # stops before any new serving-context compile is attempted.
    old_gemm_path=rows['gemm']['8']['baseline']['source_path']
    assert old_gemm_path=='/mnt/review/w4a4_component/baseline/kernels.py'
    assert all(rows['gemm'][str(t)][arm]['source_path']==old_gemm_path
               for t in (8,16) for arm in ('baseline','candidate'))
    with source_module(source['kernels'],'mxfp4_component.kernels',old_gemm_path,GEMM_NAMES) as mod:
        for tokens in (8,16):
            for arm in ('baseline','candidate'):
                historical(mod._gemm_afp4wfp4_kernel,rows['gemm'][str(tokens)][arm])
                report['calibration'].append('gemm-'+str(tokens)+'-'+arm)
    with source_module(source['fused'],'opt04_candidate','/mnt/review/opt04_gatednorm_quant.py',
                       {'_mxfp4_quant_op','_gated_norm_mxfp4_kernel'}) as mod:
        for tokens in (8,16):
            historical(mod._gated_norm_mxfp4_kernel,rows['f1'][str(tokens)])
            report['calibration'].append('f1-'+str(tokens))
    native_path='/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py'
    native_name='sglang.kernels.ops.attention.decode_attention'
    with source_module(source['attention_baseline'],native_name,native_path,ATT_NAMES) as baseline:
        for cap in (16,32):
            historical(baseline._fwd_grouped_kernel_stage1,rows['attention']['B' if cap==16 else 'S']['stage1'])
            historical(baseline._fwd_kernel_stage2,rows['attention']['B' if cap==16 else 'S']['stage2'])
        with source_module(source['attention_candidate'],native_name,native_path,{'_fwd_grouped_kernel_stage1'},
                           {'tanh':baseline.tanh}) as candidate:
            historical(candidate._fwd_grouped_kernel_stage1,rows['attention']['C']['stage1'])
        report['calibration'].append('attention-stock-prefetch-cap16-cap32')
    report['calibration_complete_before_serving'] = True
    pins = {'control':{},'mtp':{}}
    def retain(key,function,row,arms=('control','mtp')):
        value,blobs = compile_record(function,row)
        for arm in arms: pins[arm][key] = value
        jit = function if hasattr(function,'signature') else function.fn
        storage=key if len(arms)>1 else arms[0]+'-'+key
        report['serving'][storage] = dict(source_path=jit.fn.__code__.co_filename,
                                     signature=row['signature'],constants=row['constants'],attrs=row['attrs'],options=row['options'])
        report['artifacts'][storage] = blobs
    with source_module(source['kernels'],'mxfp4_component.kernels','/mnt/review/kernels.py',GEMM_NAMES) as mod:
        for tokens in (8,16):
            for arm,wave in (('baseline',1),('candidate',0)):
                retain('gate-up-m'+str(tokens)+'-wave'+str(wave),mod._gemm_afp4wfp4_kernel,rows['gemm'][str(tokens)][arm])
    with source_module(source['fused'],'opt04_candidate','/mnt/review/opt04_gatednorm_quant.py',
                       {'_mxfp4_quant_op','_gated_norm_mxfp4_kernel'}) as mod:
        for tokens in (8,16): retain('f1-t'+str(tokens),mod._gated_norm_mxfp4_kernel,rows['f1'][str(tokens)])
    for arm,source_key,name,path,variant in (
        ('control','attention_baseline',native_name,native_path,'B'),
        ('mtp','attention_candidate','decode_attention_fwd_candidate','/mnt/review/decode_attention_fwd_candidate.py','C')):
        with source_module(source[source_key],name,path,ATT_NAMES) as mod:
            # Record names are arm-local: stage1 consumes dynamic strides and
            # therefore has one specialization across all frozen prefix lengths.
            retain('attention-stage1',mod._fwd_grouped_kernel_stage1,rows['attention'][variant]['stage1'],(arm,))
            cap=16 if arm=='control' else 32
            retain('attention-stage2-cap'+str(cap),mod._fwd_kernel_stage2,
                   rows['attention']['B' if cap==16 else 'C']['stage2'],(arm,))
    assert selected.consumed_sources(inventory)==consumed
    assert not torch.cuda.is_initialized()
    report.update(status='CPU_SERVING_COMPILERS_PROVEN',pins=pins,compiled_objects_unloaded=True,
                  original_modules_and_linecache_restored=True,consumed_sources_unchanged=True,
                  fresh_codegen_for_every_compile=True,artifact_source_paths_verified=True)
    return report


try:
    bundle_report=run()
except Exception as error:
    import traceback
    bundle_report=dict(status='CPU_COMPILE_REFUSED',error=repr(error),traceback=traceback.format_exc(),
                       gpu_launches=0,model_starts=0)
assert not torch.cuda.is_initialized()
print(json.dumps(bundle_report,sort_keys=True))
if bundle_report['status']!='CPU_SERVING_COMPILERS_PROVEN':raise SystemExit(1)
