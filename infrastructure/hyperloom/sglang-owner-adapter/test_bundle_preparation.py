"""Check the actual generated owner without importing numerical frameworks.

BUNDLE_AUTHOR selects an inert build, never a run root. These tests execute the
generated readiness sequence and sandbox environment projection on CPU stubs.
"""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]/'lib/workstation'))
import build_bundle as build


def need(value, message):
    if not value:
        raise ValueError(message)


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.author = Path(os.environ['BUNDLE_AUTHOR']).resolve()
        cls.review = cls.author/'package-v2/review'

    def source(self, name):
        return (self.review/name).read_text()

    def test_live_archive_complete_and_no_fixture(self):
        manifest = json.loads((self.author/'package-v2/MANIFEST.json').read_bytes())
        with tarfile.open(self.author/'package-v2/payload.tar') as archive:
            members = archive.getmembers()
            self.assertEqual(len(members), 93)
            self.assertTrue(all(m.isfile() for m in members))
            self.assertEqual(set(m.name for m in members), {'MANIFEST.json'} | set(manifest['package_files']))
            for member in members:
                raw = archive.extractfile(member).read()
                self.assertEqual(raw, (self.author/'package-v2'/member.name).read_bytes())
                if member.name != 'MANIFEST.json':
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest['package_files'][member.name])
        self.assertFalse(any('fixture' in name.lower() or Path(name).name.startswith('test_')
                             for name in manifest['package_files']))

    def test_ops_reverse_instrumentation_recovers_exact_selected_bytes(self):
        raw = self.source('ops.py')
        edits = [
            ('from .prefill_reference import quantizer_geometry\nimport bundle_runtime\n',
             'from .prefill_reference import quantizer_geometry\n'),
            ('    return torch, triton, bundle_runtime.kernel_view(kernels)\n', '    return torch, triton, kernels\n'),
            ('    config["waves_per_eu"] = bundle_runtime.wave_hint(m, n, k, config["waves_per_eu"])\n'
             '    compiled = kernels._gemm_afp4wfp4_kernel[', '    return kernels._gemm_afp4wfp4_kernel['),
            ('    bundle_runtime.observe_gemm(x, weight, x_scales, weight_scales, y, expected_device=expected_device)\n'
             '    return compiled\n', '')]
        for before, after in edits:
            self.assertEqual(raw.count(before), 1)
            raw = raw.replace(before, after)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(),
                         '66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776')

    def test_same_reviewed_f1_sources_and_fixed_members(self):
        for key, name in [('f1_host', 'bundle_host.py'), ('f1_fused', 'opt04_gatednorm_quant.py'),
                          ('f1_scheme', 'isolated_scheme.py'), ('f1_model', 'qwen3_5_candidate.py')]:
            self.assertEqual(hashlib.sha256((self.review/name).read_bytes()).hexdigest(), build.FIXED[key])
        ids = json.loads((self.author/'PACKAGE-IDENTITIES.json').read_bytes())
        self.assertEqual(ids['four_member']['members'],
                         ['verify_prefetch', 'verify_cap32', 'gatednorm_quant', 'gate_up_wave'])
        self.assertEqual(ids['four_member']['session_max_bytes'], 529006592)
        self.assertLess(ids['four_member']['session_max_bytes'], 512 << 20)

    def test_scoped_regenerator_changes_only_declared_runner_datum(self):
        captured = []
        namespace = {'COMBINED_RUNNER': build.FIXED['runner'], 'captured': captured}
        exec('def regenerate_pins(files,review):\n captured.append(COMBINED_RUNNER)\n return files\n', namespace)
        donor = NS(COMBINED_RUNNER=namespace['COMBINED_RUNNER'], regenerate_pins=namespace['regenerate_pins'])
        function = donor.regenerate_pins
        raw = b'declared runner'
        files = {'d3b_combined_model_runner.py': raw}
        self.assertIs(build.regenerate_pins(donor, files, self.review), files)
        self.assertEqual(captured, [hashlib.sha256(raw).hexdigest()])
        self.assertIs(donor.regenerate_pins, function)
        self.assertEqual(namespace['COMBINED_RUNNER'], build.FIXED['runner'])

    def test_real_readiness_sequence_charges_probes_before_original_client(self):
        tree = ast.parse(self.source('serving_runtime.py'))
        # Execute the generated block between its single deadline assignment and
        # the untouched original client creation, with the real compact gate row.
        callback = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and
                        any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'client_deadline'
                            for t in x.targets) for x in n.body))
        start = next(i for i, n in enumerate(callback.body) if isinstance(n, ast.Assign) and
                     any(isinstance(t, ast.Name) and t.id == 'client_deadline' for t in n.targets))
        stop = next(i for i, n in enumerate(callback.body) if i > start and isinstance(n, ast.Assign) and
                    any(isinstance(t, ast.Name) and t.id == 'raw_gate' for t in n.targets))
        sequence = callback.body[start:stop]
        self.assertEqual(sum(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'client_deadline'
                              for t in n.targets) for n in ast.walk(callback)), 1)
        workers = [dict(rank=i, pid=101+i, start_ticks=3, boot_id='b', identity_sha256='i') for i in (0, 1)]
        events = []
        def ready(material, rows):
            self.assertEqual(rows, workers); events.append('ready')
        def run(material, port, deadline, rows):
            self.assertEqual(deadline, 2045); self.assertEqual(rows, workers); events.append('probes')
        space = dict(deadline=5000, time=NS(monotonic=lambda: 5), mtp_contract=NS(phase_config=lambda: (0,2040)),
                     material={}, port=123, gate={}, expected={},
                     serving_client=NS(validate_gate=lambda *a: workers),
                     _bundle_entry=lambda: NS(modules=lambda: events.append('modules')))
        with patch.dict(sys.modules, {'bundle_readout': NS(ready=ready), 'bundle_client': NS(run=run)}):
            exec(compile(ast.Module(body=copy.deepcopy(sequence), type_ignores=[]), 'generated-readiness', 'exec'), space)
        self.assertEqual(events, ['modules', 'ready', 'probes'])

    def test_exact_native_llvm_projection_and_fail_closed_mutations(self):
        tree = ast.parse(self.source('benchmark_owner_adapter.py'))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'bwrap_argv')
        block = next(n for n in ast.walk(fn) if isinstance(n, ast.If) and
                     any(isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'llvm'
                         for t in x.targets) for x in n.body))
        start = next(i for i,n in enumerate(block.body) if isinstance(n,ast.Assign) and
                     any(isinstance(t,ast.Name) and t.id=='llvm' for t in n.targets))
        end = next(i for i,n in enumerate(block.body) if isinstance(n,ast.Assign) and
                   any(isinstance(t,ast.Name) and t.id=='before_toolchain' for t in n.targets))
        code = compile(ast.Module(body=copy.deepcopy(block.body[start:end]),type_ignores=[]),'live-llvm-projection','exec')
        value = '/usr/local/lib/python3.14/dist-packages/_rocm_sdk_core/lib/llvm'
        for old in ([], ['--setenv','LLVM_PATH',value]):
            projected = old + ['python']
            exec(code, dict(projected=projected, position=len(old), _need=need))
            self.assertEqual(projected, ['--setenv','LLVM_PATH',value,'python'])
        for old in (['--setenv','LLVM_PATH','foreign'], ['--setenv','LLVM_PATH',value]*2):
            with self.assertRaises(ValueError):
                exec(code,dict(projected=old+['python'],position=len(old),_need=need))

    def test_worker_scope_covers_adapter_and_native_target_and_route_caps(self):
        tree = ast.parse(self.source('worker_entry.py'))
        scopes = [n for n in ast.walk(tree) if isinstance(n, ast.With) and
                  any(isinstance(x.context_expr,ast.Call) and isinstance(x.context_expr.func,ast.Attribute)
                      and x.context_expr.func.attr=='worker' for x in n.items)]
        self.assertEqual(len(scopes),1)
        text = ast.unparse(scopes[0]); self.assertLess(text.index('.install('),text.index('return target('))
        self.assertIn('bundle_entry',self.source('worker_entry.py'))
        self.assertIn('private verification and unchanged ordinary decode caps', self.source('serving_integration.py'))
        self.assertIn("record['verification_caps'] == ([32] if binding['arm'] == 'mtp' else [16])",
                      self.source('serving_integration.py'))

    def test_actual_generated_bootstrap_refuses_tamper_and_foreign_module(self):
        # A fresh interpreter ensures canonical test imports cannot mask the
        # loader behaviour of the actual packaged module graph.
        code = '''import importlib.util,sys,hashlib
from pathlib import Path
from types import SimpleNamespace
root=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('entry',root/'bundle_entry.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
runtime=entry.modules()
assert runtime is sys.modules['bundle_runtime']
assert sys.modules['bundle_contract'].CONFIG['members']==['verify_prefetch','verify_cap32','gatednorm_quant','gate_up_wave']
sys.modules['bundle_hooks']=SimpleNamespace(__file__='foreign')
try:entry.modules()
except ValueError as e:assert 'foreign bundle dependency' in str(e)
else:raise AssertionError('foreign module accepted')
sys.modules.pop('bundle_hooks')
p=root/'bundle_hooks.py';p.write_bytes(p.read_bytes()+b'\\n# changed\\n')
try:entry.modules()
except ValueError as e:assert 'dependency source' in str(e)
else:raise AssertionError('tampered module accepted')
assert not any(x in sys.modules for x in ('torch','triton','sglang'))
'''
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            for name in [n+'.py' for n in build.MODULES]+['bundle_entry.py']:
                (root/name).write_bytes((self.review/name).read_bytes())
            result=subprocess.run([sys.executable,'-I','-B','-S','-c',code,str(root)],
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__ == '__main__':
    unittest.main(verbosity=2)
