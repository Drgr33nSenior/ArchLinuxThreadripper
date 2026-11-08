"""CPU tests of the opt-in reversed-order three-member cap16 confirmation and the passive sampler.

The physical owner order stays bf16 then kv4; the reversed packet maps bf16 to the candidate (mtp)
and kv4 to the control. Inputs (all read-only except fresh TMPDIR fixtures):

REVERSED_BUILD    inert reversed build root (author, launcher, quality template, caller sources)
DEFAULT_BUILD     inert default build by the candidate generators (frozen op226 inputs)
CANONICAL_BUILD   the same default build by the unchanged canonical generators
OP226_REFERENCE   sealed op226 reference preparation (author, launcher, quality template)
OP226_EXECUTION   sealed op226 execution root (owner outcome, replay proof, native CPU grades)
OP226_HTTP        compressed validated op226 HTTP arm results captured from its unchanged retained replay
OP226_SAMPLES     retained op226 host-samples/SAMPLES.jsonl
PARENT_TOOLS      directory holding resource_summary.py
PASSIVE_SAMPLER   the pinned prospective caller sampler consumed by the support build's opt-in

Nothing here imports a numerical framework, starts a model, opens a device node or launches a kernel.
"""
from contextlib import contextmanager
import ast
import copy
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import zlib

HERE = Path(__file__).resolve().parent
LIB = HERE.parents[2] / 'lib/workstation'
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(LIB))
import build_bundle as build
from hyperloom_exchange import canonical, digest

REVERSED_MAP = {'bf16': 'mtp', 'kv4': 'control'}
FORWARD_MAP = {'bf16': 'control', 'kv4': 'mtp'}
CONSUMED = {'f6ebaaf7b67fb166', 'c759eeb5179094dd', 'a4777122b5277d13', 'ac6c108f39cbae77', '37a950031a7b7ff7',
            '5a66cd00a218b2cb'}
DECLARED_REVIEW_EDITS = {'FINAL-SETTINGS.json', 'mtp_contract.py', 'benchmark_physical_controller.py'}
DERIVED_REVIEW_DIGESTS = {'BENCHMARK-SPEC2.json', 'HTTP-BINDING-control.json', 'HTTP-BINDING-mtp.json',
                          'RESOURCE-MANIFEST.json'}
WIRE_ERROR = 'complete owner-validated composed HTTP wire required'


def env(name):
    return Path(os.environ[name]).resolve()


def tree(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


def read(path):
    return json.loads(Path(path).read_bytes())


def http_fixture():
    raw = env('OP226_HTTP').read_bytes()
    provenance = read(env('OP226_HTTP').with_name('OP226-ACTUAL-HTTP.provenance.json'))
    assert len(raw) == provenance['compressed_bytes'] and digest(raw) == provenance['compressed_sha256']
    decoder = zlib.decompressobj()
    data = decoder.decompress(raw, provenance['decoded_limit_bytes'] + 1)
    assert decoder.eof and not decoder.unused_data and len(data) == provenance['decoded_bytes']
    assert digest(data) == provenance['decoded_sha256'] and provenance['transformation'] is None
    return json.loads(data), provenance


def swapped_arms(actual):
    """Declared identity transformation: the reversed packet runs the candidate on the bf16 start."""
    return {'bf16': copy.deepcopy(actual['kv4']), 'kv4': copy.deepcopy(actual['bf16'])}


def swapped_campaign(campaign, arms):
    value = copy.deepcopy(campaign)
    value['http_bindings'] = {arm: copy.deepcopy(arms[arm]['binding']) for arm in ('bf16', 'kv4')}
    return value


def fresh_copy(source, parent, name='review'):
    target = Path(parent) / name
    shutil.copytree(source, target)
    return target


@contextmanager
def generated_controller(review, name='_reversed_test_controller'):
    """Load the actual generated controller the way the retained replay does, then restore modules."""
    previous, old_path = dict(sys.modules), list(sys.path)
    sys.path.insert(0, str(review))
    try:
        path = review / 'benchmark_physical_controller.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec(compile(path.read_bytes(), str(path), 'exec', dont_inherit=True), module.__dict__)
        yield module
    finally:
        sys.path[:] = old_path
        for key in list(sys.modules):
            if key not in previous:
                del sys.modules[key]
        sys.modules.update(previous)


def source_module(path, name):
    path = Path(path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, path))
    sys.modules[name] = module
    exec(compile(path.read_bytes(), str(path), 'exec', dont_inherit=True), module.__dict__)
    return module


class ConfirmationOptInTests(unittest.TestCase):
    """The opt-in is closed: exactly this comparison, exactly the reversed order."""

    def comparison(self):
        return dict(kind=build.COMPARISON)

    def test_exact_confirmation_accepted(self):
        value = build.confirmation_contract(dict(kind=build.CONFIRMATION, execution_order=['mtp', 'control']),
                                            self.comparison())
        self.assertEqual(value['owner_arm_map'], REVERSED_MAP)
        self.assertEqual(value['physical_owner_order'], ['bf16', 'kv4'])
        self.assertIs(value['protected_helper_exception_approved'], False)
        self.assertEqual((value['control_verification_cap'], value['candidate_verification_cap']), (16, 16))
        self.assertEqual(value['arm_members'], build.COMPARISON_MEMBERS)

    def test_unrelated_family_order_or_variable_refused(self):
        good = dict(kind=build.CONFIRMATION, execution_order=['mtp', 'control'])
        bad = [dict(good, kind='verify_cap32_single_variable'), dict(good, execution_order=['control', 'mtp']),
               dict(good, execution_order=('mtp', 'control')), dict(good, execution_order=['mtp']),
               dict(good, members=['verify_cap32']), dict(good, verification_cap=32), {'kind': build.CONFIRMATION},
               dict(good, execution_order=['control', 'control']), None, [good]]
        for value in bad:
            with self.assertRaisesRegex(ValueError, 'closed reversed-order confirmation input'):
                build.confirmation_contract(value, self.comparison())
        for comparison in (None, dict(kind=build.ABLATION)):
            with self.assertRaisesRegex(ValueError, 'requires the three-member cap16 comparison'):
                build.confirmation_contract(good, comparison)

    def test_build_input_key_sets_closed(self):
        base = dict(files={}, compiler_pins={}, probe_nonce='0' * 16, scheduler_pins={})
        for extra in ({'confirmation': {}}, {'ablation': {}, 'confirmation': {}},
                      {'comparison': {}, 'confirmation': {}, 'ablation': {}}, {'execution_order': ['mtp', 'control']}):
            inputs = dict(four_member=dict(base, **extra), mamba_max_states_per_path=3, kind='serving')
            with self.assertRaisesRegex(ValueError, 'closed four-member build inputs'):
                build.apply({}, None, inputs, None)

    def test_reverse_order_edits_exactly_three_declared_surfaces(self):
        review = env('DEFAULT_BUILD') / 'author/assembled-review'
        out = {name: (review / name).read_bytes() for name in DECLARED_REVIEW_EDITS}
        before = dict(out)
        build.reverse_order(out)
        old, new = json.loads(before['FINAL-SETTINGS.json']), json.loads(out['FINAL-SETTINGS.json'])
        self.assertEqual({k for k in old if old[k] != new.get(k)} | (set(new) - set(old)), {'execution_order'})
        self.assertEqual((old['execution_order'], new['execution_order']), (['control', 'mtp'], ['mtp', 'control']))
        for name, (a, b) in (('mtp_contract.py', (build.PAIRED_ORDER_OLD, build.PAIRED_ORDER_NEW)),
                             ('benchmark_physical_controller.py', (build.WIRE_ROLE_OLD, build.WIRE_ROLE_NEW))):
            self.assertEqual(out[name], before[name].replace(a.encode(), b.encode()))
            self.assertEqual(before[name].count(a.encode()), 1)
        with self.assertRaisesRegex(ValueError, 'selected control-first paired settings'):
            build.reverse_order(dict(out))
        drifted = dict(before, **{'benchmark_physical_controller.py': before['benchmark_physical_controller.py'].replace(
            build.WIRE_ROLE_OLD.encode(), b'value.get("arm") != ROLE[owner_arm]):')})
        with self.assertRaisesRegex(ValueError, 'four-member source drift'):
            build.reverse_order(drifted)


class DefaultReproductionTests(unittest.TestCase):
    """Absent the opt-ins, the candidate generators reproduce op226 byte for byte."""

    def test_candidate_and_canonical_default_trees_identical(self):
        a, b = tree(env('CANONICAL_BUILD')), tree(env('DEFAULT_BUILD'))
        self.assertEqual(set(a), set(b))
        self.assertEqual([k for k in a if a[k] != b[k]], [])
        self.assertEqual(len(a), 291)

    def test_default_reproduces_op226_pins_and_path_free_bytes(self):
        ref, built = tree(env('OP226_REFERENCE')), tree(env('DEFAULT_BUILD'))
        ids = json.loads(built['author/PACKAGE-IDENTITIES.json'])
        old = json.loads(ref['author/PACKAGE-IDENTITIES.json'])
        for key in ('archive_sha256', 'manifest_sha256', 'package_sha256', 'source_manifest_sha256', 'session_sha256', 'nonce'):
            self.assertEqual(ids[key], old[key])
        self.assertEqual({k for k in ids if ids[k] != old[k]}, {'archive'})
        path_bearing = {'SUPPORT-INPUTS.json', 'SUPPORT-RESULT.json', 'author/PACKAGE-IDENTITIES.json', 'launcher/CALLER-PLAN.json',
                        'launcher/caller-preview/PACKAGE-IDENTITIES.json', 'launcher/execute_once.py', 'launcher/execute_once.py.diff'}
        sealed_only = {'FINAL-ARTIFACTS.sha256', 'author.closeout.json', 'author/FINAL-ARTIFACTS.sha256', 'support-build-inputs.json'}
        self.assertEqual(set(ref) - set(built), sealed_only)
        self.assertEqual(set(built) - set(ref), set())
        self.assertEqual({k for k in built if built[k] != ref[k]}, path_bearing)
        # Path-bearing files are equal after the declared preparation-path and dependent-digest substitution.
        old_prep = str(Path(old['archive']).parents[2])
        new_prep = str(Path(ids['archive']).parents[2])
        tokens = [(old_prep.encode(), new_prep.encode())]
        for name in ('author/PACKAGE-IDENTITIES.json', 'launcher/execute_once.py', 'launcher/CALLER-PLAN.json',
                     'launcher/caller-preview/PACKAGE-IDENTITIES.json'):
            tokens.append((digest(ref[name]).encode(), digest(built[name]).encode()))
        for name in sorted(path_bearing):
            raw = ref[name]
            for a, b in tokens:
                raw = raw.replace(a, b)
            self.assertEqual(raw, built[name], name)


class ReversedPacketTests(unittest.TestCase):
    """The reversed packet differs only in the declared edits, derived digests, sampler and grading joins."""

    @classmethod
    def setUpClass(cls):
        cls.default, cls.reversed = tree(env('DEFAULT_BUILD')), tree(env('REVERSED_BUILD'))
        cls.ids = json.loads(cls.reversed['author/PACKAGE-IDENTITIES.json'])

    def review(self, which, name):
        return getattr(self, which)['author/package-v2/review/' + name]

    def test_review_tree_delta_is_declared(self):
        prefix = 'author/package-v2/review/'
        names = {k[len(prefix):] for k in self.default if k.startswith(prefix)}
        changed = {n for n in names if self.default[prefix + n] != self.reversed[prefix + n]}
        self.assertEqual(changed, DECLARED_REVIEW_EDITS | DERIVED_REVIEW_DIGESTS)
        self.assertEqual(self.review('default', 'BUNDLE-COMPILER-PINS.json'), self.review('reversed', 'BUNDLE-COMPILER-PINS.json'))
        self.assertEqual(digest(self.review('reversed', 'BUNDLE-COMPILER-PINS.json')),
                         '3cf410c072530972a68065b337d48448e89b3dbfcc97bb677efc6ad9b3fb4aa0')
        kernels = [n for n in names if n.endswith('.py') and n not in DECLARED_REVIEW_EDITS]
        self.assertTrue(all(self.review('default', n) == self.review('reversed', n) for n in kernels))
        self.assertIn('decode_attention_fwd_candidate.py', kernels)
        self.assertIn('opt04_gatednorm_quant.py', kernels)
        self.assertIn('ops.py', kernels)

    def test_derived_files_equal_after_declared_digest_substitution(self):
        tokens = [(digest(self.review('default', n)), digest(self.review('reversed', n))) for n in DECLARED_REVIEW_EDITS]
        old_bindings = {a: digest(self.review('default', 'HTTP-BINDING-%s.json' % a)) for a in ('control', 'mtp')}
        new_bindings = {a: digest(self.review('reversed', 'HTTP-BINDING-%s.json' % a)) for a in ('control', 'mtp')}
        for arm in ('control', 'mtp'):
            old, new = json.loads(self.review('default', 'HTTP-BINDING-%s.json' % arm)), json.loads(
                self.review('reversed', 'HTTP-BINDING-%s.json' % arm))
            tokens.append((old['source_manifest_sha256'], new['source_manifest_sha256']))
        tokens += [(old_bindings[a], new_bindings[a]) for a in old_bindings]
        spec_old, spec_new = digest(self.review('default', 'BENCHMARK-SPEC2.json')), digest(self.review('reversed', 'BENCHMARK-SPEC2.json'))
        for name in ('HTTP-BINDING-control.json', 'HTTP-BINDING-mtp.json', 'BENCHMARK-SPEC2.json'):
            raw = self.review('default', name).decode()
            for a, b in tokens:
                raw = raw.replace(a, b)
            self.assertEqual(raw.encode(), self.review('reversed', name), name)
        controller_old = self.review('default', 'benchmark_physical_controller.py').replace(spec_old.encode(), spec_new.encode())
        self.assertEqual(controller_old.replace(build.WIRE_ROLE_OLD.encode(), build.WIRE_ROLE_NEW.encode()),
                         self.review('reversed', 'benchmark_physical_controller.py'))

    def test_declared_diffs_are_one_line_each(self):
        for name, old, new in (('mtp_contract.py', build.PAIRED_ORDER_OLD, build.PAIRED_ORDER_NEW),):
            self.assertEqual(self.review('default', name).replace(old.encode(), new.encode()), self.review('reversed', name))
        diff = self.reversed['author/diffs/benchmark_physical_controller.py.diff'].decode()
        self.assertIn('+            ' + build.WIRE_ROLE_NEW, diff)
        self.assertNotIn(build.WIRE_ROLE_OLD, self.review('reversed', 'benchmark_physical_controller.py').decode())

    def test_identities_disclose_confirmation_and_keep_limits(self):
        ids = self.ids
        self.assertNotIn(ids['nonce'], CONSUMED)
        self.assertRegex(ids['nonce'], r'^[0-9a-f]{16}$')
        self.assertEqual((ids['owner_seconds'], ids['caller_seconds'], ids['starts'], ids['requests_per_start']), (6360, 7160, 2, 30))
        fm = ids['four_member']
        self.assertEqual(fm['confirmation']['owner_arm_map'], REVERSED_MAP)
        self.assertEqual((fm['ordinary_decode_cap'], fm['candidate_verification_cap']), (16, 16))
        self.assertEqual(fm['comparison']['arm_members'], build.COMPARISON_MEMBERS)
        self.assertIs(fm['confirmation']['protected_helper_exception_approved'], False)
        self.assertEqual(ids['mamba_max_states_per_path'], 3)
        old = json.loads(self.default['author/PACKAGE-IDENTITIES.json'])
        self.assertEqual(fm['http_export_bounds'], old['four_member']['http_export_bounds'])
        self.assertLessEqual(ids['staged_bytes'], 32 << 20)
        plan_old, plan_new = (json.loads(t['launcher/CALLER-PLAN.json']) for t in (self.default, self.reversed))
        self.assertEqual(plan_old['limits'], plan_new['limits'])
        self.assertEqual(plan_old['qualification_probes'], plan_new['qualification_probes'])
        self.assertEqual(plan_old['protected_helper_pins'], plan_new['protected_helper_pins'])
        self.assertEqual({k for k in plan_new['caller_inventory'] if plan_new['caller_inventory'][k] != plan_old['caller_inventory'][k]},
                         {'host_sample.py', 'LAUNCHER-BINDINGS.json', 'PACKAGE-IDENTITIES.json'})

    def test_support_changes_only_grading_joins_and_sampler(self):
        changed = {k for k in self.default if self.default[k] != self.reversed.get(k)}
        template = {k for k in changed if k.startswith('quality-template-c4ab/')}
        self.assertEqual(template, {'quality-template-c4ab/bind_future.py', 'quality-template-c4ab/producer/final_quality_inputs.py',
                                    'quality-template-c4ab/PROJECTION-INVENTORY.json'})
        self.assertEqual(self.reversed['caller-sources/host_sample.py'], env('PASSIVE_SAMPLER').read_bytes())
        self.assertEqual(self.reversed['launcher/caller-preview/host_sample.py'], self.reversed['caller-sources/host_sample.py'])
        result = json.loads(self.reversed['SUPPORT-RESULT.json'])
        self.assertEqual(result['grading_role_map'], REVERSED_MAP)
        self.assertEqual(result['passive_resource_sampler']['interval_seconds'], 10)
        self.assertIs(result['passive_resource_sampler']['comparator_input'], False)
        for name in ('retained_replay.py', 'readout.py', 'host_cpu.py', 'workstation.py', 'prepare_packet.py'):
            self.assertEqual(self.default['quality-template-c4ab/' + name], self.reversed['quality-template-c4ab/' + name])

    def test_http_bindings_share_the_bootstrap_contract_source(self):
        """model_benchmark bootstraps from HTTP-BINDING-control.json in either order; both maps pin one contract."""
        sources = {a: json.loads(self.review('reversed', 'HTTP-BINDING-%s.json' % a))['binding_template']['sources']
                   for a in ('control', 'mtp')}
        self.assertEqual(sources['control']['mtp_contract'], sources['mtp']['mtp_contract'])
        self.assertEqual(sources['control']['mtp_contract']['sha256'], digest(self.review('reversed', 'mtp_contract.py')))
        bench = self.review('reversed', 'model_benchmark.py').decode()
        self.assertEqual(bench.count("bootstrap = http['bindings']['control']"), 1)
        self.assertIn("mapping = mapping_module.owner_arm_map()", bench)
        self.assertIn("if sys.modules['mtp_contract'].owner_arm_map() != mapping:", bench)


class RoleLiteralCensusTests(unittest.TestCase):
    """No unmapped physical->logical literal remains on the executed or parent consumer path."""

    PATTERN = re.compile(r"""[\(\{,]\s*['"](bf16|kv4)['"]\s*[:,]\s*['"](control|mtp)['"]""")

    def census(self, files):
        hits = {}
        for name, raw in files.items():
            found = self.PATTERN.findall(raw.decode(errors='replace'))
            if found:
                hits[name] = sorted(set(found))
        return hits

    def test_reversed_executed_and_parent_sources(self):
        rev = tree(env('REVERSED_BUILD'))
        files = {k: v for k, v in rev.items() if k.endswith('.py') and (k.startswith('author/package-v2/review/') or
                 k.startswith('quality-template-c4ab/') or k.startswith('launcher/caller-preview/'))}
        for name in ('owner_operation.py', 'verify_serving_run.py', 'controller.py'):
            files['adapter/' + name] = (HERE / name).read_bytes()
        for name in ('hyperloom_owner_result.py', 'hyperloom_owner_backend.py', 'hyperloom_latency.py'):
            files['lib/' + name] = (LIB / name).read_bytes()
        hits = self.census(files)
        expected = {
            # OWNER_ARMS supplies only the physical names; owner_arm_map() zips them with the frozen order.
            'author/package-v2/review/mtp_contract.py': [('bf16', 'control'), ('kv4', 'mtp')],
            # ARM_MAP is used only for membership of the owner argument (checked below).
            'author/package-v2/review/serving_runtime.py': [('bf16', 'control'), ('kv4', 'mtp')],
            # Reversed grading joins, declared by the confirmation.
            'quality-template-c4ab/bind_future.py': [('bf16', 'mtp'), ('kv4', 'control')],
            'quality-template-c4ab/producer/final_quality_inputs.py': [('bf16', 'mtp'), ('kv4', 'control')],
            # Selected default and exact reversed mapping; serving_report receives the authenticated one.
            'lib/hyperloom_owner_result.py': [('bf16', 'control'), ('bf16', 'mtp'), ('kv4', 'control'), ('kv4', 'mtp')],
        }
        self.assertEqual(hits, expected)
        runtime = ast.parse(files['author/package-v2/review/serving_runtime.py'])
        uses = [n for n in ast.walk(runtime) if isinstance(n, ast.Name) and n.id == 'ARM_MAP' and isinstance(n.ctx, ast.Load)]
        parents = {id(c): p for p in ast.walk(runtime) for c in ast.iter_child_nodes(p)}
        self.assertTrue(uses)
        for node in uses:
            parent = parents[id(node)]
            self.assertTrue(isinstance(parent, ast.Compare) and isinstance(parent.ops[0], ast.In), 'ARM_MAP membership only')
        contract = files['author/package-v2/review/mtp_contract.py'].decode()
        self.assertIn("order = settings().get('execution_order', ['control', 'mtp'])\n    return dict(zip(OWNER_ARMS, order))", contract)

    def test_default_template_keeps_selected_joins(self):
        default = tree(env('DEFAULT_BUILD'))
        for name in ('quality-template-c4ab/bind_future.py', 'quality-template-c4ab/producer/final_quality_inputs.py'):
            self.assertEqual(self.census({name: default[name]})[name], [('bf16', 'control'), ('kv4', 'mtp')])


class EndReportRegressionTests(unittest.TestCase):
    """Real op226 validated wire through the actual generated terminal report in both orders."""

    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.provenance = http_fixture()
        cls.actual, cls.campaign, cls.report = cls.fixture['actual'], cls.fixture['campaign'], cls.fixture['report']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reversed-end-report-')
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def review(self, which, mutate=None):
        source = env('REVERSED_BUILD' if which == 'reversed' else 'DEFAULT_BUILD') / 'author/package-v2/review'
        target = fresh_copy(source, self.root / which)
        if mutate is not None:
            path = target / 'benchmark_physical_controller.py'
            raw = path.read_bytes()
            os.chmod(path, 0o600)  # fresh fixture copy; the packaged source is read-only
            path.write_bytes(mutate(raw))
        return target

    def test_default_order_reproduces_the_published_op226_reference(self):
        with generated_controller(self.review('default')) as controller:
            self.assertEqual(controller.mtp_contract.owner_arm_map(), FORWARD_MAP)
            value = controller._http_serving_report(copy.deepcopy(self.actual), copy.deepcopy(self.campaign))
        self.assertEqual(value['http_serving'], self.report['http_serving'])
        self.assertEqual(value['http_serving_report_format'], self.report['http_serving_report_format'])

    def test_reversed_order_closes_to_the_published_report(self):
        arms = swapped_arms(self.actual)
        campaign = swapped_campaign(self.campaign, arms)
        with generated_controller(self.review('reversed')) as controller:
            self.assertEqual(controller.mtp_contract.owner_arm_map(), REVERSED_MAP)
            value = controller._http_serving_report(copy.deepcopy(arms), copy.deepcopy(campaign))
            for arm, logical in REVERSED_MAP.items():
                reference = value['http_serving'][arm]
                old = next(r for r in self.report['http_serving'].values() if r['logical_arm'] == logical)
                self.assertEqual((reference['owner_arm'], reference['logical_arm']), (arm, logical))
                self.assertEqual(reference['canonical_wire_sha256'], old['canonical_wire_sha256'])
                self.assertEqual(reference['canonical_wire_bytes'], old['canonical_wire_bytes'])
                self.assertEqual(reference['retained_artifacts']['result.json'], 'results/' + arm + '-artifacts/result.json')
                self.assertEqual((reference['request_count'], reference['response_count']), (30, 30))
            published = copy.deepcopy(self.report)
            published['campaign'] = campaign
            published.update(value)
            path = self.root / 'CONTROLLER-RESULT.json'
            controller._publish(path, published)
            raw = path.read_bytes()
            self.assertEqual(raw, (json.dumps(published, sort_keys=True, allow_nan=False) + '\n').encode())
            self.assertLessEqual(len(raw), 8 << 20)
            reread = json.loads(raw)
            for arm in ('bf16', 'kv4'):
                # The retained replay's own acceptance expression, against the reread published report.
                accepted = controller.http_integration.accept_with_owner(arms[arm], reread['campaign'], owner_arm=arm)
                self.assertEqual(reread['http_serving'][arm], controller._http_wire_reference(accepted, owner_arm=arm))

    def test_restored_hard_coded_literal_fails_after_both_starts(self):
        restore = lambda raw: raw.replace(build.WIRE_ROLE_NEW.encode(), build.WIRE_ROLE_OLD.encode())
        arms = swapped_arms(self.actual)
        with generated_controller(self.review('reversed', restore)) as controller:
            self.assertEqual(controller.mtp_contract.owner_arm_map(), REVERSED_MAP)
            with self.assertRaisesRegex(ValueError, WIRE_ERROR):
                controller._http_serving_report(arms, swapped_campaign(self.campaign, arms))

    def test_mismatched_orders_bindings_and_values_refuse(self):
        arms = swapped_arms(self.actual)
        with generated_controller(self.review('reversed')) as controller:
            with self.assertRaises(ValueError):
                controller._http_serving_report(copy.deepcopy(self.actual), copy.deepcopy(self.campaign))
            with self.assertRaises(ValueError):
                controller._http_serving_report(copy.deepcopy(arms), copy.deepcopy(self.campaign))
            accepted = controller.http_integration.accept_with_owner(arms['bf16'], swapped_campaign(self.campaign, arms), owner_arm='bf16')
            for bad in (dict(accepted, arm='control'), dict(accepted, status='HTTP_ARM_COMPLETE'),
                        dict(accepted, rows=accepted['rows'][:-1]), dict(accepted, wire_complete=False)):
                with self.assertRaisesRegex(ValueError, 'wire|menu'):
                    controller._http_wire_reference(bad, owner_arm='bf16')
            with self.assertRaisesRegex(ValueError, WIRE_ERROR):
                controller._http_wire_reference(accepted, owner_arm='mtp')
        with generated_controller(self.review('default')) as controller:
            with self.assertRaises(ValueError):
                controller._http_serving_report(arms, swapped_campaign(self.campaign, arms))

    def test_pre_start_and_terminal_role_maps_agree(self):
        """The binding chosen before each model start is the role checked after both; no late mismatch path."""
        review = self.review('reversed')
        spec = read(review / 'BENCHMARK-SPEC2.json')
        session = '0' * 64
        with generated_controller(review) as controller:
            contract = controller.mtp_contract
            for arm, logical in REVERSED_MAP.items():
                expected = controller.http_expected_binding(review.parent, spec, owner_arm=arm, session_sha256=session)
                self.assertEqual(expected['arm'], logical)
                self.assertEqual(contract.contract_for_owner_arm(arm)['logical_arm'], logical)
                adapter_contract = controller.adapter._adjacent('final_projection_contract', 'mtp_contract.py')
                self.assertEqual(adapter_contract.owner_arm_map()[arm], logical)
            self.assertEqual(contract.settings()['execution_order'], ['mtp', 'control'])
            self.assertEqual(contract.phase_config()[:3], (131040, 2040, 30))


class ParentProjectionTests(unittest.TestCase):
    """Grading joins, latency series projection and the owner's mapping derivation in both orders."""

    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.provenance = http_fixture()
        ex = env('OP226_EXECUTION')
        cls.execution = ex
        cls.result = read(ex / 'RESULT.json')
        cls.outcome = read(cls.result['outcome']['path'])
        cls.campaign_root = Path(cls.outcome['campaign_root'])
        cls.proof = read(cls.campaign_root / 'quality-binding/cpu/ACTUAL-INPUT-BINDING.json')

    def template(self, which, name):
        root = env('REVERSED_BUILD' if which == 'reversed' else 'DEFAULT_BUILD') / 'quality-template-c4ab'
        return source_module(root / name, '_template_%s_%s' % (which, Path(name).stem))

    def swapped_replay(self):
        value = copy.deepcopy(self.fixture['replay_result'])
        value['arm_results'] = {'bf16': value['arm_results']['kv4'], 'kv4': value['arm_results']['bf16']}
        artifacts = value['provenance']['artifacts']
        value['provenance']['artifacts'] = {'bf16': dict(artifacts['kv4'], owner_arm='bf16'),
                                            'kv4': dict(artifacts['bf16'], owner_arm='kv4')}
        return value

    def test_bind_future_mapping_in_both_orders(self):
        request = self.provenance['request']
        original = copy.deepcopy(self.fixture['replay_result'])
        swapped = self.swapped_replay()
        default, reversed_ = self.template('default', 'bind_future.py'), self.template('reversed', 'bind_future.py')
        self.assertEqual(default.validate_replay(copy.deepcopy(original), request)['status'], original['status'])
        self.assertEqual(reversed_.validate_replay(copy.deepcopy(swapped), request)['status'], original['status'])
        for module, value in ((default, swapped), (reversed_, original)):
            with self.assertRaisesRegex(ValueError, 'selected physical/logical arm mapping'):
                module.validate_replay(copy.deepcopy(value), request)

    def test_quality_producer_mapping_in_both_orders(self):
        original, swapped = self.fixture['replay_result']['arm_results'], self.swapped_replay()['arm_results']
        default = self.template('default', 'producer/final_quality_inputs.py')
        reversed_ = self.template('reversed', 'producer/final_quality_inputs.py')
        a, b = default.build_input(copy.deepcopy(original)), reversed_.build_input(copy.deepcopy(swapped))
        self.assertEqual((len(a['rows']), len(b['rows'])), (60, 60))
        self.assertEqual([r['arm'] for r in b['rows']], ['bf16'] * 30 + ['kv4'] * 30)
        self.assertEqual([r['response_raw_sha256'] for r in b['rows'][:30]], [r['response_raw_sha256'] for r in a['rows'][30:]])
        for module, value in ((default, swapped), (reversed_, original)):
            with self.assertRaisesRegex(ValueError, 'complete recomputed unqualified serving arm'):
                module.build_input(copy.deepcopy(value))

    def projection_inputs(self):
        cpu = self.campaign_root / 'quality-binding/cpu'
        prep = read(self.execution / 'PREPARATION-RESULT.json')
        requests = list((self.execution / 'requests').rglob('REQUEST.json'))
        self.assertEqual(len(requests), 1)
        quality = read(cpu / 'RESULT-SUMMARY.json')
        return dict(summaries={a: read(self.campaign_root / 'quality-binding/serving-replay' / (a + '-http-client-tree') /
                                       'paired/RUN-SUMMARY.json') for a in ('bf16', 'kv4')},
                    decoded=read(cpu / 'retained-cpu/output/quality/DECODE-RESULT.json'), quality=quality,
                    quality_sha256=digest(canonical(quality)), workload=read(prep['workload']['path']),
                    request=read(requests[0]), binding=read(self.execution / 'BINDING.json'),
                    owner_seal_sha256=self.outcome['observed_release']['operation_seal_sha256'],
                    observation_id=self.outcome['nonce'])

    def swap_projection(self, value):
        value = copy.deepcopy(value)
        value['summaries'] = {'bf16': value['summaries']['kv4'], 'kv4': value['summaries']['bf16']}
        for key, rows in (('decoded', value['decoded']['answers']), ('quality', value['quality']['grades'])):
            first, second = rows[:30], rows[30:]
            swapped = [dict(r, arm='bf16') for r in second] + [dict(r, arm='kv4') for r in first]
            if key == 'decoded':
                value['decoded']['answers'] = swapped
            else:
                value['quality']['grades'] = swapped
        value['quality_sha256'] = digest(canonical(value['quality']))
        return value

    def test_owner_series_projection_reproduces_and_detects_inversion(self):
        projection = source_module(LIB / 'hyperloom_owner_result.py', '_candidate_owner_result')
        backend = source_module(LIB / 'hyperloom_owner_backend.py', '_candidate_owner_backend')
        inputs = self.projection_inputs()
        original = self.outcome['payload']['benchmark_report']
        self.assertEqual(projection.serving_report(**copy.deepcopy(inputs)), original)
        self.assertEqual(projection.serving_report(**copy.deepcopy(inputs), arms=projection.ARMS), original)
        swapped = self.swap_projection(inputs)
        reversed_report = projection.serving_report(**copy.deepcopy(swapped), arms=projection.REVERSED_ARMS)
        self.assertEqual(reversed_report['paired_comparison'], original['paired_comparison'])
        self.assertEqual(reversed_report['paired_comparison']['verdict'], 'KEEP')
        for key in ('workstation_latency', 'workstation_latency_control'):
            self.assertEqual(dict(reversed_report[key], quality_sha256=None), dict(original[key], quality_sha256=None))
        envelope = dict(status='SUCCEEDED', independent_release=True, payload={'benchmark_report': reversed_report},
                        owner_seal_sha256=swapped['owner_seal_sha256'], request_sha256=digest(canonical(swapped['request'])))
        self.assertEqual(backend.validate_report(envelope, swapped['request'], swapped['binding']), reversed_report)
        # The unchanged selected mapping applied to a reversed run silently inverts the comparison.
        inverted = projection.serving_report(**copy.deepcopy(swapped))
        self.assertNotEqual(inverted['paired_comparison']['verdict'], 'KEEP')
        self.assertLess(inverted['paired_comparison']['gain_pct'], 0)
        for arms in ((('bf16', 'mtp'), ('kv4', 'mtp')), (('kv4', 'control'), ('bf16', 'mtp')), (), None):
            with self.assertRaisesRegex(ValueError, 'exact physical/logical serving arm mapping'):
                projection.serving_report(**copy.deepcopy(inputs), arms=arms)

    def test_owner_operation_derives_mapping_from_sealed_settings_and_replay(self):
        previous = dict(sys.modules)
        try:
            owner = source_module(HERE / 'owner_operation.py', '_candidate_owner_operation')
            default = dict(author=self.execution / 'preparation/author',
                           plan=read(self.execution / 'preparation/launcher/CALLER-PLAN.json'))
            reversed_ = dict(author=env('REVERSED_BUILD') / 'author', plan=read(env('REVERSED_BUILD') / 'launcher/CALLER-PLAN.json'))
            swapped = copy.deepcopy(self.proof)
            artifacts = swapped['replay_provenance']['artifacts']
            swapped['replay_provenance']['artifacts'] = {'bf16': dict(artifacts['kv4'], owner_arm='bf16'),
                                                         'kv4': dict(artifacts['bf16'], owner_arm='kv4')}
            self.assertEqual(owner._serving_arms(default, self.proof), owner.projection.ARMS)
            self.assertEqual(owner._serving_arms(reversed_, swapped), owner.projection.REVERSED_ARMS)
            for verified, proof in ((default, swapped), (reversed_, self.proof)):
                with self.assertRaisesRegex(ValueError, 'replayed logical arms differ'):
                    owner._serving_arms(verified, proof)
            bad = dict(reversed_, plan=dict(reversed_['plan'], expected_package_pins=default['plan']['expected_package_pins']))
            with self.assertRaises(ValueError):
                owner._serving_arms(bad, swapped)
            source = (HERE / 'owner_operation.py').read_text()
            self.assertEqual(source.count('projection.serving_report(**arguments, arms=_serving_arms(verified, proof))'), 1)
            self.assertNotIn('projection.serving_report(**arguments)\n', source)
        finally:
            for key in list(sys.modules):
                if key not in previous:
                    del sys.modules[key]


class PassiveSamplerTests(unittest.TestCase):
    """The actual remote sampler code over fresh fixture sysfs trees; caps, states and bounds."""

    @classmethod
    def setUpClass(cls):
        path = env('PASSIVE_SAMPLER')
        cls.raw = path.read_bytes()
        previous = sys.modules.get('connection')
        sys.modules['connection'] = types.SimpleNamespace(SSH=['/nonexistent-ssh'], check=lambda: None)
        try:
            cls.ns = {'__name__': '_passive_sampler_under_test', '__file__': str(path)}
            exec(compile(cls.raw, str(path), 'exec', dont_inherit=True), cls.ns)
        finally:
            if previous is None:
                sys.modules.pop('connection', None)
            else:
                sys.modules['connection'] = previous

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='passive-sampler-')
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        for path in self.root.rglob('*'):
            if path.is_file() or path.is_dir():
                os.chmod(path, 0o700)
        self.tmp.cleanup()

    def device(self, bdf, *, hwmon=('hwmon3',), runtime='active', files=None, metrics=bytes([0x80, 0, 1, 3]) + bytes(124),
               freq=2, temp=3, labels=True):
        d = self.root / 'sys' / bdf
        (d / 'power').mkdir(parents=True)
        (d / 'power/runtime_status').write_text(runtime + '\n')
        values = dict(mem_info_vram_used='100', mem_info_vram_total='200', gpu_busy_percent='99', mem_busy_percent='41',
                      pp_dpm_sclk='0: 500Mhz\n1: 2900Mhz *', pp_dpm_mclk='0: 96Mhz\n1: 1258Mhz *')
        values.update(files or {})
        for name, value in values.items():
            if value is None:
                continue
            (d / name).write_bytes(value if isinstance(value, bytes) else (value + '\n').encode())
        if metrics is not None:
            (d / 'gpu_metrics').write_bytes(metrics)
        for name in hwmon:
            h = d / 'hwmon' / name
            h.mkdir(parents=True)
            for key, value in (('name', 'amdgpu'), ('power1_cap', '235000000'), ('power1_average', '234000000')):
                (h / key).write_text(value + '\n')
            for i in range(1, freq + 1):
                (h / ('freq%d_input' % i)).write_text(str(1000000 * i) + '\n')
                if labels:
                    (h / ('freq%d_label' % i)).write_text(('sclk', 'mclk')[i - 1] + '\n' if i <= 2 else 'x\n')
            for i in range(1, temp + 1):
                (h / ('temp%d_input' % i)).write_text(str(50000 + i) + '\n')
                if labels:
                    (h / ('temp%d_label' % i)).write_text(('edge', 'junction', 'mem', 'extra')[min(i - 1, 3)] + '\n')
        return d

    def run_code(self, transform=None):
        code = self.ns['CODE'].replace('/sys/bus/pci/devices', str(self.root / 'sys')).replace(
            '/proc/sys/kernel/random/boot_id', str(self.root / 'boot_id'))
        if transform is not None:
            code = transform(code)
        (self.root / 'boot_id').write_text('cf30344d-a1d7-4350-825b-e097c8e60f2b\n')
        done = subprocess.run([sys.executable, '-I', '-B', '-S', '-'], input=code.encode(), capture_output=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        self.assertLessEqual(len(done.stdout), self.ns['OUTPUT_LIMIT'])
        return json.loads(done.stdout), len(done.stdout)

    def test_caps_and_interface_unchanged(self):
        self.assertEqual((self.ns['INTERVAL'], self.ns['SAMPLE_SECONDS'], self.ns['OUTPUT_LIMIT']), (10, 8, 16384))
        self.assertEqual(self.raw.count(b'INTERVAL, SAMPLE_SECONDS, OUTPUT_LIMIT = 10, 8, 16384\n'), 1)
        self.assertEqual(self.ns['BDFS'], ('0000:23:00.0', '0000:c3:00.0'))
        compile(self.ns['CODE'], 'host-sample', 'exec', dont_inherit=True)
        for forbidden in ('/dev/', 'amdsmi', 'amd-smi', 'rocprof', 'ioctl', 'subprocess', 'socket', 'write', 'pp_od_clk',
                          'power_dpm_force', 'power1_cap_', "open('w", 'O_WRONLY', 'O_RDWR', 'sudo'):
            self.assertNotIn(forbidden, self.ns['CODE'].replace('"/dev/"', ''), forbidden)
        self.assertIn('O_RDONLY|os.O_NOFOLLOW', self.ns['CODE'])

    def test_supported_values_discovered_labels_and_legacy_fields(self):
        for i, bdf in enumerate(self.ns['BDFS']):
            self.device(bdf, hwmon=('hwmon%d' % (7 - 4 * i),))
        record, _ = self.run_code()
        legacy = record['devices']['0000:23:00.0']
        self.assertEqual(legacy['busy_percent'], '99')
        self.assertEqual(legacy['temperatures'], {'edge': '50001', 'junction': '50002', 'mem': '50003'})
        passive = record['passive']['devices']['0000:c3:00.0']
        self.assertEqual(list(passive['hwmon']['directories']), ['hwmon3'])
        h = passive['hwmon']['directories']['hwmon3']
        self.assertEqual({k: v['label']['text'] for k, v in h['freq'].items()}, {'freq1': 'sclk', 'freq2': 'mclk'})
        self.assertEqual(h['power']['power1_input'], {'absent': True})
        self.assertEqual(passive['gpu_metrics']['header_hex'], '80000103')
        self.assertEqual(passive['gpu_metrics']['bytes'], 128)
        self.assertEqual(passive['mem_busy_percent'], {'text': '41'})
        self.assertIs(record['passive']['decoded_gpu_metrics'], False)
        self.assertEqual(passive['runtime_status_before'], passive['runtime_status_after'])
        self.assertTrue(isinstance(record['monotonic'], float))

    def test_unavailable_absent_overflow_and_malformed_states(self):
        self.device('0000:23:00.0', files=dict(gpu_busy_percent=None, mem_busy_percent=b'\x00\xff7\x1b[0m',
                                               pp_dpm_sclk='x' * 385), metrics=bytes(1025), freq=0, labels=False)
        d = self.device('0000:c3:00.0', hwmon=(), metrics=None, runtime='suspended')
        (d / 'hwmon').write_text('not a directory\n')
        locked = d / 'pp_dpm_mclk'
        os.chmod(locked, 0)
        (d / 'mem_busy_percent').unlink()
        (d / 'mem_busy_percent').mkdir()
        record, _ = self.run_code()
        a = record['passive']['devices']['0000:23:00.0']
        b = record['passive']['devices']['0000:c3:00.0']
        self.assertEqual(a['gpu_busy_percent'], {'absent': True})
        self.assertEqual(a['mem_busy_percent'], {'text': '??7?[0m'})
        self.assertEqual(a['pp_dpm_sclk'], {'overflow': True, 'cap': 384})
        self.assertEqual(a['gpu_metrics'], {'overflow': True, 'cap': 1024})
        self.assertEqual(a['hwmon']['directories']['hwmon3']['freq'], {})
        self.assertEqual(b['gpu_metrics'], {'absent': True})
        self.assertEqual(b['hwmon']['error']['error'], 'ENOTDIR')
        self.assertEqual(b['hwmon']['discovered'], 0)
        if os.geteuid() != 0:
            self.assertEqual((b['pp_dpm_mclk']['error'], b['pp_dpm_mclk']['stage']), ('EACCES', 'open'))
        self.assertEqual((b['mem_busy_percent']['error'], b['mem_busy_percent']['stage']), ('EISDIR', 'read'))
        self.assertEqual(b['runtime_status_before'], {'text': 'suspended'})
        self.assertEqual(record['devices']['0000:c3:00.0']['hwmon_count'], 0)
        self.assertEqual(errno.errorcode[16], 'EBUSY')

    def test_directory_and_entry_caps_with_unfixed_hwmon_numbers(self):
        self.device('0000:23:00.0', hwmon=('hwmon11', 'hwmon2', 'hwmon5'), freq=5, temp=9)
        self.device('0000:c3:00.0')
        record, _ = self.run_code()
        hw = record['passive']['devices']['0000:23:00.0']['hwmon']
        self.assertEqual((hw['discovered'], hw['retained']), (3, 2))
        self.assertEqual(sorted(hw['directories']), ['hwmon11', 'hwmon2'])
        item = hw['directories']['hwmon11']
        self.assertEqual((item['freq_discovered'], len(item['freq']), item['temp_discovered'], len(item['temp'])), (5, 2, 9, 3))
        self.assertEqual(record['devices']['0000:23:00.0']['hwmon_count'], 3)

    def test_worst_case_record_bound_and_drop_rule(self):
        caps = self.ns['PASSIVE_CAPS']
        quote = lambda n: '"' * n
        for bdf in self.ns['BDFS']:
            d = self.device(bdf, hwmon=('hwmon1', 'hwmon2'), freq=2, temp=3,
                            files=dict(gpu_busy_percent=quote(caps['percent']), mem_busy_percent=quote(caps['percent']),
                                       pp_dpm_sclk=quote(caps['dpm'] - 1), pp_dpm_mclk=quote(caps['dpm'] - 1)),
                            metrics=bytes(range(256)) * (caps['gpu_metrics'] // 256))
            (d / 'power/runtime_status').write_text(quote(caps['state'] - 1) + '\n')
            for h in (d / 'hwmon').iterdir():
                for p in h.iterdir():
                    cap = caps['hwmon_label'] if p.name.endswith('_label') else caps['hwmon_name'] if p.name == 'name' else caps['hwmon_value']
                    p.write_text(quote(cap - 1) + '\n')
        record, size = self.run_code()
        self.assertNotIn('dropped_for_output_bound', record['passive'])
        self.assertLessEqual(size, self.ns['OUTPUT_LIMIT'] - self.ns['PASSIVE_RESERVE'])
        self.__class__.worst_case_record_bytes = size
        # A larger record keeps every legacy field and replaces only the passive block by its size and hash.
        record, size = self.run_code(lambda code: code.replace(
            'if len(line)>%d:' % (self.ns['OUTPUT_LIMIT'] - self.ns['PASSIVE_RESERVE']), 'if len(line)>1000:'))
        self.assertIs(record['passive']['dropped_for_output_bound'], True)
        self.assertEqual(set(record['devices']['0000:23:00.0']), {'busy_percent', 'hwmon_count', 'vram_total', 'vram_used'})

    def test_passive_failure_keeps_legacy_sample(self):
        for bdf in self.ns['BDFS']:
            self.device(bdf)
        record, _ = self.run_code(lambda code: code.replace('out["passive"]=_passive()', 'out["passive"]=1/0'))
        self.assertTrue(record['passive']['failure'].startswith('ZeroDivisionError'))
        self.assertEqual(record['devices']['0000:23:00.0']['busy_percent'], '99')

    def test_sample_loop_bounds_and_host_faults_window(self):
        for bdf in self.ns['BDFS']:
            self.device(bdf)
        code = self.ns['CODE'].replace('/sys/bus/pci/devices', str(self.root / 'sys')).replace(
            '/proc/sys/kernel/random/boot_id', str(self.root / 'boot_id'))
        (self.root / 'boot_id').write_text('cf30344d-a1d7-4350-825b-e097c8e60f2b\n')
        real_run = subprocess.run
        calls = []

        def local(argv, input, capture_output, timeout):
            calls.append(argv)
            self.assertEqual(argv, ['/nonexistent-ssh', '/usr/bin/python3 -I -B -S -'])
            self.assertLessEqual(timeout, 8)
            return real_run([sys.executable, '-I', '-B', '-S', '-'], input=code.encode(), capture_output=True, timeout=timeout)
        self.ns['subprocess'].run = local
        try:
            row = self.ns['sample'](0, 0)
            self.assertIsNone(row['failure'])
            self.assertIn('passive', row['record'])
            original = self.ns['OUTPUT_LIMIT']
            self.ns['OUTPUT_LIMIT'] = 100
            try:
                over = self.ns['sample'](1, 10)
            finally:
                self.ns['OUTPUT_LIMIT'] = original
            self.assertIsNone(over['record'])
            self.assertIn('sample output bound exceeded', over['failure'])
        finally:
            self.ns['subprocess'].run = real_run
        self.assertEqual(len(calls), 2)
        # host_faults.window() reads record.monotonic from successful rows only.
        rows = [row, over]
        stamps = [r['record']['monotonic'] for r in rows if r['record'] is not None]
        self.assertEqual(len(stamps), 1)
        line_bytes = len(json.dumps(row, sort_keys=True)) + 1
        self.assertLess(line_bytes, 20 << 10)

    def test_retention_arithmetic(self):
        launch_seconds, margin = 6440, 180
        samples = -(-(launch_seconds + margin) // self.ns['INTERVAL'])
        row_bound = self.ns['OUTPUT_LIMIT'] + 6 * 512 + 512
        self.assertEqual(samples, 662)
        self.assertLessEqual(samples * row_bound, 13 << 20)


class ResourceSummaryTests(unittest.TestCase):
    """Report-only offline summary over the retained legacy op226 samples and new passive rows."""

    @classmethod
    def setUpClass(cls):
        cls.summary = source_module(env('PARENT_TOOLS') / 'resource_summary.py', '_resource_summary_under_test')

    def test_retained_op226_legacy_samples(self):
        value = self.summary.summarize(env('OP226_SAMPLES'))
        self.assertEqual((value['rows'], value['malformed_rows'], value['failed_samples'], value['passive_rows']), (149, 0, 1, 0))
        card = value['windows']['all']['0000:23:00.0']['legacy']
        self.assertEqual(card['busy_percent']['n'], 142)
        self.assertEqual(card['power1_average_w']['maximum'], 235.0)
        self.assertGreater(card['at_power_cap'], 0)
        self.assertIs(value['report_only'], True)
        self.assertIs(value['comparator_input'], False)
        self.assertIn('not physical GB/s', value['scope'])
        keys = json.dumps(sorted(value['windows']['all']['0000:23:00.0']['legacy']))
        self.assertFalse(any(word in keys.lower() for word in ('bandwidth', 'gbps', 'umc', 'utilization')))

    def test_bounds_and_malformed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'SAMPLES.jsonl'
            good = dict(seq=0, record=dict(monotonic=1.0, devices={}, passive=dict(schema=1, devices={'0000:23:00.0': dict(
                gpu_busy_percent={'text': '50'}, mem_busy_percent={'error': 'EBUSY', 'errno': 16, 'stage': 'read'},
                gpu_metrics={'bytes': 128, 'sha256': '0' * 64, 'header_hex': '80000103', 'base64': ''},
                hwmon=dict(directories={'hwmon3': dict(freq={'freq1': dict(input={'text': '2900000000'}, label={'text': 'sclk'})},
                                                        temp={}, power={})}))})))
            path.write_text(json.dumps(good) + '\n' + '{not json\n' + json.dumps(dict(seq='x')) + '\n' + 'x' * (65 << 10) + '\n')
            value = self.summary.summarize(path, windows={'candidate-first': [0.5, 2.0]})
            self.assertEqual((value['rows'], value['malformed_rows'], value['passive_rows']), (1, 3, 1))
            card = value['windows']['candidate-first']['0000:23:00.0']['passive']
            self.assertEqual(card['states']['mem_busy_percent:error:EBUSY'], 1)
            self.assertEqual(card['numeric']['hwmon.freq1_input[sclk]']['median'], 2900000000)
            self.assertEqual(card['gpu_metrics']['header_hex'], ['80000103'])
            self.assertIs(card['gpu_metrics']['decoded'], False)
            path.write_text('\n'.join(json.dumps(dict(seq=i, record=None)) for i in range(701)) + '\n')
            with self.assertRaisesRegex(ValueError, 'row count bound'):
                self.summary.summarize(path)
            with self.assertRaisesRegex(ValueError, 'window shape'):
                self.summary.summarize(self._one(tmp), windows={'x': [2, 1]})

    def _one(self, tmp):
        path = Path(tmp) / 'ONE.jsonl'
        path.write_text(json.dumps(dict(seq=0, record=None)) + '\n')
        return path


if __name__ == '__main__':
    unittest.main()
