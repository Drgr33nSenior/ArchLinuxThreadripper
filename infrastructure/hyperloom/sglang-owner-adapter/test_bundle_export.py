"""Exercise the actual generated four-member HTTP export and receipt on CPU.

BUNDLE_AUTHOR selects an inert build, never a run root. The producer block,
exporter, bounded capture, receiver, controller completeness/resource rules and
transport readers are the packet's own generated code, executed unchanged.
This proves transport and byte budgets only. Numerical, quality and timing
acceptance, and semantic reading of the retained verify-prefetch fixture
under the four-member binding, are outside this proof.
"""
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest

HERE = Path(__file__).resolve().parent
MIB = 1 << 20
HTTP_MODE = 'native-http-fp8-sprint01-v1'
APPROVED_EXPORTER = '20fcb49f1340d8eabab771c22ac7334dfb15a36c7a3ead458bafc7cdfb7a07cb'
FIXED_LINE = "    if binding.get('experiment_mode') == HTTP_MODE:\n        return encoded_limit(binding)\n"
STALE_LINE = "    if binding.get('experiment_mode') == HTTP_MODE:\n        return 63 << 20\n"
# Retained verify-prefetch HTTP export (operation manifest 6d60d8e6...) and the
# operation223 control stdout with its two real resource records.
FIXTURE_PINS = {'http-client-bundle.json': '4010dc96d2def6aa5df33ae95fb348f3d790a43e008d2f31df18e671c673abb1',
                'native-http-bundle.json': 'de07dea73d4bcc581f37da13dccc4ad2f557cff7602cdb9ccc5442bde59d14a0',
                'result.json': '85fac917a9687e63730f15503a07c7ce1df9a17e5e837364a5646aa336733a87'}
OP223_STDOUT = 'ea72500c5a6aaedde77ebb0c6f7751ea5061a60a118163f5ccebdafd7418a309'
COMPILER_PINS = '7fd566e8853ebf88496fe7a6c07c22b1f2b3a803a12f2a59ce01c20fec048a13'
RECORD = 256 << 10  # generated bundle_contract.MAX_RECORD_BYTES
CHILD = r'''
import ast, hashlib, json, sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
exporter, exporter_pin, producer, producer_pin, output, binding, before, after, fill = sys.argv[1:]
def checked(path, pin):
    raw = Path(path).read_bytes(); assert hashlib.sha256(raw).hexdigest() == pin; return raw
artifacts = ModuleType('generated_export'); artifacts.__file__ = exporter
exec(compile(checked(exporter, exporter_pin), exporter, 'exec', dont_inherit=True), artifacts.__dict__)
main = next(n for n in ast.parse(checked(producer, producer_pin)).body
            if isinstance(n, ast.FunctionDef) and n.name == 'main')
blocks = [n for n in ast.walk(main) if isinstance(n, ast.If) and isinstance(n.body[0], ast.Expr) and
          isinstance(n.body[0].value, ast.Call) and getattr(n.body[0].value.func, 'attr', '') == 'pack_http_artifacts']
assert len(blocks) == 1
class Counter:
    def __init__(self, raw): self.raw, self.count = raw, 0
    def write(self, data): self.raw.write(data); self.count += len(data); return len(data)
    def flush(self): self.raw.flush()
stdout = Counter(sys.stdout.buffer)
tail = Path(after).read_bytes()
try:
    stdout.write(Path(before).read_bytes()); stdout.flush()
    exec(compile(ast.Module(body=[blocks[0]], type_ignores=[]), producer, 'exec'),
         dict(output=Path(output), binding=json.loads(binding), artifacts=artifacts, raw_transport=False,
              sys=SimpleNamespace(stdout=SimpleNamespace(buffer=stdout))))
    if fill != 'none':
        missing = int(fill) - stdout.count - len(tail)
        assert missing > 0
        stdout.write((b'filler log line\n' * (missing // 16 + 1))[:missing - 1] + b'\n')
finally:
    stdout.write(tail); stdout.flush()
    sys.stderr.write('STDOUT_BYTES %d\n' % stdout.count)
'''


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked(path, pin):
    raw = Path(path).read_bytes()
    if sha(raw) != pin:
        raise ValueError('pinned input changed: ' + str(path))
    return raw


def module(name, raw, filename):
    value = ModuleType(name)
    value.__file__ = filename
    exec(compile(raw, filename, 'exec', dont_inherit=True), value.__dict__)
    return value


def closure(raw, filename, roots, exclude=()):
    """Compile only the named generated definitions and what they reference."""
    tree = ast.parse(raw)
    nodes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            nodes.update({t.id: node for t in node.targets if isinstance(t, ast.Name)})
    wanted, used, queue = set(), set(), list(roots)
    while queue:
        name = queue.pop()
        if name in wanted or name in exclude or name not in nodes:
            continue
        wanted.add(name)
        names = {x.id for x in ast.walk(nodes[name]) if isinstance(x, ast.Name)}
        used |= names
        queue.extend(names)
    if not set(roots) <= wanted:
        raise ValueError('generated reader definition missing')
    def bound(node):
        return {(a.asname or a.name.split('.')[0]) if isinstance(node, ast.Import) else (a.asname or a.name)
                for a in node.names}
    body = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and bound(n) & used]
    chosen = {id(nodes[n]) for n in wanted}
    if any(isinstance(x, ast.Call) for n in wanted if isinstance(nodes[n], ast.Assign) for x in ast.walk(nodes[n])):
        raise ValueError('generated reader closure would execute module code')
    body += [n for n in tree.body if id(n) in chosen]
    space = {'__name__': 'closure_' + Path(filename).stem}
    exec(compile(ast.Module(body=body, type_ignores=[]), filename, 'exec', dont_inherit=True), space)
    return space


def canonical_dir(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix)).resolve()


def fresh_report(value):
    """Measurement output may only create a new file outside every sealed evidence root.

    An earlier run pointed BUNDLE_EXPORT_REPORT into a sealed packet and rewrote a
    byte-identical member, invalidating that packet's timestamp seal.
    """
    path = Path(value)
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('BUNDLE_EXPORT_REPORT must name a new absolute file: ' + str(path))
    for directory in path.parents:
        if (directory / 'FINAL-ARTIFACTS.sha256').exists():
            raise ValueError('BUNDLE_EXPORT_REPORT lies inside sealed root ' + str(directory))
    return path


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


def write_tree(root, files):
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def padded_json(value, size):
    """Canonical JSON object of exactly size bytes; transport fixtures only."""
    value = dict(value, synthetic_transport_fixture=True, padding='')
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    value['padding'] = 'p' * (size - len(raw))
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    assert len(raw) == size
    return raw


class Generated:
    """Actual generated exporter, controller rules, producer and readers."""
    def __init__(self):
        author = Path(os.environ['BUNDLE_AUTHOR']).resolve()
        self.review = author / 'package-v2/review'
        self.exporter_path = self.review / 'benchmark_artifacts.py'
        self.exporter_raw = self.exporter_path.read_bytes()
        self.exporter_pin = sha(self.exporter_raw)
        self.m = module('generated_export', self.exporter_raw, str(self.exporter_path))
        self.producer_path = self.review / 'model_benchmark.py'
        self.producer_pin = sha(self.producer_path.read_bytes())
        raw = (self.review / 'benchmark_physical_controller.py').read_bytes()
        self.controller = closure(raw, 'benchmark_physical_controller.py',
                                  ['need', 'require_complete_artifact_members'], exclude={'adapter'})
        parse = [n for n in ast.walk(ast.parse(raw)) if isinstance(n, ast.FunctionDef) and n.name == 'parse']
        body = parse[0].body if len(parse) == 1 else []
        start = [i for i, n in enumerate(body) if isinstance(n, ast.Assign) and
                 any(isinstance(t, ast.Name) and t.id == 'resources_raw' for t in n.targets)]
        statements = body[start[0]:start[0] + 4] if len(start) == 1 else []
        text = ''.join(map(ast.unparse, statements))
        if not all(x in text for x in ('SPRY_RESOURCE_V1', 'exact initial/final resource records required',
                                       'artifacts._json', 'resource record phase order')):
            raise ValueError('generated controller resource parse drift')
        self.resource_parse = compile(ast.Module(body=statements, type_ignores=[]), 'controller-resources', 'exec')
        self.reader = closure((self.review / 'serving_integration.py').read_bytes(), 'serving_integration.py',
                              ['_read', '_json', '_unpack_client', '_decode_member', 'MODE'])
        self.read_bounded = closure(self.producer_path.read_bytes(), 'model_benchmark.py',
                                    ['read_bounded_file'])['read_bounded_file']
        identities = json.loads((author / 'PACKAGE-IDENTITIES.json').read_bytes())
        self.session = identities['session_sha256']
        self.spec = sha((self.review / 'BENCHMARK-SPEC2.json').read_bytes())

    def binding(self, arm='bf16'):
        return dict(session_sha256=self.session, arm=arm, spec_sha256=self.spec, experiment_mode=HTTP_MODE)


class Fixtures:
    def __init__(self):
        root = Path(os.environ['BUNDLE_EXPORT_FIXTURE']).resolve()
        raw = {name: checked(root / name, pin) for name, pin in FIXTURE_PINS.items()}
        client = json.loads(raw['http-client-bundle.json'])
        self.client = {n: base64.b64decode(r['base64'], validate=True) for n, r in client['files'].items()}
        native = json.loads(raw['native-http-bundle.json'])
        self.gate = base64.b64decode(native['gate']['base64'], validate=True)
        self.workers = native['workers']
        self.observations = {n: base64.b64decode(r['base64'], validate=True)
                             for n, r in native['observations'].items()}
        self.result = raw['result.json']
        stdout = checked(os.environ['BUNDLE_OP223_STDOUT'], OP223_STDOUT)
        lines = stdout.splitlines(keepends=True)
        self.resources = [x for x in lines if x.startswith(b'SPRY_RESOURCE_V1 ')]
        self.logs = b''.join(x for x in lines if not x.startswith(b'SPRY_'))
        assert len(self.resources) == 2 and len(self.logs) == 6219
        self.compiler = json.loads(checked(os.environ['BUNDLE_COMPILER_PINS'], COMPILER_PINS))

    def resource(self, phase, size=None):
        line = self.resources[0 if phase == 'initial' else 1]
        if size is None:
            return line
        value = json.loads(line[len(b'SPRY_RESOURCE_V1 '):])
        return b'SPRY_RESOURCE_V1 ' + padded_json(value, size) + b'\n'

    def four_member_native(self, size=None):
        """Real verify-prefetch native files plus the four-member receipt names."""
        files = {'NATIVE-READINESS.json': self.gate, **self.observations}
        for worker in self.workers:
            files['worker-rank-%d.json' % worker['rank']] = json.dumps(worker, sort_keys=True).encode()
        for rank in (0, 1):
            files['FULL-ENABLED-rank-%d.json' % rank] = padded_json(dict(rank=rank), 4096)
            files['BUNDLE-STARTUP-rank-%d.json' % rank] = padded_json(dict(rank=rank, compiler=self.compiler), RECORD)
            files['BUNDLE-NUMERICS-rank-%d.json' % rank] = padded_json(dict(rank=rank), RECORD)
        files['BUNDLE-HTTP.json'] = padded_json({}, RECORD)
        if size is not None:
            remaining, index = size - sum(map(len, files.values())), 0
            while remaining:
                chunk = min(remaining, 32 << 10)
                files['filler/observation-%03d.bin' % index] = random.Random(index).randbytes(chunk)
                remaining, index = remaining - chunk, index + 1
        return files


class ExportCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g, cls.f = Generated(), Fixtures()
        cls.m = cls.g.m
        cls.tmp = canonical_dir('bundle-export-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def output(self, name, client=None, native=None, result=b'{}'):
        root = self.tmp / name
        root.mkdir(mode=0o700)
        output = root / 'output'
        output.mkdir(mode=0o700)
        if client is not None:
            (output / 'client').mkdir()
            write_tree(output / 'client', client)
        if native is not None:
            (output / 'native').mkdir()
            write_tree(output / 'native', native)
        if result is not None:
            (output / 'result.json').write_bytes(result)
        return root, output

    def arm(self, root, output, arm='bf16', before=None, after=None, fill=None, binding=None):
        """Producer child -> CapturePipes -> bounded read -> receive_bundle."""
        before = self.f.resource('initial') + self.f.logs if before is None else before
        after = self.f.resource('final') if after is None else after
        (root / 'before').write_bytes(before)
        (root / 'after').write_bytes(after)
        binding = self.g.binding(arm) if binding is None else binding
        limit = self.m.stream_limit(HTTP_MODE)
        cap = self.m.CapturePipes(root / (arm + '-capture'), stream_limit=limit, mode=HTTP_MODE)
        try:
            child = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', CHILD, str(self.g.exporter_path),
                                    self.g.exporter_pin, str(self.g.producer_path), self.g.producer_pin, str(output),
                                    json.dumps(binding), str(root / 'before'), str(root / 'after'),
                                    'none' if fill is None else str(fill)],
                                   stdout=cap.stdout, stderr=cap.stderr, timeout=240)
        finally:
            capture = cap.finish()
        stdout = self.g.read_bounded(root / (arm + '-capture/stdout.raw'), limit)
        stderr = self.g.read_bounded(root / (arm + '-capture/stderr.raw'), limit)
        found = re.findall(rb'^STDOUT_BYTES (\d+)$', stderr, re.M)
        self.assertEqual(len(found), 1)
        written = int(found[0])
        self.assertEqual((capture['streams']['stdout']['bytes'], len(stdout)), (written, written))
        self.assertEqual(capture['streams']['stdout']['sha256'], sha(stdout))
        return child.returncode, capture, stdout, stderr

    def receive(self, root, stdout, stderr=b'', arm='bf16', binding=None):
        binding = self.g.binding(arm) if binding is None else binding
        destination = root / (arm + '-artifacts')
        receipt = self.m.receive_bundle(stdout, stderr, destination, binding)
        return receipt, destination

    def controller_checks(self, receipt, destination, stdout):
        self.g.controller['require_complete_artifact_members'](
            receipt, {'experiment_mode': HTTP_MODE}, destination, None, self.m)
        space = dict(stdout=stdout, artifacts=self.m, need=self.g.controller['need'])
        exec(self.g.resource_parse, space)
        return space['records']

    def readback(self, root, destination, client, native, result, arm='bf16'):
        """Generated reader decode; byte equality with the producer trees."""
        r = self.g.reader
        client_raw = r['_read'](destination / 'http-client-bundle.json')
        native_raw = r['_read'](destination / 'native-http-bundle.json')
        self.assertEqual(r['_read'](destination / 'result.json'), result)
        extracted, _ = r['_unpack_client'](client_raw, root / (arm + '-http-client-tree'))
        self.assertEqual(tree_bytes(extracted), client)
        value = r['_json'](native_raw)
        self.assertEqual(set(value), {'schema', 'mode', 'gate', 'workers', 'observations'})
        self.assertEqual(value['mode'], r['MODE'])
        expected = dict(native)
        self.assertEqual(r['_decode_member'](value['gate']), expected.pop('NATIVE-READINESS.json'))
        self.assertEqual(value['workers'], [json.loads(expected.pop('worker-rank-%d.json' % i)) for i in (0, 1)])
        decoded = {n: r['_decode_member'](row) for n, row in value['observations'].items()}
        self.assertEqual(decoded, expected)
        return dict(client_bundle_bytes=len(client_raw), native_bundle_bytes=len(native_raw),
                    extracted_client_bytes=sum(map(len, tree_bytes(extracted).values())),
                    decoded_native_bytes=sum(map(len, decoded.values())) + len(r['_decode_member'](value['gate'])))

    def line(self, stdout):
        lines = [x for x in stdout.splitlines(keepends=True) if x.startswith(self.m.PREFIX)]
        self.assertEqual(len(lines), 1)
        return lines[0]


class RoundtripTests(ExportCase):
    def test_representative_complete_receipts_roundtrip(self):
        native = self.f.four_member_native()
        root, output = self.output('representative', self.f.client, native, self.f.result)
        code, capture, stdout, stderr = self.arm(root, output)
        self.assertEqual((code, capture['capture_complete']), (0, True), stderr[-2000:])
        receipt, destination = self.receive(root, stdout, stderr)
        self.assertTrue(receipt['transport_valid'] and receipt['all_members_present'])
        self.assertFalse(receipt['qualification'])
        records = self.controller_checks(receipt, destination, stdout)
        self.assertEqual([r['phase'] for r in records], ['initial', 'final'])
        sizes = self.readback(root, destination, self.f.client, native, self.f.result)
        self.assertLessEqual(len(self.line(stdout)), self.m.encoded_limit(self.g.binding()))
        self.assertEqual(len(self.f.client), 66)
        self.assertGreater(sizes['client_bundle_bytes'], 20 << 20)

    def test_tiny_and_parent_reproduction_cases_now_export(self):
        for name, members in (('empty', {}), ('tiny', dict.fromkeys(self.m.HTTP_MEMBER_LIMITS, b'{}'))):
            directory = self.tmp / ('direct-' + name)
            directory.mkdir(mode=0o700)
            write_tree(directory, members)
            sink = io.BytesIO()
            self.m.export_bundle(directory, self.g.binding(), sink)
            receipt, destination = self.receive(directory, sink.getvalue())
            self.assertEqual(receipt['all_members_present'], bool(members))
            if not members:
                with self.assertRaisesRegex(ValueError, 'complete bound HTTP result bundle required'):
                    self.controller_checks(receipt, destination, sink.getvalue())
        root, output = self.output('tiny-producer', {'RUN-SUMMARY.json': b'{}'},
                                   {'NATIVE-READINESS.json': b'{}'}, b'{}')
        code, capture, stdout, stderr = self.arm(root, output)
        self.assertEqual((code, capture['capture_complete']), (0, True), stderr[-2000:])
        receipt, destination = self.receive(root, stdout, stderr)
        self.assertTrue(receipt['all_members_present'])
        self.controller_checks(receipt, destination, stdout)

    def test_missing_and_incomplete_exports_fail_closed(self):
        # No result: the generated producer block exports nothing.
        root, output = self.output('missing-result', {'a': b'1'}, {'b': b'2'}, None)
        code, capture, stdout, stderr = self.arm(root, output)
        self.assertEqual((code, capture['capture_complete']), (0, True))
        self.assertNotIn(self.m.PREFIX, stdout)
        with self.assertRaisesRegex(ValueError, 'exactly one bounded V2 artifact bundle'):
            self.receive(root, stdout, stderr)
        # Result without native/client trees exports, but the reader refuses it.
        root, output = self.output('missing-trees', None, None, b'{}')
        code, capture, stdout, stderr = self.arm(root, output)
        receipt, destination = self.receive(root, stdout, stderr)
        self.assertTrue(receipt['all_members_present'])
        with self.assertRaisesRegex(ValueError, 'HTTP client bundle schema'):
            self.g.reader['_unpack_client'](self.g.reader['_read'](destination / 'http-client-bundle.json'),
                                            root / 'bf16-http-client-tree')
        # Staging without packing leaves two members explicitly absent.
        root, output = self.output('unpacked', None, None, b'{}')
        sink = io.BytesIO()
        self.m.export_bundle(self.m.stage_artifacts(output, self.g.binding()), self.g.binding(), sink)
        receipt, destination = self.receive(root, sink.getvalue())
        self.assertEqual(receipt['members']['native-http-bundle.json'], {'exists': False})
        with self.assertRaisesRegex(ValueError, 'complete bound HTTP result bundle required'):
            self.controller_checks(receipt, destination, sink.getvalue())

    def test_just_over_member_bounds_refuse_before_export(self):
        cases = (('result', None, None, b'x' * (8 * MIB + 1), 'bounded regular singly linked artifact required'),
                 ('client', {'a.bin': b'x' * (8 * MIB), 'b.bin': b'x' * (8 * MIB), 'c.bin': b'x' * (8 * MIB),
                             'd.bin': b'x'}, None, b'{}', 'HTTP evidence aggregate bound'),
                 ('client-file', {'a.bin': b'x' * (8 * MIB + 1)}, None, b'{}',
                  'bounded regular singly linked artifact required'),
                 ('native', None, {'a.bin': b'x' * (5 * MIB), 'b.bin': b'x'}, b'{}', 'HTTP evidence aggregate bound'))
        for name, client, native, result, message in cases:
            root, output = self.output('over-' + name, client, native, result)
            code, capture, stdout, stderr = self.arm(root, output)
            self.assertNotEqual(code, 0)
            self.assertIn(message.encode(), stderr)
            self.assertNotIn(self.m.PREFIX, stdout)


class ReceiverRefusalTests(ExportCase):
    def setUp(self):
        directory = self.tmp / ('base-' + self.id().rsplit('.', 1)[1])
        directory.mkdir(mode=0o700)
        write_tree(directory, dict.fromkeys(self.m.HTTP_MEMBER_LIMITS, b'{"x":1}'))
        sink = io.BytesIO()
        self.value = self.m.export_bundle(directory, self.g.binding(), sink)
        self.raw, self.root = sink.getvalue(), directory

    def refuse(self, stdout, message, binding=None):
        with self.assertRaisesRegex(ValueError, message):
            self.receive(self.root, stdout, binding=binding)
        self.assertFalse((self.root / 'bf16-artifacts').exists())

    def test_truncated_foreign_extra_duplicate_and_tampered_records(self):
        self.refuse(self.raw[:-1], 'incomplete artifact record line')
        with self.assertRaises(ValueError):
            self.receive(self.root, self.raw[:len(self.raw) // 2] + b'\n')
        self.assertFalse((self.root / 'bf16-artifacts').exists())
        self.refuse(self.raw, 'artifact schema/binding mismatch', dict(self.g.binding(), session_sha256='0' * 64))
        self.refuse(self.raw, 'artifact schema/binding mismatch', self.g.binding('kv4'))
        self.refuse(self.raw + self.raw, 'exactly one bounded V2 artifact bundle')
        extra = json.loads(self.raw[len(self.m.PREFIX):])
        extra['members']['extra.json'] = {'exists': False}
        self.refuse(self.m.PREFIX + self.m._canonical(extra) + b'\n', 'exact artifact members required')
        tampered = json.loads(self.raw[len(self.m.PREFIX):])
        tampered['members']['result.json']['sha256'] = '0' * 64
        self.refuse(self.m.PREFIX + self.m._canonical(tampered) + b'\n', 'artifact payload identity')
        self.receive(self.root, self.raw)

    def test_exporter_refuses_extra_staged_member(self):
        (self.root / 'extra.json').write_bytes(b'{}')
        sink = io.BytesIO()
        with self.assertRaisesRegex(ValueError, 'extra export member or directory'):
            self.m.export_bundle(self.root, self.g.binding(), sink)
        self.assertEqual(sink.getvalue(), b'')


class MaximumBudgetTests(ExportCase):
    """Near-limit legal members for both starts; exact stdout cap and overflow."""

    def max_inputs(self):
        # 512 files of 48 KiB with long names: exactly the 24 MiB client tree.
        client = {'paired/' + 'p' * 200 + '-%03d.json' % i: random.Random(i).randbytes(48 << 10) for i in range(512)}
        native = self.f.four_member_native(size=5 * MIB)
        result = padded_json({}, 8 * MIB)
        self.assertEqual((sum(map(len, client.values())), sum(map(len, native.values())), len(result)),
                         (24 * MIB, 5 * MIB, 8 * MIB))
        return client, native, result

    def test_maximum_two_starts_stream_and_session_budget(self):
        m, binding = self.m, self.g.binding()
        client, native, result = self.max_inputs()
        stream = m.stream_limit(HTTP_MODE)
        before = self.f.resource('initial', 64 << 10) + self.f.logs
        after = self.f.resource('final', 64 << 10)
        retained, report = {}, {}
        for arm in ('bf16', 'kv4'):
            root, output = self.output('max-' + arm, client, native, result)
            code, capture, stdout, stderr = self.arm(root, output, arm, before, after, fill=stream)
            self.assertEqual((code, capture['capture_complete'], len(stdout)), (0, True, stream), stderr[-2000:])
            line = self.line(stdout)
            value = json.loads(line[len(m.PREFIX):])
            encoded = sum(len(r['base64']) for r in value['members'].values())
            self.assertLessEqual(len(line), m.encoded_limit(binding))
            self.assertLessEqual(len(line), m.bundle_limit(binding))
            receipt, destination = self.receive(root, stdout, stderr, arm)
            records = self.controller_checks(receipt, destination, stdout)
            self.assertEqual(len(records), 2)
            sizes = self.readback(root, destination, client, native, result, arm)
            self.assertLessEqual(sizes['client_bundle_bytes'], m.HTTP_MEMBER_LIMITS['http-client-bundle.json'])
            self.assertLessEqual(sizes['native_bundle_bytes'], m.HTTP_MEMBER_LIMITS['native-http-bundle.json'])
            self.assertLessEqual(sizes['decoded_native_bytes'], 5 * MIB)
            shutil.rmtree(output)  # Sandbox tmpfs, not session retention.
            kept = (root / (arm + '-capture'), root / (arm + '-artifacts'), root / (arm + '-http-client-tree'))
            retained[arm] = sum(p.stat().st_size for d in kept for p in d.rglob('*') if p.is_file())
            report[arm] = dict(line_bytes=len(line), base64_bytes=encoded, framing_bytes=len(line) - encoded,
                               stdout_bytes=len(stdout), stderr_bytes=len(stderr),
                               non_bundle_stdout_bytes=len(stdout) - len(line), retained_bytes=retained[arm], **sizes)
            if arm == 'bf16':
                self.overflow_and_receiver_cut(root, stdout, line, before, after)
            shutil.rmtree(root)
        partition = (2 * stream + sum(m.HTTP_MEMBER_LIMITS.values()) + 24 * MIB + 5 * MIB + MIB)
        fixed = (m.SOURCE_COPIES_LIMIT + m.OBSERVER_ALL_COPIES_LIMIT + m.OUTER_STDIO_LIMIT +
                 m.SUMMARY_AND_CLOSEOUT_LIMIT + m.SMALL_METADATA_LIMIT)
        self.assertEqual(m.HTTP_SESSION_MAX_BYTES, 2 * partition + fixed)
        self.assertEqual(m.HTTP_SESSION_MAX_BYTES, 529006592)
        self.assertLess(m.HTTP_SESSION_MAX_BYTES, 512 * MIB)
        self.assertTrue(all(value <= partition - MIB for value in retained.values()))
        self.assertLessEqual(sum(retained.values()) + fixed, m.HTTP_SESSION_MAX_BYTES)
        out = os.environ.get('BUNDLE_EXPORT_REPORT')
        if out:
            with fresh_report(out).open('x') as handle:
                handle.write(json.dumps(dict(schema=1, arms=report, partition_bytes=partition,
                    fixed_partition_bytes=fixed, session_max_bytes=m.HTTP_SESSION_MAX_BYTES,
                    encoded_limit=m.encoded_limit(binding), bundle_limit=m.bundle_limit(binding),
                    stream_limit=stream, exporter_sha256=self.g.exporter_pin), indent=2, sort_keys=True) + '\n')

    def overflow_and_receiver_cut(self, root, stdout, line, before, after):
        m, binding = self.m, self.g.binding()
        # One byte over the stream: the actual bounded drain and the receiver refuse.
        source = root / 'stdout-plus-one'
        source.write_bytes(stdout + b'x')
        cap = m.CapturePipes(root / 'overflow-capture', stream_limit=m.stream_limit(HTTP_MODE), mode=HTTP_MODE)
        try:
            subprocess.run([sys.executable, '-I', '-B', '-S', '-c',
                            'import sys;sys.stdout.buffer.write(open(sys.argv[1],"rb").read())', str(source)],
                           stdout=cap.stdout, stderr=cap.stderr, timeout=60, check=True)
        finally:
            capture = cap.finish()
        self.assertEqual((capture['capture_complete'], capture['failure']), (False, 'stdout hard capture overflow'))
        self.assertEqual(capture['streams']['stdout']['bytes'], len(stdout))
        with self.assertRaisesRegex(ValueError, 'stdio capture overflow'):
            m.receive_bundle(stdout + b'x', b'', root / 'overflow-artifacts', binding)
        source.unlink()
        shutil.rmtree(root / 'overflow-capture')
        # Whitespace-padded lines at and one byte over the receiver bound.
        body = line[len(m.PREFIX):]
        for extra, accepted in ((0, True), (1, False)):
            pad = m.bundle_limit(binding) + extra - len(line)
            padded = m.PREFIX + b'{' + b' ' * pad + body[1:]
            destination = root / ('cut-%d' % extra)
            if accepted:
                receipt = m.receive_bundle(before + padded + after, b'', destination, binding)
                self.assertTrue(receipt['all_members_present'])
                shutil.rmtree(destination)
            else:
                with self.assertRaisesRegex(ValueError, 'exactly one bounded V2 artifact bundle'):
                    m.receive_bundle(before + padded + after, b'', destination, binding)
                self.assertFalse(destination.exists())


class MutationAndScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = Generated()
        cls.approved = checked(os.environ['BUNDLE_REFERENCE_EXPORTER'], APPROVED_EXPORTER)

    def test_reverting_derived_bound_reproduces_operation223_refusal(self):
        text = self.g.exporter_raw.decode()
        self.assertEqual((text.count(FIXED_LINE), text.count(STALE_LINE)), (1, 0))
        reverted = text.replace(FIXED_LINE, STALE_LINE).encode()
        self.assertEqual(sha(reverted), APPROVED_EXPORTER)
        self.assertEqual(reverted, self.approved)
        old = module('approved_export', reverted, 'approved/benchmark_artifacts.py')
        tmp = canonical_dir('bundle-revert-')
        try:
            for name, members in (('empty', {}), ('tiny', dict.fromkeys(old.HTTP_MEMBER_LIMITS, b'{}'))):
                directory = tmp / name
                directory.mkdir(mode=0o700)
                write_tree(directory, members)
                sink = io.BytesIO()
                with self.assertRaisesRegex(ValueError, 'encoded bundle overflow'):
                    old.export_bundle(directory, self.g.binding(), sink)
                self.assertEqual(sink.getvalue(), b'')
        finally:
            shutil.rmtree(tmp)
        binding = self.g.binding()
        self.assertGreater(old.encoded_limit(binding), old.bundle_limit(binding))
        self.assertEqual(self.g.m.bundle_limit(binding), old.encoded_limit(binding))

    def test_only_http_receiver_bound_changes(self):
        new, old = self.g.m, module('approved_export', self.approved, 'approved/benchmark_artifacts.py')
        left, right = ast.parse(self.approved), ast.parse(self.g.exporter_raw)
        changed = [a.name for a, b in zip(left.body, right.body, strict=True) if ast.dump(a) != ast.dump(b)]
        self.assertEqual(changed, ['bundle_limit'])
        constants = {k for k in vars(old) if k.isupper()}
        self.assertEqual(constants, {k for k in vars(new) if k.isupper()})
        self.assertEqual({k: getattr(old, k) for k in constants}, {k: getattr(new, k) for k in constants})
        modes = [None, 'v4-repeatability-control-v1', old.CAPTURE_MODE, old.NATIVE_CAPTURE_MODE,
                 old.DECODE_LAYER_CAPTURE_MODE, old.PREFILL_ROW_AUDIT_MODE, old.ASYNC_PREFILL_MODE,
                 'native-model-context-retrieval-32k-v1', old.INDEPENDENT_MODE, HTTP_MODE]
        for mode in modes:
            binding = {} if mode is None else {'experiment_mode': mode}
            stream = mode or 'model-benchmark-v1'
            self.assertEqual(new.member_limits(binding), old.member_limits(binding))
            self.assertEqual(new.encoded_limit(binding), old.encoded_limit(binding))
            self.assertEqual(new.stream_limit(stream), old.stream_limit(stream))
            if mode != HTTP_MODE:
                self.assertEqual(new.bundle_limit(binding), old.bundle_limit(binding))
        self.assertEqual((old.bundle_limit({'experiment_mode': HTTP_MODE}), new.bundle_limit({'experiment_mode': HTTP_MODE})),
                         (63 * MIB, 67117086))


class BuilderBoundTests(unittest.TestCase):
    """Canonical builder check; it imports the builder and runs on the build host."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERE))
        sys.path.insert(0, str(HERE.parents[2] / 'lib/workstation'))
        import build_bundle
        cls.build, cls.g = build_bundle, Generated()

    def test_builder_records_and_refuses_bound_chains(self):
        bounds = self.build.export_bounds(self.g.exporter_raw)
        self.assertEqual(bounds, dict(encoded_line_bytes=67117086, receiver_line_bytes=67117086,
                                      stream_bytes=65 * MIB, resource_record_bytes=131108, session_max_bytes=529006592))
        identities = json.loads((self.g.review.parents[1] / 'PACKAGE-IDENTITIES.json').read_bytes())
        self.assertEqual(identities['four_member']['http_export_bounds'], bounds)
        text = self.g.exporter_raw.decode()
        for old, new in ((FIXED_LINE, STALE_LINE),
                         (FIXED_LINE, FIXED_LINE.replace('encoded_limit(binding)', 'HTTP_STREAM_LIMIT')),
                         ('HTTP_STREAM_LIMIT = 65 << 20', 'HTTP_STREAM_LIMIT = 64 << 20')):
            self.assertEqual(text.count(old), 1)
            with self.assertRaisesRegex(ValueError, 'coherent HTTP export bound chain'):
                self.build.export_bounds(text.replace(old, new).encode())
        with self.assertRaisesRegex(ValueError, 'four-member source drift'):
            self.build.edit({'benchmark_artifacts.py': self.g.exporter_raw}, 'benchmark_artifacts.py', STALE_LINE, FIXED_LINE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
