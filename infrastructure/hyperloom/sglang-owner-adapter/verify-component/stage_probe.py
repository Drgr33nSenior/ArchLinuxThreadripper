"""Grouped verification baseline versus index prefetch: compile, numerical gates, timing; cards run sequentially."""
import gc
import json
import math
from pathlib import Path
import time
import traceback

import attention_ops as ops
import compiler_gate
import contract
import source_loader
import support as s

ROOT=Path(__file__).resolve().parent
OUTPUT=Path('/mnt/output/mxfp4')
MODE,CAPTURE_PROVENANCE=contract.MODE,contract.PROVENANCE
RUNTIME_NAMES=('attention_ops.py','compiler_gate.py','contract.py','decode_attention_baseline.py',
               'decode_attention_candidate.py','source_loader.py','metadata.py','LAUNCHES.json','read_component.py','stage_probe.py','support.py')


def source_identity():
    raw=(ROOT/'RUNTIME-SOURCE-PINS.json').read_bytes()
    if len(raw)>32768:raise ValueError('bounded source inventory')
    pins=json.loads(raw)
    if set(pins)!=set(RUNTIME_NAMES):raise ValueError('closed runtime source set')
    for name,pin in pins.items():
        p=ROOT/name
        if p.is_symlink() or p.resolve()!=p or not p.is_file() or p.stat().st_size>2<<20 or s.sha(p.read_bytes())!=pin:
            raise ValueError('runtime source identity: '+name)
    if pins['decode_attention_baseline.py']!=contract.BASELINE_SHA256:raise ValueError('selected grouped-verification baseline')
    if pins['decode_attention_candidate.py']!=contract.CANDIDATE_SHA256:raise ValueError('frozen index-prefetch candidate')
    if pins['metadata.py']!=contract.METADATA_SHA256:raise ValueError('selected singleton metadata source')
    return dict(pins=pins,inventory_sha256=s.sha(raw))


def peak(torch,card):
    a,b=torch.cuda.max_memory_allocated(card),torch.cuda.max_memory_reserved(card)
    if not 0<=a<=b<=contract.DEVICE_LIMIT:raise ValueError('allocated/reserved device cap')
    return dict(allocated=a,reserved=b,total=torch.cuda.get_device_properties(card).total_memory)


def compile_pair(torch,modules,layers,card,artifacts,device_core_count):
    idx=ops.indices(torch,modules,contract.CASES[0],card,device_core_count);records={}
    for arm,module in zip(('B','C'),modules):
        with ops.LaunchAudit(module,arm,contract.CASES[0],card,launch=False) as audit:
            ops.served_call(module.decode_attention_fwd,layers[0],idx,layers[0]['o'])
        torch.cuda.synchronize(card)
        if len(audit.compiled)!=1 or len(audit.reductions)!=1:raise ValueError('one compile-only witness per stage/arm')
        records[arm]=compiler_gate.store(audit.compiled[0],card,arm,artifacts)
        if arm=='B':shared=audit.reductions[0]
        elif audit.reductions[0] is not shared:raise ValueError('shared compiled stage2 object')
    records['S']=compiler_gate.store(shared,card,'S',artifacts)
    return records


def correctness(torch,modules,layers,case,card,compiler,deadline,device_core_count):
    s.check(deadline);b,c=modules;idx=ops.indices(torch,modules,case,card,device_core_count);q=case['queries']
    row=dict(case=case['name'],metadata=idx['witness']);start=time.monotonic()
    with ops.LaunchAudit(b,'B',case,card) as audit:
        ops.chunk(torch,b.decode_attention_fwd,layers,idx,'o_base')
    torch.cuda.synchronize(card);row['baseline_wall_seconds']=time.monotonic()-start
    if {s.sha(k.asm['hsaco']) for k in audit.compiled}!={compiler['B']['parts']['hsaco']['raw_sha256']}:
        raise ValueError('baseline specialization changed without compiler witness')
    if not ops.all_finite(torch,layers,'o_base',q):raise ValueError('baseline nonfinite')
    ref=ops.reference(torch,layers[0],idx);base=ops.errors(torch,layers[0]['o_base'][:q],ref)
    ref_max=float(ref.abs().max())
    if not contract.valid_errors(base) or not math.isfinite(ref_max) or ref_max<=0:raise ValueError('finite FP32 reference and baseline')
    row.update(baseline=base,reference_max=ref_max)
    s.check(deadline)
    with ops.LaunchAudit(b,'B',case,card):
        ops.chunk(torch,b.decode_attention_fwd,layers,idx,'o')
    torch.cuda.synchronize(card)
    identity=sum(ops.bitwise_equal(torch,t['o'][:q],t['o_base'][:q]) for t in layers)
    row['baseline_repeat_equal_layers']=identity
    if identity!=contract.LAYERS:raise ValueError('baseline repeat changed selected verification attention')
    s.check(deadline)
    with ops.LaunchAudit(c,'C',case,card) as audit:
        ops.chunk(torch,c.decode_attention_fwd,layers,idx,'o')
    torch.cuda.synchronize(card)
    if {s.sha(k.asm['hsaco']) for k in audit.compiled}!={compiler['C']['parts']['hsaco']['raw_sha256']}:
        raise ValueError('candidate specialization changed without compiler witness')
    finite=ops.all_finite(torch,layers,'o',q)
    equal=sum(ops.bitwise_equal(torch,t['o'][:q],t['o_base'][:q]) for t in layers)
    err=ops.errors(torch,layers[0]['o'][:q],ref)
    ok=finite and equal==contract.LAYERS and contract.within_tolerance(err,base,ref_max)
    row.update(candidate=err,finite_all_layers=finite,equal_layers=equal,
               status='CORRECT' if ok else 'INCORRECT')
    return row


def projection(rows,deadline):
    walls={r['case']:r['baseline_wall_seconds'] for r in rows}
    estimate=contract.CANDIDATE_COST_FACTOR*2*(contract.WARMUP_CALLS+contract.TIMING_ROUNDS)*sum(walls[k] for k in contract.TIMED)
    remaining=deadline-time.monotonic()-10;needed=contract.TIMING_HEADROOM*estimate
    result=dict(estimated_seconds=estimate,projected_seconds=needed,remaining_seconds=remaining,fits=needed<=remaining)
    if not result['fits']:raise ValueError('timing projection does not fit; no timing started')
    return result


def timing(torch,modules,layers,card,deadline,device_core_count):
    samples=[];by_name={c['name']:c for c in contract.CASES};funcs=dict(zip(('B','C'),modules))
    for name in contract.TIMED:
        idx=ops.indices(torch,modules,by_name[name],card,device_core_count)
        for arm in ('B','C'):
            for _ in range(contract.WARMUP_CALLS):
                s.check(deadline);ops.timed(torch,funcs[arm].decode_attention_fwd,layers,idx)
        for round_index in range(contract.TIMING_ROUNDS):
            for position,arm in enumerate(contract.timing_order(round_index)):
                s.check(deadline);ms=ops.timed(torch,funcs[arm].decode_attention_fwd,layers,idx)
                if type(ms) not in (int,float) or not math.isfinite(ms) or ms<=0:raise ValueError('positive finite timing')
                samples.append(dict(case=name,round=round_index,position=position,arm=arm,elapsed_ms=ms,measurement=contract.MEASUREMENT))
    return samples


def run_card(torch,modules,row,artifacts,deadline):
    row['started_monotonic']=time.monotonic()
    card=row['card'];layers=None;torch.cuda.set_device(card)
    before=torch.cuda.memory_allocated(card);torch.cuda.reset_peak_memory_stats(card)
    row.update(allocated_before=before,cases=[],timings=[],timing_started=False)
    try:
        device_core_count=ops.validate_device_core_count(row['device_core_count'])
        layers,row['memory']=ops.allocate(torch,card)
        row['compiler']=compile_pair(torch,modules,layers,card,artifacts,device_core_count)
        row['candidate_status']=compiler_gate.compare(row['compiler']['B'],row['compiler']['C'])
        if row['candidate_status']=='DISTINCT_CODE':
            for case in contract.CASES:
                record=correctness(torch,modules,layers,case,card,row['compiler'],deadline,device_core_count)
                row['cases'].append(record);peak(torch,card)
                if record['status']!='CORRECT':
                    row['candidate_status']='INCORRECT';break
            else:row['candidate_status']='CORRECT'
        gate={k:row[k] for k in ('card','device_core_count','memory','compiler','cases','candidate_status','timing_started')}
        row['gates_artifact']=artifacts.write(f'GATES-card{card}-BEFORE-TIMING.json',s.canonical(gate))
        if row['candidate_status']=='CORRECT':
            row['timing_projection']=projection(row['cases'],deadline)
            row['timing_started']=True
            row['timings']=timing(torch,modules,layers,card,deadline,device_core_count)
            row['usefulness']=contract.usefulness(row['timings'])
        s.check(deadline)
    except BaseException as error:
        # Completed traceback frames can otherwise keep operand tensors alive.
        traceback.clear_frames(error.__traceback__)
        raise
    finally:
        try:row['peak']=peak(torch,card)
        finally:
            torch.cuda.synchronize(card);layers=None;gc.collect()
            # The FP32 oracle leaves a cached hipBLASLt workspace on this runtime.
            torch._C._cuda_clearCublasWorkspaces()
            torch.cuda.empty_cache()
            after=torch.cuda.memory_allocated(card)
            row['cleanup']=dict(allocated_after=after,complete=after<=before)
            row['released_monotonic']=time.monotonic()
            if not row['cleanup']['complete']:raise ValueError('card allocation not released before next card')


def run(deadline,*,fixture_scope=None):
    s.check(deadline);sources=source_identity();artifacts=s.Artifacts(OUTPUT,deadline)
    result=dict(schema=1,mode=MODE,capture_provenance=CAPTURE_PROVENANCE,sources=sources,contract=contract.bounds(),
                status='INCOMPLETE',complete=False,qualification=False,serving_ready=False,
                model_starts=0,http_requests=0,cards=[],failure=None,profiling=False)
    if fixture_scope is not None:result['fixture_scope']=fixture_scope
    started=time.monotonic()
    try:
        import torch
        import triton
        result['runtime']=dict(torch_version=str(torch.__version__),hip_version=str(torch.version.hip),triton_version=str(triton.__version__))
        if (result['runtime']['torch_version'],result['runtime']['hip_version'])!=(contract.TORCH_VERSION,contract.HIP_VERSION):
            raise ValueError('selected runtime')
        with source_loader.ModuleScope():
            modules=ops.kernels()
            with torch.no_grad():
                for card in contract.CARDS:
                    s.check(deadline-10)
                    properties=torch.cuda.get_device_properties(card)
                    row=dict(card=card,device_name=torch.cuda.get_device_name(card),arch=properties.gcnArchName,
                             device_core_count=properties.multi_processor_count)
                    result['cards'].append(row)
                    if properties.gcnArchName.split(':')[0]!='gfx1201':
                        raise ValueError('gfx1201 card required; observed='+repr(properties.gcnArchName))
                    run_card(torch,modules,row,artifacts,min(deadline-10,time.monotonic()+contract.CARD_SECONDS))
                    if row['candidate_status']!='CORRECT':break
        if source_identity()!=sources:raise ValueError('source drift after component')
        good=len(result['cards'])==len(contract.CARDS) and all(r['candidate_status']=='CORRECT' for r in result['cards'])
        result['useful']=good and all(r['usefulness']['useful'] for r in result['cards'])
        result['candidate_disposition']=('USEFUL' if result['useful'] else 'NOT_USEFUL') if good else result['cards'][-1]['candidate_status']
        result.update(status='COMPLETE',complete=True)
    except BaseException as error:
        result['failure']=dict(type=type(error).__name__,message=str(error)[:2000],traceback=traceback.format_exc()[-16000:])
    result['elapsed_seconds']=time.monotonic()-started
    result['artifacts']=list(artifacts.members);result['artifact_bytes_before_result']=artifacts.bytes
    artifacts.write(s.REPORT,s.canonical(result))
    return result
