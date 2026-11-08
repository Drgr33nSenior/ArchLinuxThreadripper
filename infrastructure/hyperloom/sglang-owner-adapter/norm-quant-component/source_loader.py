"""Exact source nodes and compiler filenames; aliases live only in ModuleScope."""
import ast
from contextlib import nullcontext
import functools
import linecache
import sys
import types

import contract
import support as s

SPECS={
 'norm':('norm_baseline.py','opt04_baseline','/mnt/composition/image-source/python/sglang/kernels/ops/attention/fla/layernorm_gated.py'),
 'candidate':('norm_candidate.py','opt04_candidate','/mnt/review/opt04_gatednorm_quant.py'),
 'kernels':('mxfp4_kernels.py','mxfp4.kernels','/mnt/review/mxfp4/kernels.py'),
 'geometry':('prefill_reference.py','f1_geometry','/mnt/review/prefill_reference.py'),
 'ops':('mxfp4_ops.py','f1_mxfp4_ops','/mnt/review/mxfp4_ops.py')}

class ModuleScope:
    NAMES=tuple(v[1] for v in SPECS.values())
    PATHS=tuple(v[2] for v in SPECS.values())
    def __enter__(self):
        self.absent=object()
        self.modules={k:sys.modules.get(k,self.absent) for k in self.NAMES}
        self.lines={k:linecache.cache.get(k,self.absent) for k in self.PATHS}
        return self
    def __exit__(self,*exc):
        for mapping,old in ((sys.modules,self.modules),(linecache.cache,self.lines)):
            for key,value in old.items():
                if value is self.absent:mapping.pop(key,None)
                else:mapping[key]=value
        return False

def load(root,torch,triton,tl):
    def module(label,names,namespace,assignments=()):
        file,name,path=SPECS[label];raw=(root/file).read_bytes()
        if s.sha(raw)!=contract.INPUT_PINS[file]:raise ValueError('frozen source: '+file)
        tree=ast.parse(raw,filename=path)
        tree.body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names or
                   isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in assignments for t in n.targets)]
        if {n.name for n in tree.body if isinstance(n,ast.FunctionDef)}!=set(names):raise ValueError('closed source nodes')
        obj=types.ModuleType(name);obj.__file__=path;obj.__dict__.update(namespace)
        text=raw.decode();linecache.cache[path]=(len(text),None,text.splitlines(True),path);sys.modules[name]=obj
        exec(compile(tree,path,'exec'),obj.__dict__)
        return obj
    norm=module('norm',{'_layer_norm_fwd_1pass_kernel','_get_sm_count','calc_rows_per_block','_layer_norm_fwd'},
      dict(torch=torch,triton=triton,tl=tl,lru_cache=functools.lru_cache,cdiv=triton.cdiv,
       next_power_of_2=triton.next_power_of_2,MAX_ROWS_PER_BLOCK=4,
       is_batch_invariant_mode_enabled=lambda:False,check_cuda_graph_backend=lambda *args:False,
       Phase=types.SimpleNamespace(PREFILL=1),Backend=types.SimpleNamespace(TC_PIECEWISE=1),
       is_arch_support_pdl=lambda:False,nullcontext=nullcontext,device_context=torch.cuda.device))
    candidate=module('candidate',{'_mxfp4_quant_op','_gated_norm_mxfp4_kernel'},dict(triton=triton,tl=tl))
    kernels=module('kernels',{'_mxfp4_quant_op','_dynamic_mxfp4_quant_kernel','_gemm_afp4wfp4_kernel','pid_grid','remap_xcd'},dict(triton=triton,tl=tl))
    geometry=module('geometry',{'quantizer_geometry'}, {})
    def runtime(card):
        if type(card)is not int or card not in contract.CARDS or torch.cuda.current_device()!=card:
            raise ValueError('current declared card')
        target=triton.runtime.driver.active.get_current_target()
        if (target.backend,target.arch)!=('hip','gfx1201'):raise ValueError('gfx1201 HIP target')
        return torch,triton,kernels
    ops=module('ops',{'validate_geometry','_tensor','_distinct_storage','quantize_preallocated','gemm_preallocated'},
               dict(runtime=runtime,quantizer_geometry=geometry.quantizer_geometry),('CONFIG',))
    return dict(norm=norm,candidate=candidate,kernels=kernels,ops=ops)
