"""Explicit CPU fixtures for the real producer and reader, never GPU evidence."""
import contextlib
import math
import types
from unittest import mock

import attention_ops as ops
import contract
import stage_probe


def isa(name,arm,variant):
    reduction=arm in ('B2','C2');candidate=arm=='C2' and variant!='same-code'
    fields=dict(group_segment_fixed_size=0,private_segment_fixed_size=0,sgpr_count=33 if reduction else 77,sgpr_spill_count=0,
                vgpr_count=13 if reduction else 245,vgpr_spill_count=4 if arm=='C' and variant=='spill' else 0,
                wavefront_size=32,max_flat_workgroup_size=128)
    body='v_mul_f32 v0, v1, v2' if candidate else 'v_add_f32 v0, v1, v2'
    return name+':\n '+body+'\n s_endpgm\n.Lfunc_end0:\n'+''.join(' .'+k+': '+str(v)+'\n' for k,v in fields.items())


class Tensor:
    def __init__(self,shape,dtype='bfloat16',card=0,cuda=None,base=None,value=0):
        self.shape=tuple(shape);self.ndim=len(shape);self.dtype=dtype;self.device=types.SimpleNamespace(type='cuda',index=card)
        self.cuda=cuda;self.base=base;self.storage=base.storage if base is not None else [value]
        self.layer=getattr(base,'layer',None)
        strides=[];n=1
        for size in reversed(shape):strides.insert(0,n);n*=size
        self.strides=base.strides if base is not None else tuple(strides)
        self.nbytes=n*({'bfloat16':2,'int32':4,'int64':8,'float32':4}[dtype]) if base is None and cuda else 0
        if cuda:cuda.allocated[card]+=self.nbytes
    def __del__(self):
        if getattr(self,'cuda',None):self.cuda.allocated[self.device.index]-=self.nbytes
    @property
    def value(self):return self.storage[0]
    @value.setter
    def value(self,value):self.storage[0]=value
    def __getitem__(self,key):
        if isinstance(key,slice):
            start,stop,step=key.indices(self.shape[0])
            return Tensor((len(range(start,stop,step)),*self.shape[1:]),self.dtype,self.device.index,self.cuda,self)
        return self
    def stride(self,dim=None):return self.strides if dim is None else self.strides[dim]
    def view(self,dtype):return self
    def abs(self):return self
    def max(self):return 1.0


class Cuda:
    def __init__(self,variant):
        self.variant=variant;self.card=0;self.allocated={0:0,1:0};self.workspaces={0:0,1:0};self.leaked={0:0,1:0}
        self.events=[];self.launches=[];self.last_arm='B';self.case=None
        self.reference_layers=[];self.buffers=[]
    def set_device(self,card):self.card=card
    def synchronize(self,*a):self.events.append(('sync',self.card))
    def memory_allocated(self,card):return self.allocated[card]+self.workspaces[card]+self.leaked[card]
    def reset_peak_memory_stats(self,*a):pass
    def max_memory_allocated(self,card):return contract.RESIDENT_BYTES+(80<<20)+self.leaked[card]
    def max_memory_reserved(self,card):return self.max_memory_allocated(card)+(32<<20)
    def get_device_properties(self,card):
        count=0 if self.variant=='invalid-device-count' else 32
        return types.SimpleNamespace(gcnArchName='gfx1201',total_memory=32<<30,multi_processor_count=count)
    def get_device_name(self,card):return 'CPU FIXTURE'
    def empty_cache(self):self.events.append(('empty_cache',self.card))
    def clear(self):
        self.events.append(('clear',self.card))
        if self.variant=='clear-fails':raise RuntimeError('workspace clear failed')
        if self.variant!='clear-noop':self.workspaces={0:0,1:0}
    def Event(self,**kwargs):
        def elapsed(other):
            if self.last_arm=='B':return 10.0
            if self.variant=='short-regression' and self.case=='p724-rows8':return 12.0
            return 9.5 if self.variant=='null-gain' else 8.0
        return types.SimpleNamespace(record=lambda:self.events.append(('event',self.card)),
            synchronize=lambda:self.events.append(('event_sync',self.card)),elapsed_time=elapsed)


def run(root,variant=None):
    cuda=Cuda(variant);torch=types.ModuleType('torch');torch.cuda=cuda;torch._C=types.SimpleNamespace(_cuda_clearCublasWorkspaces=cuda.clear)
    if variant=='clear-absent':del torch._C._cuda_clearCublasWorkspaces
    torch.__version__=contract.TORCH_VERSION;torch.version=types.SimpleNamespace(hip=contract.HIP_VERSION)
    torch.no_grad=contextlib.nullcontext;torch.int16='int16'
    torch.equal=lambda a,b:a.value==b.value
    torch.isfinite=lambda t:types.SimpleNamespace(all=lambda:t.value!=3)
    triton=types.ModuleType('triton');triton.__path__=[];triton.__version__='CPU-FIXTURE'
    triton.cdiv=lambda a,b:(a+b-1)//b;triton.next_power_of_2=lambda n:1<<(n-1).bit_length()
    tl=types.ModuleType('triton.language');tl.constexpr=object();triton.language=tl
    jit_count=0
    def jit(fn):
        nonlocal jit_count
        name=fn.__name__;arm='S'
        if name=='_fwd_grouped_kernel_stage1':arm='B' if jit_count==0 else 'C';jit_count+=1
        compiled_cache={}
        def compiled_for(kwargs):
            key=('B2' if kwargs.get('MAX_KV_SPLITS')==16 else 'C2') if name=='_fwd_kernel_stage2' else arm
            if key not in compiled_cache:
                code=isa(name,key,variant)
                raw=code.encode() if name=='_fwd_grouped_kernel_stage1' else b'CPU-FIXTURE-'+key.encode()
                compiled_cache[key]=types.SimpleNamespace(name=name,asm={'amdgcn':code,'hsaco':raw},
                    metadata=types.SimpleNamespace(shared=16384 if arm in ('B','C') else 0))
            return compiled_cache[key]
        class Kernel:
            def warmup(self,*args,**kwargs):return compiled_for(kwargs)
            def __getitem__(self,grid):
                def launch(*args,**kwargs):
                    cuda.launches.append((name,arm,cuda.card))
                    if name=='_fwd_grouped_kernel_stage1':
                        cuda.events.append(('stage1',cuda.card))
                        cuda.last_arm=arm;tag=0
                        if arm=='C' and cuda.card==1:
                            tag={'bad-numerical':2,'nonfinite':3,'bitwise':1,'mean-band':4}.get(variant,0)
                        if arm=='C' and variant=='last-layer' and args[0].layer==15:tag=2
                        args[6].value=tag
                    elif name=='_fwd_kernel_stage2':
                        cuda.events.append(('stage2',cuda.card));args[2].value=args[0].value
                    elif name=='get_num_kv_splits_triton':cuda.events.append(('helper',cuda.card))
                    return compiled_for(kwargs)
                return launch
        return Kernel()
    triton.jit=jit
    def allocate(torch,card):
        if any(cuda.memory_allocated(c) for c in contract.CARDS if c!=card):raise ValueError('other card allocation')
        layers=[{name:Tensor(shape,card=card,cuda=cuda) for name,shape in {
            'q':(16,12,256),'o':(16,12,256),'o_base':(16,12,256),
            'k_buf':(contract.SLOTS,2,256),'v_buf':(contract.SLOTS,2,256)}.items()} for _ in range(16)]
        for number,t in enumerate(layers):
            t['layer']=number
            for v in t.values():
                if isinstance(v,Tensor):v.layer=number
        if variant=='leak':cuda.leaked[card]=512
        expected=dict(kv_bytes=contract.KV_BYTES,extend_bytes=contract.EXTEND_BYTES,output_bytes=contract.OUTPUT_BYTES)
        return layers,dict(expected,expected=expected,slack=contract.ALLOCATION_SLACK)
    def indices(torch,modules,case,card,device_core_count,arm):
        cuda.case=case['name'];rows=case['queries']
        if variant=='split-mismatch':raise ValueError('device singleton split/length/indptr mismatch')
        cap=contract.arm_cap(arm)
        result=dict(case=case['name'],queries=rows,arm=arm,cap=cap,witness=ops.metadata_record(case,arm,device_core_count),
            kv_indptr=Tensor((rows+1,),'int32',card,cuda),kv_indices=Tensor((rows*(131040+8),),'int64',card,cuda),
            lengths=Tensor((rows,),'int32',card,cuda),splits=Tensor((rows,),'int32',card,cuda),
            partial=Tensor((rows,12,cap,256),'float32',card,cuda),lse=Tensor((rows,12,cap),'float32',card,cuda))
        cuda.buffers.append((case['name'],arm,id(result['partial']),id(result['lse'])))
        return result
    def reference(torch,t,idx):
        cuda.workspaces[cuda.card]=80<<20
        cuda.reference_layers.append((cuda.card,idx['case'],t['layer']))
        return Tensor((idx['queries'],12,256),'float32',cuda.card,cuda,value=1)
    def errors(torch,out,ref):
        if out.value==3:return dict(max_abs=None,mean_abs=None,finite=False)
        return dict(max_abs=.2 if out.value==2 else .01,mean_abs=.1 if out.value==2 else .00102001 if out.value==4 else .001,finite=True)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict('sys.modules',{'torch':torch,'triton':triton,'triton.language':tl}))
        stack.enter_context(mock.patch.object(stage_probe,'OUTPUT',root))
        if variant!='pin-mismatch':stack.enter_context(mock.patch.object(ops,'verify_compiled',lambda *a:None))
        for name,value in [('allocate',allocate),('indices',indices),('reference',reference),('errors',errors),
                           ('empty_partials_untouched',lambda torch,idx:variant!='empty-clobber')]:
            stack.enter_context(mock.patch.object(ops,name,value))
        if variant=='projection':stack.enter_context(mock.patch.object(stage_probe,'projection',side_effect=ValueError('projected time exceeds budget')))
        value=stage_probe.run(stage_probe.time.monotonic()+600,fixture_scope='CPU_FAKE_PRODUCER_NOT_GPU_EVIDENCE')
    return value,cuda
