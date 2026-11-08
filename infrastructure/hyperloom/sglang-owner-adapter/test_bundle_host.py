"""Execute the inherited native host tail through the actual bundle subclass.

This is a metadata/lifecycle fixture, never a numerical or GPU claim. The
separately sealed F1 component provides its mathematical/device evidence.
"""
import copy
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent))
import bundle_contract as c
import bundle_runtime as r


class HostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(os.environ['F1_HOST_FIXTURE'])
        spec=importlib.util.spec_from_file_location('_bundle_inert_host_fixture',path)
        cls.fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.fixture)

    def tearDown(self):
        r.STATE=None;c.PHASE=None;c.SHADOW=False

    def state(self,f,ids=(4,),arm='mtp',dependencies=None):
        manifest=dict(arm=arm,session_sha256='s',source_manifest_sha256='m',evidence_dir='/tmp')
        worker=dict(manifest,rank=0,pid=1,boot_id='b',start_ticks=2,identity_sha256='i')
        return r.configure(manifest,worker,f.torch,f.ops,dependencies or {},{})

    def route(self,f,state):
        route=r.route_class(type(f.route))(f.model,torch=f.torch,ops=f.ops,norm_module=f.norm_module,
            kernel=f.fused,classes=(type(f.model),type(f.model.norm),type(f.proj),type(f.proj.scheme),type(f.proj.quant_method)),
            get_forward=lambda:f.flags,is_allocation_symmetric=lambda:f.flags.symmetric)
        route.bundle_layer=4;state.routes[4]=route;state.fallback8=False;state.device_count=32
        route.attach();return route

    def tensor_methods(self):
        return (patch.object(self.fixture.Tensor,'contiguous',lambda self:self,create=True),
                patch.object(self.fixture.Tensor,'view',lambda self,dtype:self,create=True),
                patch.object(self.fixture.Tensor,'fill_',lambda self,value:self,create=True))

    def test_eager_actual_host_tail_checks_packed_scales_and_projection(self):
        f=self.fixture.fixture(16)
        with patch.object(c,'CONFIG',dict(gdn_ids=[4],probe_ids={})),self.tensor_methods()[0],self.tensor_methods()[1],self.tensor_methods()[2]:
            state=self.state(f);self.route(f,state);f.torch.equal=lambda first,second:True
            with c.phase('c2'):output=f.tail(f.model,f.batch,f.x,f.z)
            self.assertEqual(output.shape,(16,5120));self.assertEqual(state.shadow_calls,3)
            self.assertEqual(state.f1_exact['4'],{'16':1})
            self.assertEqual([v for v in f.events if v in ('fused','norm','quant','gemm')],
                             ['fused','gemm','norm','quant','gemm'])
            self.assertNotIn('all-reduce',f.events)

    def test_real_mismatch_propagates_and_shadow_context_restores(self):
        f=self.fixture.fixture(16)
        with patch.object(c,'CONFIG',dict(gdn_ids=[4],probe_ids={})),self.tensor_methods()[0],self.tensor_methods()[1],self.tensor_methods()[2]:
            state=self.state(f);self.route(f,state);f.torch.equal=lambda first,second:False
            with self.assertRaisesRegex(ValueError,'packed byte mismatch'):
                with c.phase('c2'):f.tail(f.model,f.batch,f.x,f.z)
            self.assertFalse(c.SHADOW);self.assertIsNone(c.PHASE)
            self.assertFalse(state.f1_exact['4'])

    def test_actual_capture_tail_has_no_shadow_norm_or_quantizer(self):
        for arm in ('control','mtp'):
            f=self.fixture.fixture(16)
            with patch.object(c,'CONFIG',dict(gdn_ids=[4],probe_ids={})):
                state=self.state(f,arm=arm);self.route(f,state);f.torch.capturing=True
                state.exact=lambda *a:(_ for _ in ()).throw(AssertionError('shadow during capture'))
                output=f.tail(f.model,f.batch,f.x,f.z)
                self.assertEqual(output.shape,(16,5120));self.assertFalse(state.f1_exact['4'])
                kinds=[v for v in f.events if v in ('fused','norm','quant','gemm')]
                self.assertEqual(kinds,['fused','gemm'] if arm=='mtp' else ['norm','quant','gemm'])
                self.assertEqual(state.f1_capture['4']['16'],1)
            r.STATE=None

    def runner(self,f,core_count):
        f.torch.core_count=core_count
        f.torch.device=lambda kind,index:self.fixture.Device(index)
        class GateUp:pass
        layers=[];ids=[i for i in range(64) if i%4!=3]
        for i in range(64):
            gate=GateUp();gate.bias=None;gate._isolated_mxfp4_loaded=True
            gate.scheme=copy.copy(f.proj.scheme)
            gate.scheme.input_size_per_partition=5120;gate.scheme.output_size_per_partition=17408
            gate.weight=self.fixture.Tensor((17408,2560),'uint8')
            gate.weight_scale=self.fixture.Tensor((17408,160),'uint8')
            layers.append(NS(mlp=NS(gate_up_proj=gate),linear_attn=copy.deepcopy(f.model) if i in ids else None))
        dependencies=dict(norm=NS(calc_rows_per_block=f.norm_module.calc_rows_per_block,RMSNorm=type(f.model.norm)),
            linear=NS(MergedColumnParallelLinear=GateUp,RowParallelLinear=type(f.proj)),scheme=type(f.proj.scheme),
            model=NS(Qwen3_5GatedDeltaNet=type(f.model)),quark=NS(QuarkLinearMethod=type(f.proj.quant_method)),
            host=NS(Route=type(f.route)),fused=NS(_gated_norm_mxfp4_kernel=f.fused),
            get_forward=lambda:f.flags,is_allocation_symmetric=lambda:f.flags.symmetric)
        runner=NS(is_draft_worker=False,tp_rank=0,gpu_id=0,startup_weight_load=None,model=NS(model=NS(layers=layers)))
        return runner,dependencies,ids

    def test_all_48_sparse_gdn_and_64_gate_up_layers_attach_before_capture(self):
        for count in (32,64):
            f=self.fixture.fixture(16);runner,dependencies,ids=self.runner(f,count)
            with patch.object(c,'CONFIG',dict(gdn_ids=ids,probe_ids={})):
                state=self.state(f,ids,dependencies=dependencies);r.attach_runner(runner)
                self.assertTrue(state.attached);self.assertEqual(set(state.routes),set(ids))
                self.assertEqual(len(state.gate_up),64);self.assertEqual(state.fallback8,count==64)
                for i in ids:self.assertIs(runner.model.model.layers[i].linear_attn._opt07_f1_route,state.routes[i])
                with self.assertRaises(ValueError):r.attach_runner(runner)
            r.STATE=None

    def test_bad_gate_up_or_gdn_layer_cannot_claim_complete_attachment(self):
        for mutation in ('weight','layer'):
            f=self.fixture.fixture(16);runner,dependencies,ids=self.runner(f,32)
            if mutation=='weight':runner.model.model.layers[0].mlp.gate_up_proj.weight=self.fixture.Tensor((1,1),'uint8')
            else:runner.model.model.layers[0].linear_attn=None
            with patch.object(c,'CONFIG',dict(gdn_ids=ids,probe_ids={})):
                state=self.state(f,ids,dependencies=dependencies)
                with self.assertRaises(ValueError):r.attach_runner(runner)
                self.assertFalse(state.attached)
            r.STATE=None


if __name__=='__main__':unittest.main(verbosity=2)
