"""Inert tests of probe isolation, audited launches and complete layer coverage."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle_contract as c
import bundle_kernels as k
import bundle_runtime as r
import bundle_client as client
import bundle_hooks as hooks
import bundle_readout as readout


def compiled():
    return NS(asm={key: key.encode() for key in k.PARTS}, metadata=NS(arch='gfx1201',
        global_scratch_size=0, profile_scratch_size=0, num_stages=1, num_warps=4,
        shared=4096, warp_size=32, waves_per_eu=1))


class Jit:
    def __init__(self):
        self.value = compiled(); self.launches = []; self.warms = []
        self.launched = self.value
    def warmup(self, *args, **kw):
        self.warms.append((args, kw)); return self.value
    def __getitem__(self, grid):
        def launch(*args, **kw):
            self.launches.append((grid, args, kw)); return self.launched
        return launch


class CoreTests(unittest.TestCase):
    def tearDown(self):
        c.PHASE = None; c.SHADOW = False; r.STATE = None

    def test_all_bundle_preparation_and_runtime_sources_parse(self):
        import ast
        import os
        runtime=Path(os.environ.get('BUNDLE_RUNTIME_ROOT',Path(__file__).parent))
        preparation=Path(os.environ.get('BUNDLE_PREPARATION_ROOT',Path(__file__).parent))
        names=('bundle_contract','bundle_auth','bundle_kernels','bundle_runtime','bundle_hooks',
               'bundle_client','bundle_readout','bundle_entry')
        paths=[runtime/(name+'.py') for name in names]
        paths += [preparation/name for name in ('build_bundle.py','bundle_compile.py',
                   'bundle_compile_read.py','bundle_compile_target.py')]
        for path in paths:ast.parse(path.read_bytes(),filename=str(path))

    def test_phase_restores_original_exception_and_interrupt(self):
        for typ in (RuntimeError, KeyboardInterrupt, SystemExit):
            error = typ('original')
            with self.assertRaises(typ) as got:
                with c.phase('c1'):
                    self.assertEqual(c.PHASE, 'c1'); raise error
            self.assertIs(got.exception, error); self.assertIsNone(c.PHASE)

    def test_no_nested_phase_or_shadow(self):
        with c.phase('c1'):
            with self.assertRaises(ValueError):
                with c.phase('c2'): pass
        with c.shadow():
            with self.assertRaises(ValueError):
                with c.shadow(): pass
        self.assertFalse(c.SHADOW)

    def batch(self, rids, decode=True):
        return NS(reqs=[NS(rid=x) for x in rids], forward_mode=NS(is_decode=lambda: decode,
                                                         is_target_verify=lambda: False))

    def test_probe_ids_exact_and_prefill_not_shadowed(self):
        ids = {'q1': 'c1', 'q2a': 'c2', 'q2b': 'c2'}
        self.assertEqual(c.batch_phase(self.batch(['q1']), ids), 'c1')
        self.assertEqual(c.batch_phase(self.batch(['q2a', 'q2b']), ids), 'c2')
        self.assertIsNone(c.batch_phase(self.batch(['q1'], False), ids))
        self.assertIsNone(c.batch_phase(self.batch(['normal']), ids))
        for rows in (['q1', 'normal'], ['q1', 'q2a'], ['q1', 'q1'], [None]):
            with self.assertRaises(ValueError): c.batch_phase(self.batch(rows), ids)

    def test_counts_require_sparse_global_ids_and_both_geometries(self):
        ids = [0, 1, 2, 4]
        rows = {str(i): {'8': 1, '16': 2} for i in ids}
        c.complete_counts(rows, ids, (8, 16))
        for mutate in (lambda x: x.pop('4'), lambda x: x['0'].update({'8': 0}),
                       lambda x: x['0'].update({'8': True}), lambda x: x.update({'3': {'8': 1, '16': 1}})):
            bad = copy.deepcopy(rows); mutate(bad)
            with self.assertRaises(ValueError): c.complete_counts(bad, ids, (8, 16))

    def test_safe_file_and_exclusive_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root/'receipt.json'
            pin = c.publish(path, {'value': 1})
            self.assertEqual(c.checked_file(path, 100, pin['sha256']), b'{"value":1}\n')
            with self.assertRaises(FileExistsError): c.publish(path, {'value': 2})
            self.assertEqual(json.loads(path.read_bytes()), {'value': 1})
            link = root/'link'; link.symlink_to(path)
            with self.assertRaises((OSError, ValueError)): c.checked_file(link, 100)
            with self.assertRaises(ValueError): c.checked_file(path, 1)

    def test_identity_joins_full_worker_fields(self):
        manifest = dict(arm='mtp', session_sha256='s', source_manifest_sha256='m')
        worker = dict(manifest, rank=0, pid=12, start_ticks=3, boot_id='b', identity_sha256='i')
        self.assertEqual(c.worker_identity(manifest, worker)['worker_identity_sha256'], 'i')
        worker['arm'] = 'control'
        with self.assertRaises(ValueError): c.worker_identity(manifest, worker)


class AuditTests(unittest.TestCase):
    def audited(self, key=lambda a, kw: 'key', capturing=lambda: False):
        jit = Jit(); pins = {'key': k.compiler_record(jit.value)}; records = {}
        return jit, k.Audited(jit, pins, capturing, key, records), records

    def test_audit_exact_cached_object_before_launch(self):
        jit, audit, rows = self.audited()
        self.assertIs(audit[(3,)](1, flag=True), jit.value)
        self.assertIs(audit[(3,)](1, flag=True), jit.value)
        self.assertEqual(len(jit.launches), 2); self.assertEqual(set(rows), {'key'})

    def test_bad_compile_refuses_before_launch(self):
        jit, audit, rows = self.audited(); jit.value.asm['hsaco'] = b'wrong'
        with self.assertRaisesRegex(ValueError, 'compiler drift'): audit[(1,)]()
        self.assertEqual(jit.launches, [])

    def test_compile_refusal_preserves_expected_and_observed_record(self):
        jit, audit, rows = self.audited()
        expected=copy.deepcopy(audit.expected['key'])
        jit.value.asm['llir']=b'changed-live-llir'
        with self.assertRaisesRegex(ValueError,'compiler drift') as got:
            audit[(1,)]()
        records=json.loads(str(got.exception).split('; records=',1)[1])
        self.assertEqual(records['expected'],expected)
        self.assertEqual(records['observed'],k.compiler_record(jit.value))
        self.assertNotEqual(records['observed']['parts']['llir'],expected['parts']['llir'])
        self.assertFalse(jit.launches);self.assertFalse(audit.cached);self.assertFalse(rows)

    def test_alternate_arguments_refuse_before_second_launch(self):
        jit, audit, rows = self.audited(); audit[(1,)]()
        jit.value = compiled()
        with self.assertRaisesRegex(ValueError, 'foreign bundle specialization'): audit[(1,)]()
        self.assertEqual(len(jit.launches), 1)

    def test_cached_binary_mutation_refuses(self):
        jit, audit, rows = self.audited(); audit[(1,)]()
        jit.value.asm['amdgcn'] = b'mutated'
        with self.assertRaisesRegex(ValueError, 'cached bundle compiler'): audit[(1,)]()
        self.assertEqual(len(jit.launches), 1)

    def test_capture_must_have_warmed(self):
        jit, audit, rows = self.audited(capturing=lambda: True)
        with self.assertRaisesRegex(ValueError, 'first seen inside capture'): audit[(1,)]()
        self.assertFalse(jit.warms); self.assertFalse(jit.launches)

    def test_same_cached_specialization_in_capture(self):
        capture = [False]; jit, audit, rows = self.audited(capturing=lambda: capture[0])
        audit[(1,)](); capture[0] = True; audit[(1,)]()
        self.assertEqual(len(jit.launches), 2)

    def test_launched_object_must_be_audited(self):
        jit, audit, rows = self.audited(); jit.launched = compiled()
        with self.assertRaisesRegex(ValueError, 'launched object differs'): audit[(1,)]()

    def test_heuristic_wrapper_uses_real_run_warmup_interface(self):
        class Heuristic:
            def __init__(self): self.jit=Jit();self.warmups=0
            def run(self,*args,**kw):
                self.asserted=kw.pop('warmup') is True;self.warmups+=1
                return self.jit.value
            def __getitem__(self,grid):return self.jit[grid]
        jit=Heuristic(); audit=k.Audited(jit,{'key':k.compiler_record(jit.jit.value)},lambda:False,lambda a,kw:'key',{})
        audit[(1,)](1)
        self.assertTrue(jit.asserted);self.assertEqual(jit.warmups,2);self.assertEqual(len(jit.jit.launches),1)

    def test_undeclared_key_refuses_and_other_shapes_delegate(self):
        jit, audit, rows = self.audited(key=lambda a, kw: 'foreign')
        with self.assertRaisesRegex(ValueError, 'undeclared'): audit[(1,)]()
        self.assertFalse(jit.launches)
        jit, audit, rows = self.audited(key=lambda a, kw: None)
        audit[(1,)](); self.assertEqual(len(jit.launches), 1); self.assertFalse(rows)

    def test_function_view_leaves_native_globals_and_code_fixed(self):
        namespace = {'value': lambda x: x+1}
        exec('def f(x): return value(x)\n', namespace)
        native = namespace['f']; replacement = lambda x: x+10
        view = k.function_view(native, {'value': replacement})
        self.assertIs(view.__code__, native.__code__)
        self.assertEqual(native(1), 2); self.assertEqual(view(1), 11)
        self.assertIsNot(view.__globals__, namespace)
        with self.assertRaises(ValueError): k.function_view(native, {'absent': replacement})

    def test_gemm_closed_shape_key_excludes_down_and_other_rows(self):
        self.assertEqual(k.gemm_key([None]*5+[8,17408,2560], {'waves_per_eu': 0}), 'gate-up-m8-wave0')
        for dims in ([16,5120,4352], [1,17408,2560], [32,17408,2560]):
            self.assertIsNone(k.gemm_key([None]*5+dims, {'waves_per_eu': 0}))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.capture = False; self.calls = 0
        r.STATE = NS(routes={4: object()}, fallback8=False, manifest={'arm': 'mtp'},
            f1_capture={'4': {'8': 0, '16': 0}}, f1_exact={'4': {}},
            capturing=lambda: self.capture)
        outer = self
        class Base:
            def eligible(self, *args): return self.admit
            def try_project(self, *args): outer.calls += 1; return 'output'
        self.route = r.route_class(Base)(); self.route.bundle_layer=4; self.route.admit=True
        self.batch = NS(forward_mode=NS(is_target_verify=lambda: True))
    def tearDown(self): r.STATE = None; c.PHASE = None; c.SHADOW = False
    def call(self, tokens=16):
        return self.route.try_project(None, self.batch, None, None, (tokens, 24, 128), False)

    def test_candidate_routes_and_control_skips_fusion_in_capture(self):
        self.capture = True; self.assertEqual(self.call(), 'output')
        r.STATE.manifest['arm']='control'; self.assertIsNone(self.call())
        self.assertEqual(self.calls, 1); self.assertEqual(r.STATE.f1_capture['4']['16'], 2)

    def test_control_eager_probe_compares_then_uses_original_path(self):
        r.STATE.manifest['arm']='control'
        self.assertIsNone(self.call()); self.assertEqual(self.calls, 0)
        with c.phase('c2'): self.assertIsNone(self.call())
        self.assertEqual(self.calls, 1)
        r.STATE.f1_exact['4']['16']=1
        with c.phase('c2'): self.assertIsNone(self.call())
        self.assertEqual(self.calls, 1)

    def test_selected_t16_must_engage_and_t8_fallback_is_explicit(self):
        self.route.admit=False
        with self.assertRaisesRegex(ValueError, 'geometry refused'): self.call()
        r.STATE.fallback8=True
        self.assertIsNone(self.call(8))
        self.assertEqual(self.calls, 0)

    def test_wave_change_is_exactly_gate_up_and_shadow_is_opposite(self):
        for arm in ('control', 'mtp'):
            r.STATE.manifest['arm']=arm
            self.assertEqual(r.wave_hint(8,17408,5120,1), int(arm=='control'))
            with c.shadow(): self.assertEqual(r.wave_hint(16,17408,5120,1), int(arm=='mtp'))
            for m,n,kdim in ((16,5120,8704),(1,17408,5120),(32,17408,5120)):
                self.assertEqual(r.wave_hint(m,n,kdim,1), 1)


class HookTests(unittest.TestCase):
    def test_exactly_once_context_normal_delegation_and_exception_restoration(self):
        events=[]
        class Scheduler:
            enable_overlap=False
            def run_batch(self,batch):
                events.append(('run', c.PHASE)); return graph.can_run_graph(batch)
        scheduler=Scheduler(); original=Scheduler.run_batch
        sources={}
        # Source authentication itself is tested against retained native code in
        # the owner suite. These patches isolate the hook lifecycle only.
        with patch.object(hooks,'authenticate_target',return_value=(Scheduler,{'run_batch': original})), \
             patch.object(hooks,'_source',return_value=[]), patch.object(hooks,'_match'), \
             patch.object(c,'CONFIG',{'probe_ids': {'q': 'c1'}}):
            from types import ModuleType
            actual=ModuleType('_bundle_graph_test')
            exec('class DecodeCudaGraphRunner:\n def can_run_graph(self,batch): return "native"\n',actual.__dict__)
            graph=actual.DecodeCudaGraphRunner(); saved=actual.DecodeCudaGraphRunner.can_run_graph
            batch=NS(reqs=[NS(rid='q')],forward_mode=NS(is_decode=lambda:True,is_target_verify=lambda:False))
            with hooks.worker_scope(None,sources,actual,{}):
                self.assertIs(scheduler.run_batch(batch), False)
                batch.reqs=[NS(rid='normal')]
                self.assertEqual(scheduler.run_batch(batch),'native')
                self.assertIsNone(c.PHASE)
            self.assertIs(Scheduler.run_batch,original)
            self.assertIs(actual.DecodeCudaGraphRunner.can_run_graph,saved)
        self.assertEqual(events,[('run','c1'),('run',None)])


class ProbeTests(unittest.TestCase):
    def menu(self):
        return dict(bound=[dict(case=name, body_sha256='source-'+str(i), input_tokens=123+i,
            body=dict(ignore_eos=False,max_completion_tokens=4096,stream=False,n=1,
                      messages=[{'role':'user','content':'fixed-'+str(i)}],rid='old-'+str(i),
                      cache_salt='old-'+str(i),temperature=0.0,model='selected-test-model',
                      return_meta_info=True,return_token_ids=True,return_spec_tokens_details=True)) for i,name in enumerate(
                ('warmup-c1','warmup-c2-a','warmup-c2-b'))])

    def test_probe_prompts_preserved_and_only_four_fields_change(self):
        menu=self.menu(); probes=client.derive(menu,'0123456789abcdef')
        self.assertEqual([r['kind'] for r in probes],['c1','c2','c2'])
        for index,(old,new) in enumerate(zip(menu['bound'],probes)):
            self.assertEqual({key for key in old['body'] if old['body'][key]!=new['body'][key]},
                             {'rid','cache_salt','ignore_eos','max_completion_tokens',
                              'return_meta_info','return_token_ids','return_spec_tokens_details'} |
                             ({'stream'} if index==1 else set()))
            self.assertEqual(set(new['body'])-set(old['body']),{'stream_options'} if index==1 else set())
            self.assertEqual(new['body']['max_completion_tokens'],256)
            self.assertEqual(new['body_sha256'],c.sha(c.canonical(new['body'])))
        self.assertFalse(menu['bound'][0]['body']['ignore_eos'])

    def test_probe_bad_menu_and_nonce_refuse(self):
        for nonce in ('', 'x'*16, True):
            with self.assertRaises(ValueError): client.derive(self.menu(),nonce)
        menu=self.menu();menu['bound'].pop()
        with self.assertRaises(ValueError): client.derive(menu,'0'*16)

    def numerical(self):
        ids=[i for i in range(64) if i%4!=3]
        pins={key:{'fixture':'compiler'} for key in
              ('gate-up-m8-wave0','gate-up-m8-wave1','gate-up-m16-wave0','gate-up-m16-wave1','f1-t16')}
        config=dict(gdn_ids=ids,probe_ids={'q1':'c1','q2a':'c2','q2b':'c2'},compiler_records={'mtp': pins})
        record=dict(schema=1,identity={'rank':0,'arm':'mtp'},status='F1_GEMM_REAL_INPUT_EXACT',members=list(c.MEMBERS),
            f1={str(i):{'16':1} for i in ids},gemm={str(i):{'8':1,'16':1} for i in range(64)},
            f1_fallback8=True,device_api_count=64,probe_ids=config['probe_ids'],scored=False,
            shadow_calls=200,capture_shadow_calls=0,compilers=copy.deepcopy(pins),completed_monotonic=.5,
            exact_members=['gatednorm_quant','gate_up_wave'])
        return record,config

    def test_complete_real_input_record_and_missing_layer_or_shadow_fail(self):
        record,config=self.numerical(); c.validate_numerics(record,{'rank':0,'arm':'mtp'},config)
        mutations=(lambda x:x['f1'].pop('62'),lambda x:x['gemm']['63'].update({'16':0}),
                   lambda x:x.update(capture_shadow_calls=1),lambda x:x.update(scored=True),
                   lambda x:x.update(identity={'rank':1,'arm':'mtp'}),lambda x:x.update(f1_fallback8=False),
                   lambda x:x['compilers'].pop('gate-up-m8-wave0'))
        for mutate in mutations:
            bad=copy.deepcopy(record);mutate(bad)
            with self.assertRaises(ValueError): c.validate_numerics(bad,{'rank':0,'arm':'mtp'},config)


def cancel_fixture(probe):
    def event(delta,finish=None,usage=None):
        return b'data: '+c.canonical(dict(id=probe['body']['rid'],model=probe['body']['model'],
            choices=[] if usage else [dict(index=0,delta=delta,finish_reason=finish)],
            **({'usage':usage} if usage else {}))).strip()+b'\n\n'
    prefix=event({'reasoning_content':'one'})+event({'content':'two'})
    raw=prefix+event({},'abort')+event({},usage=dict(prompt_tokens=probe['input_tokens'],completion_tokens=4))+b'data: [DONE]\n\n'
    abort=dict(body_sha256=c.sha(c.canonical(dict(rid=probe['body']['rid'],abort_all=False))),
        status=200,response_bytes=0,response_sha256=c.sha(b''),prefix_bytes=len(prefix),prefix_sha256=c.sha(prefix),
        first_generated=1.,started=2.,finished=3.,drained=4.)
    return raw,abort


class CancelTests(unittest.TestCase):
    def test_reasoning_and_content_then_targeted_native_abort(self):
        probe=client.derive(ProbeTests().menu(),'0'*16)[1];raw,abort=cancel_fixture(probe)
        self.assertEqual(client.inspect_cancel(raw,abort,probe)['completion_tokens'],4)

    def test_abort_requires_live_prefix_exact_rid_ack_and_done(self):
        probe=client.derive(ProbeTests().menu(),'0'*16)[1];raw,abort=cancel_fixture(probe)
        cases=[(raw.replace(b'"abort"',b'"length"'),abort),
               (raw.replace(b'data: [DONE]\n\n',b''),abort),
               (raw,dict(abort,status=500)),(raw,dict(abort,body_sha256='wrong')),
               (raw,dict(abort,prefix_sha256='wrong')),(raw,dict(abort,started=0.))]
        for body,receipt in cases:
            with self.assertRaises(ValueError):client.inspect_cancel(body,receipt,probe)

    def test_truncated_duplicate_done_and_foreign_stream_refuse(self):
        probe=client.derive(ProbeTests().menu(),'0'*16)[1];raw,abort=cancel_fixture(probe)
        for body in (raw[:-2],raw+b'data: [DONE]\n\n',raw.replace(probe['body']['rid'].encode(),b'foreign-rid')):
            with self.assertRaises(ValueError):client.inspect_cancel(body,abort,probe)


class ReadoutTests(unittest.TestCase):
    def fixture(self):
        probes=client.derive(ProbeTests().menu(),'0'*16)
        binding=dict(arm='mtp',session_sha256='s',source_manifest_sha256='m')
        workers=[dict(rank=i,pid=12+i,start_ticks=4,boot_id='b',identity_sha256='i'+str(i)) for i in (0,1)]
        numerical,config=ProbeTests().numerical()
        pins=config['compiler_records']['mtp'];pins.update({'attention-stage1':{},'attention-stage2-cap32':{}})
        config.update(probe_ids={p['body']['rid']:p['kind'] for p in probes},probes_sha256=c.sha(c.canonical(probes)))
        observations={};http=dict(schema=1,**binding,records=[],numerical_pins=[],scored=False,
                                 natural_stop_quality=False,numerical_status='F1_GEMM_REAL_INPUT_EXACT')
        import base64
        for i,probe in enumerate(probes):
            abort=None
            if i==1:
                raw,abort=cancel_fixture(probe)
            else:
                raw=c.canonical(dict(usage=dict(prompt_tokens=probe['input_tokens'],completion_tokens=256),choices=[{}]))
            http['records'].append(dict(index=i,kind=probe['kind'],rid=probe['body']['rid'],
                body_sha256=probe['body_sha256'],response_sha256=c.sha(raw),response_bytes=len(raw),
                response_base64=base64.b64encode(raw).decode(),prompt_tokens=probe['input_tokens'],
                completion_tokens=4 if i==1 else 256,scored=False,abort=abort))
        selected={key:pins[key] for key in ('attention-stage1','attention-stage2-cap32','gate-up-m8-wave0','gate-up-m16-wave0','f1-t16')}
        for worker in workers:
            ident=readout.identity(binding,worker)
            first=dict(schema=1,identity=ident,attached=True,members=list(c.MEMBERS),
                f1_capture={str(i):{'8':0,'16':1} for i in config['gdn_ids']},
                gemm_capture={str(i):{'8':1,'16':1} for i in range(64)},f1_fallback8=True,
                device_api_count=64,selected_compilers=selected,numerical_qualified=False,
                scored_shadow_calls=0,before_external_generations=True)
            observations['BUNDLE-STARTUP-rank-%d.json'%worker['rank']]=c.canonical(first)
            num=copy.deepcopy(numerical);num.update(identity=ident,probe_ids=config['probe_ids'],compilers=pins)
            raw=c.canonical(num);name='BUNDLE-NUMERICS-rank-%d.json'%worker['rank']
            observations[name]=raw;http['numerical_pins'].append(dict(file=name,bytes=len(raw),sha256=c.sha(raw)))
        observations['BUNDLE-HTTP.json']=c.canonical(http)
        return observations,binding,workers,config,probes

    def test_roundtrip_and_foreign_worker_digest_or_http_work_refuse(self):
        observations,binding,workers,config,probes=self.fixture()
        with patch.object(c,'CONFIG',config),patch.object(c,'checked_file',return_value=c.canonical(probes)):
            self.assertEqual(readout.read(observations,binding,workers)['status'],'FOUR_MEMBER_PREBENCH_CHECKS_PASSED')
            for kind in ('foreign-worker','changed-numerical','changed-http','missing-startup'):
                bad=copy.deepcopy(observations);w=copy.deepcopy(workers)
                if kind=='foreign-worker':w[1]['pid']+=1
                if kind=='changed-numerical':bad['BUNDLE-NUMERICS-rank-1.json']+=b' '
                if kind=='missing-startup':bad.pop('BUNDLE-STARTUP-rank-1.json')
                if kind=='changed-http':
                    value=json.loads(bad['BUNDLE-HTTP.json']);value['records'][1]['completion_tokens']=31
                    bad['BUNDLE-HTTP.json']=c.canonical(value)
                with self.assertRaises((ValueError,KeyError)):readout.read(bad,binding,w)

    def test_capture_receipt_refuses_wrong_cap_compiler_and_missing_layer(self):
        observations,binding,workers,config,probes=self.fixture()
        first=json.loads(observations['BUNDLE-STARTUP-rank-0.json'])
        with patch.object(c,'CONFIG',config):
            for mutation in (lambda x:x['gemm_capture']['63'].update({'16':0}),
                             lambda x:x['selected_compilers'].pop('attention-stage2-cap32'),
                             lambda x:x['f1_capture'].pop('62')):
                bad=copy.deepcopy(first);mutation(bad)
                with self.assertRaises(ValueError):readout.startup(bad,readout.identity(binding,workers[0]))


class ClientTests(unittest.TestCase):
    def execute(self, *, missing=False, terminal_first=False):
        observations,binding,workers,config,probes=ReadoutTests().fixture()
        calls=[]
        class Response:
            status=200
            def __init__(self,chunks):self.chunks=list(chunks)
            def read(self,count):
                if not self.chunks:return b''
                raw=self.chunks.pop(0)
                if len(raw)>count:self.chunks.insert(0,raw[count:]);return raw[:count]
                return raw
            read1=read
        class Connection:
            sock=None
            def __init__(self,*args,**kwargs):pass
            def request(self,method,path,body,headers):
                self.path=path;self.body=json.loads(body);calls.append((method,path,self.body))
            def getresponse(self):
                if self.path=='/abort_request':return Response([])
                probe=next(p for p in probes if p['body']['rid']==self.body['rid'])
                if self.body['stream']:
                    raw,abort=cancel_fixture(probe);prefix=abort['prefix_bytes']
                    return Response([raw] if terminal_first else [raw[:prefix],raw[prefix:]])
                raw=c.canonical(dict(usage=dict(prompt_tokens=probe['input_tokens'],completion_tokens=256),choices=[{}]))
                return Response([raw])
            def close(self):pass
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp).resolve()
            (directory/'BUNDLE-PROBES.json').write_bytes(c.canonical(probes))
            for name,raw in observations.items():
                if name=='BUNDLE-HTTP.json' or missing and name=='BUNDLE-NUMERICS-rank-1.json':continue
                (directory/name).write_bytes(raw)
            material=dict(native_dir=directory,entry_binding=binding)
            with patch.object(c,'CONFIG',config),patch.object(client,'__file__',str(directory/'bundle_client.py')):
                import time
                if missing or terminal_first:
                    with self.assertRaises(ValueError):client.run(material,18088,time.monotonic()+10,workers,factory=Connection)
                    self.assertFalse((directory/'BUNDLE-HTTP.json').exists())
                else:
                    pin=client.run(material,18088,time.monotonic()+10,workers,factory=Connection)
                    raw=c.checked_file(directory/pin['file'],256<<10,pin['sha256'])
                    observations['BUNDLE-HTTP.json']=raw
                    with patch.object(readout,'__file__',str(directory/'bundle_readout.py')):
                        value=readout.read(observations,binding,workers)
                        self.assertTrue(value['targeted_c2_abort'])
        return calls

    def test_actual_client_to_offline_reader_with_one_c2_abort(self):
        calls=self.execute()
        self.assertEqual(sum(path=='/v1/chat/completions' for _,path,_ in calls),3)
        aborted=[body for _,path,body in calls if path=='/abort_request']
        self.assertEqual(len(aborted),1);self.assertIs(aborted[0]['abort_all'],False)

    def test_no_abort_or_scored_admission_without_both_rank_qualification(self):
        calls=self.execute(missing=True)
        self.assertFalse(any(path=='/abort_request' for _,path,_ in calls))

    def test_completed_stream_cannot_be_relabelled_cancelled(self):
        calls=self.execute(terminal_first=True)
        self.assertFalse(any(path=='/abort_request' for _,path,_ in calls))


if __name__ == '__main__': unittest.main(verbosity=2)
