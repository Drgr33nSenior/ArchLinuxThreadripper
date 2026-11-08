"""Replay source-bound capture, real-input and HTTP qualification receipts."""
import base64
import json
from pathlib import Path

import bundle_contract as c
from bundle_client import PROBE_TOKENS, inspect_cancel


def identity(binding, worker):
    return dict(**{k: binding[k] for k in ('arm','session_sha256','source_manifest_sha256')},
        **{k: worker[k] for k in ('rank','pid','boot_id','start_ticks')},
        worker_identity_sha256=worker['identity_sha256'])


def startup(record, ident):
    c.need(type(record) is dict and set(record)=={'schema','identity','attached','members',
        'f1_capture','gemm_capture','f1_fallback8','device_api_count','selected_compilers',
        'numerical_qualified','scored_shadow_calls','before_external_generations'}, 'bundle startup fields')
    c.need(type(record['schema']) is int and record['schema']==1 and record['identity']==ident and
        record['attached'] is True and record['members']==list(c.MEMBERS) and
        type(record['device_api_count']) is int and record['device_api_count']>0 and
        type(record['f1_fallback8']) is bool and record['numerical_qualified'] is False and
        type(record['scored_shadow_calls']) is int and record['scored_shadow_calls']==0 and
        record['before_external_generations'] is True, 'bundle startup identity/scope')
    c.complete_counts(record['gemm_capture'],list(range(64)),(8,16))
    f1=record['f1_capture']; c.need(set(f1)=={str(i) for i in c.CONFIG['gdn_ids']},'F1 capture layer set')
    for row in f1.values():
        c.need(type(row) is dict and set(row)=={'8','16'} and all(type(v) is int for v in row.values()) and
            row['16']>0 and (row['8']==0 if record['f1_fallback8'] else row['8']>0),'F1 capture geometry')
    arm=ident['arm']; wave='0' if arm=='mtp' else '1'
    keys={'attention-stage1','attention-stage2-cap'+('32' if arm=='mtp' else '16'),
          'gate-up-m8-wave'+wave,'gate-up-m16-wave'+wave}
    if arm=='mtp':
        keys.add('f1-t16')
        if not record['f1_fallback8']: keys.add('f1-t8')
    expected=c.CONFIG['compiler_records'][arm]
    c.need(type(record['selected_compilers']) is dict and set(record['selected_compilers'])==keys and
        all(c.same(record['selected_compilers'][key],expected[key]) for key in keys),'selected compiler identity')
    return record


def ready(material, workers):
    c.need(len(workers)==2 and [w['rank'] for w in workers]==[0,1], 'both ordered startup workers')
    for worker in workers:
        name='BUNDLE-STARTUP-rank-%d.json'%worker['rank']
        raw=c.checked_file(material['native_dir']/name,64<<10)
        startup(json.loads(raw),identity(material['entry_binding'],worker))


def read(observations,binding,workers):
    c.need(len(workers)==2 and [w['rank'] for w in workers]==[0,1], 'both numerical worker identities')
    http_raw=observations['BUNDLE-HTTP.json']; c.need(len(http_raw)<=256<<10,'bounded numerical HTTP receipt')
    http=json.loads(http_raw)
    c.need(type(http) is dict and set(http)=={'schema','arm','session_sha256','source_manifest_sha256',
        'records','numerical_pins','scored','natural_stop_quality','numerical_status'} and
        type(http['schema']) is int and http['schema']==1 and
        all(http[key]==binding[key] for key in ('arm','session_sha256','source_manifest_sha256')) and
        http['scored'] is False and http['natural_stop_quality'] is False and
        http['numerical_status']=='F1_GEMM_REAL_INPUT_EXACT','unscored HTTP identity/scope')
    c.need(type(http['records']) is list and len(http['records'])==3 and
           type(http['numerical_pins']) is list and len(http['numerical_pins'])==2,'complete probe records')
    probes=json.loads(c.checked_file(Path(__file__).with_name('BUNDLE-PROBES.json'),4<<20,c.CONFIG['probes_sha256']))
    for index,(row,probe) in enumerate(zip(http['records'],probes,strict=True)):
        c.need(type(row) is dict and set(row)=={'index','kind','rid','body_sha256','response_sha256',
            'response_bytes','response_base64','prompt_tokens','completion_tokens','scored','abort'} and
            type(row['index']) is int and row['index']==index and row['kind']==probe['kind'] and
            row['rid']==probe['body']['rid'] and row['body_sha256']==probe['body_sha256'] and row['scored'] is False,
            'probe menu/work identity')
        raw=base64.b64decode(row['response_base64'],validate=True)
        c.need(type(row['response_bytes']) is int and len(raw)==row['response_bytes'] and len(raw)<=32<<10 and
               c.sha(raw)==row['response_sha256'],'retained probe HTTP bytes')
        if probe['body']['stream']:
            c.need(index==1,'only the declared C2 member may be aborted')
            usage=inspect_cancel(raw,row['abort'],probe)
        else:
            c.need(row['abort'] is None,'no abort on normal probe')
            value=json.loads(raw); usage=value['usage']
            c.need(len(value['choices'])==1 and usage['completion_tokens']==PROBE_TOKENS,'normal probe output bound')
        c.need(type(usage['prompt_tokens']) is int and usage['prompt_tokens']==row['prompt_tokens']==probe['input_tokens'] and
               type(usage['completion_tokens']) is int and usage['completion_tokens']==row['completion_tokens'],
               'replayed numerical token work')
    for worker,pin in zip(workers,http['numerical_pins'],strict=True):
        ident=identity(binding,worker)
        start_name='BUNDLE-STARTUP-rank-%d.json'%worker['rank']
        start_raw=observations[start_name]; c.need(len(start_raw)<=64<<10,'bounded bundle startup')
        first=startup(json.loads(start_raw),ident)
        name='BUNDLE-NUMERICS-rank-%d.json'%worker['rank']; raw=observations[name]
        c.need(type(pin) is dict and set(pin)=={'file','bytes','sha256'} and pin['file']==name and
               type(pin['bytes']) is int and pin['bytes']==len(raw)<=64<<10 and c.sha(raw)==pin['sha256'],
               'parent-before-scored numerical receipt pin')
        numerical=c.validate_numerics(json.loads(raw),ident,c.CONFIG)
        c.need(numerical['completed_monotonic'] <= http['records'][1]['abort']['started'],
               'both-rank qualification must precede native cancel')
        c.need(numerical['device_api_count']==first['device_api_count'] and
               numerical['f1_fallback8']==first['f1_fallback8'],'capture/real-input device policy equality')
    return dict(status='FOUR_MEMBER_PREBENCH_CHECKS_PASSED',benchmark_menu_requests=30,
                scored_requests=27,auxiliary_requests=3,targeted_c2_abort=True,
                exact_members=['gatednorm_quant','gate_up_wave'],
                attention_numerics='prior-component tolerance and later serving quality gates',
                serving_gain_established=False,adoption_authorized=False)
