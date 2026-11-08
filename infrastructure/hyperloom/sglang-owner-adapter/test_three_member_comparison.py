"""CPU tests of the opt-in three-member cap16 serving comparison.

Control is the selected baseline (native verification attention, original norm/
quantizer, gate/up wave1); the candidate adds verification prefetch, F1 and gate/up
wave0. Both arms keep private verification cap16. Inputs:

EVIDENCE           private optimization-continuation evidence root (read only)
FROZEN_INPUTS      frozen four-member owner inputs (legacy reproduction)
LEGACY_AUTHOR      inert build of FROZEN_INPUTS by this checkout's builder
COMPARISON_AUTHOR  inert comparison build (test fixture or the real packet author)
COMPARISON_PROOF   fresh native comparison compiler root (ProofAuthenticityTests only)

Nothing here imports a numerical framework, starts a model or launches a kernel.
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
KERNELS_SHA = '5571b10654697561455081455b6cd28d43314702f56c80160ef2e33e535b3693'
FUSED_SHA = '3fe82935dd41b0647b1589dd5ff0e47542af1f58d76ff2f84b87509853ebbf9e'
PARTS = ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')
CODE_EDITS = {'bundle_contract.py', 'bundle_runtime.py', 'bundle_readout.py', 'verify_decode_adapter.py',
              'worker_entry.py', 'serving_integration.py', 'BUNDLE-COMPILER-PINS.json'}
DIGEST_ONLY = {'bundle_entry.py', 'serving_runtime.py', 'HTTP-BINDING-control.json', 'HTTP-BINDING-mtp.json',
               'BENCHMARK-SPEC2.json', 'benchmark_physical_controller.py', 'RESOURCE-MANIFEST.json'}
# Nonces of executed or prepared historical four-member packets. A fresh packet
# must never reuse one of them.
CONSUMED_NONCES = {'a4777122b5277d13', 'ac6c108f39cbae77', '37a950031a7b7ff7', '5a66cd00a218b2cb'}
HEX = re.compile(rb'[0-9a-f]{16,64}')
CAP16_KEYS = {'gate-up-m8-wave0', 'gate-up-m8-wave1', 'gate-up-m16-wave0', 'gate-up-m16-wave1', 'f1-t8', 'f1-t16',
              'control-attention-stage1', 'control-attention-stage2-cap16', 'mtp-attention-stage1',
              'mtp-attention-stage2-cap16'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def evidence():
    return Path(os.environ['EVIDENCE']).resolve()


def cold():
    return evidence() / 'four-member-serving-qualification-20261104-v1/compiler-cold-proof-v1'


def legacy_pins():
    path = cold() / 'BUNDLE-COMPILER-PINS.json'
    raw = path.read_bytes()
    assert sha(raw) == LEGACY_PINS_SHA
    return path, json.loads(raw)


COLD_PROOF = cold


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
    """Explicitly synthetic candidate cap16 stage 2; never a native proof."""
    serving_old = json.loads((cold() / 'COMPILE-RESULT.json').read_bytes())['serving']
    pins = copy.deepcopy(pins_legacy)
    pins['mtp'].pop('attention-stage2-cap32')
    record = copy.deepcopy(pins['control']['attention-stage2-cap16'])
    for part in PARTS:
        record['parts'][part] = dict(bytes=9, sha256=sha(b'TEST-ONLY' + part.encode()))
    pins['mtp']['attention-stage2-cap16'] = record
    serving = {k: v for k, v in copy.deepcopy(serving_old).items() if k != 'mtp-attention-stage2-cap32'}
    serving['mtp-attention-stage2-cap16'] = dict(copy.deepcopy(serving_old['control-attention-stage2-cap16']),
                                                 source_path=build.CANDIDATE_ATTENTION)
    report = dict(status='CPU_SERVING_COMPILERS_PROVEN', comparison=build.COMPARISON, gpu_launches=0,
                  model_starts=0, fresh_codegen_for_every_compile=True, artifact_source_paths_verified=True,
                  pins=pins, serving=serving, test_fixture_only=True)
    return pins, report


def contract(pins, report, legacy_path, legacy_sha=LEGACY_PINS_SHA, **extra):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp).resolve() / 'REPORT.json'
        path.write_bytes(canonical(report))
        value = dict(kind=build.COMPARISON, compiler_report=dict(path=str(path), sha256=sha(path.read_bytes())),
                     legacy_compiler_pins=dict(path=str(legacy_path), sha256=legacy_sha), **extra)
        return build.comparison_contract(value, pins)


class ContractTests(unittest.TestCase):
    def test_bound_fixture_accepted_and_every_identity_mutation_refused(self):
        path, old = legacy_pins()
        pins, report = fixture(old)
        result = contract(pins, report, path)
        self.assertEqual(result['arm_members'], {'control': [], 'mtp': ['verify_prefetch', 'gatednorm_quant', 'gate_up_wave']})
        self.assertEqual((result['control_verification_cap'], result['candidate_verification_cap']), (16, 16))
        self.assertFalse(result['cap32_member'] or result['unique_member_causality'])
        self.assertEqual(result['new_compiler_records'], ['mtp/attention-stage2-cap16'])

        def refused(pins_value, report_value, **kw):
            with self.assertRaises((ValueError, KeyError, TypeError)):
                contract(pins_value, report_value, path, **kw)

        def mutated(change):
            value = copy.deepcopy(pins)
            change(value)
            return value, dict(copy.deepcopy(report), pins=value)

        refused(pins, report, unexpected=1)
        refused(pins, dict(report, status='CPU_COMPILE_REFUSED'))
        refused(pins, {k: v for k, v in report.items() if k != 'comparison'})
        refused(pins, dict(report, comparison=build.ABLATION))
        refused(pins, dict(report, ablation=build.ABLATION))
        refused(pins, dict(report, gpu_launches=1))
        refused(pins, dict(report, fresh_codegen_for_every_compile=False))
        refused(pins, dict(report, artifact_source_paths_verified=False))
        refused(old, report)                                       # report does not bind these pins
        refused(*mutated(lambda p: p['control'].update({'attention-stage1': p['mtp']['attention-stage1']})))
        refused(*mutated(lambda p: p['control'].update({'gate-up-m8-wave1': p['control']['gate-up-m8-wave0']})))
        refused(*mutated(lambda p: p['mtp'].update({'gate-up-m16-wave0': p['mtp']['gate-up-m16-wave1']})))
        refused(*mutated(lambda p: p['mtp'].update({'attention-stage1': p['control']['attention-stage1']})))
        refused(*mutated(lambda p: p['mtp']['f1-t8']['parts'].update(ttir=dict(bytes=1, sha256='0' * 64))))
        refused(*mutated(lambda p: p['mtp'].update({'attention-stage2-cap16': p['control']['attention-stage2-cap16']})))
        refused(*mutated(lambda p: p['mtp'].update({'attention-stage2-cap16': old['mtp']['attention-stage2-cap32']})))
        refused(*mutated(lambda p: p['mtp']['attention-stage2-cap16']['metadata'].update(num_warps=8)))
        refused(*mutated(lambda p: p['mtp'].update({'attention-stage2-cap32': old['mtp']['attention-stage2-cap32']})))
        refused(*mutated(lambda p: p['mtp'].pop('attention-stage2-cap16')))
        refused(*mutated(lambda p: p['control'].pop('f1-t8')))
        for key, path_value in (('control-attention-stage1', build.CANDIDATE_ATTENTION),
                                ('control-attention-stage2-cap16', build.CANDIDATE_ATTENTION),
                                ('mtp-attention-stage1', build.NATIVE_ATTENTION),
                                ('mtp-attention-stage2-cap16', build.NATIVE_ATTENTION)):
            bad = copy.deepcopy(report)
            bad['serving'][key]['source_path'] = path_value
            refused(pins, bad)
        bad = copy.deepcopy(report)
        bad['serving']['mtp-attention-stage2-cap32'] = bad['serving']['mtp-attention-stage2-cap16']
        refused(pins, bad)
        serving_old = json.loads((cold() / 'COMPILE-RESULT.json').read_bytes())['serving']
        bad = copy.deepcopy(report)          # candidate stage 2 compiled with the cap32 row
        bad['serving']['mtp-attention-stage2-cap16'] = dict(serving_old['mtp-attention-stage2-cap32'])
        refused(pins, bad)
        with self.assertRaises(ValueError):
            contract(pins, report, path, legacy_sha='0' * 64)
        value = dict(kind=build.ABLATION, compiler_report=dict(path='/x', sha256='0' * 64),
                     legacy_compiler_pins=dict(path=str(path), sha256=LEGACY_PINS_SHA))
        with self.assertRaises(ValueError):
            build.comparison_contract(value, pins)


class GeneratedTreeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = Path(os.environ['LEGACY_AUTHOR']).resolve() / 'assembled-review'
        cls.candidate = Path(os.environ['COMPARISON_AUTHOR']).resolve() / 'assembled-review'
        cls.ids = json.loads((cls.candidate.parent / 'PACKAGE-IDENTITIES.json').read_bytes())
        cls.legacy_ids = json.loads((cls.legacy.parent / 'PACKAGE-IDENTITIES.json').read_bytes())

    def test_legacy_default_reproduced_and_comparison_identity(self):
        frozen = json.loads(Path(os.environ['FROZEN_INPUTS']).read_bytes())['frozen_packet']
        self.assertEqual(self.legacy_ids['nonce'], frozen['nonce'])
        for key, pin in frozen['pins'].items():
            self.assertEqual(self.legacy_ids[key], pin, key)
        self.assertNotIn('comparison', self.legacy_ids['four_member'])
        self.assertNotIn('ablation', self.legacy_ids['four_member'])
        self.assertEqual(self.legacy_ids['four_member']['candidate_verification_cap'], 32)
        bundle = self.ids['four_member']
        self.assertNotIn(self.ids['nonce'], CONSUMED_NONCES)
        self.assertEqual(bundle['members'], ['verify_prefetch', 'verify_cap32', 'gatednorm_quant', 'gate_up_wave'])
        self.assertEqual(bundle['comparison']['kind'], build.COMPARISON)
        self.assertEqual(bundle['comparison']['arm_members'], build.COMPARISON_MEMBERS)
        self.assertEqual((bundle['ordinary_decode_cap'], bundle['candidate_verification_cap']), (16, 16))
        self.assertFalse(bundle['comparison']['cap32_member'] or bundle['comparison']['unique_member_causality'])
        for key in ('model_starts', 'auxiliary_requests_per_start', 'benchmark_menu_requests_per_start',
                    'scored_requests_per_start', 'kv_capacity_unchanged', 'session_max_bytes', 'http_export_bounds',
                    'native_tree_bytes', 'encoded_native_bytes', 'additional_package_members'):
            self.assertEqual(bundle[key], self.legacy_ids['four_member'][key], key)
        for key in ('requests_per_start', 'starts', 'caller_seconds', 'owner_seconds', 'workload_sha256',
                    'baseline_sha256', 'candidate_sha256', 'mamba_max_states_per_path', 'kind'):
            self.assertEqual(self.ids[key], self.legacy_ids[key], key)
        self.assertEqual(self.ids['runtime_config_deviation']['bindings'].keys(), {'control', 'mtp'})
        self.assertFalse(self.ids['launch_authorized'] or self.ids['qualification'] or self.ids['adoption_authorized'])
        self.assertLessEqual(self.ids['staged_bytes'], 32 << 20)

    def test_review_trees_differ_only_in_declared_edits_and_derived_digests(self):
        names = sorted(p.name for p in self.legacy.iterdir())
        self.assertEqual(names, sorted(p.name for p in self.candidate.iterdir()))
        nonce = (self.legacy_ids['nonce'], self.ids['nonce'])
        changed = {n for n in names if (self.legacy / n).read_bytes() != (self.candidate / n).read_bytes()}
        self.assertEqual(changed, CODE_EDITS | DIGEST_ONLY)
        for name in DIGEST_ONLY:
            old = HEX.sub(b'H', (self.legacy / name).read_bytes().replace(nonce[0].encode(), b'N'))
            new = HEX.sub(b'H', (self.candidate / name).read_bytes().replace(nonce[1].encode(), b'N'))
            self.assertEqual(old, new, name)
        for name in ('decode_attention_fwd_candidate.py', 'ops.py', 'opt04_gatednorm_quant.py', 'bundle_host.py',
                     'mtp_contract.py', 'FINAL-SETTINGS.json', 'extend_attention_fwd_baseline.py',
                     'extend_attention_fwd_candidate.py', 'BUNDLE-PROBES.json', 'qwen3_5_candidate.py',
                     'd3b_combined_model_runner.py', 'isolated_scheme.py', 'benchmark_artifacts.py'):
            self.assertEqual((self.legacy / name).read_bytes(), (self.candidate / name).read_bytes(), name)
        for arm in ('control', 'mtp'):
            old = json.loads((self.legacy / ('HTTP-BINDING-' + arm + '.json')).read_bytes())['binding_template']
            new = json.loads((self.candidate / ('HTTP-BINDING-' + arm + '.json')).read_bytes())['binding_template']
            for key in ('canonical_server_cli', 'resolved_contract'):
                self.assertEqual(old[key], new[key], (arm, key))

    def test_documented_edits_reproduce_generated_code_from_legacy_bytes(self):
        out = {n: (self.legacy / n).read_bytes() for n in CODE_EDITS if n.endswith('.py')}
        build.ablate_modules(out)
        build.ablate_adapter(out, 'The three-member cap16 comparison')
        build.compare_owner(out)
        for name in ('bundle_runtime.py', 'bundle_readout.py', 'verify_decode_adapter.py', 'worker_entry.py'):
            self.assertEqual(out[name], (self.candidate / name).read_bytes(), name)
        self.assertEqual(HEX.sub(b'H', out['serving_integration.py']),
                         HEX.sub(b'H', (self.candidate / 'serving_integration.py').read_bytes()))
        old_contract = re.sub(rb'\nCONFIG = .*\n', b'\nCONFIG\n', out['bundle_contract.py'])
        new_contract = re.sub(rb'\nCONFIG = .*\n', b'\nCONFIG\n', (self.candidate / 'bundle_contract.py').read_bytes())
        self.assertEqual(old_contract, new_contract)
        with self.assertRaises(ValueError):
            build.compare_owner(out)                    # anchors are consumed: no double application
        drift = {n: (self.legacy / n).read_bytes() for n in ('worker_entry.py', 'serving_integration.py')}
        drift['serving_integration.py'] = drift['serving_integration.py'].replace(b"([32] if binding", b"([32]  if binding")
        with self.assertRaises(ValueError):
            build.compare_owner(drift)

    def test_compiler_pins_change_only_candidate_stage2(self):
        old = json.loads((self.legacy / 'BUNDLE-COMPILER-PINS.json').read_bytes())
        new = json.loads((self.candidate / 'BUNDLE-COMPILER-PINS.json').read_bytes())
        self.assertEqual(new['control'], old['control'])
        self.assertEqual(set(new['mtp']), set(old['mtp']) - {'attention-stage2-cap32'} | {'attention-stage2-cap16'})
        self.assertEqual({k: v for k, v in new['mtp'].items() if k != 'attention-stage2-cap16'},
                         {k: v for k, v in old['mtp'].items() if k != 'attention-stage2-cap32'})
        self.assertNotEqual(new['mtp']['attention-stage2-cap16'], new['control']['attention-stage2-cap16'])
        self.assertEqual(new['mtp']['attention-stage2-cap16']['metadata'], new['control']['attention-stage2-cap16']['metadata'])

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

    # Executes the generated bundle modules and adapter call sites on CPU stubs.
    PROGRAM = r'''
import importlib.util, json, sys, types
from pathlib import Path
from unittest.mock import patch
root = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('entry', root / 'bundle_entry.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
runtime = entry.modules(); c = sys.modules['bundle_contract']; readout = sys.modules['bundle_readout']
assert not any(x in sys.modules for x in ('torch', 'triton', 'sglang'))
out = dict(has_enabled=hasattr(c, 'enabled'), arm_members=c.CONFIG.get('arm_members'),
           comparison=c.CONFIG.get('comparison'), arms={})
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
    row['attention_view'] = [m.tag for m in views]
    row['prefetch_loaded'] = adapter.prefetch_decode is candidate.decode_attention_fwd
    row['split_selector'] = adapter.split_selector
    row['cap'] = adapter.verification_cap(types.SimpleNamespace())
    row['fp8_target_cap'] = adapter.verification_cap(types.SimpleNamespace(bf16_query_fp8_target_kv=True))
    q = types.SimpleNamespace(shape=(8, 12, 256))
    row['bf16_result'] = list(adapter.bf16_attention(types.SimpleNamespace(), q))
    row['prefetch_calls'] = adapter.prefetch_calls[8]
    records = c.CONFIG['compiler_records'][arm]
    ident = dict(arm=arm)
    accepted = []
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
    row['record_keys'] = sorted(records)
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

    def test_generated_call_sites_select_actual_per_arm_members(self):
        new = self.run_generated(self.candidate, self.PROGRAM)
        self.assertEqual(new['comparison'], build.COMPARISON)
        control, mtp = new['arms']['control'], new['arms']['mtp']
        self.assertEqual(control['enabled'], dict.fromkeys(control['enabled'], False))
        self.assertEqual(mtp['enabled'], {'verify_prefetch': True, 'verify_cap32': False,
                                          'gatednorm_quant': True, 'gate_up_wave': True})
        self.assertEqual((control['wave'], control['shadow_wave']), ({'8': 1, '16': 1, '32': 1}, 0))
        self.assertEqual((mtp['wave'], mtp['shadow_wave']), ({'8': 0, '16': 0, '32': 1}, 1))
        self.assertEqual((control['attention_view'], control['prefetch_loaded'], control['bf16_result'],
                          control['prefetch_calls']), (['native'], False, ['view', 'native'], 0))
        self.assertEqual((mtp['attention_view'], mtp['prefetch_loaded'], mtp['bf16_result'], mtp['prefetch_calls']),
                         (['prefetch'], True, ['view', 'prefetch'], 1))
        for row in (control, mtp):
            self.assertEqual((row['cap'], row['fp8_target_cap'], row['split_selector']), (16, 16, None))
        self.assertIsNone(control['f1_route_result'])
        self.assertEqual(mtp['f1_route_result'], 'fused')
        self.assertEqual(control['startup_accepts'], [['1', '16', False]])
        self.assertEqual(mtp['startup_accepts'], [['0', '16', True]])
        self.assertNotIn('attention-stage2-cap32', mtp['record_keys'])
        legacy = self.run_generated(self.legacy, self.PROGRAM)
        self.assertFalse(legacy['has_enabled'])
        self.assertIsNone(legacy['arm_members'])
        old_control, old_mtp = legacy['arms']['control'], legacy['arms']['mtp']
        self.assertEqual(old_control, {k: v for k, v in control.items() if k != 'enabled'})
        self.assertEqual((old_mtp['cap'], old_mtp['split_selector'], old_mtp['startup_accepts']),
                         (32, 'selector', [['0', '32', True]]))
        self.assertEqual({k: old_mtp[k] for k in ('wave', 'shadow_wave', 'attention_view', 'prefetch_loaded',
                                                   'bf16_result', 'prefetch_calls', 'f1_route_result')},
                         {k: mtp[k] for k in ('wave', 'shadow_wave', 'attention_view', 'prefetch_loaded',
                                              'bf16_result', 'prefetch_calls', 'f1_route_result')})

    # Generated startup producer -> startup reader, and real-input numerics reader, per arm.
    STARTUP = r'''
import importlib.util, json, sys, types
from pathlib import Path
root = Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location('entry', root / 'bundle_entry.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
runtime = entry.modules(); c = sys.modules['bundle_contract']; readout = sys.modules['bundle_readout']
out = {}
for arm in ('control', 'mtp'):
    for fallback in (False, True):
        ident = dict(arm=arm, rank=0)
        records = json.loads(json.dumps(c.CONFIG['compiler_records'][arm]))
        f1 = {str(i): {'8': 0 if fallback else 1, '16': 1} for i in c.CONFIG['gdn_ids']}
        runtime.STATE = types.SimpleNamespace(attached=True, capturing=lambda: False, worker={'rank': 0},
            torch=types.SimpleNamespace(cuda=types.SimpleNamespace(current_device=lambda: 0)),
            gemm_capture={str(i): {'8': 1, '16': 1} for i in range(64)}, f1_capture=f1, fallback8=fallback,
            manifest={'arm': arm}, records=records, identity=ident, device_count=32)
        record = json.loads(json.dumps(runtime.startup_record()))
        runtime.STATE = None
        readout.startup(record, ident)
        row = dict(selected=sorted(record['selected_compilers']),
                   exact=all(record['selected_compilers'][k] == records[k] for k in record['selected_compilers']))
        compilers = {k: records[k] for k in records if k.startswith('gate-up-') or k == 'f1-t16' or (k == 'f1-t8' and not fallback)}
        numerics = dict(schema=1, identity=ident, status='F1_GEMM_REAL_INPUT_EXACT', members=list(c.MEMBERS),
            f1={str(i): ({'16': 1} if fallback else {'8': 1, '16': 1}) for i in c.CONFIG['gdn_ids']},
            gemm={str(i): {'8': 1, '16': 1} for i in range(64)}, f1_fallback8=fallback, device_api_count=32,
            probe_ids=c.CONFIG['probe_ids'], scored=False, shadow_calls=1, capture_shadow_calls=0, compilers=compilers,
            completed_monotonic=1.0, exact_members=['gatednorm_quant', 'gate_up_wave'])
        c.validate_numerics(numerics, ident, c.CONFIG)
        refused = []
        for name, value in (('foreign-cap32', dict(compilers, **{'attention-stage2-cap32': records.get('attention-stage2-cap16') or records['attention-stage2-cap32']})),
                            ('drifted-wave', dict(compilers, **{'gate-up-m8-wave0': records['gate-up-m8-wave1']}))):
            try: c.validate_numerics(dict(numerics, compilers=value), ident, c.CONFIG)
            except ValueError: refused.append(name)
        row['numerics_refused'] = refused
        out[arm + ('-fallback8' if fallback else '')] = row
print(json.dumps(out, sort_keys=True))
'''

    def test_startup_producer_reader_and_numerics_per_arm(self):
        new = self.run_generated(self.candidate, self.STARTUP)
        base = ['attention-stage1', 'attention-stage2-cap16']
        self.assertEqual(new['control']['selected'], base + ['gate-up-m16-wave1', 'gate-up-m8-wave1'])
        self.assertEqual(new['control-fallback8']['selected'], new['control']['selected'])
        self.assertEqual(new['mtp']['selected'], base + ['f1-t16', 'f1-t8', 'gate-up-m16-wave0', 'gate-up-m8-wave0'])
        self.assertEqual(new['mtp-fallback8']['selected'], base + ['f1-t16', 'gate-up-m16-wave0', 'gate-up-m8-wave0'])
        for row in new.values():
            self.assertTrue(row['exact'])
            self.assertEqual(row['numerics_refused'], ['foreign-cap32', 'drifted-wave'])
        old = self.run_generated(self.legacy, self.STARTUP)
        self.assertEqual(old['control'], new['control'])
        self.assertEqual(old['mtp']['selected'], ['attention-stage1', 'attention-stage2-cap32', 'f1-t16', 'f1-t8',
                                                  'gate-up-m16-wave0', 'gate-up-m8-wave0'])

    def witness(self, arm, prefetch, cap):
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
            verification_caps=[cap], native_decode_caps=[16])
        return record, binding, worker

    def read(self, review, record, binding, worker):
        import importlib.util
        spec = importlib.util.spec_from_file_location('_comparison_reader_' + review.parent.name,
                                                      review / 'serving_integration.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        contract_value = types.SimpleNamespace(contract_for_arm=lambda arm: dict(enabled=True))
        with patch.object(module, '_mtp_contract', return_value=contract_value):
            return module.validate_full_enabled_record(record, binding, worker)

    def test_generated_route_witness_reader_requires_actual_per_arm_selection(self):
        accepted = (('control', False, 16), ('mtp', True, 16))
        for arm, prefetch, cap in accepted:
            record, binding, worker = self.witness(arm, prefetch, cap)
            self.assertEqual(self.read(self.candidate, record, binding, worker), record)
        for arm, prefetch, cap in (('control', True, 16), ('control', False, 32), ('mtp', True, 32),
                                   ('mtp', False, 16), ('mtp', False, 32)):
            with self.subTest(arm=arm, prefetch=prefetch, cap=cap), self.assertRaises(ValueError):
                self.read(self.candidate, *self.witness(arm, prefetch, cap))
        record, binding, worker = self.witness('mtp', True, 32)          # op224's candidate witness
        self.assertEqual(self.read(self.legacy, record, binding, worker), record)
        with self.assertRaises(ValueError):
            self.read(self.legacy, *self.witness('mtp', True, 16))
        record, binding, worker = self.witness('control', False, 16)
        self.assertEqual(self.read(self.legacy, record, binding, worker), record)

    def test_worker_kernel_identity_is_observed_from_adapter_selection(self):
        def expression(review):
            tree = ast.parse((review / 'worker_entry.py').read_text())
            values = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and
                      any(isinstance(t, ast.Subscript) and getattr(t.slice, 'value', None) == 'verification_kernel_sha256'
                          for t in n.targets)]
            self.assertEqual(len(values), 1)
            return ast.unparse(values[0].test)
        self.assertEqual(expression(self.candidate), 'adapter.prefetch_decode is not None')
        self.assertEqual(expression(self.legacy), "manifest['arm'] == 'mtp'")

    def test_live_archive_complete_without_fixture_members(self):
        import tarfile
        manifest = json.loads((self.candidate.parent / 'package-v2/MANIFEST.json').read_bytes())
        with tarfile.open(self.candidate.parent / 'package-v2/payload.tar') as archive:
            members = archive.getmembers()
            self.assertEqual(len(members), 93)
            for member in members:
                raw = archive.extractfile(member).read()
                self.assertEqual(raw, (self.candidate.parent / 'package-v2' / member.name).read_bytes())
                if member.name != 'MANIFEST.json':
                    self.assertEqual(sha(raw), manifest['package_files'][member.name])
        self.assertFalse(any('fixture' in n.lower() or Path(n).name.startswith('test_') for n in manifest['package_files']))
        review = self.candidate.parent / 'package-v2/review'
        for name in os.listdir(review):
            self.assertEqual((review / name).read_bytes(), (self.candidate / name).read_bytes(), name)


class CompileTests(unittest.TestCase):
    def test_legacy_target_reproduced_and_comparison_changes_two_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            boot = 'cf30344d-a1d7-4350-825b-e097c8e60f2b'
            legacy = bundle_compile.prepare(root / 'legacy', evidence(), boot)
            check_legacy_preparation(self, legacy, root / 'legacy')
            self.assertNotIn('comparison', legacy)
            result = bundle_compile.prepare(root / 'comparison', evidence(), boot, comparison=build.COMPARISON)
            self.assertEqual(result['comparison'], build.COMPARISON)
            old = (root / 'legacy/TARGET.py').read_text().splitlines()
            new = (root / 'comparison/TARGET.py').read_text().splitlines()
            changed = [(a, b) for a, b in zip(old, new) if a != b]
            self.assertEqual(len(old), len(new))
            self.assertEqual(changed, [("            cap=16 if arm=='control' else 32", '            cap=16'),
                (changed[1][0], changed[1][0].replace("status='CPU_SERVING_COMPILERS_PROVEN',",
                    "status='CPU_SERVING_COMPILERS_PROVEN',comparison='three_member_cap16_serving',"))])
            self.assertEqual((root / 'legacy/INPUTS.json').read_bytes(), (root / 'comparison/INPUTS.json').read_bytes())
            compile((root / 'comparison/TARGET.py').read_bytes(), 'TARGET.py', 'exec', dont_inherit=True)
            for kwargs in (dict(comparison='other'), dict(ablation=build.ABLATION, comparison=build.COMPARISON)):
                with self.subTest(**kwargs), self.assertRaises(AssertionError):
                    bundle_compile.prepare(root / 'refused', evidence(), boot, **kwargs)
                self.assertFalse((root / 'refused').exists())
        body = (HERE / 'bundle_compile_target.py').read_bytes()
        with self.assertRaises(AssertionError):
            bundle_compile.comparison_body(bundle_compile.comparison_body(body))
        with self.assertRaises(AssertionError):
            bundle_compile.comparison_body(bundle_compile.ablation_body(body))

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
            for target in ((('control', 'mtp') if not key.startswith(('mtp-', 'control-')) else (arm,))):
                pins.setdefault(target, {})[name] = dict(parts=parts, metadata=dict(shared=shared))
            artifacts[key] = blobs
        value = dict(status='CPU_SERVING_COMPILERS_PROVEN', gpu_launches=0, model_starts=0, device_nodes=False,
                     calibration_complete_before_serving=True, compiled_objects_unloaded=True,
                     original_modules_and_linecache_restored=True, fresh_codegen_for_every_compile=True,
                     artifact_source_paths_verified=True, artifacts=artifacts, pins=pins, serving=report['serving'])
        for marker in ('comparison', 'ablation'):
            if report.get(marker):
                value[marker] = report[marker]
        (root / 'proof').mkdir()
        (root / 'proof/RECIPE.py').write_bytes(b'recipe')
        stdout = json.dumps(dict(returncode=0, stderr='', stdout=json.dumps(value))).encode()
        (root / 'run.stdout').write_bytes(stdout)
        (root / 'run.json').write_text(json.dumps(dict(exit_code=0, child_reaped=True, owned_group_absent=True,
            stdin_sha256=sha(b'recipe'), stdout=dict(bytes=len(stdout), sha256=sha(stdout)))))

    def reader(self, keys=None, paths=None, stage2=(13, 33, 0), **markers):
        keys = sorted(CAP16_KEYS) if keys is None else keys
        resources = {k: ((90, 41, 4096) if k.endswith('wave0') else (109, 41, 4096) if k.startswith('gate-up') else
                         (62, 31, 0) if k.startswith('f1') else (236, 100, 16384) if k == 'mtp-attention-stage1' else
                         (245, 77, 16384) if k == 'control-attention-stage1' else
                         stage2 if k == 'mtp-attention-stage2-cap16' else (13, 33, 0)) for k in keys}
        paths = dict(dict(control=build.NATIVE_ATTENTION, mtp=build.CANDIDATE_ATTENTION), **(paths or {}))
        serving = {k: dict(source_path=(paths['mtp'] if k.startswith('mtp-attention') else
                                        paths['control'] if k.startswith('control-attention') else '/mnt/review/k.py'))
                   for k in keys}
        report = dict(pins={}, serving=serving, artifacts_keys=keys,
                      **({'comparison': build.COMPARISON} if not markers else markers))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.receipt(root, report, resources)
            return bundle_compile_read.extract(root, 'run')

    def test_reader_accepts_comparison_and_refuses_wrong_sources_records_or_resources(self):
        result = self.reader()
        self.assertEqual((result['binaries'], result['comparison']), (10, build.COMPARISON))
        cap32 = sorted(CAP16_KEYS - {'mtp-attention-stage2-cap16'} | {'mtp-attention-stage2-cap32'})
        for kwargs in (dict(keys=cap32), dict(paths=dict(control=build.CANDIDATE_ATTENTION)),
                       dict(paths=dict(mtp=build.NATIVE_ATTENTION)), dict(stage2=(14, 33, 0)),
                       dict(comparison=build.COMPARISON, ablation=build.ABLATION), dict(comparison='other')):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}), self.assertRaises(AssertionError):
                self.reader(**kwargs)


class ProofAuthenticityTests(unittest.TestCase):
    """The fresh native comparison proof: real records, pinned recipe, exact origins."""
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ['COMPARISON_PROOF']).resolve()
        cls.report = json.loads((cls.root / 'COMPILE-RESULT.json').read_bytes())
        cls.pins_raw = (cls.root / 'BUNDLE-COMPILER-PINS.json').read_bytes()
        cls.preparation = json.loads((cls.root / 'PREPARATION.json').read_bytes())

    def test_native_report_binds_real_records_and_reproduces_op224(self):
        path, old = legacy_pins()
        pins = json.loads(self.pins_raw)
        self.assertEqual(canonical(self.report['pins']), self.pins_raw)
        result = contract(pins, self.report, path)
        self.assertEqual(result['new_compiler_records'], ['mtp/attention-stage2-cap16'])
        self.assertNotIn(b'TEST-ONLY', self.pins_raw)
        self.assertNotIn('test_fixture_only', self.report)
        self.assertEqual(pins['control'], old['control'])
        self.assertEqual({k: v for k, v in pins['mtp'].items() if k != 'attention-stage2-cap16'},
                         {k: v for k, v in old['mtp'].items() if k != 'attention-stage2-cap32'})
        self.assertEqual(self.report['calibration'], ['gemm-8-baseline', 'gemm-8-candidate', 'gemm-16-baseline',
            'gemm-16-candidate', 'f1-8', 'f1-16', 'attention-stock-prefetch-cap16-cap32'])
        for key in ('calibration_complete_before_serving', 'compiled_objects_unloaded', 'consumed_sources_unchanged',
                    'original_modules_and_linecache_restored', 'fresh_codegen_for_every_compile',
                    'artifact_source_paths_verified'):
            self.assertIs(self.report[key], True, key)
        self.assertIs(self.report['device_nodes'], False)
        self.assertEqual((self.report['gpu_launches'], self.report['model_starts']), (0, 0))

    def test_receipt_is_the_pinned_comparison_recipe_on_the_declared_boot(self):
        boot = os.environ['COMPARISON_BOOT_ID']
        self.assertEqual(self.preparation['boot_id'], boot)
        self.assertEqual(self.preparation['comparison'], build.COMPARISON)
        recipe = (self.root / 'proof/RECIPE.py').read_bytes()
        target = (self.root / 'proof/TARGET.py').read_bytes()
        self.assertEqual((sha(recipe), sha(target)), (self.preparation['recipe_sha256'], self.preparation['target_sha256']))
        receipt = json.loads((self.root / 'native-comparison-compiler.json').read_bytes())
        self.assertEqual(receipt['stdin_sha256'], sha(recipe))
        self.assertTrue(receipt['exit_code'] == 0 and receipt['child_reaped'] and receipt['owned_group_absent'])
        self.assertLessEqual(receipt['work_seconds'], 255)
        tree = ast.parse(recipe)
        values = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        argv, source = ast.literal_eval(values['argv']), ast.literal_eval(values['source'])
        self.assertEqual(source, target)
        self.assertIn("assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==" + repr(boot), recipe.decode())
        self.assertIn('timeout=225', recipe.decode())
        self.assertTrue('--unshare-all' in argv and '--clearenv' in argv and '--dev-bind' not in argv and
                        '/dev/kfd' not in argv and not any('dri' in a for a in argv))
        body = bundle_compile.comparison_body((HERE / 'bundle_compile_target.py').read_bytes())
        self.assertTrue(target.endswith(body))
        self.assertEqual(target.count(b"            cap=16\n"), 1)

    def test_artifacts_match_pins_with_exact_source_origin_and_no_cached_debug_paths(self):
        sources = {
            build.CANDIDATE_ATTENTION: (evidence() / 'verify-owner-preparation-v4/source/decode_attention_candidate.py', PREFETCH_SHA),
            build.NATIVE_ATTENTION: (evidence() / 'verify-owner-preparation-v4/source/decode_attention_baseline.py', NATIVE_SHA),
            '/mnt/review/kernels.py': (evidence() / 'opt-00-baseline-and-route-witness-v1/native-source/author/package-v2/review/kernels.py', KERNELS_SHA),
            '/mnt/review/opt04_gatednorm_quant.py': (evidence() / 'opt07-f1-component-preparation-v1/packet-v3/source/norm_candidate.py', FUSED_SHA)}
        functions = dict(attention_stage1='_fwd_grouped_kernel_stage1', attention_stage2='_fwd_kernel_stage2',
                         gemm='_gemm_afp4wfp4_kernel', f1='_gated_norm_mxfp4_kernel')
        pins = json.loads(self.pins_raw)
        standard = '/usr/local/lib/python3.14/dist-packages/triton/language/standard.py'
        files = sorted(p.name for p in (self.root / 'artifacts').iterdir())
        self.assertEqual(files, sorted(k + '.' + part for k in CAP16_KEYS for part in PARTS))
        for key in sorted(CAP16_KEYS):
            arm, name = key.split('-', 1) if key.startswith(('mtp-', 'control-')) else ('mtp', key)
            path = self.report['serving'][key]['source_path']
            local, pin = sources[path]
            raw_source = local.read_bytes()
            self.assertEqual(sha(raw_source), pin)
            fn = (functions['attention_stage1'] if 'stage1' in name else functions['attention_stage2'] if 'stage2' in name
                  else functions['gemm'] if name.startswith('gate-up') else functions['f1'])
            node = next(n for n in ast.parse(raw_source).body if isinstance(n, ast.FunctionDef) and n.name == fn)
            for part in PARTS:
                raw = (self.root / 'artifacts' / (key + '.' + part)).read_bytes()
                self.assertEqual(dict(bytes=len(raw), sha256=sha(raw)), pins[arm][name]['parts'][part], (key, part))
            ttir = (self.root / 'artifacts' / (key + '.ttir')).read_text()
            self.assertEqual(ttir.splitlines()[0],
                             '#loc = loc(' + json.dumps(path) + ':' + str(node.lineno) + ':' + str(node.col_offset + 1) + ')', key)
            files_seen = set(re.findall(r'loc\("(/[^"]+)":\d+:\d+\)', ttir))
            self.assertEqual(files_seen - {standard}, {path}, key)
            llir = (self.root / 'artifacts' / (key + '.llir')).read_text()
            self.assertEqual(set(re.findall(r'filename: "([^"]+)"', llir)), {Path(path).name, 'standard.py'}, key)
            self.assertEqual(set(re.findall(r'directory: "(/mnt/[^"]*)"', llir)), {str(Path(path).parent)}, key)
            hsaco = (self.root / 'artifacts' / (key + '.hsaco')).read_bytes()
            for other in set(sources) - {path}:
                self.assertNotIn(Path(other).name.encode() + b'\x00', hsaco, (key, other))
            self.assertNotIn(b'w4a4_component', hsaco + llir.encode() + ttir.encode(), key)


if __name__ == '__main__':
    unittest.main(verbosity=2)
