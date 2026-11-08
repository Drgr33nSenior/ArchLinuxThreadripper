"""Frozen real wrapper calls, compiler audit, exact bytes and captured boundary."""
from pathlib import Path
import time
import contract
import compiler_gate
import corpus
import source_loader
import support as s

ROOT=Path(__file__).resolve().parent
ATTRS={'norm':('norm','_layer_norm_fwd_1pass_kernel'),
       'candidate':('candidate','_gated_norm_mxfp4_kernel'),
       'quantizer':('kernels','_dynamic_mxfp4_quant_kernel'),
       'gemm':('kernels','_gemm_afp4wfp4_kernel')}

def kernels():
    import torch
    import triton
    import triton.language as tl
    return source_loader.load(ROOT,torch,triton,tl)

def check_launch(args,kwargs,grid,tokens,stage,card):
    want=contract.LAUNCHES[str(tokens)][stage]
    if list(grid)!=want['grid'] or kwargs!=want['kwargs'] or len(args)!=len(want['args']):
        raise ValueError('exact launch grid/options/arity')
    if any(type(kwargs[k])is not type(v) for k,v in want['kwargs'].items()):raise ValueError('launch option types')
    for value,expected in zip(args,want['args']):
        if isinstance(expected,dict):
            if (list(value.shape)!=expected['shape'] or list(value.stride())!=expected['stride']
                or str(value.dtype).removeprefix('torch.')!=expected['dtype']
                or value.device.type!='cuda' or value.device.index!=card):raise ValueError('launch tensor metadata')
        elif type(value)is not type(expected) or value!=expected:raise ValueError('launch scalar')

def poison(torch,tensor,which):
    p=contract.POISONS[which]
    if tensor.dtype==torch.uint8:tensor.fill_(p['byte'])
    elif tensor.dtype==torch.bfloat16:tensor.view(torch.int16).fill_(p['bf16'] if p['bf16']<32768 else p['bf16']-65536)
    elif tensor.dtype==torch.float32:tensor.view(torch.int32).fill_(p['fp32'] if p['fp32']<1<<31 else p['fp32']-(1<<32))
    else:raise ValueError('poison dtype')

class LaunchAudit:
    def __init__(self,torch,modules,tokens,card,*,launch=True,poison_index=None):
        self.torch,self.modules,self.tokens,self.card=torch,modules,tokens,card
        self.launch,self.poison_index=launch,poison_index
        self.records={};self.counts={k:0 for k in contract.STAGES}
    def __enter__(self):
        self.saved={stage:getattr(self.modules[group],attr) for stage,(group,attr) in ATTRS.items()}
        outer=self
        class Audit:
            def __init__(self,stage):self.stage=stage
            def __getitem__(self,grid):
                def call(*args,**kwargs):
                    check_launch(args,kwargs,grid,outer.tokens,self.stage,outer.card)
                    kernel=outer.saved[self.stage]
                    compiled=kernel.run(*args,grid=grid,warmup=True,**kwargs)
                    compiler_gate.verify(compiled,outer.tokens,self.stage)
                    if self.stage in outer.records and outer.records[self.stage] is not compiled:
                        raise ValueError('compile object changed within audit')
                    outer.records[self.stage]=compiled;outer.counts[self.stage]+=1
                    if outer.launch:
                        if outer.poison_index is not None:
                            indices={'norm':(1,6),'candidate':(1,2,9),'quantizer':(1,2),'gemm':(2,)}[self.stage]
                            for index in indices:poison(outer.torch,args[index],outer.poison_index)
                        if kernel[grid](*args,**kwargs) is not compiled:raise ValueError('launched object differs from audited object')
                    return compiled
                return call
        for stage,(group,attr) in ATTRS.items():setattr(self.modules[group],attr,Audit(stage))
        return self
    def __exit__(self,*exc):
        for stage,(group,attr) in ATTRS.items():setattr(self.modules[group],attr,self.saved[stage])
        return False

def from_bits(torch,raw,shape,device):
    return torch.frombuffer(bytearray(raw),dtype=torch.uint16).view(torch.bfloat16).reshape(shape).to(device=device)

def layer(torch,case,card,number=0,weights=None):
    tokens=case['tokens'];device=f'cuda:{card}'
    values={k:from_bits(torch,corpus.raw(case,k),(tokens*24,128) if k!='w' else (128,),device) for k in ('x','z','w')}
    if weights is None:
        g=torch.Generator(device=device);g.manual_seed(contract.SEED+number)
        weights=(torch.randint(0,256,(5120,1536),dtype=torch.uint8,device=device,generator=g),
                 torch.randint(122,128,(5120,96),dtype=torch.uint8,device=device,generator=g))
    values.update(weight=weights[0],weight_scale=weights[1],tokens=tokens)
    return values

def execute(torch,modules,t,arm,*,inclusive=True):
    tokens=t['tokens'];m=tokens*24;x,z,w=t['x'],t['z'],t['w'];device=x.device
    packed=torch.empty((tokens,1536),dtype=torch.uint8,device=device)
    scales=torch.empty_strided((tokens,96),(1,tokens),dtype=torch.uint8,device=device)
    if arm=='B':
        normalized,mean,rstd=modules['norm']._layer_norm_fwd(x,w,None,1e-6,z=z,is_rms_norm=True)
        if mean is not None:raise ValueError('RMSNorm only')
        modules['ops'].quantize_preallocated(normalized.reshape(tokens,3072),packed,scales,expected_device=device.index)
    elif arm=='C':
        rstd=torch.empty((m,),dtype=torch.float32,device=device)
        modules['candidate']._gated_norm_mxfp4_kernel[(m//4,1)](
          x,packed,scales,tokens,24,w,None,z,None,rstd,128,128,128,0,0,m,128,1e-6,
          **contract.LAUNCHES[str(tokens)]['candidate']['kwargs'])
    else:raise ValueError('frozen arm')
    out=dict(packed=packed,scales=scales,rstd=rstd)
    if arm=='B':out['normalized']=normalized
    if inclusive:
        output=torch.empty((tokens,5120),dtype=torch.bfloat16,device=device)
        modules['ops'].gemm_preallocated(packed,t['weight'],scales,t['weight_scale'],output,expected_device=device.index)
        out['output']=output
    return out

def digest(torch,t):
    raw=bytes(t.detach().contiguous().view(torch.uint8).reshape(-1).cpu().tolist())
    return dict(bytes=len(raw),sha256=s.sha(raw),finite=bool(torch.isfinite(t).all().item()))

def snapshots(torch,outputs):return {name:digest(torch,value) for name,value in outputs.items()}

def expected_counts(arm,inclusive=True):
    return dict(norm=int(arm=='B'),quantizer=int(arm=='B'),candidate=int(arm=='C'),gemm=int(inclusive))

def check_case(torch,modules,t,case,card,deadline):
    records={};walls={};launches={}
    for arm in contract.ARMS:
        for poison_index in (0,1):
            s.check(deadline);start=time.monotonic()
            with LaunchAudit(torch,modules,t['tokens'],card,poison_index=poison_index) as audit:
                out=execute(torch,modules,t,arm)
            torch.cuda.synchronize(card)
            key=arm+str(poison_index);walls[key]=time.monotonic()-start
            launches[key]=dict(audit.counts)
            if audit.counts!=expected_counts(arm):raise ValueError('exact per-case stage count')
            records[key]=snapshots(torch,out);del out
    row=dict(case=case['name'],corpus=corpus.identity(case),outputs=records,launches=launches,
             baseline_wall_seconds=walls['B0'])
    row['status']='CORRECT' if numerical_pass(row,case) else 'INCORRECT'
    return row

def numerical_pass(row,case):
    values=row['outputs']
    if set(values)!=set(('B0','B1','C0','C1')):return False
    for arm in contract.ARMS:
        if values[arm+'0']!=values[arm+'1']:return False
    for key in ('packed','scales','rstd','output'):
        if values['B0'][key]!=values['C0'][key]:return False
    return not case['finite'] or all(v['finite'] for arm in values.values() for v in arm.values())

def capture(torch,modules,layers,arm,inclusive,card,deadline):
    s.check(deadline);stream=torch.cuda.Stream(device=card)
    if stream.device.type!='cuda' or stream.device.index!=card:raise ValueError('capture stream card identity')
    stream.wait_stream(torch.cuda.current_stream(card))
    with torch.cuda.stream(stream):
        warm=[execute(torch,modules,t,arm,inclusive=inclusive) for t in layers]
    stream.synchronize();del warm
    graph=torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph,stream=stream):
        outputs=[execute(torch,modules,t,arm,inclusive=inclusive) for t in layers]
    for row in outputs:
        for value in row.values():poison(torch,value,0 if arm=='B' else 1)
    graph.replay();torch.cuda.synchronize(card);s.check(deadline)
    return graph,outputs

def replay_ms(torch,graph,card):
    start,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
    start.record();graph.replay();end.record();end.synchronize()
    return float(start.elapsed_time(end))
