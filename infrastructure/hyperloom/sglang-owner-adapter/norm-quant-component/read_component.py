"""Recompute the frozen F1 component gates from bounded retained artifacts."""
import json
import math
from pathlib import Path
import re
import compiler_gate
import contract
import corpus
import fusion_ops as ops
import stage_probe
import support as s

def need(value,message):
    if not value:raise ValueError(message)

def finite(value,positive=False):
    return type(value) in (int,float) and math.isfinite(value) and (value>0 if positive else value>=0)

def output(value,tokens,arm,inclusive=True):
    sizes=dict(packed=tokens*1536,scales=tokens*96,rstd=tokens*24*4)
    if arm=='B':sizes['normalized']=tokens*3072*2
    if inclusive:sizes['output']=tokens*5120*2
    need(set(value)==set(sizes),'exact output menu')
    for name,size in sizes.items():
        row=value[name]
        need(set(row)=={'bytes','sha256','finite'} and type(row['bytes'])is int and row['bytes']==size
             and type(row['sha256'])is str and re.fullmatch('[0-9a-f]{64}',row['sha256'])
             and type(row['finite'])is bool,'output byte/digest witness')

def classify(row,case):
    need(row['case']==case['name'] and row['corpus']==corpus.identity(case),'fixed corpus identity')
    need(finite(row['baseline_wall_seconds'],True),'positive correctness wall')
    need(set(row['outputs'])==set(row['launches'])=={'B0','B1','C0','C1'},'four poison launches')
    for name,value in row['outputs'].items():
        output(value,case['tokens'],name[0])
        need(row['launches'][name]==ops.expected_counts(name[0]) and
             all(type(n)is int for n in row['launches'][name].values()),'per-stage launch counts')
    status='CORRECT' if ops.numerical_pass(row,case) else 'INCORRECT'
    need(row['status']==status,'byte verdict recomputation')
    return status

def sequence(rows,cases):
    need(type(rows)is list and len(rows)<=len(cases),'bounded correctness sequence')
    statuses=[classify(row,case) for row,case in zip(rows,cases)]
    if 'INCORRECT' in statuses:
        need(statuses[-1]=='INCORRECT' and statuses.count('INCORRECT')==1,'stop at first failure')
        return False
    need(len(rows)==len(cases),'all correctness cases required')
    return True

def graphs(rows):
    need([r['tokens'] for r in rows]==list(contract.TOKENS),'both graph geometries')
    for row in rows:
        for phase in ('initial','changed'):
            need(set(row[phase])==set(contract.ARMS),'both graph arms')
            for arm,values in row[phase].items():output(values,row['tokens'],arm)
        need(stage_probe.graph_pass(row),'graph parity, writes, storage and changed-input witness')

def validate(value,blobs,*,allow_fixture=False):
    fixture=value.get('fixture_scope')
    need(fixture is None or allow_fixture is True,'CPU fixture cannot establish GPU evidence')
    need(value['schema']==1 and value['mode']==contract.MODE and value['capture_provenance']==contract.PROVENANCE,'component identity')
    need(value['contract']==contract.bounds() and value['sources']==stage_probe.source_identity(),'source and contract closure')
    need(value['qualification'] is value['serving_ready'] is value['profiling'] is False,'unqualified component')
    need(type(value['model_starts'])is int and value['model_starts']==0 and type(value['http_requests'])is int and value['http_requests']==0,'zero model and HTTP work')
    need(type(value['complete'])is bool,'completion Boolean')
    if not value['complete']:
        need(value['status']=='INCOMPLETE' and type(value['failure'])is dict and value['failure'].get('message'),'retained failure')
        return dict(status='INCOMPLETE_ARTIFACTS_AUTHENTICATED',complete=False,failure=value['failure'],qualification=False)
    need(value['status']=='COMPLETE' and value['failure'] is None,'complete without failure')
    runtime=value['runtime']
    need((runtime['torch_version'],runtime['hip_version'])==(contract.TORCH_VERSION,contract.HIP_VERSION),'runtime pins')
    rows=value['cards'];need(1<=len(rows)<=2 and [r['card'] for r in rows]==list(contract.CARDS[:len(rows)]),'sequential cards')
    artifacts=[];previous=0
    for index,row in enumerate(rows):
        card=row['card']
        need(type(card)is int and row['arch'].split(':')[0]=='gfx1201' and type(row['device_core_count'])is int
             and row['device_core_count']==32 and row['rows_per_block']=={'8':4,'16':4},'device and helper geometry')
        start,end=row['started_monotonic'],row['released_monotonic']
        need(finite(start,True) and finite(end,True) and previous<=start<=end and end-start<=contract.CARD_SECONDS,'card order and duration')
        previous=end;cleanup=row['cleanup'];peak=row['peak']
        need(type(row['allocated_before'])is int and row['allocated_before']>=0 and cleanup['complete'] is True
             and type(cleanup['allocated_after'])is int and 0<=cleanup['allocated_after']<=row['allocated_before'],'released allocation')
        need(all(type(peak[k])is int for k in ('allocated','reserved','total')) and
             0<peak['allocated']<=peak['reserved']<=contract.DEVICE_LIMIT<=peak['total'],'memory peak vetoes')
        need(set(row['compiler'])=={'8','16'},'all compiled geometries')
        for tokens in contract.TOKENS:
            need(set(row['compiler'][str(tokens)])==set(contract.STAGES),'four compiled stages')
            for stage in contract.STAGES:
                record=row['compiler'][str(tokens)][stage]
                for part in compiler_gate.PARTS:
                    art=record['parts'][part]['artifact'];name=f'compiler-card{card}-{tokens}-{stage}-{part}.gz'
                    need(art['path']==name and art in value['artifacts'],'compiler artifact binding');artifacts.append(name)
                compiler_gate.replay(record,tokens,stage,blobs)
        good=sequence(row['cases'],contract.CASES)
        if good:
            graphs(row['graphs'])
            layer_cases=[dict(name=f't{t}-timing-layer{i}',tokens=t,kind='finite',finite=True)
                         for t in contract.TOKENS for i in range(contract.LAYERS)]
            good=sequence(row['timing_layers'],layer_cases)
        else:need(row['graphs']==row['timing_layers']==[],'failure skips graphs and timing layers')
        need(row['candidate_status']==('CORRECT' if good else 'INCORRECT'),'card numerical verdict')
        gate=row['gates_artifact'];name=f'GATES-card{card}-BEFORE-TIMING.json'
        need(gate['path']==name and gate in value['artifacts'],'pre-timing artifact identity');artifacts.append(name)
        expected={k:row[k] for k in ('card','device_core_count','rows_per_block','compiler','cases','graphs','timing_layers','candidate_status')}
        need(json.loads(blobs[name])==dict(expected,timing_started=False),'complete immutable pre-timing gate')
        if good:
            need(row['timing_started'] is True and [r['case'] for r in row['timing_checks']]==list(contract.TIMED),'all timing checks')
            for check in row['timing_checks']:
                tokens=int(check['case'].split('-')[0][1:]);inclusive=check['case'].endswith('inclusive')
                need(all(finite(check[k],True) for k in ('capture_seconds','baseline_replay_wall_seconds','projected_seconds','remaining_seconds')),'finite projection')
                estimate=check['baseline_replay_wall_seconds']*2*(contract.WARMUP_CALLS+contract.TIMING_ROUNDS)*1.5
                need(math.isclose(check['projected_seconds'],estimate) and estimate<=check['remaining_seconds'],'timing projection fits')
                need(check['post_timing_audit']==dict(norm=1,candidate=1,quantizer=1,gemm=2 if inclusive else 0),'post-timing compiler audit')
                need(set(check['outputs'])==set(contract.ARMS),'both captured arms')
                for arm,values in check['outputs'].items():
                    need(len(values)==contract.LAYERS,'48 captured layers')
                    for out in values:
                        output(out,tokens,arm,inclusive);need(all(v['finite'] for v in out.values()),'finite captured outputs')
                keys=('packed','scales','rstd')+(('output',) if inclusive else ())
                need(all(check['outputs']['B'][i][k]==check['outputs']['C'][i][k] for i in range(contract.LAYERS) for k in keys),'all captured layer byte parity')
            need(contract.usefulness(row['timings'])==row['usefulness'],'timing verdict recomputation')
        else:need(row['timing_started'] is False and row['timings']==row['timing_checks']==[] and index==len(rows)-1,'failure skips timing and next card')
    need([a['path'] for a in value['artifacts']]==artifacts,'closed ordered artifacts')
    good=len(rows)==2 and all(r['candidate_status']=='CORRECT' for r in rows)
    if not good:need(rows[-1]['candidate_status']=='INCORRECT','both cards required')
    useful=good and all(r['usefulness']['useful'] for r in rows)
    disposition=('USEFUL' if useful else 'NOT_USEFUL') if good else 'INCORRECT'
    need(type(value['useful'])is bool and value['useful']==useful and value['candidate_disposition']==disposition
         and value['cumulative_disposition']==contract.cumulative(rows),'component dispositions')
    need(finite(value['elapsed_seconds'],True) and value['elapsed_seconds']<=contract.WORKER_SECONDS,'finite worker budget')
    return dict(status='CPU_FIXTURE_VALIDATED' if fixture else 'NORM_QUANT_COMPONENT_VALIDATED',complete=True,
      candidate_disposition=disposition,cumulative_disposition=value['cumulative_disposition'],useful=['NORM-QUANT'] if useful else [],
      cards=len(rows),candidates={'NORM-QUANT':disposition},profiles=[],sentinel=None,qualification=False,serving_ready=False)

def read_component(directory,*,require_complete=True,allow_fixture=False):
    root=Path(directory)
    need(root.resolve()==root and root.is_dir() and not root.is_symlink(),'canonical evidence directory')
    report=root/s.REPORT;need(report.is_file() and not report.is_symlink() and report.stat().st_size<=s.RESULT_LIMIT,'bounded report')
    raw=report.read_bytes();value=json.loads(raw);blobs={};total=len(raw)
    need(type(value['artifacts'])is list and len(value['artifacts'])<s.MEMBER_LIMIT,'member cap')
    for row in value['artifacts']:
        name=row['path'];need(type(name)is str and Path(name).name==name and name not in ('','.','..',s.REPORT) and name not in blobs,'unique member')
        path=root/name
        need(path.is_file() and not path.is_symlink() and type(row['bytes'])is int and 0<=row['bytes']<=s.FILE_LIMIT and path.stat().st_size==row['bytes'],'bounded regular member')
        data=path.read_bytes();need(s.sha(data)==row['sha256'],'member digest');blobs[name]=data;total+=len(data)
    need(total<=s.COMPONENT_LIMIT and {p.name for p in root.iterdir()}==set(blobs)|{s.REPORT},'exact membership and aggregate cap')
    need(value['artifact_bytes_before_result']==total-len(raw),'artifact byte join')
    result=validate(value,blobs,allow_fixture=allow_fixture)
    if require_complete:need(result['complete'],'incomplete; original failure retained')
    return result
