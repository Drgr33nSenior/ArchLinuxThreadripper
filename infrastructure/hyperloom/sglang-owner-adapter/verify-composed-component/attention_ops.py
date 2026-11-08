"""Frozen grouped verification operands, two-stage audit and FP32 oracle."""
import json
import math
from pathlib import Path

import contract
import source_loader
import support as s

ROOT = Path(__file__).resolve().parent


def kernels():
    import triton
    import triton.language as tl
    return source_loader.load_pair((ROOT/'decode_attention_baseline.py').read_bytes(),
                                   (ROOT/'decode_attention_candidate.py').read_bytes(),
                                   (ROOT/'metadata.py').read_bytes(), triton, tl)


def allocate(torch, card):
    device=f'cuda:{card}';layers=[]
    torch.cuda.synchronize(card);before=torch.cuda.memory_allocated(card)
    for layer in range(contract.LAYERS):
        g=torch.Generator(device=device);g.manual_seed(contract.SEED+layer)
        layers.append({key:torch.randn((contract.SLOTS,2,256),dtype=torch.bfloat16,device=device,generator=g)
                       for key in ('k_buf','v_buf')})
    torch.cuda.synchronize(card);after_kv=torch.cuda.memory_allocated(card)
    for layer,t in enumerate(layers):
        g=torch.Generator(device=device);g.manual_seed(contract.SEED+1000+layer)
        t['q']=torch.randn((contract.CHUNK,12,256),dtype=torch.bfloat16,device=device,generator=g)
    torch.cuda.synchronize(card);after_q=torch.cuda.memory_allocated(card)
    for t in layers:
        for target in contract.OUTPUTS.values():t[target]=torch.empty_like(t['q'])
    torch.cuda.synchronize(card);after_out=torch.cuda.memory_allocated(card)
    expected=dict(kv_bytes=contract.KV_BYTES,extend_bytes=contract.EXTEND_BYTES,output_bytes=contract.OUTPUT_BYTES)
    measured=dict(kv_bytes=after_kv-before,extend_bytes=after_q-after_kv,output_bytes=after_out-after_q)
    if any(not v<=measured[k]<=v+contract.ALLOCATION_SLACK for k,v in expected.items()):
        raise ValueError('measured allocation outside frozen bound')
    return layers,dict(measured,expected=expected,slack=contract.ALLOCATION_SLACK)


def metadata_record(case,arm,device_core_count):
    cap=contract.arm_cap(arm);validate_device_core_count(device_core_count)
    lengths=[p+r+1 for p in case['prefixes'] for r in range(8)]
    if len(lengths)!=case['queries'] or any(type(v) is not int or not 1<=v<=131048 for v in lengths):
        raise ValueError('positive bounded verification lengths')
    indptr=[0]
    for length in lengths:indptr.append(indptr[-1]+length)
    empty=[];splits=[];spans=[]
    for length in lengths:
        requested=min((int(device_core_count*max(math.log2(length/64),1))+1)//2,cap)
        chunk=(length+requested-1)//requested
        count=(length+chunk-1)//chunk
        span=((length+count-1)//count+31)//32*32
        if not 1<=count<=cap or span*count<length:raise ValueError('split coverage')
        splits.append(count);spans.append(span);empty.append(sum(i*span>=length for i in range(cap)))
    return dict(arm=arm,cap=cap,device_core_count=device_core_count,lengths=lengths,indptr=indptr,splits=splits,spans=spans,
                indices_verified=True,indices=case['indices'],empty_splits=empty)


def validate_device_core_count(value):
    # The selected backend passes this API value directly to its split helper.
    # HIP may report WGPs rather than physical CUs; do not substitute a CU count.
    if type(value) is not int or not 0 < value < (1 << 31):
        raise ValueError('positive int32 runtime device_core_count required; observed=' + repr(value))
    return value


def indices(torch, modules, case, card, device_core_count,arm):
    if case not in contract.CASES:raise ValueError('frozen metadata case')
    validate_device_core_count(device_core_count)
    device=f'cuda:{card}';record=metadata_record(case,arm,device_core_count);rows=case['queries'];cap=contract.arm_cap(arm)
    lengths=torch.tensor(record['lengths'],dtype=torch.int32,device=device)
    indptr=torch.tensor(record['indptr'],dtype=torch.int32,device=device)
    packed=torch.full((rows*(131040+8),),contract.SLOTS-1,dtype=torch.int64,device=device)
    base=1;row=0
    for prefix in case['prefixes']:
        slots=torch.arange(base,base+prefix,dtype=torch.int64,device=device)
        if case['indices']=='even-odd':slots=torch.cat((slots[::2],slots[1::2]))
        for causal in range(8):
            tail=torch.arange(base+prefix,base+prefix+causal+1,dtype=torch.int64,device=device)
            values=torch.cat((slots,tail))
            lo,hi=record['indptr'][row:row+2]
            packed[lo:hi].copy_(values)
            if not bool(torch.equal(packed[lo:hi],values)):raise ValueError('packed causal index stream')
            row+=1
        base+=prefix+8
    if base>contract.SLOTS:raise ValueError('bounded physical slot fixture')
    splits=torch.empty((rows,),dtype=torch.int32,device=device)
    for row in range(rows):
        modules[0]._split_helper[(1,)](splits[row:row+1],lengths[row:row+1],1,1,12,2,cap,device_core_count,MAX_NUM_SEQ=256)
    torch.cuda.synchronize(card)
    observed=dict(splits=splits.tolist(),lengths=lengths.tolist(),indptr=indptr.tolist())
    if any(observed[k]!=record[k] for k in observed):
        raise ValueError('device singleton split/length/indptr mismatch: ' + json.dumps(
            dict(case=case['name'],device_core_count=device_core_count,observed=observed,
                 expected={k:record[k] for k in observed}),sort_keys=True))
    return dict(case=case['name'],arm=arm,cap=cap,queries=rows,kv_indptr=indptr,kv_indices=packed,lengths=lengths,splits=splits,
                partial=torch.full((rows,12,cap,256),float('nan'),dtype=torch.float32,device=device),
                lse=torch.full((rows,12,cap),float('nan'),dtype=torch.float32,device=device),witness=record)


def refresh_splits(module,idx):
    for row in range(idx['queries']):
        module._split_helper[(1,)](idx['splits'][row:row+1],idx['lengths'][row:row+1],1,1,12,2,
                                  idx['cap'],idx['witness']['device_core_count'],MAX_NUM_SEQ=256)


def served_call(function,t,idx,out):
    q=idx['queries']
    function(t['q'][:q],t['k_buf'],t['v_buf'],out[:q],idx['kv_indptr'],idx['kv_indices'],
             idx['partial'],idx['lse'],idx['splits'],idx['cap'],0.0625,1.0,1.0,enable_lean=False,page_size=1)


def verify_compiled(compiled, arm, stage):
    pins=contract.COMPILER_PINS[arm] if stage==1 else contract.STAGE2_PINS[arm]
    name='_fwd_grouped_kernel_stage1' if stage==1 else '_fwd_kernel_stage2'
    if compiled.name!=name or type(pins) is not dict or set(pins)!={'amdgcn','hsaco'}:
        raise ValueError('exact kernel and completed native compiler identity')
    for part,pin in pins.items():
        raw=compiled.asm[part];raw=raw.encode() if isinstance(raw,str) else raw
        if s.sha(raw)!=pin:raise ValueError('pinned CPU compiler identity mismatch: '+arm+'/'+part)
    if stage==1 and compiled.metadata.shared!=contract.DYNAMIC_LDS_BYTES:
        raise ValueError('pinned dynamic LDS allocation')
    if stage==2 and compiled.metadata.shared!=contract.STAGE2_DYNAMIC_LDS_BYTES:
        raise ValueError('pinned shared reduction LDS allocation')


def check_launch(args,kwargs,grid,expected,card):
    if list(grid)!=expected['grid'] or kwargs!=expected['kwargs'] or len(args)!=len(expected['args']):
        raise ValueError('frozen two-stage launch geometry/constants/arity')
    if any(type(kwargs[k]) is not type(v) for k,v in expected['kwargs'].items()):
        raise ValueError('exact launch constant types')
    for actual,want in zip(args,expected['args']):
        if isinstance(want,dict):
            if (list(actual.shape)!=want['shape'] or list(actual.stride())!=want['stride']
                    or str(actual.dtype).removeprefix('torch.')!=want['dtype']
                    or actual.device.type!='cuda' or actual.device.index!=card):
                raise ValueError('frozen launch tensor metadata/device')
        elif type(actual) is not type(want) or actual!=want:
            raise ValueError('exact launch scalar types/values')


class LaunchAudit:
    def __init__(self,module,arm,case,card,*,launch=True):
        if arm not in contract.ARMS or case not in contract.CASES or card not in contract.CARDS:
            raise ValueError('frozen arm/case/card')
        self.module,self.arm,self.case,self.card,self.launch=module,arm,case,card,launch
        self.compiled=[];self.reductions=[]
        self.expected=json.loads((ROOT/'LAUNCHES.json').read_bytes())[case['name']][arm]

    def __enter__(self):
        self.saved=(self.module._fwd_grouped_kernel_stage1,self.module._fwd_kernel_stage2)
        outer=self
        class AuditKernel:
            def __init__(self,stage):self.stage=stage
            def __getitem__(self,grid):
                def call(*args,**kwargs):
                    check_launch(args,kwargs,grid,outer.expected['stage'+str(self.stage)],outer.card)
                    kernel=outer.saved[self.stage-1]
                    compiled=kernel.warmup(*args,grid=grid,**kwargs)
                    verify_compiled(compiled,outer.arm,self.stage)
                    if outer.launch and kernel[grid](*args,**kwargs) is not compiled:
                        raise ValueError('launched object differs from audited compile')
                    (outer.compiled if self.stage==1 else outer.reductions).append(compiled)
                    return compiled
                return call
        self.module._fwd_grouped_kernel_stage1=AuditKernel(1)
        self.module._fwd_kernel_stage2=AuditKernel(2)
        return self

    def __exit__(self,*exc):
        self.module._fwd_grouped_kernel_stage1,self.module._fwd_kernel_stage2=self.saved
        return False


def chunk(torch,function,layers,idx,target):
    for t in layers:served_call(function,t,idx,t[target])


def write_witness(torch,idx):
    def masks(value):
        rows=value.tolist()
        if len(rows)!=idx['queries'] or any(len(row)!=idx['cap'] or any(type(v) is not bool for v in row) for row in rows):
            raise ValueError('exact write-mask geometry')
        return [sum(1<<slot for slot,v in enumerate(row) if v) for row in rows]
    return dict(partial_finite=masks(torch.isfinite(idx['partial']).all(dim=-1).all(dim=1)),
                partial_nan=masks(torch.isnan(idx['partial']).all(dim=-1).all(dim=1)),
                lse_finite=masks(torch.isfinite(idx['lse']).all(dim=1)),
                lse_nan=masks(torch.isnan(idx['lse']).all(dim=1)))


def expected_writes(metadata):
    cap=metadata['cap'];finite=[];empty=[]
    for length,span,count in zip(metadata['lengths'],metadata['spans'],metadata['splits']):
        active=sum(1<<slot for slot in range(cap) if slot<count and slot*span<length)
        finite.append(active);empty.append(((1<<cap)-1)^active)
    return dict(partial_finite=finite,partial_nan=empty,lse_finite=finite,lse_nan=empty)


def correct_chunk(torch,function,layers,idx,target):
    writes=[]
    for t in layers:
        # Poison every layer, not only the first use of reused partial buffers.
        t[target][:idx['queries']].fill_(float('nan'))
        idx['partial'].fill_(float('nan'));idx['lse'].fill_(float('nan'))
        served_call(function,t,idx,t[target])
        writes.append(write_witness(torch,idx))
    return writes


def bitwise_equal(torch,a,b):return bool(torch.equal(a.view(torch.int16),b.view(torch.int16)))


def all_finite(torch,layers,target,q):return all(bool(torch.isfinite(t[target][:q]).all()) for t in layers)


def reference(torch,t,idx):
    q=t['q'][:idx['queries']].float()
    out=torch.empty_like(q,dtype=torch.float32)
    ptr=idx['witness']['indptr']
    for row in range(idx['queries']):
        slots=idx['kv_indices'][ptr[row]:ptr[row+1]]
        for group in range(2):
            k=t['k_buf'].index_select(0,slots)[:,group].float()
            v=t['v_buf'].index_select(0,slots)[:,group].float()
            for head in range(group*6,(group+1)*6):
                scores=(q[row,head:head+1]@k.T)*contract.SM_SCALE
                out[row,head:head+1]=torch.softmax(scores,dim=-1)@v
                del scores
            del k,v
    return out


def errors(torch,output,ref):
    if not bool(torch.isfinite(output).all()) or not bool(torch.isfinite(ref).all()):
        return dict(max_abs=None,mean_abs=None,finite=False)
    delta=(output.float()-ref).abs();maximum,mean=float(delta.max()),float(delta.mean())
    return dict(max_abs=maximum,mean_abs=mean,finite=math.isfinite(maximum) and math.isfinite(mean))


def timed(torch,module,layers,idx):
    start,end=torch.cuda.Event(enable_timing=True),torch.cuda.Event(enable_timing=True)
    start.record();refresh_splits(module,idx);chunk(torch,module.decode_attention_fwd,layers,idx,'o');end.record();end.synchronize()
    return start.elapsed_time(end)
