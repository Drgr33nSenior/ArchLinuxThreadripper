"""Finite sequential F1 component: compiler, byte parity, graphs, then timing."""
import gc
import json
import math
from pathlib import Path
import time
import traceback
import compiler_gate
import contract
import corpus
import fusion_ops as ops
import source_loader
import support as s

ROOT=Path(__file__).resolve().parent
OUTPUT=Path('/mnt/output/mxfp4')
MODE,CAPTURE_PROVENANCE=contract.MODE,contract.PROVENANCE
RUNTIME_NAMES=('compiler_gate.py','contract.py','corpus.py','fusion_ops.py','source_loader.py',
 'stage_probe.py','read_component.py','support.py','power_gate.py','norm_baseline.py','norm_candidate.py',
 'mxfp4_kernels.py','mxfp4_ops.py','prefill_reference.py','LAUNCHES.json','COMPILER-PINS.json','INPUT-PINS.json')

def source_identity():
    raw=(ROOT/'RUNTIME-SOURCE-PINS.json').read_bytes();pins=json.loads(raw)
    if set(pins)!=set(RUNTIME_NAMES) or len(raw)>32768:raise ValueError('closed F1 runtime source set')
    for name,pin in pins.items():
        path=ROOT/name
        if path.is_symlink() or path.resolve()!=path or not path.is_file() or path.stat().st_size>2<<20 or s.sha(path.read_bytes())!=pin:
            raise ValueError('F1 source identity: '+name)
    return dict(pins=pins,inventory_sha256=s.sha(raw))

def peak(torch,card):
    a,b=torch.cuda.max_memory_allocated(card),torch.cuda.max_memory_reserved(card)
    if not 0<=a<=b<=contract.DEVICE_LIMIT:raise ValueError('observed device memory veto')
    return dict(allocated=a,reserved=b,total=torch.cuda.get_device_properties(card).total_memory)

def compile_pair(torch,modules,weights,card,artifacts,deadline):
    records={}
    for tokens in contract.TOKENS:
        s.check(deadline);t=ops.layer(torch,dict(tokens=tokens,kind='finite'),card,weights=weights)
        with ops.LaunchAudit(torch,modules,tokens,card,launch=False) as audit:
            for arm in contract.ARMS:ops.execute(torch,modules,t,arm)
        if audit.counts!=dict(norm=1,candidate=1,quantizer=1,gemm=2):raise ValueError('compile witness counts')
        records[str(tokens)]={stage:compiler_gate.store(audit.records[stage],card,tokens,stage,artifacts) for stage in contract.STAGES}
    return records

def graph_check(torch,modules,t,card,deadline):
    pairs={arm:ops.capture(torch,modules,[t],arm,True,card,deadline) for arm in contract.ARMS}
    pointers=[v.untyped_storage().data_ptr() for _,outputs in pairs.values() for row in outputs for v in row.values()]
    if len(pointers)!=len(set(pointers)):raise ValueError('independent captured output storage')
    first={arm:ops.snapshots(torch,value[1][0]) for arm,value in pairs.items()}
    t['x'].neg_()
    for arm,(graph,outputs) in pairs.items():
        for value in outputs[0].values():ops.poison(torch,value,0 if arm=='B' else 1)
        graph.replay()
    torch.cuda.synchronize(card)
    second={arm:ops.snapshots(torch,value[1][0]) for arm,value in pairs.items()}
    result=dict(tokens=t['tokens'],initial=first,changed=second,independent_storage=True)
    if not graph_pass(result):raise ValueError('graph replay exact bytes/change witness')
    t['x'].neg_()
    return result

def graph_pass(row):
    keys=('packed','scales','rstd','output')
    return (row['independent_storage'] is True
      and all(row[phase]['B'][k]==row[phase]['C'][k] for phase in ('initial','changed') for k in keys)
      and all(row['initial'][a]['packed']['sha256']!=row['changed'][a]['packed']['sha256'] for a in contract.ARMS)
      and all(v['finite'] for phase in ('initial','changed') for arm in row[phase].values() for v in arm.values()))

def timed_case(torch,modules,layers,tokens,mode,card,deadline):
    inclusive=mode=='inclusive';name=f't{tokens}-{mode}';started=time.monotonic()
    pairs={a:ops.capture(torch,modules,layers,a,inclusive,card,deadline) for a in contract.ARMS}
    snapshots={a:[ops.snapshots(torch,v) for v in pair[1]] for a,pair in pairs.items()}
    keys=('packed','scales','rstd')+(('output',) if inclusive else ())
    if any(snapshots['B'][i][k]!=snapshots['C'][i][k] for i in range(contract.LAYERS) for k in keys):
        raise ValueError('all-layer captured timing outputs differ')
    capture_seconds=time.monotonic()-started
    start=time.monotonic();pairs['B'][0].replay();torch.cuda.synchronize(card)
    wall=time.monotonic()-start
    estimated=wall*2*(contract.WARMUP_CALLS+contract.TIMING_ROUNDS)*1.5
    remaining=deadline-time.monotonic()-10
    if not 0<estimated<=remaining:raise ValueError('captured replay projection does not fit')
    samples=[]
    for r in range(contract.WARMUP_CALLS):
        for a in contract.order(r):s.check(deadline);ops.replay_ms(torch,pairs[a][0],card)
    for r in range(contract.TIMING_ROUNDS):
        for position,a in enumerate(contract.order(r)):
            s.check(deadline);ms=ops.replay_ms(torch,pairs[a][0],card)
            if not math.isfinite(ms) or ms<=0:raise ValueError('positive GPU elapsed time')
            samples.append(dict(case=name,round=r,position=position,arm=a,elapsed_ms=ms))
    # Revisit the exact launch signature after timing, outside the event interval.
    with ops.LaunchAudit(torch,modules,tokens,card,launch=False) as audit:
        for a in contract.ARMS:ops.execute(torch,modules,layers[0],a,inclusive=inclusive)
    expected=dict(norm=1,quantizer=1,candidate=1,gemm=2 if inclusive else 0)
    if audit.counts!=expected:raise ValueError('post-timing signature witness')
    return samples,dict(case=name,capture_seconds=capture_seconds,baseline_replay_wall_seconds=wall,
       projected_seconds=estimated,remaining_seconds=remaining,post_timing_audit=expected,outputs=snapshots)

def work_card(torch,modules,row,artifacts,deadline):
    card=row['card']
    if type(row['device_core_count'])is not int or row['device_core_count']!=32:raise ValueError('frozen API device count32 required')
    row['rows_per_block']={str(t):modules['norm'].calc_rows_per_block(t*24,torch.device(f'cuda:{card}')) for t in contract.TOKENS}
    if row['rows_per_block']!={'8':4,'16':4}:raise ValueError('frozen actual norm helper')
    base=ops.layer(torch,dict(tokens=8,kind='finite'),card);weights=(base['weight'],base['weight_scale'])
    row['compiler']=compile_pair(torch,modules,weights,card,artifacts,deadline)
    row['candidate_status']='CORRECT'
    for case in contract.CASES:
        s.check(deadline);t=ops.layer(torch,case,card,weights=weights)
        result=ops.check_case(torch,modules,t,case,card,deadline);row['cases'].append(result);peak(torch,card)
        if result['status']!='CORRECT':row['candidate_status']='INCORRECT';break
    if row['candidate_status']=='CORRECT':
        for tokens in contract.TOKENS:
            s.check(deadline);t=ops.layer(torch,dict(tokens=tokens,kind='finite'),card,weights=weights)
            row['graphs'].append(graph_check(torch,modules,t,card,deadline))
    timing_layers={}
    if row['candidate_status']=='CORRECT':
        for tokens in contract.TOKENS:
            layers=[]
            for number in range(contract.LAYERS):
                s.check(deadline);t=ops.layer(torch,dict(tokens=tokens,kind='finite'),card,number=number)
                case=dict(name=f't{tokens}-timing-layer{number}',tokens=tokens,kind='finite',finite=True)
                checked=ops.check_case(torch,modules,t,case,card,deadline)
                row['timing_layers'].append(checked)
                if checked['status']!='CORRECT':
                    row['candidate_status']='INCORRECT';break
                layers.append(t);peak(torch,card)
            timing_layers[tokens]=layers
            if row['candidate_status']!='CORRECT':break
    gate={k:row[k] for k in ('card','device_core_count','rows_per_block','compiler','cases','graphs','timing_layers','candidate_status')}
    row['gates_artifact']=artifacts.write(f'GATES-card{card}-BEFORE-TIMING.json',s.canonical(dict(gate,timing_started=False)))
    if row['candidate_status']!='CORRECT':return
    for tokens in contract.TOKENS:
        for mode in ('boundary','inclusive'):
            row['timing_started']=True
            samples,record=timed_case(torch,modules,timing_layers[tokens],tokens,mode,card,deadline)
            row['timings'].extend(samples);row['timing_checks'].append(record);peak(torch,card)
    row['usefulness']=contract.usefulness(row['timings'])

def run_card(torch,modules,row,artifacts,deadline):
    card=row['card'];row['started_monotonic']=time.monotonic();torch.cuda.set_device(card)
    before=torch.cuda.memory_allocated(card);torch.cuda.reset_peak_memory_stats(card)
    row.update(allocated_before=before,cases=[],graphs=[],timing_layers=[],timing_checks=[],timings=[],timing_started=False)
    try:work_card(torch,modules,row,artifacts,deadline)
    except BaseException as error:
        traceback.clear_frames(error.__traceback__);raise
    finally:
        try:row['peak']=peak(torch,card)
        finally:
            torch.cuda.synchronize(card);gc.collect();torch._C._cuda_clearCublasWorkspaces();torch.cuda.empty_cache()
            after=torch.cuda.memory_allocated(card)
            row['cleanup']=dict(allocated_after=after,complete=after<=before)
            row['released_monotonic']=time.monotonic()
            if after>before:raise ValueError('card allocation did not return before next card')

def run(deadline,*,fixture_scope=None):
    s.check(deadline);sources=source_identity();artifacts=s.Artifacts(OUTPUT,deadline);started=time.monotonic()
    result=dict(schema=1,mode=MODE,capture_provenance=CAPTURE_PROVENANCE,sources=sources,contract=contract.bounds(),
      status='INCOMPLETE',complete=False,qualification=False,serving_ready=False,profiling=False,model_starts=0,http_requests=0,cards=[],failure=None)
    if fixture_scope is not None:result['fixture_scope']=fixture_scope
    try:
        import torch
        import triton
        result['runtime']=dict(torch_version=str(torch.__version__),hip_version=str(torch.version.hip),triton_version=str(triton.__version__))
        if (result['runtime']['torch_version'],result['runtime']['hip_version'])!=(contract.TORCH_VERSION,contract.HIP_VERSION):raise ValueError('selected runtime')
        with source_loader.ModuleScope(),torch.no_grad():
            modules=ops.kernels()
            for card in contract.CARDS:
                s.check(deadline-10);props=torch.cuda.get_device_properties(card)
                row=dict(card=card,device_name=torch.cuda.get_device_name(card),arch=props.gcnArchName,device_core_count=props.multi_processor_count)
                result['cards'].append(row)
                if props.gcnArchName.split(':')[0]!='gfx1201':raise ValueError('gfx1201 device')
                run_card(torch,modules,row,artifacts,min(deadline-10,time.monotonic()+contract.CARD_SECONDS))
                if row['candidate_status']!='CORRECT':break
        if source_identity()!=sources:raise ValueError('source drift')
        good=len(result['cards'])==2 and all(r['candidate_status']=='CORRECT' for r in result['cards'])
        result['useful']=good and all(r['usefulness']['useful'] for r in result['cards'])
        result['candidate_disposition']=('USEFUL' if result['useful'] else 'NOT_USEFUL') if good else 'INCORRECT'
        result['cumulative_disposition']=contract.cumulative(result['cards'])
        result.update(status='COMPLETE',complete=True)
    except BaseException as error:
        result['failure']=dict(type=type(error).__name__,message=str(error)[:2000],traceback=traceback.format_exc()[-16000:])
    result['elapsed_seconds']=time.monotonic()-started;result['artifacts']=list(artifacts.members);result['artifact_bytes_before_result']=artifacts.bytes
    artifacts.write(s.REPORT,s.canonical(result));return result
