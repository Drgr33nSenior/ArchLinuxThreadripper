"""Source-faithful regression for the operation221 F1 refusal. CPU only."""
import ast
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import bundle_contract as c


def function(path,name,namespace):
    tree=ast.parse(path.read_bytes())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


class AllocatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ['BUNDLE_ALLOCATOR_PROOF']).resolve()
        cls.fixture_path=Path(os.environ['F1_HOST_FIXTURE']).resolve()
        pins={'dp_attention.py':'93a2fe6a000428c5b81004b3039a3ed773a036becc1b2ffd4fc8b13a3481e6f6',
              'runtime_context.py':'50f1e87490d0b5c2bb1ddf820d65719cd1c9b876d265e902934692cd3719b10e',
              'pynccl_allocator.py':'cec2f1df527cc2c5569bcd555734bc1b97f7ce2280c33b099e1a6b5accb93c22'}
        for name,pin in pins.items():
            assert hashlib.sha256((cls.root/name).read_bytes()).hexdigest()==pin
        spec=importlib.util.spec_from_file_location('_allocator_host_fixture',cls.fixture_path)
        cls.fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.fixture)

    def predicates(self,dp=False,padding=False,enabled=False):
        eligibility=function(self.root/'dp_attention.py','is_allocation_symmetric',
                             dict(is_dp_attention_enabled=lambda:dp,is_dp_max_padding=lambda:padding))
        active=function(self.root/'pynccl_allocator.py','is_symmetric_memory_enabled',
                        dict(get_exec=lambda:NS(comm=NS(enable_symm_mem=enabled))))
        allocation=object()
        linear=NS(is_allocation_symmetric=eligibility,use_symmetric_memory=allocation,get_forward=lambda:None)
        allocator=NS(is_symmetric_memory_enabled=active,use_symmetric_memory=allocation)
        return linear,allocator

    def dependency_predicate(self,linear,allocator):
        tree=ast.parse(Path(__file__).with_name('bundle_entry.py').read_bytes())
        worker=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='worker')
        dep=next(n for n in worker.body if isinstance(n,ast.FunctionDef) and n.name=='dependencies')
        names={'bundle_norm':NS(),'head_linear':linear,'bundle_allocator':allocator,
               'quark_config':NS(),'head_model':NS(),'bundle_fused':NS(),'bundle_host':NS()}
        space=dict(manifest={'sources':{key:key for key in names}},native=lambda key:names[key],
                   private=lambda key:names[key],scheme=object(),c=c)
        exec(compile(ast.Module(body=[dep],type_ignores=[]),'generated-dependencies','exec'),space)
        return space['dependencies']()['is_allocation_symmetric']

    def test_no_dp_is_eligible_but_disabled_allocator_is_not_active(self):
        linear,allocator=self.predicates()
        self.assertTrue(linear.is_allocation_symmetric())
        self.assertFalse(allocator.is_symmetric_memory_enabled())
        fixed=self.dependency_predicate(linear,allocator)
        self.assertFalse(fixed())
        f=self.fixture.fixture(16)
        f.route.is_allocation_symmetric=linear.is_allocation_symmetric
        args=(f.model,f.batch,f.x.reshape(-1,128),f.z.reshape(-1,128),(16,24,128),False)
        self.assertFalse(f.route.eligible(*args)) # reproduces operation221
        f.route.is_allocation_symmetric=fixed
        self.assertTrue(f.route.eligible(*args))

    def test_genuinely_enabled_symmetric_allocator_still_refuses(self):
        linear,allocator=self.predicates(enabled=True)
        fixed=self.dependency_predicate(linear,allocator)
        self.assertTrue(fixed())
        f=self.fixture.fixture(16);f.route.is_allocation_symmetric=fixed
        self.assertFalse(f.route.eligible(f.model,f.batch,f.x.reshape(-1,128),f.z.reshape(-1,128),(16,24,128),False))

    def test_exact_native_predicate_truth_table(self):
        for dp in (False,True):
            for padding in (False,True):
                for enabled in (False,True):
                    linear,allocator=self.predicates(dp,padding,enabled)
                    self.assertEqual(self.dependency_predicate(linear,allocator)(),((not dp) or padding) and enabled)

    def test_matches_the_native_pool_selection_for_tp2(self):
        for eligible in (False,True):
            for enabled in (False,True):
                choose=function(self.root/'pynccl_allocator.py','use_symmetric_memory',
                    dict(GroupCoordinator=object,is_symmetric_memory_enabled=lambda:enabled,
                         SymmetricMemoryContext=lambda group:NS(active=True),nullcontext=lambda:NS(active=False)))
                observed=choose(NS(world_size=2),disabled=not eligible)
                self.assertEqual(observed.active,eligible and enabled)

    def test_foreign_allocator_call_path_is_not_accepted(self):
        linear,allocator=self.predicates();allocator.use_symmetric_memory=object()
        with self.assertRaisesRegex(ValueError,'allocator call path identity'):
            self.dependency_predicate(linear,allocator)()


if __name__=='__main__':unittest.main(verbosity=2)
