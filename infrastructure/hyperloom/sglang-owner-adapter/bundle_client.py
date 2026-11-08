"""Three unscored, bounded requests before the unchanged serving menu.

There is no retry and no quality or timing promotion. The parent must validate
both worker numerical receipts before constructing the scored client.
"""
import base64
import http.client
import json
from pathlib import Path
import socket
import threading
import time

import bundle_contract as c

RESPONSE_BYTES = 32 << 10
PROBE_SECONDS = 400
PROBE_TOKENS = 256


def derive(menu, nonce):
    """Copy frozen prompts; change only RID, salt and the unscored output bound."""
    c.need(type(nonce) is str and len(nonce) == 16 and
           all(x in '0123456789abcdef' for x in nonce), 'fixed probe nonce')
    selected = []
    for kind in ('warmup-c1', 'warmup-c2'):
        rows = [r for r in menu['bound'] if r['case'] == kind or r['case'].startswith(kind+'-')]
        c.need(len(rows) == (1 if 'c1' in kind else 2), 'frozen probe source menu')
        for row in rows:
            body = json.loads(c.canonical(row['body']))
            c.need(body['ignore_eos'] is False and body['max_completion_tokens'] == 4096 and
                   body['stream'] is False and body['n'] == 1, 'frozen unscored probe derivation')
            rid = 'bundle-qual-'+nonce+'-'+str(len(selected))
            body.update(rid=rid, cache_salt=rid, ignore_eos=True, max_completion_tokens=PROBE_TOKENS)
            for key in ('return_meta_info','return_token_ids','return_spec_tokens_details'):
                c.need(key not in body or body[key] is True, 'frozen response verbosity')
                if key in body: body[key]=False
            # Abort one member only after both workers publish the complete
            # M8/M16 numerical checks. The other member drains normally; the
            # unchanged benchmark warmups then check post-abort recovery.
            if len(selected) == 1:
                body.update(stream=True,stream_options={'include_usage':True})
            selected.append(dict(kind='c1' if 'c1' in kind else 'c2', source_case=row['case'],
                source_body_sha256=row['body_sha256'], body=body,
                input_tokens=row['input_tokens'], body_sha256=c.sha(c.canonical(body))))
    c.need(len(selected) == 3 and selected[1]['kind'] == selected[2]['kind'] == 'c2',
           'one C1 and one paired C2 numerical group')
    return selected


def sse_events(raw, *, final=False):
    c.need(type(raw) is bytes, 'SSE bytes')
    normalized=raw.replace(b'\r\n',b'\n')
    blocks=normalized.split(b'\n\n')
    tail=blocks.pop()
    c.need(not final or not tail.strip(), 'truncated qualification SSE')
    values=[]
    for block in blocks:
        if not block.strip():continue
        lines=block.split(b'\n')
        c.need(len(lines)==1 and lines[0].startswith(b'data: '),'qualification SSE framing')
        value=lines[0][6:]
        values.append(None if value==b'[DONE]' else json.loads(value))
    return values


def inspect_cancel(raw, abort, probe):
    """Require a targeted native abort while output is live, then graceful DONE."""
    c.need(type(abort) is dict and set(abort)=={'body_sha256','status','response_bytes',
        'response_sha256','prefix_bytes','prefix_sha256','first_generated','started','finished','drained'},
        'numerical abort receipt fields')
    expected=c.canonical(dict(rid=probe['body']['rid'],abort_all=False))
    c.need(abort['body_sha256']==c.sha(expected) and abort['status']==200 and
        abort['response_bytes']==0 and abort['response_sha256']==c.sha(b''),'targeted native abort acknowledgment')
    times=[abort[key] for key in ('first_generated','started','finished','drained')]
    c.need(all(type(t) in (int,float) and 0<=t<float('inf') for t in times) and times==sorted(times),
           'abort chronology')
    size=abort['prefix_bytes']
    c.need(type(size) is int and 0<size<=len(raw) and c.sha(raw[:size])==abort['prefix_sha256'],
           'live generated prefix pin')
    prefix=sse_events(raw[:size])
    generated=lambda event:any(any(type(choice.get('delta',{}).get(key)) is str and
        bool(choice['delta'][key]) for key in ('content','reasoning_content')) for choice in event.get('choices',[]))
    c.need(None not in prefix and sum(generated(event) for event in prefix)>=2 and
           all(not choice.get('finish_reason') for event in prefix for choice in event.get('choices',[])),
           'abort must follow live content and precede terminal output')
    events=sse_events(raw,final=True)
    c.need(events and events[-1] is None and events.count(None)==1,'one final SSE DONE')
    terminal=0;usage=None
    for event in events[:-1]:
        c.need(type(event) is dict and event.get('id')==probe['body']['rid'] and
               event.get('model')==probe['body']['model'],'SSE request/model identity')
        choices=event.get('choices')
        c.need(type(choices) is list and len(choices)<=1 and (not terminal or not choices),
               'SSE choice ordering')
        for choice in choices:
            c.need(type(choice) is dict and type(choice.get('index')) is int and choice['index']==0 and type(choice.get('delta',{})) is dict,
                   'SSE selected choice')
            finish=choice.get('finish_reason')
            if finish is not None:
                c.need(finish=='abort' and terminal==0,'one native abort terminal')
                terminal+=1
        if event.get('usage') is not None:
            c.need(usage is None,'one SSE usage receipt');usage=event['usage']
    c.need(terminal==1 and type(usage) is dict and type(usage.get('prompt_tokens')) is int and
           usage['prompt_tokens']==probe['input_tokens'] and type(usage.get('completion_tokens')) is int and
           2<=usage['completion_tokens']<PROBE_TOKENS,'graceful abort token work')
    return usage


def run(material, port, deadline, workers, *, factory=http.client.HTTPConnection,
        clock=time.monotonic):
    probes = json.loads(c.checked_file(Path(__file__).with_name('BUNDLE-PROBES.json'),
                                      4 << 20, c.CONFIG['probes_sha256']))
    c.need(len(probes) == 3 and type(port) is int and 0 < port < 65536 and
           clock() < deadline, 'bounded unscored client admission')
    end = min(deadline, clock()+PROBE_SECONDS)
    records, failures, connections = {}, {}, {}
    def qualified():
        for rank,worker in enumerate(workers):
            path=material['native_dir']/('BUNDLE-NUMERICS-rank-%d.json'%rank)
            if not path.exists():return False
            raw=c.checked_file(path,64<<10)
            identity=dict(**{k:material['entry_binding'][k] for k in
                            ('arm','session_sha256','source_manifest_sha256')},
                          **{k:worker[k] for k in ('rank','pid','boot_id','start_ticks')},
                          worker_identity_sha256=worker['identity_sha256'])
            c.validate_numerics(json.loads(raw),identity,c.CONFIG)
        return True

    def abort_request(probe,prefix,first):
        c.need(qualified(),'both-rank numerical qualification before abort')
        body=c.canonical(dict(rid=probe['body']['rid'],abort_all=False))
        connection=factory('127.0.0.1',port,timeout=max(.001,end-clock()))
        started=clock()
        try:
            connection.request('POST','/abort_request',body=body,headers={'Content-Type':'application/json'})
            response=connection.getresponse();raw=response.read(4097)
            c.need(response.status==200 and raw==b'','targeted abort acknowledgment')
            return dict(body_sha256=c.sha(body),status=response.status,response_bytes=0,response_sha256=c.sha(raw),
                prefix_bytes=len(prefix),prefix_sha256=c.sha(prefix),first_generated=first,
                started=started,finished=clock())
        finally:connection.close()

    def one(index, barrier=None):
        connection = None
        try:
            if barrier is not None:
                barrier.wait(timeout=max(0.001, end-clock()))
            row = probes[index]
            body = c.canonical(row['body'])
            c.need(c.sha(body) == row['body_sha256'] and
                   c.CONFIG['probe_ids'].get(row['body']['rid']) == row['kind'], 'probe source binding')
            c.need(end > clock(), 'probe deadline')
            connection = factory('127.0.0.1', port, timeout=end-clock())
            connections[index] = connection
            connection.request('POST', '/v1/chat/completions', body=body,
                               headers={'Content-Type': 'application/json',
                                        'Accept': 'text/event-stream' if row['body']['stream'] else 'application/json'})
            response = connection.getresponse()
            raw = bytearray();abort=None;first=None
            streaming=row['body']['stream']
            while True:
                remaining = end-clock()
                c.need(remaining > 0, 'probe response deadline')
                if connection.sock is not None: connection.sock.settimeout(remaining)
                reader=(getattr(response,'read1',None) or response.read) if streaming else response.read
                chunk = reader(min(4096, RESPONSE_BYTES+1-len(raw)))
                if not chunk: break
                raw.extend(chunk)
                c.need(len(raw) <= RESPONSE_BYTES, 'probe response byte limit')
                if streaming and abort is None:
                    events=sse_events(bytes(raw))
                    generated=sum(any(any(type(choice.get('delta',{}).get(key)) is str and bool(choice['delta'][key])
                                          for key in ('content','reasoning_content')) for choice in event.get('choices',[]))
                                  for event in events if event is not None)
                    if generated and first is None:first=clock()
                    if generated>=2 and qualified():
                        c.need(None not in events and all(not choice.get('finish_reason')
                            for event in events for choice in event.get('choices',[])),'abort target already terminal')
                        abort=abort_request(row,bytes(raw),first)
            c.need(response.status == 200, 'numerical probe HTTP status')
            if streaming:
                c.need(abort is not None,'numerical abort was not dispatched')
                abort['drained']=clock()
                usage=inspect_cancel(bytes(raw),abort,row)
            else:
                value = json.loads(raw);usage = value['usage']
                c.need(type(usage['prompt_tokens']) is int and usage['prompt_tokens'] == row['input_tokens'] and
                       type(usage['completion_tokens']) is int and usage['completion_tokens'] == PROBE_TOKENS and
                       len(value['choices']) == 1, 'numerical probe token work')
            records[index] = dict(index=index, kind=row['kind'], rid=row['body']['rid'],
                body_sha256=c.sha(body), response_sha256=c.sha(bytes(raw)), response_bytes=len(raw),
                response_base64=base64.b64encode(raw).decode(), prompt_tokens=usage['prompt_tokens'],
                completion_tokens=usage['completion_tokens'], scored=False,abort=abort)
        except Exception as error:
            failures[index] = type(error).__name__+': '+str(error)[:512]
            if barrier is not None: barrier.abort()
        finally:
            if connection is not None: connection.close()
    one(0)
    c.need(not failures, 'C1 numerical request failed: '+str(failures))
    barrier = threading.Barrier(2)
    threads = [threading.Thread(target=one, args=(i, barrier), name='bundle-probe-'+str(i)) for i in (1, 2)]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(max(0., end-clock()))
    finally:
        if any(thread.is_alive() for thread in threads):
            barrier.abort()
            for connection in list(connections.values()):
                sock = getattr(connection, 'sock', None)
                if sock is not None:
                    try: sock.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                connection.close()
            for thread in threads:
                if thread.ident is not None: thread.join(1.)
    c.need(not any(thread.is_alive() for thread in threads) and not failures and
           set(records) == {0, 1, 2} and clock() < end, 'paired numerical requests incomplete: '+str(failures))
    pins = []
    for rank, worker in enumerate(workers):
        c.need(worker['rank'] == rank, 'ordered gate worker identities')
        path = material['native_dir'] / ('BUNDLE-NUMERICS-rank-%d.json' % rank)
        raw = c.checked_file(path, c.MAX_RECORD_BYTES)
        identity = dict(**{k: material['entry_binding'][k] for k in
                          ('arm', 'session_sha256', 'source_manifest_sha256')},
                        **{k: worker[k] for k in ('rank', 'pid', 'boot_id', 'start_ticks')},
                        worker_identity_sha256=worker['identity_sha256'])
        c.validate_numerics(json.loads(raw), identity, c.CONFIG)
        pins.append(dict(file=path.name, bytes=len(raw), sha256=c.sha(raw)))
    return c.publish(material['native_dir'] / 'BUNDLE-HTTP.json', dict(schema=1,
        arm=material['entry_binding']['arm'], session_sha256=material['entry_binding']['session_sha256'],
        source_manifest_sha256=material['entry_binding']['source_manifest_sha256'],
        records=[records[i] for i in range(3)], numerical_pins=pins,
        scored=False, natural_stop_quality=False, numerical_status='F1_GEMM_REAL_INPUT_EXACT'))
