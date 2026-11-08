"""CPU tests of the opt-in cap-only ablation through the real builder and generated owner.

LEGACY_AUTHOR and ABLATION_AUTHOR select two inert builds made by this checkout's
build_owner.py: the frozen four-member inputs and their cap-ablation derivative.
FROZEN_INPUTS names the frozen build inputs; EVIDENCE names the private evidence
root holding the executed compiler pins and component compile reports. Nothing
here imports a numerical framework, starts a model or launches a kernel.
"""
import ast
import base64
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / 'lib/workstation'))
import build_bundle as build
import bundle_compile
import bundle_compile_read

LEGACY_PINS_SHA = '7fd566e8853ebf88496fe7a6c07c22b1f2b3a803a12f2a59ce01c20fec048a13'
PREFETCH_SHA = 'ed26c67b69da358944999fdf9f769fafc4c2aef821335fbb777679b966e9e4e3'
NATIVE_SHA = '34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a'
PARTS = ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')
CODE_EDITS = {'bundle_contract.py', 'bundle_runtime.py', 'bundle_readout.py', 'verify_decode_adapter.py',
              'worker_entry.py', 'serving_integration.py', 'BUNDLE-COMPILER-PINS.json'}
DIGEST_ONLY = {'bundle_entry.py', 'serving_runtime.py', 'HTTP-BINDING-control.json', 'HTTP-BINDING-mtp.json',
               'BENCHMARK-SPEC2.json', 'benchmark_physical_controller.py', 'RESOURCE-MANIFEST.json'}
HEX = re.compile(rb'[0-9a-f]{16,64}')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def evidence():
    return Path(os.environ['EVIDENCE']).resolve()


def legacy_pins():
    path = evidence() / 'four-member-serving-qualification-20261104-v1/compiler-cold-proof-v1/BUNDLE-COMPILER-PINS.json'
    raw = path.read_bytes()
    assert sha(raw) == LEGACY_PINS_SHA
    return path, json.loads(raw)


def COLD_PROOF():
    return evidence() / 'four-member-serving-qualification-20261104-v1/compiler-cold-proof-v1'


FROZEN_TARGET = '1d32106a3936adc18e8b8cc72b7a5a15fbe09aaf7b61ab764b0e8ba3888a1003'
FROZEN_RECIPE = '22d81c1121f93ee753cba8b14689982659152587de5e99640812ba8c20a1643e'
PAYLOAD_LITERAL = re.compile(rb"PAYLOAD=json\.loads\(gzip\.decompress\(base64\.b64decode\(b'([A-Za-z0-9+/=]+)'\)\)\)")


def payload_identity(raw):
    """TARGET bytes with the one embedded gzip literal replaced by the SHA-256 of its decoded payload."""
    found = list(PAYLOAD_LITERAL.finditer(raw))
    assert len(found) == 1
    payload = gzip.decompress(base64.b64decode(found[0].group(1), validate=True))
    return (raw[:found[0].start(1)] + hashlib.sha256(payload).hexdigest().encode() + raw[found[0].end(1):],
            payload, found[0].group(1))


def check_legacy_preparation(case, result, directory):
    """Legacy prepare() reproduces the frozen cold-proof TARGET and RECIPE.

    Every byte outside the embedded gzip literal, and the decoded payload, must match on
    every host. The exact frozen hashes are additionally required wherever this host's
    deflate encoder reproduces the frozen stream (the declared Mac preparation interpreter).
    """
    proof = COLD_PROOF() / 'proof'
    frozen_target, frozen_recipe = (proof / 'TARGET.py').read_bytes(), (proof / 'RECIPE.py').read_bytes()
    case.assertEqual((sha(frozen_target), sha(frozen_recipe)), (FROZEN_TARGET, FROZEN_RECIPE))
    target, recipe = (directory / 'TARGET.py').read_bytes(), (directory / 'RECIPE.py').read_bytes()
    case.assertEqual((sha(target), sha(recipe)), (result['target_sha256'], result['recipe_sha256']))
    frozen_identity, payload, blob = payload_identity(frozen_target)
    case.assertEqual(payload_identity(target)[0], frozen_identity)
    case.assertEqual(recipe, frozen_recipe.replace(repr(frozen_target).encode(), repr(target).encode()))
    if base64.b64encode(gzip.compress(payload, mtime=0)) == blob:
        case.assertEqual((result['target_sha256'], result['recipe_sha256']), (FROZEN_TARGET, FROZEN_RECIPE))
    else:
        # This host's encoder yields another stream for the same payload; the identities
        # above prove the bytes differ only inside that compressed literal.
        case.assertNotEqual(result['target_sha256'], FROZEN_TARGET)


def fixture(pins_legacy):
    """Explicitly synthetic cap16 prefetch-source record; never a native proof."""
    pins = copy.deepcopy(pins_legacy)
    control = pins['control']
    control['attention-stage1'] = copy.deepcopy(pins['mtp']['attention-stage1'])
    record = copy.deepcopy(control['attention-stage2-cap16'])
    for part in PARTS:
        record['parts'][part] = dict(bytes=9, sha256=sha(b'TEST-ONLY' + part.encode()))
    control['attention-stage2-cap16'] = record
    serving = {arm + '-' + key: dict(source_path=build.CANDIDATE_ATTENTION) for arm, key in (
        ('control', 'attention-stage1'), ('control', 'attention-stage2-cap16'),
        ('mtp', 'attention-stage1'), ('mtp', 'attention-stage2-cap32'))}
    report = dict(status='CPU_SERVING_COMPILERS_PROVEN', ablation=build.ABLATION, gpu_launches=0,
                  model_starts=0, pins=pins, serving=serving, test_fixture_only=True)
    return pins, report


class ContractTests(unittest.TestCase):
    def contract(self, pins, report, legacy_path, legacy_sha=LEGACY_PINS_SHA, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'REPORT.json'
            path.write_bytes(canonical(report))
            value = dict(kind=build.ABLATION, compiler_report=dict(path=str(path), sha256=sha(path.read_bytes())),
                         legacy_compiler_pins=dict(path=str(legacy_path), sha256=legacy_sha), **extra)
            return build.ablation_contract(value, pins)

    def test_bound_fixture_accepted_and_every_identity_mutation_refused(self):
        path, old = legacy_pins()
        pins, report = fixture(old)
        result = self.contract(pins, report, path)
        self.assertEqual(result['only_difference'], ['verify_cap32'])
        self.assertEqual(result['arm_members']['mtp'], ['verify_prefetch', 'verify_cap32', 'gatednorm_quant', 'gate_up_wave'])
        self.assertEqual(set(result['arm_members']['mtp']) - set(result['arm_members']['control']), {'verify_cap32'})

        def refused(pins_value, report_value, **kw):
            with self.assertRaises((ValueError, KeyError, TypeError)):
                self.contract(pins_value, report_value, path, **kw)

        refused(pins, report, unexpected=1)
        refused(pins, dict(report, status='CPU_COMPILE_REFUSED'))
        refused(pins, {k: v for k, v in report.items() if k != 'ablation'})
        refused(pins, dict(report, gpu_launches=1))
        refused(old, report)                                  # report does not bind these pins
        native_stage1 = copy.deepcopy(pins)
        native_stage1['control']['attention-stage1'] = old['control']['attention-stage1']
        refused(native_stage1, dict(report, pins=native_stage1))   # control kept the native kernel
        wave = copy.deepcopy(pins)
        wave['control']['gate-up-m8-wave0'] = old['control']['gate-up-m8-wave1']
        refused(wave, dict(report, pins=wave))                # any non-cap record differs
        same = copy.deepcopy(pins)
        same['control']['attention-stage2-cap16'] = same['mtp']['attention-stage2-cap32']
        refused(same, dict(report, pins=same))
        drift = copy.deepcopy(pins)
        drift['mtp']['f1-t8'] = drift['mtp']['f1-t16']; drift['control']['f1-t8'] = drift['mtp']['f1-t16']
        refused(drift, dict(report, pins=drift))              # cap32 arm differs from op224's
        missing = copy.deepcopy(pins); missing['control'].pop('attention-stage2-cap16')
        refused(missing, dict(report, pins=missing))
        path_report = copy.deepcopy(report)
        path_report['serving']['control-attention-stage1']['source_path'] = '/mnt/composition/native.py'
        refused(pins, path_report)
        with self.assertRaises(ValueError):
            self.contract(pins, report, path, legacy_sha='0' * 64)


class GeneratedTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = Path(os.environ['LEGACY_AUTHOR']).resolve() / 'assembled-review'
        cls.ablation = Path(os.environ['ABLATION_AUTHOR']).resolve() / 'assembled-review'

    def test_frozen_legacy_packet_reproduced_by_patched_builder(self):
        frozen = json.loads(Path(os.environ['FROZEN_INPUTS']).read_bytes())['frozen_packet']
        ids = json.loads((self.legacy.parent / 'PACKAGE-IDENTITIES.json').read_bytes())
        self.assertEqual(ids['nonce'], frozen['nonce'])
        for key, pin in frozen['pins'].items():
            self.assertEqual(ids[key], pin, key)
        self.assertNotIn('ablation', ids['four_member'])
        ablation = json.loads((self.ablation.parent / 'PACKAGE-IDENTITIES.json').read_bytes())
        self.assertEqual(ablation['four_member']['ablation']['only_difference'], ['verify_cap32'])
        self.assertNotEqual(ablation['nonce'], frozen['nonce'])

    def test_review_trees_differ_only_in_declared_edits_and_derived_digests(self):
        names = sorted(p.name for p in self.legacy.iterdir())
        self.assertEqual(names, sorted(p.name for p in self.ablation.iterdir()))
        nonce = (json.loads((self.legacy.parent / 'PACKAGE-IDENTITIES.json').read_bytes())['nonce'],
                 json.loads((self.ablation.parent / 'PACKAGE-IDENTITIES.json').read_bytes())['nonce'])
        changed = {n for n in names if (self.legacy / n).read_bytes() != (self.ablation / n).read_bytes()}
        self.assertEqual(changed, CODE_EDITS | DIGEST_ONLY)
        for name in DIGEST_ONLY:
            old = HEX.sub(b'H', (self.legacy / name).read_bytes().replace(nonce[0].encode(), b'N'))
            new = HEX.sub(b'H', (self.ablation / name).read_bytes().replace(nonce[1].encode(), b'N'))
            self.assertEqual(old, new, name)
        for name in ('decode_attention_fwd_candidate.py', 'ops.py', 'opt04_gatednorm_quant.py', 'bundle_host.py',
                     'mtp_contract.py', 'FINAL-SETTINGS.json', 'extend_attention_fwd_baseline.py',
                     'extend_attention_fwd_candidate.py', 'BUNDLE-PROBES.json'):
            self.assertEqual((self.legacy / name).read_bytes(), (self.ablation / name).read_bytes(), name)

    def test_documented_edits_reproduce_generated_code_from_legacy_bytes(self):
        out = {n: (self.legacy / n).read_bytes() for n in CODE_EDITS if n.endswith('.py')}
        build.ablate_modules(out); build.ablate_adapter(out); build.ablate_owner(out)
        for name in ('bundle_runtime.py', 'bundle_readout.py', 'verify_decode_adapter.py', 'worker_entry.py'):
            self.assertEqual(out[name], (self.ablation / name).read_bytes(), name)
        # CONFIG and the bound bundle-entry pin are the only remaining derived differences.
        self.assertEqual(HEX.sub(b'H', out['serving_integration.py']),
                         HEX.sub(b'H', (self.ablation / 'serving_integration.py').read_bytes()))
        old_contract = re.sub(rb'\nCONFIG = .*\n', b'\nCONFIG\n', out['bundle_contract.py'])
        new_contract = re.sub(rb'\nCONFIG = .*\n', b'\nCONFIG\n', (self.ablation / 'bundle_contract.py').read_bytes())
        self.assertEqual(old_contract, new_contract)
        with self.assertRaises(ValueError):          # anchors are consumed: no double application
            build.ablate_adapter(out)
        drift = {n: (self.legacy / n).read_bytes() for n in ('bundle_runtime.py', 'bundle_contract.py', 'bundle_readout.py')}
        drift['bundle_runtime.py'] = drift['bundle_runtime.py'].replace(b"== 'mtp'\n", b"== 'mtp' \n", 1)
        with self.assertRaises(ValueError):
            build.ablate_modules(drift)

    def test_compiler_pins_change_only_control_attention_records(self):
        old = json.loads((self.legacy / 'BUNDLE-COMPILER-PINS.json').read_bytes())
        new = json.loads((self.ablation / 'BUNDLE-COMPILER-PINS.json').read_bytes())
        self.assertEqual(new['mtp'], old['mtp'])
        self.assertEqual({k for k in new['control'] if new['control'][k] != old['control'].get(k)},
                         {'attention-stage1', 'attention-stage2-cap16'})
        self.assertEqual(new['control']['attention-stage1'], old['mtp']['attention-stage1'])

    def run_generated(self, review, code):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in [n + '.py' for n in build.MODULES] + ['bundle_entry.py', 'verify_decode_adapter.py',
                                                              'BUNDLE-PROBES.json', 'BUNDLE-COMPILER-PINS.json']:
                (root / name).write_bytes((review / name).read_bytes())
            result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', code, str(root)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode()[-3000:])
            return json.loads(result.stdout)

    PROGRAM = r'''
import importlib.util, json, sys, types
from pathlib import Path
from unittest.mock import patch
root = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('entry', root / 'bundle_entry.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
runtime = entry.modules(); c = sys.modules['bundle_contract']; readout = sys.modules['bundle_readout']
assert not any(x in sys.modules for x in ('torch', 'triton', 'sglang'))
out = dict(has_enabled=hasattr(c, 'enabled'), arm_members=c.CONFIG.get('arm_members'), arms={})
spec = importlib.util.spec_from_file_location('verify_decode_adapter', root / 'verify_decode_adapter.py')
adapter_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter_module)
views = []
runtime.attention_function = lambda module: views.append(module) or (lambda *a, **k: ('view', module.tag))
candidate = types.SimpleNamespace(tag='prefetch', decode_attention_fwd=lambda *a, **k: None)
native = types.SimpleNamespace(tag='native')
selector = types.SimpleNamespace(get_num_kv_splits_triton='selector')
def loader(source):
    if source.get('module') == 'decode_attention_fwd_candidate': return candidate
    if source.get('module') == 'native.decode': return native
    return selector
torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_current_stream_capturing=lambda: False))
for arm in ('control', 'mtp'):
    row = {}
    if out['has_enabled']:
        row['enabled'] = {m: c.enabled(arm, m) for m in c.MEMBERS}
    runtime.STATE = types.SimpleNamespace(manifest=dict(arm=arm))
    row['wave'] = {str(m): runtime.wave_hint(m, 17408, 5120, 1) for m in (8, 16, 32)}
    with c.shadow():
        row['shadow_wave'] = runtime.wave_hint(8, 17408, 5120, 1)
    runtime.STATE = None
    manifest = dict(arm=arm, sources=dict(triton_backend={'module': 'native.backend'},
        decode_attention_fwd=dict(path='/native', module='native.decode', sha256='n')))
    cls = type('Backend', (), dict(init_forward_metadata_in_graph=lambda self, batch: None))
    with patch.object(adapter_module, 'checked_method', return_value=lambda *a: None), \
         patch.object(adapter_module, 'checked_noop', return_value=lambda *a: None), \
         patch.object(adapter_module, 'source_module', side_effect=loader):
        views.clear()
        adapter = adapter_module.Adapter(manifest, {}, cls, torch)
    backend = types.SimpleNamespace()
    row['attention_view'] = [m.tag for m in views]
    row['prefetch_loaded'] = adapter.prefetch_decode is candidate.decode_attention_fwd
    row['split_selector'] = adapter.split_selector
    row['cap'] = adapter.verification_cap(backend)
    q = types.SimpleNamespace(shape=(8, 12, 256))
    row['bf16_result'] = list(adapter.bf16_attention(backend, q))
    row['prefetch_calls'] = adapter.prefetch_calls[8]
    keys = {'attention-stage1', 'f1-t8', 'f1-t16', 'gate-up-m8-wave0', 'gate-up-m8-wave1',
            'gate-up-m16-wave0', 'gate-up-m16-wave1', 'attention-stage2-cap16', 'attention-stage2-cap32'}
    accepted = []
    records = c.CONFIG['compiler_records'][arm]
    ident = dict(arm=arm)
    for wave in ('0', '1'):
        for cap in ('16', '32'):
            for f1 in (False, True):
                selected = {'attention-stage1', 'attention-stage2-cap' + cap, 'gate-up-m8-wave' + wave,
                            'gate-up-m16-wave' + wave} | ({'f1-t8', 'f1-t16'} if f1 else set())
                if not selected <= set(records): continue
                record = dict(schema=1, identity=ident, attached=True, members=list(c.MEMBERS),
                    f1_capture={str(i): {'8': 1, '16': 1} for i in c.CONFIG['gdn_ids']},
                    gemm_capture={str(i): {'8': 1, '16': 1} for i in range(64)}, f1_fallback8=False,
                    device_api_count=32, selected_compilers={k: records[k] for k in selected},
                    numerical_qualified=False, scored_shadow_calls=0, before_external_generations=True)
                try: readout.startup(record, ident); accepted.append([wave, cap, f1])
                except ValueError: pass
    row['startup_accepts'] = accepted
    Route = runtime.route_class(type('Base', (), dict(
        eligible=lambda self, *a: True, try_project=lambda self, *a: 'fused')))
    route = Route(); route.bundle_layer = c.CONFIG['gdn_ids'][0]
    runtime.STATE = types.SimpleNamespace(manifest=dict(arm=arm), routes={route.bundle_layer: route},
        fallback8=False, capturing=lambda: False, f1_capture={}, f1_exact={str(route.bundle_layer): {}})
    batch = types.SimpleNamespace(forward_mode=types.SimpleNamespace(is_target_verify=lambda: False))
    row['f1_route_result'] = route.try_project(None, batch, None, None, (2, 8, 3072), False)
    runtime.STATE = None
    out['arms'][arm] = row
print(json.dumps(out, sort_keys=True))
'''

    def test_generated_call_sites_differ_between_arms_only_in_verification_cap(self):
        ablation = self.run_generated(self.ablation, self.PROGRAM)
        control, mtp = ablation['arms']['control'], ablation['arms']['mtp']
        self.assertEqual({m for m in control['enabled'] if control['enabled'][m] != mtp['enabled'][m]}, {'verify_cap32'})
        for row in (control, mtp):
            self.assertEqual(row['wave'], {'8': 0, '16': 0, '32': 1})
            self.assertEqual(row['shadow_wave'], 1)
            self.assertEqual(row['attention_view'], ['prefetch'])
            self.assertTrue(row['prefetch_loaded'])
            self.assertEqual(row['bf16_result'], ['view', 'prefetch'])
            self.assertEqual(row['prefetch_calls'], 1)
            self.assertEqual(row['f1_route_result'], 'fused')
        self.assertEqual((control['cap'], mtp['cap']), (16, 32))
        self.assertEqual((control['split_selector'], mtp['split_selector']), (None, 'selector'))
        self.assertEqual(control['startup_accepts'], [['0', '16', True]])
        self.assertEqual(mtp['startup_accepts'], [['0', '32', True]])
        legacy = self.run_generated(self.legacy, self.PROGRAM)
        self.assertFalse(legacy['has_enabled'])
        self.assertIsNone(legacy['arm_members'])
        old_control, old_mtp = legacy['arms']['control'], legacy['arms']['mtp']
        self.assertEqual(old_control['wave'], {'8': 1, '16': 1, '32': 1})
        self.assertEqual((old_control['attention_view'], old_control['prefetch_loaded'], old_control['cap']),
                         (['native'], False, 16))
        self.assertIsNone(old_control['f1_route_result'])
        self.assertEqual(old_control['startup_accepts'], [['1', '16', False]])
        self.assertEqual({k: v for k, v in old_mtp.items()}, {k: v for k, v in mtp.items() if k != 'enabled'})

    def witness(self, arm, prefetch=True, cap=None):
        binding = dict(mode='mode', arm=arm, session_sha256='s', spec_sha256='p', source_manifest_sha256='m',
            sources=dict(verify_decode_adapter={'sha256': 'a'}), resolved_contract=dict(context_length=131040))
        worker = dict(rank=0, pid=123, start_ticks=456, boot_id='boot', identity_sha256='w')
        counts = {'8': 16, '16': 16} if prefetch else {'8': 0, '16': 0}
        record = dict(schema=1, **{k: binding[k] for k in ('mode', 'arm', 'session_sha256', 'spec_sha256', 'source_manifest_sha256')},
            rank=0, pid=123, worker_identity_sha256='w', enabled=True, block_size=8, start_ticks=456, boot_id='boot',
            bf16_capture_calls={'8': 16, '16': 16}, prefetch_capture_calls=counts, before_external_generations=True,
            extra_tensor_operations=0, extra_model_forwards=0, qualification=False,
            adapter_source=binding['sources']['verify_decode_adapter'], eligible_host_calls=32, capture_host_calls=32,
            layers=list(range(16)), buffers=2, metadata_build_host_calls=2, metadata_reuse_host_calls=30,
            metadata_capture_build_host_calls=2, indices_elements=[8 * 131048, 16 * 131048], prefetch_calls=counts,
            verification_kernel_sha256=PREFETCH_SHA if prefetch else NATIVE_SHA,
            verification_caps=[cap or (32 if arm == 'mtp' else 16)], native_decode_caps=[16])
        return record, binding, worker

    def read(self, review, record, binding, worker):
        spec = __import__('importlib.util').util.spec_from_file_location('_ablation_reader_' + review.parent.name,
                                                                         review / 'serving_integration.py')
        module = __import__('importlib.util').util.module_from_spec(spec)
        spec.loader.exec_module(module)
        contract = types.SimpleNamespace(contract_for_arm=lambda arm: dict(enabled=True))
        with patch.object(module, '_mtp_contract', return_value=contract):
            return module.validate_full_enabled_record(record, binding, worker)

    def test_generated_route_witness_requires_prefetch_in_both_arms_and_exact_caps(self):
        for arm in ('control', 'mtp'):
            record, binding, worker = self.witness(arm)
            self.assertEqual(self.read(self.ablation, record, binding, worker), record)
            for bad in (self.witness(arm, prefetch=False), self.witness(arm, cap=48 - (32 if arm == 'mtp' else 16))):
                with self.subTest(arm=arm), self.assertRaises(ValueError):
                    self.read(self.ablation, *bad)
        # The legacy owner still refuses a prefetching control arm and accepts its native control.
        with self.assertRaises(ValueError):
            self.read(self.legacy, *self.witness('control'))
        record, binding, worker = self.witness('control', prefetch=False)
        self.assertEqual(self.read(self.legacy, record, binding, worker), record)

    def test_worker_kernel_identity_is_observed_from_adapter_selection(self):
        def expression(review):
            tree = ast.parse((review / 'worker_entry.py').read_text())
            values = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Subscript) and getattr(t.slice, 'value', None) == 'verification_kernel_sha256'
                          for t in n.targets)]
            self.assertEqual(len(values), 1)
            return ast.unparse(values[0].test)
        self.assertEqual(expression(self.ablation), 'adapter.prefetch_decode is not None')
        self.assertEqual(expression(self.legacy), "manifest['arm'] == 'mtp'")


class CompileTests(unittest.TestCase):
    def test_legacy_target_reproduced_and_ablation_changes_two_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            boot = 'cf30344d-a1d7-4350-825b-e097c8e60f2b'
            legacy = bundle_compile.prepare(root / 'legacy', evidence(), boot)
            check_legacy_preparation(self, legacy, root / 'legacy')
            self.assertNotIn('ablation', legacy)
            result = bundle_compile.prepare(root / 'ablation', evidence(), boot, ablation=build.ABLATION)
            self.assertEqual(result['ablation'], build.ABLATION)
            old = (root / 'legacy/TARGET.py').read_text().splitlines()
            new = (root / 'ablation/TARGET.py').read_text().splitlines()
            changed = [(a, b) for a, b in zip(old, new) if a != b]
            self.assertEqual(len(old), len(new))
            self.assertEqual(len(changed), 2)
            self.assertIn("'attention_candidate','decode_attention_fwd_candidate','/mnt/review/decode_attention_fwd_candidate.py','C'", changed[0][1])
            self.assertIn("ablation='verify_cap32_single_variable'", changed[1][1])
            compile((root / 'ablation/TARGET.py').read_bytes(), 'TARGET.py', 'exec', dont_inherit=True)
            with self.assertRaises(AssertionError):
                bundle_compile.prepare(root / 'unknown', evidence(), boot, ablation='other')
        body = (HERE / 'bundle_compile_target.py').read_bytes()
        with self.assertRaises(AssertionError):
            bundle_compile.ablation_body(bundle_compile.ablation_body(body))

    def receipt(self, root, report, resources):
        artifacts = {}
        pins = report['pins']
        for key in report['artifacts_keys']:
            arm, name = (key.split('-', 1) if key.startswith(('mtp-', 'control-')) else ('mtp', key))
            parts, blobs = {}, {}
            vgpr, sgpr, shared = resources[key]
            for part in PARTS:
                if part == 'ttir':
                    raw = ('loc(' + json.dumps(report['serving'][key]['source_path']) + ':1:1)').encode()
                elif part == 'amdgcn':
                    raw = ''.join('  .%s: %d\n' % (f, v) for f, v in (
                        ('sgpr_count', sgpr), ('vgpr_count', vgpr), ('sgpr_spill_count', 0), ('vgpr_spill_count', 0),
                        ('private_segment_fixed_size', 0), ('group_segment_fixed_size', 0), ('wavefront_size', 32),
                        ('max_flat_workgroup_size', 128))).encode()
                else:
                    raw = (key + part).encode()
                parts[part] = dict(bytes=len(raw), sha256=sha(raw))
                blobs[part] = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
            pins.setdefault(arm, {})
            for target in ((('control', 'mtp') if not key.startswith(('mtp-', 'control-')) else (arm,))):
                pins.setdefault(target, {})[name] = dict(parts=parts, metadata=dict(shared=shared))
            artifacts[key] = blobs
        value = dict(status='CPU_SERVING_COMPILERS_PROVEN', gpu_launches=0, model_starts=0, device_nodes=False,
                     calibration_complete_before_serving=True, compiled_objects_unloaded=True,
                     original_modules_and_linecache_restored=True, fresh_codegen_for_every_compile=True,
                     artifact_source_paths_verified=True, artifacts=artifacts, pins=pins, serving=report['serving'])
        if report.get('ablation'):
            value['ablation'] = report['ablation']
        (root / 'proof').mkdir()
        (root / 'proof/RECIPE.py').write_bytes(b'recipe')
        stdout = json.dumps(dict(returncode=0, stderr='', stdout=json.dumps(value))).encode()
        (root / 'run.stdout').write_bytes(stdout)
        (root / 'run.json').write_text(json.dumps(dict(exit_code=0, child_reaped=True, owned_group_absent=True,
            stdin_sha256=sha(b'recipe'), stdout=dict(bytes=len(stdout), sha256=sha(stdout)))))

    def reader(self, ablation, control_stage1):
        keys = ['gate-up-m8-wave0', 'gate-up-m8-wave1', 'gate-up-m16-wave0', 'gate-up-m16-wave1', 'f1-t8', 'f1-t16',
                'control-attention-stage1', 'control-attention-stage2-cap16', 'mtp-attention-stage1',
                'mtp-attention-stage2-cap32']
        resources = {k: ((90, 41, 4096) if k.endswith('wave0') else (109, 41, 4096) if k.startswith('gate-up') else
                         (62, 31, 0) if k.startswith('f1') else (236, 100, 16384) if k == 'mtp-attention-stage1' else
                         control_stage1 if k == 'control-attention-stage1' else (13, 33, 0)) for k in keys}
        path = build.CANDIDATE_ATTENTION if ablation else '/mnt/composition/native.py'
        serving = {k: dict(source_path=(build.CANDIDATE_ATTENTION if k.startswith('mtp-attention') else
                                        path if k.startswith('control-attention') else '/mnt/review/k.py')) for k in keys}
        report = dict(pins={}, serving=serving, artifacts_keys=keys, ablation=build.ABLATION if ablation else None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.receipt(root, report, resources)
            return bundle_compile_read.extract(root, 'run')

    def test_reader_resource_contract_switches_only_for_declared_ablation(self):
        self.assertEqual(self.reader(False, (245, 77, 16384))['binaries'], 10)
        self.assertEqual(self.reader(True, (236, 100, 16384))['binaries'], 10)
        for ablation, stage1 in ((False, (236, 100, 16384)), (True, (245, 77, 16384))):
            with self.subTest(ablation=ablation), self.assertRaises(AssertionError):
                self.reader(ablation, stage1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
