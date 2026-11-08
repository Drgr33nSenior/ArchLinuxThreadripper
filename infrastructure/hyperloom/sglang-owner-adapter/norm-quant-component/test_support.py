"""Inert metadata and evidence fixtures. No framework imports or GPU evidence."""
from contextlib import nullcontext
import copy
import gzip
import math
from types import SimpleNamespace as NS
import contract
import corpus
import fusion_ops as ops
import support as s

class Device:
    type='cuda'
    def __init__(self,value=0):self.index=int(str(value).split(':')[-1]) if not isinstance(value,Device) else value.index
    def __eq__(self,other):return isinstance(other,Device) and self.index==other.index
    def __hash__(self):return self.index

class Tensor:
    serial=0
    def __init__(self,shape,dtype='bfloat16',device=None,stride=None,storage=None):
        self.shape,self.dtype,self.device=tuple(shape),dtype,device if isinstance(device,Device) else Device(device or 0)
        self.ndim=len(self.shape);self._stride=tuple(stride) if stride else tuple(math.prod(self.shape[i+1:]) for i in range(self.ndim))
        Tensor.serial+=1;self.storage=storage or Tensor.serial;self.poison=None
    def stride(self,index=None):return self._stride if index is None else self._stride[index]
    def dim(self):return self.ndim
    def numel(self):return math.prod(self.shape)
    def element_size(self):return 2 if self.dtype=='bfloat16' else 4 if self.dtype=='float32' else 1
    def is_contiguous(self):return self._stride==tuple(math.prod(self.shape[i+1:]) for i in range(self.ndim))
    def untyped_storage(self):return NS(data_ptr=lambda:self.storage)
    def reshape(self,*shape):
        if len(shape)==1 and isinstance(shape[0],(tuple,list)):shape=tuple(shape[0])
        assert math.prod(shape)==self.numel()
        return Tensor(shape,self.dtype,self.device,storage=self.storage)
    def view(self,dtype):
        other=Tensor(self.shape,dtype,self.device,self._stride,self.storage);other.parent=self;return other
    def fill_(self,value):self.poison=value;getattr(self,'parent',self).poison=value;return self
    def __setitem__(self,key,value):pass

class Torch:
    Tensor=Tensor;device=Device
    bfloat16,uint8,float32,int16,int32='bfloat16','uint8','float32','int16','int32'
    def __init__(self):
        self.core_count=32;self.card=0;self.allocations=[]
        self.cuda=NS(device=lambda device:nullcontext(),get_device_properties=lambda d:NS(multi_processor_count=self.core_count),
                     current_device=lambda:self.card)
    def empty(self,shape,*,dtype,device):
        value=Tensor(shape,dtype,device);self.allocations.append(value);return value
    def empty_strided(self,shape,stride,*,dtype,device):
        value=Tensor(shape,dtype,device,stride);self.allocations.append(value);return value
    def empty_like(self,value):return self.empty(value.shape,dtype=value.dtype,device=value.device)

class Kernel:
    def __init__(self,fn):self.fn=fn;self.__name__=fn.__name__;self.calls=[];self.objects={}
    def run(self,*args,grid,warmup=False,**kwargs):
        signature=tuple((tuple(v.shape),v.dtype,tuple(v.stride())) if isinstance(v,Tensor) else (type(v).__name__,v) for v in args)
        key=(signature,tuple(sorted(kwargs.items())))
        value=self.objects.setdefault(key,NS(name=self.__name__))
        if not warmup:self.calls.append(dict(grid=list(grid),args=args,kwargs=kwargs))
        return value
    def __getitem__(self,grid):return lambda *args,**kw:self.run(*args,grid=grid,**kw)

class Heuristics:
    def __init__(self,fn,values):self.fn,self.values=fn,values
    def run(self,*args,**kwargs):
        named=dict(zip(self.fn.fn.__code__.co_varnames,args),**kwargs)
        return self.fn.run(*args,**dict(kwargs,**{k:v(named) for k,v in self.values.items()}))
    def __getitem__(self,grid):return lambda *args,**kw:self.run(*args,grid=grid,**kw)

def frameworks():
    torch=Torch()
    triton=NS(jit=Kernel,heuristics=lambda values:lambda fn:Heuristics(fn,values),
       cdiv=lambda a,b:(a+b-1)//b,next_power_of_2=lambda n:1<<(n-1).bit_length(),
       runtime=NS(driver=NS(active=NS(get_current_target=lambda:NS(backend='hip',arch='gfx1201')))))
    return torch,triton,NS(constexpr=object())

def layer(tokens=8,card=0):
    device=Device(card)
    return dict(tokens=tokens,x=Tensor((tokens*24,128),device=device),z=Tensor((tokens*24,128),device=device),
      w=Tensor((128,),device=device),weight=Tensor((5120,1536),'uint8',device),weight_scale=Tensor((5120,96),'uint8',device))

def outputs(tokens,arm,inclusive=True,changed=False):
    sizes=dict(packed=tokens*1536,scales=tokens*96,rstd=tokens*24*4)
    if arm=='B':sizes['normalized']=tokens*3072*2
    if inclusive:sizes['output']=tokens*5120*2
    return {k:dict(bytes=size,sha256=s.sha((k+str(tokens)+str(changed)).encode()),finite=True) for k,size in sizes.items()}

def case_result(case):
    return dict(case=case['name'],corpus=corpus.identity(case),status='CORRECT',baseline_wall_seconds=0.01,
      outputs={a+str(p):outputs(case['tokens'],a) for a in contract.ARMS for p in (0,1)},
      launches={a+str(p):ops.expected_counts(a) for a in contract.ARMS for p in (0,1)})

def graph_result(tokens):
    return dict(tokens=tokens,independent_storage=True,initial={a:outputs(tokens,a) for a in contract.ARMS},
      changed={a:outputs(tokens,a,changed=True) for a in contract.ARMS})

def timings(case):
    return [dict(case=case,round=r,position=p,arm=a,elapsed_ms=10.0 if a=='B' else 8.0)
            for r in range(contract.TIMING_ROUNDS) for p,a in enumerate(contract.order(r))]

def timing_check(case):
    tokens=int(case.split('-')[0][1:]);inclusive=case.endswith('inclusive')
    return dict(case=case,capture_seconds=0.1,baseline_replay_wall_seconds=0.01,projected_seconds=0.36,
       remaining_seconds=100,post_timing_audit=dict(norm=1,candidate=1,quantizer=1,gemm=2 if inclusive else 0),
       outputs={a:[outputs(tokens,a,inclusive) for _ in range(contract.LAYERS)] for a in contract.ARMS})

def compiler_records(card,artifacts):
    result={}
    for tokens in contract.TOKENS:
        result[str(tokens)]={}
        for stage in contract.STAGES:
            parts={}
            for part in ('ttir','ttgir','llir','amdgcn','hsaco'):
                raw=b'inert compiler fixture; never valid GPU evidence'
                artifact=artifacts.write(f'compiler-card{card}-{tokens}-{stage}-{part}.gz',gzip.compress(raw,mtime=0))
                parts[part]=dict(raw_bytes=len(raw),raw_sha256=s.sha(raw),artifact=artifact)
            result[str(tokens)][stage]=dict(parts=parts,resources=copy.deepcopy(contract.COMPILER_PINS[str(tokens)][stage]['resources']))
    return result

def payload(artifacts,sources):
    rows=[]
    for card in contract.CARDS:
        row=dict(card=card,arch='gfx1201',device_core_count=32,device_name='CPU FIXTURE',rows_per_block={'8':4,'16':4},
          started_monotonic=100+card*50,released_monotonic=120+card*50,allocated_before=0,cleanup=dict(complete=True,allocated_after=0),
          peak=dict(allocated=1<<30,reserved=2<<30,total=32<<30),compiler=compiler_records(card,artifacts),
          cases=[case_result(c) for c in contract.CASES],graphs=[graph_result(t) for t in contract.TOKENS],
          timing_layers=[case_result(dict(name=f't{t}-timing-layer{i}',tokens=t,kind='finite',finite=True)) for t in contract.TOKENS for i in range(contract.LAYERS)],
          candidate_status='CORRECT',timing_started=True,timing_checks=[timing_check(c) for c in contract.TIMED],
          timings=[v for c in contract.TIMED for v in timings(c)])
        keys=('card','device_core_count','rows_per_block','compiler','cases','graphs','timing_layers','candidate_status')
        row['gates_artifact']=artifacts.write(f'GATES-card{card}-BEFORE-TIMING.json',s.canonical(dict({k:row[k] for k in keys},timing_started=False)))
        row['usefulness']=contract.usefulness(row['timings']);rows.append(row)
    return dict(schema=1,mode=contract.MODE,capture_provenance=contract.PROVENANCE,fixture_scope='inert-f1-tests',
      contract=contract.bounds(),sources=sources,complete=True,status='COMPLETE',failure=None,qualification=False,serving_ready=False,profiling=False,
      model_starts=0,http_requests=0,runtime=dict(torch_version=contract.TORCH_VERSION,hip_version=contract.HIP_VERSION,triton_version='fixture'),
      cards=rows,useful=True,candidate_disposition='USEFUL',cumulative_disposition='USEFUL',elapsed_seconds=50,
      artifacts=list(artifacts.members),artifact_bytes_before_result=artifacts.bytes)
