"""Four fixed prefetch/split arms: exact writes, all-layer oracle and timing."""
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
               'decode_attention_candidate.py','source_loader.py','metadata.py','LAUNCHES.json','read_component.py','stage_probe.py','support.py','power_gate.py')


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
    if pins['decode_attention_candidate.py']!=contract.CANDIDATE_SHA256:raise ValueError('reviewed prefetch kernel source')
    if pins['metadata.py']!=contract.METADATA_SHA256:raise ValueError('selected singleton metadata source')
    return dict(pins=pins,inventory_sha256=s.sha(raw))


def peak(torch,card):
    a,b=torch.cuda.max_memory_allocated(card),torch.cuda.max_memory_reserved(card)
    if not 0<=a<=b<=contract.DEVICE_LIMIT:raise ValueError('allocated/reserved device cap')
    return dict(allocated=a,reserved=b,total=torch.cuda.get_device_properties(card).total_memory)


def compile_pair(torch,modules,layers,card,artifacts,device_core_count):
    records={}
    for arm in contract.ARMS:
        module=modules[contract.SOURCE_INDEX[arm]]
        idx=ops.indices(torch,modules,contract.CASES[0],card,device_core_count,arm)
        with ops.LaunchAudit(module,arm,contract.CASES[0],card,launch=False) as audit:
            ops.served_call(module.decode_attention_fwd,layers[0],idx,layers[0]['o'])
        torch.cuda.synchronize(card)
        if len(audit.compiled)!=1 or len(audit.reductions)!=1:raise ValueError('one compile-only witness per stage/arm')
        records[arm]=compiler_gate.store(audit.compiled[0],card,arm,artifacts)
        records[arm+'2']=compiler_gate.store(audit.reductions[0],card,arm+'2',artifacts)
    return records


def correctness(torch,modules,layers,case,card,compiler,deadline,device_core_count):
    s.check(deadline);q=case['queries']
    idxs={arm:ops.indices(torch,modules,case,card,device_core_count,arm) for arm in contract.ARMS}
    for key in ('partial','lse'):
        if len({id(idx[key]) for idx in idxs.values()})!=len(contract.ARMS):raise ValueError('independent arm buffers')
    row=dict(case=case['name'],metadata={arm:idx['witness'] for arm,idx in idxs.items()},layers=[],writes={})
    for arm in contract.ARMS:
        s.check(deadline);module=modules[contract.SOURCE_INDEX[arm]];start=time.monotonic()
        with ops.LaunchAudit(module,arm,case,card) as audit:
            ops.refresh_splits(module,idxs[arm])
            row['writes'][arm]=ops.correct_chunk(torch,module.decode_attention_fwd,layers,idxs[arm],contract.OUTPUTS[arm])
        torch.cuda.synchronize(card)
        if {s.sha(k.asm['hsaco']) for k in audit.compiled}!={compiler[arm]['parts']['hsaco']['raw_sha256']}:
            raise ValueError('specialization changed without compiler witness')
        if arm=='B':
            row['baseline_wall_seconds']=time.monotonic()-start
            if not ops.all_finite(torch,layers,'o_base',q):raise ValueError('baseline nonfinite')
            with ops.LaunchAudit(module,arm,case,card):
                row['baseline_repeat_writes']=ops.correct_chunk(torch,module.decode_attention_fwd,layers,idxs[arm],'o')
            torch.cuda.synchronize(card)
            row['baseline_repeat_equal_layers']=sum(ops.bitwise_equal(torch,t['o'][:q],t['o_base'][:q]) for t in layers)
            if row['baseline_repeat_equal_layers']!=contract.LAYERS:raise ValueError('baseline repeat changed selected verification attention')
    finite={arm:ops.all_finite(torch,layers,target,q) for arm,target in contract.OUTPUTS.items()}
    row['finite_all_layers']=finite
    row['prefetch_equal_layers']={str(cap):sum(ops.bitwise_equal(torch,t[left][:q],t[right][:q]) for t in layers)
        for cap,left,right in ((16,'o_base','o_prefetch'),(32,'o_split','o'))}
    row['equal_layers']=sum(ops.bitwise_equal(torch,t['o'][:q],t['o_base'][:q]) for t in layers)
    for number,t in enumerate(layers):
        s.check(deadline);ref=ops.reference(torch,t,idxs['B']);ref_max=float(ref.abs().max())
        errors={arm:ops.errors(torch,t[target][:q],ref) for arm,target in contract.OUTPUTS.items()};base=errors['B']
        if not contract.valid_errors(base) or not math.isfinite(ref_max) or ref_max<=0:raise ValueError('finite per-layer FP32 reference and baseline')
        passes={arm:contract.within_tolerance(errors[arm],base,ref_max) for arm in contract.ARMS if arm!='B'}
        margins={arm:contract.MEAN_ERROR_FACTOR*base['mean_abs']-errors[arm]['mean_abs'] if contract.valid_errors(errors[arm]) else None for arm in passes}
        row['layers'].append(dict(layer=number,errors=errors,reference_max=ref_max,mean_error_margin=margins,passes=passes))
        del ref
        if not all(passes.values()):break
    written=all(len(row['writes'][a])==contract.LAYERS and all(v==ops.expected_writes(idxs[a]['witness']) for v in row['writes'][a]) for a in contract.ARMS)
    repeated=all(v==ops.expected_writes(idxs['B']['witness']) for v in row['baseline_repeat_writes'])
    ok=(all(finite.values()) and written and repeated and all(v==contract.LAYERS for v in row['prefetch_equal_layers'].values())
        and len(row['layers'])==contract.LAYERS and all(all(v['passes'].values()) for v in row['layers']))
    row['status']='CORRECT' if ok else 'INCORRECT'
    return row


def projection(rows,deadline):
    walls={r['case']:r['baseline_wall_seconds'] for r in rows}
    estimate=contract.CANDIDATE_COST_FACTOR*len(contract.ARMS)*(contract.WARMUP_CALLS+contract.TIMING_ROUNDS)*sum(walls[k] for k in contract.TIMED)
    remaining=deadline-time.monotonic()-10;needed=contract.TIMING_HEADROOM*estimate
    result=dict(estimated_seconds=estimate,projected_seconds=needed,remaining_seconds=remaining,fits=needed<=remaining)
    if not result['fits']:raise ValueError('timing projection does not fit; no timing started')
    return result


def timing(torch,modules,layers,card,deadline,device_core_count):
    samples=[];by_name={c['name']:c for c in contract.CASES};funcs={a:modules[contract.SOURCE_INDEX[a]] for a in contract.ARMS}
    for name in contract.TIMED:
        idxs={arm:ops.indices(torch,modules,by_name[name],card,device_core_count,arm) for arm in contract.ARMS}
        # Fixed mirrored settling precedes every measured case; never discard
        # scored samples or adapt the warmup count to the observed timings.
        for warmup in range(contract.WARMUP_CALLS):
            for arm in contract.timing_order(warmup):
                s.check(deadline);ops.timed(torch,funcs[arm],layers,idxs[arm])
        for round_index in range(contract.TIMING_ROUNDS):
            for position,arm in enumerate(contract.timing_order(round_index)):
                s.check(deadline);ms=ops.timed(torch,funcs[arm],layers,idxs[arm])
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
        row['candidate_status']=compiler_gate.compare(row['compiler'])
        if row['candidate_status']=='CONFIGURATION_READY':
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
        result['cumulative_disposition']=contract.cumulative(result['cards']) if good else 'REJECT'
        result.update(status='COMPLETE',complete=True)
    except BaseException as error:
        result['failure']=dict(type=type(error).__name__,message=str(error)[:2000],traceback=traceback.format_exc()[-16000:])
    result['elapsed_seconds']=time.monotonic()-started
    result['artifacts']=list(artifacts.members);result['artifact_bytes_before_result']=artifacts.bytes
    artifacts.write(s.REPORT,s.canonical(result))
    return result
