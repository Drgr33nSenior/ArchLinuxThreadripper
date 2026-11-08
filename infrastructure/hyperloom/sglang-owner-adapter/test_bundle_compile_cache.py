"""CPU-only regressions for source-path contamination in compiler proofs."""
import ast
import base64
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest


class CompileCacheTests(unittest.TestCase):
    def setUp(self):
        self.old = '/mnt/review/w4a4_component/baseline/kernels.py'
        self.new = '/mnt/review/kernels.py'
        self.knobs = NS(compilation=NS(always_compile=False))
        self.calls = []
        self.cached = self.artifact(self.old)
        self.function = NS(signature={}, file_name=self.new, def_file_line_number=261,
                           def_file_col_number=1)
        self.row = dict(signature={}, constants={}, attrs={}, options={'waves_per_eu': 1})

    def artifact(self, path):
        raw = ('#loc = loc('+json.dumps(path)+':261:1)').encode()
        return NS(module=None, function=None, _run=None,
                  asm={key: raw.decode() if key != 'hsaco' else raw for key in
                       ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')}, metadata=NS(arch='gfx1201'))

    def compile(self, source, target, options):
        self.calls.append((self.knobs.compilation.always_compile, source.file_name, options))
        return self.artifact(source.file_name) if self.knobs.compilation.always_compile else self.cached

    def load(self, mutate=False):
        tree = ast.parse(Path(__file__).with_name('bundle_compile_target.py').read_bytes())
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'compile_record')
        if mutate:
            branch = next(node for node in fn.body if isinstance(node, ast.Try))
            branch.body = [node for node in branch.body if not (
                isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == 'always_compile')]
        scope = dict(ast=ast, base64=base64, gzip=gzip, hashlib=hashlib, json=json,
                     triton=NS(compile=self.compile), knobs=self.knobs,
                     torch=NS(cuda=NS(is_initialized=lambda: False)), TARGET=object(),
                     PARTS=('ttir','ttgir','llir','amdgcn','hsaco'), META=('arch',),
                     ASTSource=lambda fn, *args: fn)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<target-test>', 'exec'), scope)
        return scope['compile_record']

    def test_warm_component_cache_cannot_supply_serving_artifact(self):
        record, blobs = self.load()(self.function, self.row)
        expected = self.artifact(self.new).asm['hsaco']
        self.assertEqual(record['parts']['hsaco']['sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(gzip.decompress(base64.b64decode(blobs['hsaco'])), expected)
        self.assertEqual(self.calls, [(True, self.new, self.row['options'])])
        self.assertFalse(self.knobs.compilation.always_compile)

    def test_deleted_fresh_compile_switch_is_detected(self):
        with self.assertRaisesRegex(AssertionError, 'compiled source origin'):
            self.load(mutate=True)(self.function, self.row)
        self.assertFalse(self.knobs.compilation.always_compile)

    def test_compile_exception_restores_switch(self):
        def fail(*args, **kwargs):
            raise RuntimeError('compiler failed')
        self.compile = fail
        with self.assertRaisesRegex(RuntimeError, 'compiler failed'):
            self.load()(self.function, self.row)
        self.assertFalse(self.knobs.compilation.always_compile)

    def test_existing_true_switch_is_preserved(self):
        self.knobs.compilation.always_compile = True
        self.load()(self.function, self.row)
        self.assertTrue(self.knobs.compilation.always_compile)

    def test_wrong_origin_refuses_even_with_fresh_switch(self):
        self.compile = lambda *args, **kwargs: self.artifact(self.old)
        with self.assertRaisesRegex(AssertionError, 'compiled source origin'):
            self.load()(self.function, self.row)

    def test_loaded_object_still_refuses(self):
        artifact = self.artifact(self.new)
        artifact.module = object()
        self.compile = lambda *args, **kwargs: artifact
        with self.assertRaises(AssertionError):
            self.load()(self.function, self.row)


if __name__ == '__main__':
    unittest.main()
