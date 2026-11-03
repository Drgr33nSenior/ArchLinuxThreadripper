"""CPU tests of the actual transformed adapter and strict retained witness."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify_serving as transform


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class VerifyServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ['OWNER_PREPARATION'])
        cls.review = cls.root / 'author/package-v2/review'
        cls.adapter = load('_test_serving_adapter', cls.review / 'verify_decode_adapter.py')
        cls.reader = load('_test_serving_reader', cls.review / 'serving_integration.py')
        cls.ids = json.loads((cls.root / 'author/PACKAGE-IDENTITIES.json').read_bytes())

    def test_exact_component_source_and_fixed_c4(self):
        self.assertEqual(hashlib.sha256((self.review / transform.FILE).read_bytes()).hexdigest(), transform.CANDIDATE_SHA)
        self.assertEqual(self.ids['baseline_sha256'], transform.BASELINE_SHA)
        self.assertEqual(self.ids['source_family'], transform.FAMILY)
        self.assertEqual(self.ids['verification_attention']['native_decode_overlay'], False)
        self.assertEqual((self.review / 'extend_attention_fwd_baseline.py').read_bytes(),
                         (self.review / 'extend_attention_fwd_candidate.py').read_bytes())
        self.assertEqual(self.ids['workload_sha256'], '0b01b0312b496447be762c453b07efe80f90fdd273bd9badaf8c42c4b5d9519d')

    def route(self, arm):
        adapter = self.adapter.Adapter.__new__(self.adapter.Adapter)
        adapter.manifest = dict(arm=arm)
        adapter.prefetch_calls = {8: 0, 16: 0}
        adapter.bf16_capture_calls = {8: 0, 16: 0}
        adapter.prefetch_capture_calls = {8: 0, 16: 0}
        adapter.torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_current_stream_capturing=lambda: False))
        calls = []
        def native(*args, **kwargs):
            calls.append(('native', args, kwargs))
            return 'native'
        def candidate(*args, **kwargs):
            calls.append(('candidate', args, kwargs))
            return 'candidate'
        adapter.prefetch_decode = candidate if arm == 'mtp' else None
        return adapter, types.SimpleNamespace(decode_attention_fwd=native), calls

    def test_both_row_counts_route_exact_arguments_and_native_is_unchanged(self):
        for arm in ('control', 'mtp'):
            adapter, backend, calls = self.route(arm)
            native = backend.decode_attention_fwd
            for rows in (8, 16):
                q, sentinel = types.SimpleNamespace(shape=(rows, 12, 256)), object()
                result = adapter.bf16_attention(backend, q, sentinel, page_size=1, enable_lean=False)
                self.assertEqual(result, 'candidate' if arm == 'mtp' else 'native')
                self.assertEqual(calls[-1][1], (q, sentinel))
                self.assertEqual(calls[-1][2], dict(page_size=1, enable_lean=False))
                self.assertIs(backend.decode_attention_fwd, native)
            self.assertEqual(adapter.prefetch_calls, {8: int(arm == 'mtp'), 16: int(arm == 'mtp')})

    def test_misbound_arm_missing_candidate_and_wrong_geometry_refuse(self):
        for arm in ('control', 'mtp', 'foreign'):
            adapter, backend, calls = self.route(arm)
            if arm == 'control':
                adapter.prefetch_decode = lambda: None
            else:
                adapter.prefetch_decode = None
            with self.assertRaises(ValueError):
                adapter.bf16_attention(backend, types.SimpleNamespace(shape=(8, 12, 256)))
            self.assertFalse(calls)
        adapter, backend, calls = self.route('mtp')
        for rows in (1, 9, 32):
            with self.assertRaises(ValueError):
                adapter.bf16_attention(backend, types.SimpleNamespace(shape=(rows, 12, 256)))
        self.assertFalse(calls)

    def test_candidate_loaded_only_in_mtp_constructor_with_exact_pin(self):
        for arm in ('control', 'mtp'):
            cls = type('Backend', (), dict(init_forward_metadata_in_graph=lambda self, batch: None))
            candidate = types.SimpleNamespace(decode_attention_fwd=lambda: None)
            with patch.object(self.adapter, 'checked_method', return_value=lambda *a: None), \
                 patch.object(self.adapter, 'checked_noop', return_value=lambda *a: None), \
                 patch.object(self.adapter, 'source_module', return_value=candidate) as loader:
                result = self.adapter.Adapter(dict(arm=arm, sources=dict(triton_backend={})), {}, cls, object())
            if arm == 'mtp':
                loader.assert_called_once_with(dict(path=str(self.review / transform.FILE),
                    module='decode_attention_fwd_candidate', sha256=transform.CANDIDATE_SHA))
                self.assertIs(result.prefetch_decode, candidate.decode_attention_fwd)
            else:
                loader.assert_not_called()
                self.assertIsNone(result.prefetch_decode)

    def test_guards_metadata_fp8_and_restore_are_source_identical(self):
        if 'VERIFY_DONOR_ADAPTER' in os.environ:
            donor = Path(os.environ['VERIFY_DONOR_ADAPTER'])
        else:
            donor = Path(json.loads((self.root / 'OWNER-BUILD-INPUTS.json').read_bytes())['donor_root']) / 'author/package-v2/review/verify_decode_adapter.py'
        raw = donor.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), transform.ADAPTER_SHA)
        old = ast.parse(raw)
        new = ast.parse((self.review / 'verify_decode_adapter.py').read_bytes())
        for old_node, new_node in zip(old.body, new.body):
            if isinstance(old_node, ast.ClassDef):
                before = {n.name: n for n in old_node.body if isinstance(n, ast.FunctionDef)}
                after = {n.name: n for n in new_node.body if isinstance(n, ast.FunctionDef)}
                self.assertEqual(set(after) - set(before), {'bf16_attention'})
                for name in set(before) - {'__init__', 'decode'}:
                    self.assertEqual(ast.dump(before[name]), ast.dump(after[name]), name)
                # The only edit in decode is the BF16 call target and the added backend argument.
                calls = [n for n in ast.walk(after['decode']) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == 'bf16_attention']
                self.assertEqual(len(calls), 1)
                call = calls[0]
                self.assertEqual(ast.dump(call.args.pop(0)), ast.dump(ast.Name(id='backend', ctx=ast.Load())))
                call.func = ast.Attribute(value=ast.Name(id='backend', ctx=ast.Load()), attr='decode_attention_fwd', ctx=ast.Load())
                self.assertEqual(ast.dump(before['decode']), ast.dump(after['decode']))
            else:
                self.assertEqual(ast.dump(old_node), ast.dump(new_node))
        self.assertEqual(len(old.body), len(new.body))

    def witness(self, arm):
        binding = dict(mode='mode', arm=arm, session_sha256='s', spec_sha256='p', source_manifest_sha256='m',
            sources=dict(verify_decode_adapter={'sha256': 'a'}), resolved_contract=dict(context_length=131040))
        worker = dict(rank=0, pid=123, start_ticks=456, boot_id='boot', identity_sha256='w')
        record = dict(schema=1, **{k: binding[k] for k in ('mode', 'arm', 'session_sha256', 'spec_sha256', 'source_manifest_sha256')},
            rank=0, pid=123, worker_identity_sha256='w', enabled=True, block_size=8,
            start_ticks=456, boot_id='boot', bf16_capture_calls={'8': 16, '16': 16},
            prefetch_capture_calls={'8': 16, '16': 16} if arm == 'mtp' else {'8': 0, '16': 0},
            before_external_generations=True, extra_tensor_operations=0, extra_model_forwards=0, qualification=False,
            adapter_source=binding['sources']['verify_decode_adapter'], eligible_host_calls=32, capture_host_calls=32,
            layers=list(range(16)), buffers=2, metadata_build_host_calls=2, metadata_reuse_host_calls=30,
            metadata_capture_build_host_calls=2, indices_elements=[8 * 131048, 16 * 131048],
            prefetch_calls={'8': 16, '16': 16} if arm == 'mtp' else {'8': 0, '16': 0},
            verification_kernel_sha256=transform.CANDIDATE_SHA if arm == 'mtp' else transform.BASELINE_SHA)
        return record, binding, worker

    def read(self, record, binding, worker):
        contract = types.SimpleNamespace(contract_for_arm=lambda arm: dict(enabled=True))
        with patch.object(self.reader, '_mtp_contract', return_value=contract):
            return self.reader.validate_full_enabled_record(record, binding, worker)

    def test_witness_requires_both_graphs_exact_arm_and_source(self):
        for arm in ('control', 'mtp'):
            record, binding, worker = self.witness(arm)
            self.assertEqual(self.read(record, binding, worker), record)
            bad_values = [dict(verification_kernel_sha256='0' * 64), dict(prefetch_calls={}),
                          dict(prefetch_calls={'8': True, '16': 16}), dict(prefetch_calls={'8': 16, '16': 0}),
                          dict(prefetch_calls={'8': 16, '16': 17})]
            bad_values += [dict(bf16_capture_calls={'8': 32, '16': 0}),
                           dict(prefetch_capture_calls={'8': True, '16': 16}),
                           dict(prefetch_capture_calls={'8': 0, '16': 16}),
                           dict(start_ticks=0), dict(boot_id='foreign')]
            for change in bad_values:
                with self.subTest(arm=arm, change=change), self.assertRaises((ValueError, RuntimeError)):
                    self.read(dict(record, **change), binding, worker)

    def test_wrong_candidate_refused_before_transformation(self):
        with self.assertRaisesRegex(ValueError, 'operation-211'):
            transform.apply({}, b'not the admitted candidate')

    def test_both_native_decode_bindings_and_runtime_overlays_stay_selected(self):
        contract = load('_test_serving_contract', self.review / 'mtp_contract.py')
        bindings = {}
        for arm in ('control', 'mtp'):
            bindings[arm] = json.loads((self.review / ('HTTP-BINDING-' + arm + '.json')).read_bytes())['binding_template']
            self.assertEqual(bindings[arm]['sources']['decode_attention_fwd']['sha256'], transform.BASELINE_SHA)
            self.assertEqual(bindings[arm]['sources']['verify_decode_adapter']['sha256'],
                             hashlib.sha256((self.review / 'verify_decode_adapter.py').read_bytes()).hexdigest())
            overlays = {r['key']: r for r in contract.runtime_overlays(arm)}
            self.assertNotIn('decode_attention_fwd', overlays)
            self.assertEqual(overlays['extend_attention_fwd']['sha256'],
                             'b5d145e55f391de7e515cfe164b6011444fe1696188e2c0f900e338ce36c22f0')
        cli = [list(bindings[arm]['canonical_server_cli']) for arm in ('control', 'mtp')]
        for args, port in zip(cli, ('18087', '18088')):
            self.assertEqual(args.count('--port'), 1)
            at = args.index('--port') + 1
            self.assertEqual(args[at], port)
            args[at] = '<bound-arm-port>'
        self.assertEqual(*cli)
        self.assertEqual(bindings['control']['resolved_contract'], bindings['mtp']['resolved_contract'])

    def test_warmup_calls_cannot_substitute_for_each_captured_graph(self):
        for arm in ('control', 'mtp'):
            adapter, backend, calls = self.route(arm)
            for capture in (False, True):
                adapter.torch.cuda.is_current_stream_capturing = lambda: capture
                for rows in (8, 16):
                    for _ in range(16):
                        adapter.bf16_attention(backend, types.SimpleNamespace(shape=(rows, 12, 256)))
            self.assertEqual(adapter.bf16_capture_calls, {8: 16, 16: 16})
            self.assertEqual(adapter.prefetch_capture_calls,
                             {8: 16, 16: 16} if arm == 'mtp' else {8: 0, 16: 0})
            self.assertEqual(adapter.prefetch_calls,
                             {8: 32, 16: 32} if arm == 'mtp' else {8: 0, 16: 0})

    def produced_witness(self, worker_module, arm, directory, rank=0):
        record, manifest, worker = self.witness(arm)
        worker.update(rank=rank, pid=os.getpid())
        manifest['evidence_dir'] = str(directory)
        adapter, _, _ = self.route(arm)
        adapter.enabled = True
        adapter.eligible_host_calls = adapter.capture_host_calls = 32
        adapter.layers = set(range(16))
        adapter.metadata_build_host_calls = adapter.metadata_capture_build_host_calls = 2
        adapter.metadata_reuse_host_calls = 30
        adapter.bf16_capture_calls = {8: 16, 16: 16}
        adapter.prefetch_capture_calls = adapter.prefetch_calls = (
            {8: 16, 16: 16} if arm == 'mtp' else {8: 0, 16: 0})
        adapter.buffers = {n: {'indices': types.SimpleNamespace(numel=lambda n=n: n * 131048)} for n in (8, 16)}
        contract = types.SimpleNamespace(contract_for_arm=lambda arm: dict(enabled=True, speculative_dflash_block_size=8),
            precision_profile=lambda: dict(context_length=131040), instrumentation_enabled=lambda: False,
            full_enabled_benchmark=lambda: True)
        with patch.object(worker_module, '_source_module', return_value=types.SimpleNamespace(CURRENT=adapter)):
            worker_module._full_enabled_witness(manifest, worker, contract)
        actual = json.loads((directory / ('FULL-ENABLED-rank-%d.json' % rank)).read_bytes())
        self.assertEqual(self.read(actual, manifest, worker), actual)
        return actual, manifest, worker, contract, adapter

    def test_actual_producer_is_metadata_only_and_exclusive(self):
        module = load('_test_production_worker', self.review / 'worker_entry.py')
        for arm in ('control', 'mtp'):
            with tempfile.TemporaryDirectory() as tmp:
                self.produced_witness(module, arm, Path(tmp))
                with self.assertRaises((ValueError, RuntimeError, FileExistsError)):
                    self.produced_witness(module, arm, Path(tmp))

    def test_production_scheduler_hook_authentication_restore_and_exception(self):
        module = load('_test_production_worker', self.review / 'worker_entry.py')
        source = ('class Scheduler:\n'
                  '    def __init__(self): pass\n'
                  '    def get_init_info(self): return {"ready": True}\n'
                  'def run_scheduler_process(): return Scheduler().get_init_info()\n')
        for failure in (None, 'original', 'writer', 'capture', 'identity', 'substitution'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                path = root / 'scheduler.py'
                raw = source.replace('return {"ready": True}', 'raise KeyboardInterrupt()') if failure == 'original' else source
                path.write_text(raw)
                native = load('_fixture_scheduler', path)
                _, manifest, worker, contract, adapter = self.produced_witness(module, 'mtp', root)
                manifest['sources']['scheduler'] = dict(module='_fixture_scheduler', path=str(path),
                    target='run_scheduler_process', sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                manifest['sources']['mtp_contract'] = {}
                original = native.Scheduler.get_init_info
                cuda = types.SimpleNamespace(current_device=lambda: 0, is_current_stream_capturing=lambda: failure == 'capture')
                def resolve(source):
                    return contract if source.get('module') == 'mtp_contract' else types.SimpleNamespace(CURRENT=adapter)
                if failure == 'substitution':
                    native.Scheduler.get_init_info = lambda self: None
                    with self.assertRaises(ValueError):
                        module._verification_startup_hook(manifest, 0, 0, worker, native.run_scheduler_process)
                    continue
                with patch.dict(sys.modules, torch=types.SimpleNamespace(cuda=cuda)), \
                     patch.object(module, '_process_start_ticks', return_value=0 if failure == 'identity' else worker['start_ticks']), \
                     patch.object(module, '_boot_id', return_value=worker['boot_id']), \
                     patch.object(module, '_source_module', side_effect=resolve), \
                     patch.object(module, '_full_enabled_witness', side_effect=RuntimeError('writer') if failure == 'writer' else None) as writer:
                    restore = module._verification_startup_hook(manifest, 0, 0, worker, native.run_scheduler_process)
                    wrapper = native.Scheduler.get_init_info
                    try:
                        if failure:
                            with self.assertRaises((KeyboardInterrupt, ValueError, RuntimeError)):
                                native.run_scheduler_process()
                        else:
                            self.assertEqual(native.run_scheduler_process(), {'ready': True})
                            writer.assert_called_once_with(manifest, worker, contract)
                            with self.assertRaises(ValueError):
                                wrapper(native.Scheduler())
                    finally:
                        restore()
                    self.assertIs(native.Scheduler.get_init_info, original)

    def test_actual_production_bundle_export_and_readback_require_both_routes(self):
        worker_module = load('_test_production_worker', self.review / 'worker_entry.py')
        # The unchanged native identity/HTTP validators have their own corpus;
        # stub their outputs here while executing the real exporter and production reader.
        artifacts = load('_test_route_artifacts', self.review / 'benchmark_artifacts.py')
        for arm, owner_arm in (('control', 'bf16'), ('mtp', 'kv4')):
            for mutation in (None, 'missing', 'wrong_rank', 'foreign_boot', 'uncaptured', 'candidate_control', 'extra', 'oversize'):
                with self.subTest(arm=arm, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve(); native = root / 'native'; native.mkdir()
                    client = root / 'client'; client.mkdir()
                    (client / 'RUN-SUMMARY.json').write_text('{}')
                    full_workers = []
                    for rank in (0, 1):
                        _, binding, worker, _, _ = self.produced_witness(worker_module, arm, native, rank)
                        full_workers.append(dict(worker, sources=binding['sources']))
                    binding['mode'] = self.reader.MODE
                    expected = {k: binding[k] for k in ('arm', 'session_sha256', 'spec_sha256', 'source_manifest_sha256')}
                    for rank in (0, 1):
                        path = native / ('FULL-ENABLED-rank-%d.json' % rank)
                        proof = json.loads(path.read_bytes()); proof['mode'] = binding['mode']
                        if rank == 1:
                            if mutation == 'wrong_rank': proof['rank'] = 0
                            if mutation == 'foreign_boot': proof['boot_id'] = 'other'
                            if mutation == 'uncaptured': proof['bf16_capture_calls'] = {'8': 32, '16': 0}
                            if mutation == 'candidate_control': proof['prefetch_capture_calls'] = {'8': 16, '16': 0}
                        path.write_text(json.dumps(proof) + (' ' * (16 << 10) if mutation == 'oversize' else ''))
                    if mutation == 'missing': (native / 'FULL-ENABLED-rank-1.json').unlink()
                    if mutation == 'extra': (native / 'unexpected.json').write_text('{}')
                    manifest = dict(binding)
                    raw = json.dumps(manifest).encode()
                    (native / 'WORKER-MANIFEST.json').write_bytes(raw)
                    binding['worker_manifest_sha256'] = hashlib.sha256(raw).hexdigest()
                    (native / 'SERVING-BINDING.json').write_text(json.dumps(binding))
                    for name in ('NATIVE-READINESS.json', 'NATIVE-READINESS-CALLBACK.json'):
                        (native / name).write_text('{}')
                    for rank, worker in enumerate(full_workers):
                        (native / ('worker-rank-%d.json' % rank)).write_text(json.dumps(worker))
                    with patch.object(artifacts, '_binding'):
                        artifacts.pack_http_artifacts(root, dict(experiment_mode=artifacts.HTTP_MODE))
                    packed = json.loads((root / 'native-http-bundle.json').read_bytes())
                    observations = {k: self.reader._decode_member(v) for k, v in packed['observations'].items()}
                    result = dict(schema=1, mode=self.reader.MODE, status='HTTP_ARM_COMPLETE', arm=owner_arm,
                        logical_arm=arm, session_sha256=expected['session_sha256'], spec_sha256=expected['spec_sha256'],
                        source_manifest_sha256=expected['source_manifest_sha256'], client_summary={}, native_gate={},
                        parent_error=None, qualification=False)
                    (root / 'result.json').write_text(json.dumps(result))
                    contract = types.SimpleNamespace(instrumentation_enabled=lambda: False,
                        owner_arm_map=lambda: dict(bf16='control', kv4='mtp'), contract_for_arm=lambda a: dict(enabled=True))
                    reader = types.SimpleNamespace(read_client=lambda *a, **k: dict(rows=[], wire_complete=True),
                        contract=types.SimpleNamespace(requests=lambda: []))
                    with patch.object(self.reader, '_mtp_contract', return_value=contract), \
                         patch.object(self.reader, '_unpack_client', return_value=(client, 'c')), \
                         patch.object(self.reader, '_native', return_value=(b'{}', full_workers, full_workers, observations, 'n', reader)), \
                         patch.object(self.reader, '_joined'):
                        invoke = lambda: self.reader.read_http_artifacts(root, extraction_root=root/'unpacked',
                            expected=expected, owner_arm=owner_arm, joins=[], supervision={})
                        if mutation:
                            with self.assertRaises(ValueError): invoke()
                        else:
                            result = invoke()
                            self.assertEqual(result['status'], 'HTTP_ARM_COMPLETE')
                            self.assertEqual(set(result['native_observations']), self.reader.PRODUCTION_OBSERVATIONS)

    def test_production_callback_refuses_missing_witness_before_client_creation(self):
        worker_module = load('_test_production_worker', self.review / 'worker_entry.py')
        with patch.object(sys, 'path', [str(self.review)] + sys.path):
            contract = load('mtp_contract', self.review / 'mtp_contract.py')
            runtime = load('_test_route_runtime', self.review / 'serving_runtime.py')
        for missing in (False, True):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve(); workers = []
                for rank in (0, 1):
                    _, binding, worker, _, _ = self.produced_witness(worker_module, 'mtp', root, rank)
                    workers.append(worker)
                binding['canonical_server_cli'] = ['--port', '18088']
                if missing: (root / 'FULL-ENABLED-rank-1.json').unlink()
                material = dict(entry_binding=binding, worker_template=dict(preweight=dict(graph_observer={})),
                    native_dir=root, client_dir=root/'client')
                calls = []
                def factory(**kwargs):
                    self.assertIsNone(kwargs['request_observer'])
                    calls.append('created')
                    return types.SimpleNamespace(run=lambda *a: {'fixture': True})
                with patch.dict(sys.modules, serving_integration=self.reader), \
                     patch.object(runtime.serving_client, 'validate_gate', return_value=workers), \
                     patch.object(runtime, '_hip_sampling_ready'), \
                     patch.object(self.reader, '_mtp_contract', return_value=contract):
                    callback = runtime._client_callback(material, deadline=time.monotonic()+10,
                        client_factory=factory, holder={})
                    if missing:
                        with self.assertRaises((OSError, ValueError)): callback({})
                        self.assertEqual(calls, [])
                    else:
                        self.assertEqual(callback({}), {'fixture': True})
                        self.assertEqual(calls, ['created'])

    def test_worker_production_entry_installs_and_cleans_hook_without_instrumentation(self):
        module = load('_test_production_worker', self.review / 'worker_entry.py')
        for raises in (False, True):
            with self.subTest(raises=raises), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = ('class Scheduler:\n'
                          '    def __init__(self): pass\n'
                          '    def get_init_info(self): return {"ready": True}\n'
                          'def run_scheduler_process(*args, **kwargs):\n'
                          '    result = Scheduler().get_init_info()\n' +
                          ('    raise KeyboardInterrupt()\n' if raises else '    return result\n'))
                path = root/'scheduler.py'; path.write_text(source)
                native = load('_fixture_scheduler_entry', path)
                _, manifest, worker, contract, adapter = self.produced_witness(module, 'mtp', root)
                # Remove only this test's previously produced witness; the entry must create it.
                (root/'FULL-ENABLED-rank-0.json').unlink()
                manifest['expected_ranks'] = [0, 1]
                manifest['sources'].update(scheduler=dict(module='_fixture_scheduler_entry', path=str(path),
                    target='run_scheduler_process', sha256=hashlib.sha256(path.read_bytes()).hexdigest()),
                    mtp_contract={}, gemm={})
                contract.dense_mxfp4_enabled = lambda: False
                contract.verify_attention_observed = lambda arm: True
                restored = []
                adapter.restore = lambda: restored.append('adapter')
                adapter_module = types.SimpleNamespace(CURRENT=adapter, install=lambda *a: adapter)
                resolve = lambda source: contract if source.get('module') == 'mtp_contract' else adapter_module
                original = native.Scheduler.get_init_info
                cuda = types.SimpleNamespace(current_device=lambda: 0, is_current_stream_capturing=lambda: False)
                with patch.dict(sys.modules, torch=types.SimpleNamespace(cuda=cuda)), \
                     patch.object(module, '_read_manifest', return_value=manifest), \
                     patch.object(module, '_validate_manifest', side_effect=lambda m: m), \
                     patch.object(module, '_worker_record', return_value=worker), \
                     patch.object(module, '_native_module'), \
                     patch.object(module, '_source_selection', return_value=native.run_scheduler_process), \
                     patch.object(module, '_source_module', side_effect=resolve), \
                     patch.object(module, '_process_start_ticks', return_value=worker['start_ticks']), \
                     patch.object(module, '_boot_id', return_value=worker['boot_id']), \
                     patch.object(module, '_allocator_startup_hook', side_effect=AssertionError('instrumentation forbidden')):
                    if raises:
                        with self.assertRaises(KeyboardInterrupt):
                            module.run_scheduler_process(None, None, 0, 0, 0, None, None)
                    else:
                        self.assertEqual(module.run_scheduler_process(None, None, 0, 0, 0, None, None), {'ready': True})
                proof = json.loads((root/'FULL-ENABLED-rank-0.json').read_bytes())
                self.assertEqual(self.read(proof, manifest, worker), proof)
                self.assertEqual(restored, ['adapter'])
                self.assertIs(native.Scheduler.get_init_info, original)


if __name__ == '__main__':
    unittest.main(verbosity=2)
