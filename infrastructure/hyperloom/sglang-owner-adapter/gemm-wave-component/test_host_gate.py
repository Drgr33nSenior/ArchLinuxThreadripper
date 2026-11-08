"""Framework-free tests of real host bodies and retained compiled artifacts."""
import ast
import copy
import json
import importlib.util
import linecache
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import dispatch
import host_loader
import launch_gate as gate


class Tensor:
    def __init__(self, shape, dtype, stride, card, pointer):
        self.shape, self.dtype, self._stride = tuple(shape), dtype, tuple(stride)
        self.device = types.SimpleNamespace(type='cuda', index=card)
        self.pointer = pointer
        self.offset = 0
        self.extent = shape[0] * shape[1] * (2 if dtype == 'bfloat16' else 1)
    def stride(self, dim=None):
        return self._stride if dim is None else self._stride[dim]
    def untyped_storage(self):
        return types.SimpleNamespace(data_ptr=lambda: self.pointer, nbytes=lambda: self.extent)
    def storage_offset(self):
        return self.offset


def tensors(m, n, k, card):
    specs = [((m,k//2),'uint8',(k//2,1)), ((n,k//2),'uint8',(k//2,1)),
             ((m,k//32),'uint8',(1,m)), ((n,k//32),'uint8',(k//32,1)),
             ((m,n),'bfloat16',(n,1))]
    return [Tensor(*spec, card, (i+1)*256) for i, spec in enumerate(specs)]


def record(args, kwargs, grid):
    return dict(args=[dict(shape=list(x.shape),stride=list(x.stride()),dtype=x.dtype)
                      if isinstance(x,Tensor) else x for x in args],kwargs=kwargs,grid=list(grid))


class Kernel:
    def __init__(self, compiled=None):
        self.compiled = compiled
        self.records, self.warmups = [], []
        self.foreign = False
    def run(self, *args, grid, warmup, **kwargs):
        assert warmup is True
        self.warmups.append(record(args,kwargs,grid))
        return self.compiled
    def __getitem__(self, grid):
        def call(*args, **kwargs):
            value=record(args,kwargs,grid);self.records.append(value)
            return object() if self.foreign else self.compiled if self.compiled is not None else value
        return call


def runtime(kernel, card):
    def get(expected):
        if type(expected) is not int or expected != card:
            raise ValueError('current declared card')
        return (types.SimpleNamespace(uint8='uint8',bfloat16='bfloat16'),
                types.SimpleNamespace(cdiv=lambda a,b:(a+b-1)//b),
                types.SimpleNamespace(_gemm_afp4wfp4_kernel=kernel))
    return get


def compiled(case, arm):
    _, pins=gate.load_contract()
    return types.SimpleNamespace(
        asm={p:(ROOT/'test_artifacts'/(case+'-'+arm+'.'+p)).read_bytes() for p in gate.PARTS},
        metadata=types.SimpleNamespace(**pins[case][arm]['metadata']))


class HostGate(unittest.TestCase):
    def test_exact_source_derivation_and_unchanged_quantizer(self):
        b=(ROOT/'ops_baseline.py').read_bytes();c=(ROOT/'ops_candidate.py').read_bytes()
        n=(ROOT/'ops_negative_control.py').read_bytes()
        dispatch.validate(b,c,n)
        tree=lambda raw:{x.name:ast.dump(x) for x in ast.parse(raw).body if isinstance(x,ast.FunctionDef)}
        tb,tc=tree(b),tree(c)
        self.assertEqual({k:v for k,v in tb.items() if k!='gemm_preallocated'},
                         {k:v for k,v in tc.items() if k!='gemm_preallocated'})
        self.assertRaises(ValueError,dispatch.validate,b,c+b'\n',n)

    def test_all_declared_rows_families_cards(self):
        shapes=((17408,5120),(5120,8704),(8192,5120),(7168,5120),(5120,3072),(48,5120),(8240,5120))
        count=0
        for card in (0,1):
            kernel=Kernel()
            with host_loader.HostScope(runtime(kernel,card)) as scope:
                for n,k in shapes:
                    for m in range(1,2049):
                        args=tensors(m,n,k,card)
                        b=scope.call('baseline',*args,card=card)
                        c=scope.call('candidate',*args,card=card)
                        expected=copy.deepcopy(b)
                        if m in (8,16) and (n,k)==(17408,5120):expected['kwargs']['waves_per_eu']=0
                        self.assertTrue(gate.equal(c,expected),(card,m,n,k));count+=1
                        kernel.records.clear()
        self.assertEqual(count,28672)

    def test_negative_control_separate_from_candidate(self):
        with host_loader.HostScope(runtime(Kernel(),0)) as scope:
            for m,n,k in ((16,5120,8704),(8,5120,8704),(8,17408,5120),(16,17408,5120)):
                b=scope.call('baseline',*tensors(m,n,k,0),card=0)
                c=scope.call('negative',*tensors(m,n,k,0),card=0)
                want=copy.deepcopy(b)
                if (m,n,k)==(16,5120,8704):want['kwargs']['waves_per_eu']=0
                self.assertTrue(gate.equal(c,want))

    def test_mixed_rows_do_not_mutate_global_config(self):
        kernel=Kernel()
        with host_loader.HostScope(runtime(kernel,1)) as scope:
            for m in (8,17,16,1,1024,8,2048,16,7,9):
                row=scope.call('candidate',*tensors(m,17408,5120,1),card=1)
                self.assertEqual(row['kwargs']['waves_per_eu'],0 if m in (8,16) else 1)
                self.assertEqual(scope.modules['candidate'].CONFIG['waves_per_eu'],1)

    def test_launches_match_three_case_compile_records(self):
        launches,_=gate.load_contract()
        for card in (0,1):
            with host_loader.HostScope(runtime(Kernel(),card)) as scope:
                for case,arms in launches.items():
                    call=arms['baseline'];m,n,pk=call['args'][5:8]
                    for arm,want in arms.items():
                        route='negative' if case=='down-m16' and arm=='candidate' else arm
                        actual=scope.call(route,*tensors(m,n,pk*2,card),card=card)
                        self.assertTrue(gate.equal(actual,want))

    def test_invalid_metadata_refuses_before_kernel(self):
        for change in ('alias','dtype','stride','device','rows','shape'):
            kernel=Kernel();args=tensors(8,17408,5120,0)
            if change=='alias':args[-1].pointer=args[0].pointer
            if change=='dtype':args[0].dtype='float16'
            if change=='stride':args[2]._stride=(160,1)
            if change=='device':args[1].device.index=1
            if change=='rows':args[0].shape=(True,2560)
            if change=='shape':args[1].shape=(17409,2560)
            with host_loader.HostScope(runtime(kernel,0)) as scope:
                self.assertRaises(ValueError,scope.call,'candidate',*args,card=0)
            self.assertEqual(kernel.records,[])

    def test_unsupported_dimensions_refuse(self):
        with host_loader.HostScope(runtime(Kernel(),0)) as scope:
            for m,n,k in ((0,17408,5120),(2049,17408,5120),(8,17408,5118)):
                self.assertRaises(ValueError,scope.call,'candidate',*tensors(m,n,k,0),card=0)

    def test_scope_restores_on_success_interrupt_and_partial_import(self):
        before_modules={k:v for k,v in sys.modules.items() if k.startswith(host_loader.PREFIX)}
        before_lines={k:v for k,v in linecache.cache.items() if '/gemm_wave_host/' in k}
        for mode in ('normal','interrupt','import_failure'):
            old=Path.read_bytes
            def read(path):
                if mode=='import_failure' and path.name=='ops_candidate.py':raise RuntimeError('forced partial load')
                return old(path)
            try:
                with mock.patch.object(Path,'read_bytes',read):
                    with host_loader.HostScope(runtime(Kernel(),0)):
                        if mode=='interrupt':raise KeyboardInterrupt('retained')
            except (RuntimeError,KeyboardInterrupt):pass
            self.assertEqual({k:v for k,v in sys.modules.items() if k.startswith(host_loader.PREFIX)},before_modules)
            self.assertEqual({k:v for k,v in linecache.cache.items() if '/gemm_wave_host/' in k},before_lines)

    def test_scope_requires_active_use_and_retains_existing_alias(self):
        sentinel=types.ModuleType(host_loader.PREFIX)
        with mock.patch.dict(sys.modules,{host_loader.PREFIX:sentinel}):
            scope=host_loader.HostScope(runtime(Kernel(),0))
            with scope:
                self.assertRaises(ValueError,scope.__enter__)
            self.assertIs(sys.modules[host_loader.PREFIX],sentinel)
            self.assertRaises(ValueError,scope.call,'candidate',*tensors(8,17408,5120,0),card=0)

    def test_process_wide_lease_refuses_overlapping_loader_copies(self):
        spec=importlib.util.spec_from_file_location('second_wave_loader',ROOT/'host_loader.py')
        second=importlib.util.module_from_spec(spec);spec.loader.exec_module(second)
        with host_loader.HostScope(runtime(Kernel(),0)):
            self.assertRaises(ValueError,second.HostScope(runtime(Kernel(),1)).__enter__)
            self.assertRaises(ValueError,host_loader.HostScope(runtime(Kernel(),1)).__enter__)
        self.assertNotIn(host_loader.LEASE,sys.modules)
        with second.HostScope(runtime(Kernel(),1)):pass
        self.assertNotIn(host_loader.LEASE,sys.modules)

    def test_source_manifest_drift_refuses_and_releases_lease(self):
        old=Path.read_bytes
        def read(p):return old(p)+b' ' if p.name=='SOURCE-PINS.json' else old(p)
        with mock.patch.object(Path,'read_bytes',read):
            self.assertRaises(ValueError,host_loader.HostScope(runtime(Kernel(),0)).__enter__)
        self.assertNotIn(host_loader.LEASE,sys.modules)


class BinaryGate(unittest.TestCase):
    def setUp(self):
        self.launches,self.pins=gate.load_contract()
    def args(self, case='gate_up-m16',arm='candidate',card=0):
        call=self.launches[case][arm];values=[]
        for i,v in enumerate(call['args']):
            values.append(Tensor(v['shape'],v['dtype'],v['stride'],card,(i+1)*256) if isinstance(v,dict) else v)
        return values,copy.deepcopy(call['kwargs']),tuple(call['grid'])
    def test_all_six_retained_compilers_pass(self):
        for case in gate.CASES:
            for arm in ('baseline','candidate'):
                self.assertEqual(gate.verify_compiled(compiled(case,arm),self.pins[case][arm]),self.pins[case][arm]['hashes'])
    def test_compile_only_never_launches(self):
        k=Kernel(compiled('gate_up-m16','candidate'));audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0)
        args,kw,grid=self.args();audit[grid](*args,**kw)
        self.assertEqual(len(k.warmups),1);self.assertEqual(k.records,[])
    def test_live_identity_checked_and_same_object_reused(self):
        k=Kernel(compiled('gate_up-m16','candidate'));audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True)
        args,kw,grid=self.args()
        self.assertIs(audit[grid](*args,**kw),k.compiled)
        self.assertIs(audit[grid](*args,**kw),k.compiled)
        self.assertEqual(audit.calls,2);self.assertEqual(len(k.records),2)
    def test_every_artifact_mutation_refuses_before_launch(self):
        for part in gate.PARTS:
            c=compiled('gate_up-m16','candidate');c.asm[part]+=b'\n'
            k=Kernel(c);audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True)
            args,kw,grid=self.args()
            self.assertRaises(ValueError,audit[grid],*args,**kw);self.assertEqual(k.records,[])
    def test_metadata_type_and_value_drift_refuses(self):
        for field,value in (('shared',4097),('num_warps',True),('waves_per_eu',False),('arch','gfx1100')):
            c=compiled('gate_up-m16','candidate');setattr(c.metadata,field,value)
            self.assertRaises(ValueError,gate.verify_compiled,c,self.pins['gate_up-m16']['candidate'])
    def test_foreign_launched_object_refuses(self):
        k=Kernel(compiled('gate_up-m16','candidate'));k.foreign=True
        audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True);args,kw,grid=self.args()
        self.assertRaises(ValueError,audit[grid],*args,**kw)
    def test_later_compile_replacement_refuses_before_second_launch(self):
        k=Kernel(compiled('gate_up-m16','candidate'));audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True)
        args,kw,grid=self.args();audit[grid](*args,**kw);k.compiled=compiled('gate_up-m16','candidate')
        self.assertRaises(ValueError,audit[grid],*args,**kw);self.assertEqual(len(k.records),1)
    def test_bad_launch_refuses_before_compilation(self):
        for mutation in ('grid','option','scalar','card','alias','dtype','stride','arity'):
            k=Kernel(compiled('gate_up-m16','candidate'));audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True)
            args,kw,grid=self.args()
            if mutation=='grid':grid=(True,)
            if mutation=='option':kw['waves_per_eu']=False
            if mutation=='scalar':args[5]=True
            if mutation=='card':args[0].device.index=True
            if mutation=='alias':args[1].pointer=args[0].pointer
            if mutation=='dtype':args[0].dtype='float32'
            if mutation=='stride':args[3]._stride=(160,1)
            if mutation=='arity':args.pop()
            self.assertRaises(ValueError,audit[grid],*args,**kw)
            self.assertEqual(k.warmups,[]);self.assertEqual(k.records,[])
    def test_binary_arm_swap_refuses(self):
        self.assertRaises(ValueError,gate.verify_compiled,compiled('gate_up-m16','baseline'),self.pins['gate_up-m16']['candidate'])
    def test_production_down_route_cannot_claim_negative_control(self):
        k=Kernel(compiled('down-m16','candidate'));audit=gate.AuditedKernel(k,'down-m16','candidate',0,launch=True)
        with host_loader.HostScope(runtime(audit,0)) as scope:
            self.assertRaises(ValueError,scope.call,'candidate',*tensors(16,5120,8704,0),card=0)
        self.assertEqual(k.warmups,[])
    def test_host_to_auditor_complete_chain_both_cards(self):
        for card in (0,1):
            for case in gate.CASES:
                for arm in ('baseline','candidate'):
                    k=Kernel(compiled(case,arm));audit=gate.AuditedKernel(k,case,arm,card,launch=True)
                    args=self.launches[case][arm]['args'];m,n,pk=args[5:8]
                    route='negative' if case=='down-m16' and arm=='candidate' else arm
                    with host_loader.HostScope(runtime(audit,card)) as scope:
                        scope.call(route,*tensors(m,n,pk*2,card),card=card)
                    self.assertEqual(audit.calls,1)
    def test_data_file_drift_refuses(self):
        old=Path.read_bytes
        def read(p):return old(p)+b' ' if p.name=='COMPILER-PINS.json' else old(p)
        with mock.patch.object(Path,'read_bytes',read):self.assertRaises(ValueError,gate.load_contract)
    def test_unknown_case_and_implicit_types_refuse(self):
        for case,arm,card,launch in (('gate_up-m7','candidate',0,False),('gate_up-m8','other',0,False),
                                    ('gate_up-m8','candidate',False,False),('gate_up-m8','candidate',0,1)):
            self.assertRaises(ValueError,gate.AuditedKernel,Kernel(),case,arm,card,launch=launch)

    def test_offset_or_oversized_storage_refuses_before_compile(self):
        for offset,extra,alignment in ((1,0,0),(0,1,0),(False,0,0),(0,0,1)):
            k=Kernel(compiled('gate_up-m16','candidate'));audit=gate.AuditedKernel(k,'gate_up-m16','candidate',0,launch=True)
            args,kw,grid=self.args();args[0].offset=offset;args[0].extent+=extra;args[0].pointer+=alignment
            self.assertRaises(ValueError,audit[grid],*args,**kw)
            self.assertEqual(k.warmups,[])

    def test_manifest_cannot_be_repinned_without_source_change(self):
        old=Path.read_bytes
        def read(p):return old(p)+b' ' if p.name=='DATA-PINS.json' else old(p)
        with mock.patch.object(Path,'read_bytes',read):self.assertRaises(ValueError,gate.load_contract)

    def test_negative_role_is_recorded_even_for_candidate_arm(self):
        audit=gate.AuditedKernel(Kernel(),'down-m16','candidate',1)
        self.assertEqual(audit.identity,dict(case='down-m16',arm='candidate',card=1,role='negative_control',route='negative'))


if __name__ == '__main__':
    unittest.main()
