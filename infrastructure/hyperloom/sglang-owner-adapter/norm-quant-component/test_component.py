"""Source-bound component gates with inert frameworks and adversarial records."""
import copy
from contextlib import ExitStack
import hashlib
import json
import linecache
from pathlib import Path
import sys
import tempfile
import time
import unittest
import weakref
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import compiler_gate
import contract
import corpus
import fusion_ops as ops
import read_component as reader
import source_loader
import stage_probe as producer
import support as s
import test_support as f

ROOT=Path(__file__).resolve().parent

class CorpusTests(unittest.TestCase):
    def test_all_bf16_cells_in_x_and_z(self):
        for t in contract.TOKENS:
            for key in ('x','z'):
                values=set()
                for c in range(3):values.update(corpus.values(dict(tokens=t,kind=f'{key}-all-{c}'),key))
                self.assertEqual(values,set(range(65536)))
    def test_special_in_each_input_and_negative_zero(self):
        for k in ('x','z','w'):
            self.assertTrue(set(corpus.SPECIAL)<=set(corpus.values(dict(tokens=8,kind=k+'-special'),k)))
        self.assertEqual(set(corpus.values(dict(tokens=8,kind='zeros'),'x')),{0,32768})
    def test_exact_source_assets(self):
        for name,pin in contract.INPUT_PINS.items():self.assertEqual(s.sha((ROOT/name).read_bytes()),pin)

class SourceTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close);self.stack.enter_context(source_loader.ModuleScope())
        self.torch,self.triton,self.tl=f.frameworks()
        self.modules=source_loader.load(ROOT,self.torch,self.triton,self.tl)
    def test_actual_wrappers_match_all_frozen_launches(self):
        for card in contract.CARDS:
            self.torch.card=card
            for t in contract.TOKENS:
                with patch.object(compiler_gate,'verify'),ops.LaunchAudit(self.torch,self.modules,t,card) as audit:
                    for arm in contract.ARMS:ops.execute(self.torch,self.modules,f.layer(t,card),arm)
                self.assertEqual(audit.counts,dict(norm=1,candidate=1,quantizer=1,gemm=2))
    def test_helper_count_threshold_is_not_assumed(self):
        norm=self.modules['norm'];self.assertEqual(norm.calc_rows_per_block(192,f.Device()),4)
        norm._get_sm_count.cache_clear();self.torch.core_count=64
        self.assertEqual(norm.calc_rows_per_block(192,f.Device()),2)
        self.assertEqual(norm.calc_rows_per_block(384,f.Device()),4)
    def test_compile_only_does_not_launch(self):
        with patch.object(compiler_gate,'verify'),ops.LaunchAudit(self.torch,self.modules,8,0,launch=False) as audit:
            for a in contract.ARMS:ops.execute(self.torch,self.modules,f.layer(),a)
        for kernel in audit.saved.values():
            if isinstance(kernel,f.Heuristics):kernel=kernel.fn
            self.assertEqual(kernel.calls,[])
    def test_wrong_compiler_fails_before_launch(self):
        with patch.object(compiler_gate,'verify',side_effect=ValueError('wrong binary')):
            with self.assertRaisesRegex(ValueError,'wrong binary'),ops.LaunchAudit(self.torch,self.modules,8,0) as audit:
                ops.execute(self.torch,self.modules,f.layer(),'C')
        self.assertEqual(audit.saved['candidate'].calls,[])
    def test_audited_object_must_be_launched(self):
        kernel=self.modules['candidate']._gated_norm_mxfp4_kernel
        old=kernel.run
        def bad(*args,**kw):return old(*args,**kw) if kw.get('warmup') else object()
        with patch.object(compiler_gate,'verify'),patch.object(kernel,'run',side_effect=bad):
            with self.assertRaisesRegex(ValueError,'launched object'),ops.LaunchAudit(self.torch,self.modules,8,0):
                ops.execute(self.torch,self.modules,f.layer(),'C')
    def test_geometry_scalar_dtype_stride_device_refusals(self):
        for stage in contract.STAGES:
            want=contract.LAUNCHES['8'][stage]
            args=[f.Tensor(v['shape'],v['dtype'],f.Device(),v['stride']) if isinstance(v,dict) else v for v in want['args']]
            ops.check_launch(args,want['kwargs'],want['grid'],8,stage,0)
            variants=[]
            for attr,new in (('dtype','float32'),('device',f.Device(1)),('_stride',(999,1)),('shape',(1,))):
                bad=copy.deepcopy(args);setattr(bad[0],attr,new);variants.append((bad,want['kwargs'],want['grid']))
            variants.append((args,dict(want['kwargs'],num_warps=True),want['grid']))
            variants.append((args,want['kwargs'],[999]))
            for a,k,g in variants:
                with self.subTest(stage=stage),self.assertRaises(ValueError):ops.check_launch(a,k,g,8,stage,0)
    def test_poison_every_stage_destination(self):
        for a in contract.ARMS:
            for p in (0,1):
                with patch.object(compiler_gate,'verify'),ops.LaunchAudit(self.torch,self.modules,8,0,poison_index=p):
                    out=ops.execute(self.torch,self.modules,f.layer(),a)
                self.assertTrue(all(v.poison is not None for v in out.values()))
    def test_original_modules_and_linecache_restored_on_exception(self):
        old_modules={k:sys.modules[k] for k in source_loader.ModuleScope.NAMES}
        old_lines={k:linecache.cache[k] for k in source_loader.ModuleScope.PATHS}
        with self.assertRaises(KeyboardInterrupt):
            with source_loader.ModuleScope():
                source_loader.load(ROOT,self.torch,self.triton,self.tl)
                raise KeyboardInterrupt()
        self.assertTrue(all(sys.modules[k] is v for k,v in old_modules.items()))
        self.assertTrue(all(linecache.cache[k] is v for k,v in old_lines.items()))

class GateTests(unittest.TestCase):
    def test_repeat_parity_and_finite_veto(self):
        case=contract.CASES[0];row=f.case_result(case);self.assertTrue(ops.numerical_pass(row,case))
        for arm,key in (('B1','normalized'),('C0','packed'),('C1','scales'),('C0','output'),('C0','rstd')):
            bad=copy.deepcopy(row);bad['outputs'][arm][key]['sha256']='0'*64
            self.assertFalse(ops.numerical_pass(bad,case))
        for values in row['outputs'].values():values['output']['finite']=False
        self.assertFalse(ops.numerical_pass(row,case))
    def test_exceptional_equality_is_still_exact(self):
        case=contract.CASES[4];row=f.case_result(case)
        for values in row['outputs'].values():values['output']['finite']=False
        self.assertTrue(ops.numerical_pass(row,case))
        row['outputs']['C0']['output']['sha256']='0'*64
        self.assertFalse(ops.numerical_pass(row,case))
    def test_graph_must_change_and_keep_storage_separate(self):
        row=f.graph_result(8);self.assertTrue(producer.graph_pass(row))
        for mutation in ('stale','alias','different'):
            bad=copy.deepcopy(row)
            if mutation=='stale':bad['changed']=copy.deepcopy(bad['initial'])
            elif mutation=='alias':bad['independent_storage']=False
            else:bad['changed']['C']['packed']['sha256']='0'*64
            self.assertFalse(producer.graph_pass(bad))
    def test_frozen_noise_and_cumulative_rule(self):
        rows=[v for c in contract.TIMED for v in f.timings(c)]
        self.assertTrue(contract.usefulness(rows)['useful'])
        for v in rows:
            if v['arm']=='C':v['elapsed_ms']=9.9
        self.assertEqual(contract.usefulness(rows)['cumulative'],'RETAINED_POSITIVE')
        rows[-1]['elapsed_ms']=11
        self.assertFalse(contract.usefulness(rows)['useful'])
        with self.assertRaises(ValueError):contract.usefulness(rows[::-1])
        rows[-1]['elapsed_ms']=float('nan')
        with self.assertRaises(ValueError):contract.usefulness(rows)
    def test_frozen_memory_limit(self):
        cuda=f.NS(max_memory_allocated=lambda c:contract.DEVICE_LIMIT+1,max_memory_reserved=lambda c:contract.DEVICE_LIMIT+1)
        with self.assertRaisesRegex(ValueError,'memory veto'):producer.peak(f.NS(cuda=cuda),0)

class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name).resolve()/'evidence'
        art=s.Artifacts(cls.root,time.monotonic()+120)
        cls.value=f.payload(art,producer.source_identity());cls.raw=s.canonical(cls.value)
        art.write(s.REPORT,cls.raw);cls.blobs={v['path']:(cls.root/v['path']).read_bytes() for v in cls.value['artifacts']}
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def accepted(self,value=None,blobs=None):
        with patch.object(compiler_gate,'replay'):
            return reader.validate(value or self.value,blobs or self.blobs,allow_fixture=True)
    def test_report_and_aggregate_fit(self):
        self.assertLess(len(self.raw),s.RESULT_LIMIT)
        self.assertLess(len(self.raw)+sum(map(len,self.blobs.values())),s.COMPONENT_LIMIT)
        self.assertEqual(self.accepted()['status'],'CPU_FIXTURE_VALIDATED')
    def test_fixture_blocked_live_and_fake_compiler_blocked_even_fixture(self):
        with self.assertRaisesRegex(ValueError,'fixture'):reader.validate(self.value,self.blobs)
        with self.assertRaisesRegex(ValueError,'compiler artifact'):reader.validate(self.value,self.blobs,allow_fixture=True)
    def test_reader_mutations(self):
        def mutations(v):
            yield lambda:v['cards'][0].update(device_core_count=64)
            yield lambda:v['cards'][1].update(started_monotonic=101)
            yield lambda:v['cards'][0]['cleanup'].update(allocated_after=1)
            yield lambda:v['cards'][0]['peak'].update(reserved=contract.DEVICE_LIMIT+1)
            yield lambda:v['cards'][0]['cases'][0]['outputs']['C0']['packed'].update(sha256='0'*64)
            yield lambda:v['cards'][0]['timing_layers'].pop()
            yield lambda:v['cards'][0]['timing_checks'][0]['post_timing_audit'].update(candidate=0)
            yield lambda:v['cards'][0]['timing_checks'][0].update(projected_seconds=1000)
            yield lambda:v['cards'][0]['timing_checks'][0]['outputs']['C'][0]['packed'].update(sha256='0'*64)
            yield lambda:v['cards'][0]['timings'].reverse()
            yield lambda:v.update(cumulative_disposition='REJECT')
        for index in range(11):
            bad=copy.deepcopy(self.value);list(mutations(bad))[index]()
            with self.subTest(index=index),self.assertRaises(ValueError):self.accepted(bad)
    def test_exact_artifact_membership(self):
        with patch.object(compiler_gate,'replay'):
            self.assertTrue(reader.read_component(self.root,allow_fixture=True)['complete'])
            extra=self.root/'unexpected';extra.write_bytes(b'x')
            try:
                with self.assertRaisesRegex(ValueError,'membership'):reader.read_component(self.root,allow_fixture=True)
            finally:extra.unlink()
    def test_incomplete_never_becomes_complete(self):
        bad=copy.deepcopy(self.value);bad.update(complete=False,status='INCOMPLETE',failure=dict(message='injected timeout'))
        self.assertFalse(self.accepted(bad)['complete'])

class ProducerTests(unittest.TestCase):
    def test_foreign_capture_stream_refuses_before_work(self):
        torch=f.NS(cuda=f.NS(Stream=lambda **kw:f.NS(device=f.Device(0))))
        with patch.object(ops,'execute') as execute:
            with self.assertRaisesRegex(ValueError,'capture stream card'):ops.capture(torch,{},[],'C',True,1,time.monotonic()+5)
            execute.assert_not_called()
    def exercise(self,fail_at=None,count=32):
        events=[];row=dict(card=0,device_core_count=count,cases=[],graphs=[],timing_layers=[],timings=[],timing_checks=[],timing_started=False)
        modules={'norm':f.NS(calc_rows_per_block=lambda *a:4)};torch=f.Torch()
        def check(torch,modules,t,case,card,deadline):
            events.append(case['name']);value=f.case_result(case)
            if case['name']==fail_at:value['status']='INCORRECT'
            return value
        def timed(torch,modules,layers,tokens,mode,card,deadline):
            self.assertEqual(len(row['timing_layers']),96)
            self.assertEqual(len(layers),48);events.append('TIMING')
            return f.timings(f't{tokens}-{mode}'),f.timing_check(f't{tokens}-{mode}')
        def artifact(name,raw):events.append('GATE');return dict(path=name,bytes=len(raw),sha256=s.sha(raw))
        with ExitStack() as stack:
            stack.enter_context(patch.object(ops,'layer',side_effect=lambda torch,case,card,**kw:f.layer(case['tokens'],card)))
            stack.enter_context(patch.object(producer,'compile_pair',return_value={}))
            stack.enter_context(patch.object(ops,'check_case',side_effect=check))
            stack.enter_context(patch.object(producer,'graph_check',side_effect=lambda torch,modules,t,*a:f.graph_result(t['tokens'])))
            stack.enter_context(patch.object(producer,'timed_case',side_effect=timed))
            stack.enter_context(patch.object(producer,'peak',return_value={}))
            producer.work_card(torch,modules,row,f.NS(write=artifact),time.monotonic()+120)
        return row,events
    def test_every_numerical_gate_precedes_first_timing(self):
        row,events=self.exercise()
        self.assertEqual(events.count('TIMING'),4)
        self.assertEqual(events.index('GATE'),len(contract.CASES)+96)
        self.assertEqual(events[events.index('GATE')+1],'TIMING')
        self.assertEqual(row['candidate_status'],'CORRECT')
    def test_first_numerical_failure_prevents_timing(self):
        for name in (contract.CASES[0]['name'],'t8-timing-layer0','t16-timing-layer47'):
            row,events=self.exercise(name)
            self.assertEqual(row['candidate_status'],'INCORRECT');self.assertNotIn('TIMING',events)
            self.assertEqual(events[-2:], [name,'GATE'])
    def test_device_count_refuses_before_allocation(self):
        with patch.object(ops,'layer') as allocate:
            with self.assertRaisesRegex(ValueError,'count32'):self.exercise(count=64)
            allocate.assert_not_called()
    def test_cleanup_runs_after_work_and_clears_failed_frame(self):
        events=[];refs=[]
        class Holder:pass
        def work(*args):
            value=Holder();refs.append(weakref.ref(value));events.append('work');raise ValueError('primary failure')
        cuda=f.NS(set_device=lambda c:None,memory_allocated=lambda c:0,reset_peak_memory_stats=lambda c:None,
                  synchronize=lambda c:events.append('sync'),empty_cache=lambda:events.append('empty'))
        torch=f.NS(cuda=cuda,_C=f.NS(_cuda_clearCublasWorkspaces=lambda:events.append('clear')))
        row={'card':0}
        with patch.object(producer,'work_card',side_effect=work),patch.object(producer,'peak',return_value={}):
            with self.assertRaisesRegex(ValueError,'primary failure'):producer.run_card(torch,{},row,None,time.monotonic()+10)
        self.assertEqual(events,['work','sync','clear','empty']);self.assertIsNone(refs[0]())
        self.assertTrue(row['cleanup']['complete'])
    def test_cleanup_noop_cannot_release(self):
        allocations=iter((0,83886080));cuda=f.NS(set_device=lambda c:None,memory_allocated=lambda c:next(allocations),
          reset_peak_memory_stats=lambda c:None,synchronize=lambda c:None,empty_cache=lambda:None)
        torch=f.NS(cuda=cuda,_C=f.NS(_cuda_clearCublasWorkspaces=lambda:None));row={'card':0}
        with patch.object(producer,'work_card'),patch.object(producer,'peak',return_value={}):
            with self.assertRaisesRegex(ValueError,'allocation did not return'):producer.run_card(torch,{},row,None,time.monotonic()+10)
        self.assertFalse(row['cleanup']['complete'])

class SourceIdentityTests(unittest.TestCase):
    def test_unrepinned_source_refuses(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()
            for name in (*producer.RUNTIME_NAMES,'RUNTIME-SOURCE-PINS.json'):shutil.copyfile(ROOT/name,path/name)
            with patch.object(producer,'ROOT',path):
                producer.source_identity()
                with (path/'corpus.py').open('a') as stream:stream.write('\n# changed\n')
                with self.assertRaisesRegex(ValueError,'source identity'):producer.source_identity()

if __name__=='__main__':unittest.main(verbosity=2)
