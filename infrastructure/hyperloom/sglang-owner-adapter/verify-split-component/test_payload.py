"""The real two-stage producer and offline reader with explicit CPU fixtures."""
import copy
from array import array
import json
import math
from pathlib import Path
import tempfile
import unittest
import sys
import types

sys.path.insert(0,str(Path(__file__).resolve().parent))

import attention_ops as ops
import contract
import fake_payload
import read_component
import stage_probe


class MetadataVector:
    """Small stdlib storage adapter for the real indices() path, not a GPU oracle."""
    def __init__(self,values,lo=0,hi=None):
        self.values=values if isinstance(values,array) else array('q',values)
        self.lo=lo;self.hi=len(self.values) if hi is None else hi
    def __getitem__(self,key):
        start,stop,step=key.indices(self.hi-self.lo)
        if step==1:return MetadataVector(self.values,self.lo+start,self.lo+stop)
        return MetadataVector(self.values[self.lo+start:self.lo+stop:step])
    def tolist(self):return self.values[self.lo:self.hi].tolist()
    def copy_(self,other):
        values=array('q',other.tolist())
        if len(values)!=self.hi-self.lo:raise ValueError('fixture copy length')
        self.values[self.lo:self.hi]=values


class MetadataTorch:
    int32,int64,float32='int32','int64','float32'
    def __init__(self,split=None):
        self.split=split;self.calls=[];self.allocations=0
        self.cuda=types.SimpleNamespace(synchronize=lambda card:None)
        outer=self
        class Split:
            def __getitem__(self,grid):
                def launch(dst,lengths,*args,**kwargs):
                    outer.calls.append((grid,lengths.tolist(),args,kwargs))
                    dst.copy_(MetadataVector([args[-2] if outer.split is None else outer.split]))
                return launch
        self.modules=(types.SimpleNamespace(_split_helper=Split()),)
    def tensor(self,values,**kwargs):
        self.allocations+=1;return MetadataVector(values)
    def arange(self,start,end,**kwargs):
        self.allocations+=1;return MetadataVector(range(start,end))
    def full(self,shape,value,**kwargs):
        self.allocations+=1
        if len(shape)>1:return types.SimpleNamespace(shape=shape,fill_value=value)
        return MetadataVector(array('q',[value])*shape[0])
    def empty(self,shape,**kwargs):return self.full(shape,0,**kwargs)
    def cat(self,values):
        return MetadataVector(v for tensor in values for v in tensor.tolist())
    def equal(self,a,b):return a.tolist()==b.tolist()


class Payload(unittest.TestCase):
    def build(self,variant=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        root=Path(tmp.name).resolve()/'output';value,cuda=fake_payload.run(root,variant)
        return root,value,cuda

    def test_two_cards_all_cases_both_stages_and_reader(self):
        root,value,cuda=self.build()
        self.assertTrue(value['complete'],value.get('failure'))
        result=read_component.read_component(root,allow_fixture=True)
        self.assertEqual(result['useful'],['VERIFY-SPLIT32']);self.assertEqual(result['cards'],2)
        self.assertTrue(all(len(r['cases'])==4 and len(r['timings'])==48 for r in value['cards']))
        self.assertTrue(any(n=='_fwd_kernel_stage2' for n,a,c in cuda.launches))
        self.assertEqual([r['device_core_count'] for r in value['cards']],[32,32])
        self.assertLessEqual(value['cards'][0]['released_monotonic'],value['cards'][1]['started_monotonic'])
        with self.assertRaises(ValueError):read_component.read_component(root)

    def test_resource_and_same_code_rejections_precede_target_launches(self):
        for variant,status in [('same-code','NO_DISTINCT_REDUCTION'),('spill','RESOURCE_REGRESSION')]:
            root,value,cuda=self.build(variant)
            self.assertTrue(value['complete'],value.get('failure'))
            result=read_component.read_component(root,allow_fixture=True)
            self.assertEqual(result['candidate_disposition'],status)
            self.assertEqual(cuda.launches,[])

    def test_wrong_binary_refuses_before_target_launch(self):
        root,value,cuda=self.build('pin-mismatch')
        self.assertFalse(value['complete']);self.assertEqual(cuda.launches,[])
        self.assertIn('compiler identity mismatch',value['failure']['message'])
        self.assertTrue(value['cards'][0]['cleanup']['complete'])

    def test_every_layer_gets_reference_and_last_layer_failure_stops_timing(self):
        root,value,cuda=self.build()
        for card in (0,1):
            for case in contract.CASES:
                self.assertEqual([layer for c,name,layer in cuda.reference_layers if c==card and name==case['name']],list(range(16)))
        root,value,cuda=self.build('last-layer')
        self.assertTrue(value['complete'],value.get('failure'))
        self.assertEqual(read_component.read_component(root,allow_fixture=True)['candidate_disposition'],'INCORRECT')
        self.assertEqual(len(value['cards']),1);row=value['cards'][0]
        self.assertEqual(row['cases'][0]['layers'][-1]['layer'],15)
        self.assertFalse(row['cases'][0]['layers'][-1]['passes'])
        self.assertEqual(row['timings'],[])

    def test_bitwise_difference_is_report_only_and_mean_band_is_fixed(self):
        self.assertEqual(contract.MEAN_ERROR_FACTOR,1.02)
        root,value,cuda=self.build('bitwise')
        self.assertTrue(value['complete'],value.get('failure'))
        self.assertEqual(read_component.read_component(root,allow_fixture=True)['candidate_disposition'],'USEFUL')
        self.assertEqual(value['cards'][1]['cases'][0]['equal_layers'],0)
        root,value,cuda=self.build('mean-band')
        row=value['cards'][1]['cases'][0]['layers'][0]
        self.assertLess(row['mean_error_margin'],0)
        self.assertEqual(value['cards'][1]['timings'],[])

    def test_reader_refuses_missing_reordered_and_changed_layer_witnesses(self):
        root,value,cuda=self.build();original=value['cards'][0]['cases'][0]
        for change in ('missing','order','pass','margin','mean'):
            row=copy.deepcopy(original)
            if change=='missing':row['layers'].pop()
            elif change=='order':row['layers'].reverse()
            elif change=='pass':row['layers'][-1]['passes']=False
            elif change=='margin':row['layers'][-1]['mean_error_margin']=1.0
            else:row['layers'][-1]['candidate']['mean_abs']*=2
            with self.subTest(change=change),self.assertRaises(ValueError):read_component.classify(row,32)

    def test_empty_partial_corruption_forbids_timing(self):
        root,value,cuda=self.build('empty-clobber')
        self.assertTrue(value['complete'],value.get('failure'))
        self.assertEqual(read_component.read_component(root,allow_fixture=True)['candidate_disposition'],'INCORRECT')
        self.assertEqual(len(value['cards']),1);self.assertEqual(value['cards'][0]['timings'],[])

    def test_helper_and_both_stages_are_inside_each_timing_interval(self):
        root,value,cuda=self.build()
        self.assertTrue(value['complete'],value.get('failure'))
        events=cuda.events;marks=[i for i,e in enumerate(events) if e[0]=='event']
        self.assertEqual(len(marks)%2,0)
        for start,end in zip(marks[::2],marks[1::2]):
            inside=[name for name,card in events[start+1:end]]
            self.assertIn(inside.count('helper'),(8,16))
            self.assertEqual(inside.count('stage1'),16);self.assertEqual(inside.count('stage2'),16)
            self.assertLess(max(i for i,x in enumerate(inside) if x=='helper'),inside.index('stage1'))

    def test_oracle_bitwise_and_nonfinite_rejections_precede_timing(self):
        for variant in ('bad-numerical','mean-band','nonfinite'):
            root,value,cuda=self.build(variant)
            self.assertTrue(value['complete'],value.get('failure'))
            result=read_component.read_component(root,allow_fixture=True)
            self.assertEqual(result['candidate_disposition'],'INCORRECT')
            self.assertEqual(value['cards'][1]['timings'],[])
            self.assertTrue(value['cards'][1]['cleanup']['complete'])

    def test_short_regression_and_null_gain_are_not_useful(self):
        for variant in ('short-regression','null-gain'):
            root,value,cuda=self.build(variant)
            self.assertTrue(value['complete'],value.get('failure'))
            result=read_component.read_component(root,allow_fixture=True)
            self.assertEqual(result['candidate_disposition'],'NOT_USEFUL')
            if variant=='short-regression':self.assertFalse(value['cards'][0]['usefulness']['no_regression'])

    def test_projection_and_split_failure_park_with_cleanup(self):
        for variant in ('projection','split-mismatch'):
            root,value,cuda=self.build(variant)
            self.assertFalse(value['complete']);self.assertEqual(len(value['cards']),1)
            self.assertFalse(value['cards'][0]['timing_started'])
            self.assertTrue(value['cards'][0]['cleanup']['complete'])
            self.assertFalse(read_component.read_component(root,allow_fixture=True,require_complete=False)['complete'])

    def test_workspace_and_leak_failures_stop_next_card(self):
        for variant in ('leak','clear-noop','clear-fails','clear-absent'):
            root,value,cuda=self.build(variant)
            self.assertFalse(value['complete']);self.assertEqual(len(value['cards']),1)
            self.assertFalse(read_component.read_component(root,allow_fixture=True,require_complete=False)['complete'])

    def test_cleanup_follows_last_timing_event(self):
        root,value,cuda=self.build()
        self.assertTrue(value['complete'],value.get('failure'))
        for card in (0,1):
            rows=[x for x in cuda.events if x[1]==card]
            clear=rows.index(('clear',card))
            self.assertGreater(clear,max(i for i,x in enumerate(rows) if x[0]=='event_sync'))
            self.assertGreater(rows.index(('empty_cache',card)),clear)
        self.assertEqual(cuda.memory_allocated(0),0);self.assertEqual(cuda.memory_allocated(1),0)

    def test_fixture_cannot_be_relabeled_as_gpu_result(self):
        root,value,cuda=self.build();del value['fixture_scope']
        blobs={r['path']:(root/r['path']).read_bytes() for r in value['artifacts']}
        with self.assertRaisesRegex(ValueError,'compiler identity'):read_component.validate(value,blobs)

    def test_reader_recomputes_metadata_timing_and_final_verdict(self):
        root,value,cuda=self.build();blobs={r['path']:(root/r['path']).read_bytes() for r in value['artifacts']}
        for variant in ('metadata','timing-order','useful','missing-card','missing-case','overlap',
                        'device-core-count','metadata-core-count','core-count-type'):
            v=copy.deepcopy(value)
            if variant=='metadata':v['cards'][0]['cases'][0]['metadata']['C']['splits'][0]=8
            elif variant=='timing-order':v['cards'][0]['timings'].reverse()
            elif variant=='useful':v['useful']=False
            elif variant=='missing-card':v['cards'].pop()
            elif variant=='missing-case':v['cards'][0]['cases'].pop()
            elif variant=='overlap':v['cards'][1]['started_monotonic']=v['cards'][0]['started_monotonic']
            elif variant=='device-core-count':v['cards'][0]['device_core_count']=64
            elif variant=='metadata-core-count':v['cards'][0]['cases'][0]['metadata']['C']['device_core_count']=64
            else:v['cards'][0]['device_core_count']=True
            with self.subTest(variant=variant),self.assertRaises((ValueError,KeyError)):
                read_component.validate(v,blobs,allow_fixture=True)

    def test_artifact_tamper_and_extra_file_refuse(self):
        root,value,cuda=self.build();p=root/value['artifacts'][0]['path'];p.write_bytes(p.read_bytes()+b'x')
        with self.assertRaises(ValueError):read_component.read_component(root,allow_fixture=True)
        root,value,cuda=self.build();(root/'extra').write_text('x')
        with self.assertRaises(ValueError):read_component.read_component(root,allow_fixture=True)

    def test_launch_metadata_and_scalar_mutations_refuse(self):
        expected=json.loads((ops.ROOT/'LAUNCHES.json').read_bytes())[contract.CASES[0]['name']]['B']['stage1']
        args=[fake_payload.Tensor(v['shape'],v['dtype']) if isinstance(v,dict) else v for v in expected['args']]
        ops.check_launch(args,expected['kwargs'],expected['grid'],expected,0)
        for variant in ('scale','bool','grid','dtype','device','constant'):
            values=list(args);kw=dict(expected['kwargs']);grid=list(expected['grid'])
            if variant=='scale':values[3]=1.0
            elif variant=='bool':values[3]=True
            elif variant=='grid':grid[0]+=1
            elif variant=='constant':kw['BLOCK_N']=64
            elif variant=='dtype':values[5]=fake_payload.Tensor(expected['args'][5]['shape'],'int32')
            else:values[0]=fake_payload.Tensor(expected['args'][0]['shape'],'bfloat16',1)
            with self.subTest(variant=variant),self.assertRaises(ValueError):ops.check_launch(values,kw,grid,expected,0)

    def test_short_case_has_four_empty_splits(self):
        short=ops.metadata_record(contract.CASES[-1],'B',32)
        self.assertEqual(short['empty_splits'],[4]*8)
        self.assertEqual(short['lengths'],list(range(725,733)))

    def test_real_metadata_path_passes_runtime_count_without_cu_conversion(self):
        for count in (32,64):
            for card in (0,1):
                for case in contract.CASES:
                    torch=MetadataTorch()
                    result=ops.indices(torch,torch.modules,case,card,count,'B')
                    self.assertEqual(result['witness'],ops.metadata_record(case,'B',count))
                    self.assertEqual(len(torch.calls),case['queries'])
                    for i,(grid,lengths,args,kwargs) in enumerate(torch.calls):
                        self.assertEqual(grid,(1,))
                        self.assertEqual(lengths,[result['witness']['lengths'][i]])
                        self.assertEqual(args,(1,1,12,2,16,count))
                        self.assertEqual(kwargs,dict(MAX_NUM_SEQ=256))

    def test_real_metadata_rejects_invalid_count_before_any_tensor_allocation(self):
        for count in (0,-1,True,None,'32',32.0,1<<31):
            torch=MetadataTorch()
            with self.subTest(count=count),self.assertRaisesRegex(ValueError,'observed='):
                ops.indices(torch,torch.modules,contract.CASES[0],0,count,'B')
            self.assertEqual(torch.allocations,0)
            self.assertEqual(torch.calls,[])

    def test_cap32_uses_one_cap_for_helper_buffers_and_call(self):
        for case in contract.CASES:
            torch=MetadataTorch();b=ops.indices(torch,torch.modules,case,0,32,'B')
            c=ops.indices(torch,torch.modules,case,0,32,'C')
            self.assertIsNot(b['partial'],c['partial']);self.assertIsNot(b['lse'],c['lse'])
            self.assertTrue(all(math.isnan(idx[key].fill_value) for idx in (b,c) for key in ('partial','lse')))
            self.assertEqual(c['partial'].shape,(case['queries'],12,32,256));self.assertEqual(c['lse'].shape,(case['queries'],12,32))
            self.assertEqual(c['witness'],ops.metadata_record(case,'C',32))
            self.assertTrue(all(call[2][-2]==32 for call in torch.calls[case['queries']:]))
            calls=[];t={k:fake_payload.Tensor(shape) for k,shape in {'q':(16,12,256),'k_buf':(1,2,256),'v_buf':(1,2,256),'o':(16,12,256)}.items()}
            ops.served_call(lambda *args,**kwargs:calls.append((args,kwargs)),t,c,t['o'])
            self.assertEqual(calls[0][0][9],32)
            another=ops.indices(torch,torch.modules,case,0,32,'C')
            self.assertIsNot(another['partial'],c['partial']);self.assertIsNot(another['lse'],c['lse'])

    def test_empty_check_reads_only_undefined_slots_and_detects_corruption(self):
        witness=ops.metadata_record(contract.CASES[-1],'C',32);seen=[]
        class Partial:
            def __init__(self,poison=None):self.poison=poison
            def __getitem__(self,key):
                seen.append((key[0],key[2]));return (key[0],key[2])!=self.poison
        torch=types.SimpleNamespace(isnan=lambda value:types.SimpleNamespace(all=lambda:value))
        idx=dict(cap=32,witness=witness,partial=Partial(),lse=Partial())
        self.assertTrue(ops.empty_partials_untouched(torch,idx))
        self.assertTrue(all(slot*witness['spans'][row]>=witness['lengths'][row] for row,slot in seen))
        idx['partial']=Partial((0,31));self.assertFalse(ops.empty_partials_untouched(torch,idx))

    def test_real_metadata_failure_retains_observed_and_expected_split_values(self):
        torch=MetadataTorch(split=15)
        with self.assertRaisesRegex(ValueError,'split/length/indptr mismatch') as caught:
            ops.indices(torch,torch.modules,contract.CASES[0],0,32,'B')
        message=str(caught.exception)
        self.assertLess(len(message),2000)
        witness=json.loads(message.split(': ',1)[1])
        self.assertEqual(witness['device_core_count'],32)
        self.assertEqual(witness['observed']['splits'],[15]*8)
        self.assertEqual(witness['expected']['splits'],[16]*8)

    def test_producer_retains_invalid_count_and_cleanup_without_operand_allocation(self):
        root,value,cuda=self.build('invalid-device-count')
        self.assertFalse(value['complete'])
        self.assertEqual(len(value['cards']),1)
        self.assertEqual(value['cards'][0]['device_core_count'],0)
        self.assertNotIn('memory',value['cards'][0])
        self.assertEqual(cuda.launches,[])
        self.assertTrue(value['cards'][0]['cleanup']['complete'])
        self.assertIn('observed=0',value['failure']['message'])
        self.assertFalse(read_component.read_component(root,allow_fixture=True,require_complete=False)['complete'])


if __name__=='__main__':unittest.main(verbosity=2)
