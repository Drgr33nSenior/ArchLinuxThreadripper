"""Recompute exact compiler, numerical, ordering, resource and timing gates offline."""
import json
import math
from pathlib import Path

import attention_ops as ops
import compiler_gate
import contract
import stage_probe
import support as s


def need(value,message):
    if not value:raise ValueError(message)


def finite(value,*,positive=False):
    return type(value) in (int,float) and math.isfinite(value) and (value>0 if positive else value>=0)


def classify(row,device_core_count):
    need(type(row['baseline_repeat_equal_layers']) is int and row['baseline_repeat_equal_layers']==contract.LAYERS,'baseline repeat bitwise')
    need(type(row['equal_layers']) is int and 0<=row['equal_layers']<=contract.LAYERS,'bounded bitwise layer count')
    finite_arms=row['finite_all_layers']
    need(set(finite_arms)==set(contract.ARMS) and all(type(v) is bool for v in finite_arms.values()) and finite_arms['B'],'finite-arm Booleans')
    need(finite(row['baseline_wall_seconds'],positive=True),'positive measured baseline wall')
    case=next((c for c in contract.CASES if c['name']==row['case']),None)
    need(case is not None and row['metadata']=={arm:ops.metadata_record(case,arm,device_core_count) for arm in contract.ARMS},'observed singleton metadata and causal boundaries')
    equals=row['prefetch_equal_layers']
    need(set(equals)=={'16','32'} and all(type(v) is int and 0<=v<=contract.LAYERS for v in equals.values()),'per-cap bitwise counts')
    need(set(row['writes'])==set(contract.ARMS),'all-arm write coverage')
    written=True
    for arm,witnesses in [*row['writes'].items(),('B',row['baseline_repeat_writes'])]:
        expected=ops.expected_writes(row['metadata'][arm]);cap=contract.arm_cap(arm)
        need(type(witnesses) is list and len(witnesses)==contract.LAYERS,'all-layer write witnesses')
        for value in witnesses:
            need(type(value) is dict and set(value)==set(expected),'write witness fields')
            need(all(type(v) is list and len(v)==case['queries'] and all(type(x) is int and 0<=x<(1<<cap) for x in v) for v in value.values()),'bounded exact-row write masks')
            written=written and value==expected
    layers=row['layers'];need(type(layers) is list and 1<=len(layers)<=contract.LAYERS,'per-layer reference coverage')
    statuses=[]
    for number,value in enumerate(layers):
        need(type(value['layer']) is int and value['layer']==number,'ordered layer identity')
        errors,ref=value['errors'],value['reference_max'];need(set(errors)==set(contract.ARMS),'all-arm errors')
        base=errors['B'];need(contract.valid_errors(base) and finite(ref,positive=True),'finite per-layer baseline and reference')
        need(set(value['passes'])==set(contract.ARMS)-{'B'} and set(value['mean_error_margin'])==set(value['passes']),'per-arm verdict fields')
        expected=[]
        for arm in contract.ARMS[1:]:
            err=errors[arm]
            need(contract.valid_errors(err) or err==dict(max_abs=None,mean_abs=None,finite=False),'canonical candidate error witness')
            passed=contract.within_tolerance(err,base,ref)
            margin=contract.MEAN_ERROR_FACTOR*base['mean_abs']-err['mean_abs'] if contract.valid_errors(err) else None
            need(type(value['passes'][arm]) is bool and value['passes'][arm]==passed and value['mean_error_margin'][arm]==margin,'per-layer tolerance and margin recomputation')
            if finite_arms[arm]:need(err['finite'],'finite-arm consistency')
            expected.append(passed)
        statuses.append(all(expected))
    if False in statuses:need(statuses[-1] is False and statuses.count(False)==1,'stop at first per-layer failure')
    else:need(len(layers)==contract.LAYERS,'all sixteen layer references before success')
    if len(layers)==contract.LAYERS:
        need(all(finite_arms[a]==all(v['errors'][a]['finite'] for v in layers) for a in contract.ARMS),'all-layer finite consistency')
    ok=all(finite_arms.values()) and written and all(v==contract.LAYERS for v in equals.values()) and all(statuses) and len(layers)==contract.LAYERS
    return 'CORRECT' if ok else 'INCORRECT'


def validate(value,blobs,*,allow_fixture=False):
    fixture=value.get('fixture_scope')
    need(fixture is None or allow_fixture is True,'CPU fixture cannot establish GPU evidence')
    need(value['schema']==1 and value['mode']==contract.MODE and value['capture_provenance']==contract.PROVENANCE,'component identity')
    need(value['contract']==contract.bounds() and value['sources']==stage_probe.source_identity(),'exact contract and source closure')
    need(value['qualification'] is value['serving_ready'] is value['profiling'] is False,'unqualified unprofiled component')
    need(type(value['model_starts']) is int and value['model_starts']==0 and type(value['http_requests']) is int and value['http_requests']==0,'no model or HTTP')
    need(type(value['complete']) is bool,'completion Boolean')
    if not value['complete']:
        need(value['status']=='INCOMPLETE' and type(value['failure']) is dict and value['failure'].get('message'),'original incomplete failure')
        return dict(status='INCOMPLETE_ARTIFACTS_AUTHENTICATED',complete=False,failure=value['failure'],qualification=False)
    need(value['status']=='COMPLETE' and value['failure'] is None,'completion and failure agree')
    runtime=value['runtime']
    need((runtime['torch_version'],runtime['hip_version'])==(contract.TORCH_VERSION,contract.HIP_VERSION),'exact runtime')
    rows=value['cards'];need(1<=len(rows)<=len(contract.CARDS),'bounded card count')
    need([r['card'] for r in rows]==list(contract.CARDS[:len(rows)]),'sequential card order')
    expected_artifacts=[];previous_release=0
    for index,row in enumerate(rows):
        card=row['card'];need(row['arch'].split(':')[0]=='gfx1201','device architecture')
        device_core_count=ops.validate_device_core_count(row['device_core_count'])
        need(finite(row['started_monotonic'],positive=True) and finite(row['released_monotonic'],positive=True)
             and previous_release<=row['started_monotonic']<=row['released_monotonic'],'sequential release-before-next-card')
        previous_release=row['released_monotonic']
        need(type(row['allocated_before']) is int and row['allocated_before']>=0,'initial allocation')
        cleanup=row['cleanup']
        need(cleanup['complete'] is True and type(cleanup['allocated_after']) is int and 0<=cleanup['allocated_after']<=row['allocated_before'],'card allocation released')
        peak=row['peak'];need(all(type(peak[k]) is int for k in ('allocated','reserved','total'))
             and 0<peak['allocated']<=peak['reserved']<=contract.DEVICE_LIMIT<=peak['total'],'allocated/reserved cap')
        memory=row['memory'];expected=dict(kv_bytes=contract.KV_BYTES,extend_bytes=contract.EXTEND_BYTES,output_bytes=contract.OUTPUT_BYTES)
        need(memory['expected']==expected and memory['slack']==contract.ALLOCATION_SLACK,'allocation accounting contract')
        need(all(type(memory[k]) is int and v<=memory[k]<=v+contract.ALLOCATION_SLACK for k,v in expected.items()),'measured allocations')
        compiler=row['compiler'];need(set(compiler)=={k for a in contract.ARMS for k in (a,a+'2')},'two stages for both arms')
        for arm in [k for a in contract.ARMS for k in (a,a+'2')]:
            for part in ('amdgcn','hsaco'):
                art=compiler[arm]['parts'][part]['artifact']
                need(art['path']==f'compiler-card{card}-{arm}-{part}.gz' and art in value['artifacts'],'compiler artifact binding')
                expected_artifacts.append(art['path'])
            compiler_gate.replay(compiler[arm],blobs)
            need(compiler[arm]['dynamic_lds_bytes']==(contract.STAGE2_DYNAMIC_LDS_BYTES if arm.endswith('2') else contract.DYNAMIC_LDS_BYTES),
                 'pinned dynamic shared memory for each stage')
            if fixture is None:
                need(all(compiler[arm]['parts'][part]['raw_sha256']==pin for part,pin in (contract.STAGE2_PINS[arm[0]] if arm.endswith('2') else contract.COMPILER_PINS[arm]).items()),'pinned CPU compiler identity')
        compiled=compiler_gate.compare(compiler)
        cases=row['cases']
        need([c['case'] for c in cases]==[c['name'] for c in contract.CASES[:len(cases)]] and len(cases)<=len(contract.CASES),'ordered correctness cases')
        if compiled!='CONFIGURATION_READY':
            need(cases==[] and row['candidate_status']==compiled,'compiler rejection precedes numerical work')
        else:
            statuses=[classify(c,device_core_count) for c in cases]
            need(all(c['status']==status for c,status in zip(cases,statuses)),'numerical verdict recomputation')
            if 'INCORRECT' in statuses:
                need(statuses[-1]=='INCORRECT' and statuses.count('INCORRECT')==1 and row['candidate_status']=='INCORRECT','stop at first numerical rejection')
            else:
                need(len(cases)==len(contract.CASES) and row['candidate_status']=='CORRECT','all numerical cases before timing')
        gate=row['gates_artifact'];need(gate['path']==f'GATES-card{card}-BEFORE-TIMING.json' and gate in value['artifacts'],'gate artifact identity')
        expected_artifacts.append(gate['path'])
        expected_gate={k:row[k] for k in ('card','device_core_count','memory','compiler','cases','candidate_status')}
        expected_gate['timing_started']=False
        need(json.loads(blobs[gate['path']])==expected_gate,'immutable gates before timing')
        if row['candidate_status']=='CORRECT':
            need(row['timing_started'] is True,'timing follows complete gates')
            p=row['timing_projection'];walls={c['case']:c['baseline_wall_seconds'] for c in cases}
            estimate=contract.CANDIDATE_COST_FACTOR*len(contract.ARMS)*(contract.WARMUP_CALLS+contract.TIMING_ROUNDS)*sum(walls[k] for k in contract.TIMED)
            need(all(finite(p[k],positive=True) for k in ('estimated_seconds','projected_seconds','remaining_seconds'))
                 and p['fits'] is True and math.isclose(p['estimated_seconds'],estimate)
                 and math.isclose(p['projected_seconds'],contract.TIMING_HEADROOM*estimate)
                 and p['projected_seconds']<=p['remaining_seconds'],'measured bounded timing projection')
            need(contract.usefulness(row['timings'])==row['usefulness'],'timing usefulness recomputation')
        else:
            need(row['timing_started'] is False and row['timings']==[] and index==len(rows)-1,'rejected candidate parks without timing or next card')
    need([a['path'] for a in value['artifacts']]==expected_artifacts,'closed ordered compiler and gate artifact menu')
    all_good=len(rows)==len(contract.CARDS) and all(r['candidate_status']=='CORRECT' for r in rows)
    if not all_good:need(rows[-1]['candidate_status']!='CORRECT','successful first card cannot stand for both cards')
    useful=all_good and all(r['usefulness']['useful'] for r in rows)
    disposition=('USEFUL' if useful else 'NOT_USEFUL') if all_good else rows[-1]['candidate_status']
    need(value['cumulative_disposition']==(contract.cumulative(rows) if all_good else 'REJECT'),'cumulative verdict recomputation')
    need(type(value['useful']) is bool and value['useful']==useful and value['candidate_disposition']==disposition,'final candidate verdict')
    need(finite(value['elapsed_seconds'],positive=True) and value['elapsed_seconds']<=contract.WORKER_SECONDS,'bounded elapsed worker time')
    return dict(status='CPU_FIXTURE_VALIDATED' if fixture else 'VERIFY_COMPOSED_COMPONENT_VALIDATED',complete=True,
                candidate_disposition=disposition,cumulative_disposition=value['cumulative_disposition'],useful=['VERIFY-COMPOSED'] if useful else [],cards=len(rows),
                candidates={'VERIFY-COMPOSED':disposition},profiles=[],sentinel=None,qualification=False,serving_ready=False)


def read_component(directory,*,require_complete=True,allow_fixture=False):
    root=Path(directory)
    need(root.resolve()==root and root.is_dir() and not root.is_symlink(),'canonical artifact directory')
    report=root/s.REPORT;need(report.is_file() and not report.is_symlink() and report.stat().st_size<=s.RESULT_LIMIT,'bounded report')
    raw=report.read_bytes();value=json.loads(raw);blobs={};total=len(raw)
    need(type(value['artifacts']) is list and len(value['artifacts'])<s.MEMBER_LIMIT,'artifact member cap')
    for row in value['artifacts']:
        name=row['path'];need(type(name) is str and Path(name).name==name and name not in ('','.','..',s.REPORT) and name not in blobs,'unique artifact name')
        p=root/name
        need(p.is_file() and not p.is_symlink() and type(row['bytes']) is int and 0<=row['bytes']<=s.FILE_LIMIT and p.stat().st_size==row['bytes'],'bounded regular member')
        data=p.read_bytes();need(s.sha(data)==row['sha256'],'artifact digest');blobs[name]=data;total+=len(data)
    need(total<=s.COMPONENT_LIMIT and {p.name for p in root.iterdir()}==set(blobs)|{s.REPORT},'exact evidence membership and cap')
    need(value['artifact_bytes_before_result']==total-len(raw),'evidence byte join')
    result=validate(value,blobs,allow_fixture=allow_fixture)
    if require_complete:need(result['complete'],'component incomplete; original failure retained')
    return result
