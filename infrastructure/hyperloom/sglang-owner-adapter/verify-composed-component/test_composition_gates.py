"""Mutation coverage for four-arm resources, writes and cumulative disposition."""
import copy
import itertools
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attention_ops as ops
import compiler_gate
import contract
import fake_payload
import read_component
import stage_probe


class Grid:
    """A tiny Boolean reduction fixture; no framework or device allocation."""
    def __init__(self,shape,values):self.shape=tuple(shape);self.values=list(values)
    def all(self,dim):
        dim%=len(self.shape);shape=self.shape[:dim]+self.shape[dim+1:]
        strides=[math.prod(self.shape[i+1:]) for i in range(len(self.shape))];result=[]
        for point in itertools.product(*(range(n) for n in shape)):
            result.append(all(self.values[sum(v*s for v,s in zip(point[:dim]+(i,)+point[dim:],strides))] for i in range(self.shape[dim])))
        return Grid(shape,result)
    def tolist(self):
        rows,cols=self.shape
        return [self.values[r*cols:(r+1)*cols] for r in range(rows)]


class CompositionTests(unittest.TestCase):
    def run_payload(self,variant=None):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name).resolve()/'output'
        value,cuda=fake_payload.run(root,variant)
        return root,value,cuda

    def test_exact_resource_and_distinct_prefetch_mutations_stop_before_launch(self):
        for variant,status in [('sgpr','RESOURCE_REGRESSION'),('same-stage1','NO_DISTINCT_PREFETCH')]:
            root,value,cuda=self.run_payload(variant)
            self.assertTrue(value['complete'],value['failure']);self.assertEqual(cuda.launches,[])
            self.assertEqual(read_component.read_component(root,allow_fixture=True)['candidate_disposition'],status)
        root,value,cuda=self.run_payload();original=value['cards'][0]['compiler']
        for key,field,bad in [('P','sgpr_count',101),('C','vgpr_count',235),('B2','max_flat_workgroup_size',256),('S','wavefront_size',64)]:
            records=copy.deepcopy(original);records[key]['resources'][field]=bad
            self.assertEqual(compiler_gate.compare(records),'RESOURCE_REGRESSION')

    def test_bitwise_neutrality_at_both_caps_and_no_write_rejection(self):
        root,value,cuda=self.run_payload()
        self.assertTrue(all(row['prefetch_equal_layers']=={'16':16,'32':16} for c in value['cards'] for row in c['cases']))
        for variant in ('prefetch32-bits','candidate-no-write','active-partial-unwritten','lse-unwritten'):
            root,value,cuda=self.run_payload(variant)
            self.assertTrue(value['complete'],value['failure'])
            self.assertEqual(read_component.read_component(root,allow_fixture=True)['candidate_disposition'],'INCORRECT')
            self.assertEqual(value['cards'][0]['timings'],[])

    def test_real_write_witness_reduces_all_heads_and_columns(self):
        partial=Grid((2,2,3,2),[True]*24)
        # Masks for row0: finite slot0, NaN slot1, mixed slot2.
        finite=[False]*24;nan=[False]*24
        for h in range(2):
            for d in range(2):finite[(h*3)*2+d]=True;nan[(h*3+1)*2+d]=True
        finite[4]=True
        # Row1: finite slot1 and NaN slot2; slot0 is neither.
        for h in range(2):
            for d in range(2):finite[((2+h)*3+1)*2+d]=True;nan[((2+h)*3+2)*2+d]=True
        lse=Grid((2,2,3),[True]*12)
        masks={id(partial):(Grid(partial.shape,finite),Grid(partial.shape,nan)),
               id(lse):(Grid(lse.shape,[True,False,False]*2+[False,True,False]*2),Grid(lse.shape,[False,True,False]*2+[False,False,True]*2))}
        import types
        torch=types.SimpleNamespace(isfinite=lambda t:masks[id(t)][0],isnan=lambda t:masks[id(t)][1])
        self.assertEqual(ops.write_witness(torch,dict(partial=partial,lse=lse,queries=2,cap=3)),
            dict(partial_finite=[1,2],partial_nan=[2,4],lse_finite=[1,2],lse_nan=[2,4]))

    def test_exact_active_and_empty_slots_and_reader_mutations(self):
        for arm,empty in [('B',4),('P',4),('S',9),('C',9)]:
            metadata=ops.metadata_record(contract.CASES[-1],arm,32);expected=ops.expected_writes(metadata)
            self.assertEqual([v.bit_count() for v in expected['partial_nan']],[empty]*8)
            for active,unused in zip(expected['partial_finite'],expected['partial_nan']):
                self.assertEqual(active&unused,0);self.assertEqual(active|unused,(1<<contract.arm_cap(arm))-1)
        root,value,cuda=self.run_payload();original=value['cards'][0]['cases'][-1]
        row=copy.deepcopy(original);row['writes']['C'][-1]['partial_finite'][0]^=1
        self.assertEqual(read_component.classify(row,32),'INCORRECT')
        for variant in ('missing-layer','boolean-mask','missing-arm','bad-equality'):
            row=copy.deepcopy(original)
            if variant=='missing-layer':row['writes']['P'].pop()
            elif variant=='boolean-mask':row['writes']['C'][0]['partial_nan'][0]=True
            elif variant=='missing-arm':row['writes'].pop('S')
            else:row['prefetch_equal_layers']['32']=True
            with self.assertRaises(ValueError):read_component.classify(row,32)

    def test_mirrored_timing_balances_every_pair_order(self):
        for left,right in itertools.combinations(contract.ARMS,2):
            self.assertEqual(sum(contract.timing_order(r).index(left)<contract.timing_order(r).index(right) for r in range(6)),3)
        for bad in (True,-1,6,1.5):
            with self.assertRaises(ValueError):contract.timing_order(bad)

    def test_fixed_mirrored_warmups_precede_and_are_excluded_from_samples(self):
        calls=[]
        def indices(torch,modules,case,card,core,arm):
            return dict(case=case['name'],arm=arm)
        def timed(torch,module,layers,idx):
            calls.append((idx['case'],idx['arm']))
            return float(len(calls))
        with patch.object(ops,'indices',side_effect=indices),patch.object(ops,'timed',side_effect=timed):
            samples=stage_probe.timing(None,(object(),object()),[],0,stage_probe.time.monotonic()+300,32)
        self.assertEqual(contract.WARMUP_CALLS,6)
        expected=[];sample_positions=[]
        for name in contract.TIMED:
            for r in range(6):
                expected.extend((name,a) for a in contract.timing_order(r))
            for r in range(6):
                for arm in contract.timing_order(r):
                    expected.append((name,arm));sample_positions.append(float(len(expected)))
        self.assertEqual(calls,expected)
        self.assertEqual([s['elapsed_ms'] for s in samples],sample_positions)
        self.assertEqual(len(samples),len(contract.TIMED)*6*4)

    def test_projection_charges_every_fixed_warmup(self):
        rows=[dict(case=c,baseline_wall_seconds=1.0) for c in contract.TIMED]
        with patch.object(stage_probe.time,'monotonic',return_value=0.0):
            result=stage_probe.projection(rows,10000.0)
        expected=contract.CANDIDATE_COST_FACTOR*4*(6+6)*len(contract.TIMED)
        self.assertEqual(result['estimated_seconds'],expected)
        self.assertEqual(result['projected_seconds'],expected*contract.TIMING_HEADROOM)

    def test_cumulative_formula_and_incremental_regression_veto(self):
        for variant,expected in [(None,'USEFUL'),('null-gain','RETAINED_POSITIVE'),('short-regression','REJECT'),('harmful-split','REJECT')]:
            root,value,cuda=self.run_payload(variant)
            self.assertEqual(value['cumulative_disposition'],expected)
            self.assertEqual(read_component.read_component(root,allow_fixture=True)['cumulative_disposition'],expected)
        rows=copy.deepcopy(value['cards']);rows[0]['usefulness']['cumulative_disposition']='USEFUL'
        rows[1]['usefulness']['cumulative_disposition']='REJECT';self.assertEqual(contract.cumulative(rows),'REJECT')

    def test_missing_short_case_and_timing_mutations_refuse(self):
        root,value,cuda=self.run_payload();timings=value['cards'][0]['timings']
        for changed in ([t for t in timings if t['case']!='p724-rows8'],list(reversed(timings))):
            with self.assertRaises(ValueError):contract.usefulness(changed)
        changed=copy.deepcopy(timings);changed[-1]['elapsed_ms']=float('nan')
        with self.assertRaises(ValueError):contract.usefulness(changed)

    def test_four_arm_memory_and_power_contract_are_fixed(self):
        self.assertEqual(contract.POWER_CAP_MICROWATTS,235000000)
        self.assertEqual(contract.CARD_SECONDS,280);self.assertEqual(contract.WORKER_SECONDS,600)
        self.assertEqual(contract.CAPS,{'B':16,'P':16,'S':32,'C':32})
        self.assertLess(contract.RESIDENT_BYTES+contract.REFERENCE_WORKSPACE_BYTES+(80<<20)+contract.ALLOCATION_SLACK,contract.DEVICE_LIMIT)


if __name__=='__main__':unittest.main(verbosity=2)
