"""Validate a same-campaign continuation after an inert workload-binding refusal."""
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE),str(HERE.parents[2]/'lib/workstation')]
import owner_operation as op
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
from hyperloom_sglang import checked_json,require


def validate(reference,bound,now_ms):
    require(set(reference)=={'root','manifest_sha256','workload_preflight'},'closed continuation evidence required')
    root=Path(reference['root'])
    op.sealer().verify(root,reference['manifest_sha256'])
    grant=op._json(root/'CAMPAIGN-GRANT.json')
    previous=op._json(root/'BINDING.json')
    result=op._json(root/'CAMPAIGN-RESULT.json')
    require(result['status']=='FAILED' and result.get('controller_exit_code')==0
            and result.get('controller_reaped') is True and (root/'controller/CONTROLLER-STOPPED.json').is_file(),
            'previous controller must have stopped naturally')
    for key in ('campaign_sha256','selection_sha256','runtime_sha256','controller_sha256','selected_source_set_sha256'):
        require(grant[key]==previous[key]==bound[key],'continuation changed '+key)
    start,end=grant['started_unix_ms'],grant['deadline_unix_ms']
    require(type(start)is int and type(end)is int and end-start==14400_000 and start<=now_ms<end
            and result['campaign_started_unix_ms']==start and result['deadline_unix_ms']==end,
            'continuation must preserve the first unexpired deadline')
    pre=reference['workload_preflight']
    check=checked_json(Path(pre['path']),pre['sha256'])
    require(check['status']=='PREPARED_NOT_REGISTERED_OR_RUN' and check['registered'] is False
            and check['gpu_work'] is False and check['native_action'] is False
            and check['corrected_workload']['sha256']==bound['workload_sha256']
            and check['original_workload_sha256']==previous['workload_sha256']
            and previous['workload_sha256']!=bound['workload_sha256'],
            'real inert preparation must prove the exact workload correction')
    jobs={}
    for job in (root/'controller/jobs').iterdir():
        request=op._json(job/'snapshot/REQUEST.json');outcome=op._json(job/'OUTCOME.json')
        exchange.validate_result(outcome,request,controller_sha256=bound['controller_sha256'])
        require(request['kind'] not in jobs and request['campaign_sha256']==bound['campaign_sha256'],
                'duplicate or foreign continuation job')
        published=op._json(job/'PUBLICATION.json')
        require(published['published_sha256']==exchange.digest(exchange.canonical(outcome)),
                'continuation outcome was not published intact')
        jobs[request['kind']]=(job,request,outcome)
    require(set(jobs)=={'component','serving'},'one completed component and one inert serving refusal required')
    failed,_,refusal=jobs['serving']
    require(refusal['status']=='FAILED' and refusal.get('owner_started') is False
            and refusal.get('reason')=='ValueError: owner preparation refused: ValueError: prepared workload differs from the bound workload'
            and 'index' not in refusal and not (failed/'PROPOSAL-RESULT.json').exists(),
            'an admitted or differently failed owner cannot use workload continuation')
    component,request,outcome=jobs['component']
    report=backend.validate_component(outcome,request,bound)
    require(report['correct_both_cards'] is True and report['eligible_for_serving'] is False
            and request['sources'][0]['sha256']=='e945ad72296777556ed93859263442c077e0f1d1d7082268c1f669e157a324e9',
            'only the reviewed non-serving qualification component can be carried')
    closed=outcome['checkpoints']['closed']
    checkpoint=op._pinned({k:closed[k] for k in ('path','sha256')},'component release')[1]
    require(checkpoint['independent_release_verified'] is True
            and checkpoint['last_completed_global_attempt_index']==outcome['index'],
            'completed component lacks its independent release checkpoint')
    op._idle(checkpoint)
    op.sealer().verify(Path(outcome['native_root']),outcome['owner_seal_sha256'])
    return dict(started_unix_ms=start,deadline_unix_ms=end,component_root=str(component),
                request=request,result=outcome,report=report,
                seal_path=str(Path(outcome['native_root'])/'FINAL-ARTIFACTS.sha256'))
