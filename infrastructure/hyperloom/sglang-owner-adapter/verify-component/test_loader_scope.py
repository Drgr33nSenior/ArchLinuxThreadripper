"""Exact module/cache restoration and unchanged owner guard after real producer."""
import hashlib
import linecache
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import attention_ops as ops
import fake_payload
import source_loader
import stage_probe
import test_selected_imports_fixture as guard


class LoaderScopeTests(unittest.TestCase):
    def test_scope_keeps_aliases_then_restores_exact_existing_objects(self):
        names, paths = source_loader.ModuleScope.NAMES, source_loader.ModuleScope.PATHS
        modules = {name: types.ModuleType(name) for name in names}
        caches = {path: (1, None, ['prior'], path) for path in paths}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(linecache.cache, caches):
            with source_loader.ModuleScope():
                for name in names: sys.modules[name] = types.ModuleType(name)
                for path in paths: linecache.cache[path] = (0, None, [], path)
                self.assertTrue(all(sys.modules[n] is not modules[n] for n in names))
            for name in names: self.assertIs(sys.modules[name], modules[name])
            for path in paths: self.assertIs(linecache.cache[path], caches[path])

    def test_absent_aliases_removed_and_unrelated_entries_preserved_on_baseexception(self):
        with mock.patch.dict(sys.modules), mock.patch.dict(linecache.cache):
            for name in source_loader.ModuleScope.NAMES: sys.modules.pop(name, None)
            for path in source_loader.ModuleScope.PATHS: linecache.cache.pop(path, None)
            unrelated = types.ModuleType('scope_unrelated')
            with self.assertRaises(KeyboardInterrupt):
                with source_loader.ModuleScope():
                    sys.modules[source_loader.ModuleScope.NAMES[0]] = types.ModuleType('temporary')
                    linecache.cache[source_loader.SOURCE_PATH] = (0, None, [], source_loader.SOURCE_PATH)
                    sys.modules['scope_unrelated'] = unrelated
                    raise KeyboardInterrupt('partial-load fixture')
            self.assertTrue(all(n not in sys.modules for n in source_loader.ModuleScope.NAMES))
            self.assertTrue(all(p not in linecache.cache for p in source_loader.ModuleScope.PATHS))
            self.assertIs(sys.modules['scope_unrelated'], unrelated)

    def test_partial_real_jit_load_restores(self):
        raws = [(ops.ROOT/n).read_bytes() for n in ('decode_attention_baseline.py','decode_attention_candidate.py','metadata.py')]
        def fail(fn): raise RuntimeError('JIT fixture failure')
        names = source_loader.ModuleScope.NAMES
        with mock.patch.dict(sys.modules), mock.patch.dict(linecache.cache):
            before = {n:sys.modules.get(n) for n in names}
            lines = {p:linecache.cache.get(p) for p in source_loader.ModuleScope.PATHS}
            with self.assertRaisesRegex(RuntimeError, 'JIT fixture failure'):
                with source_loader.ModuleScope():
                    source_loader.load_pair(*raws,types.SimpleNamespace(jit=fail),types.SimpleNamespace(constexpr=object()))
            for n in names:self.assertIs(sys.modules.get(n),before[n])
            for p in lines:self.assertIs(linecache.cache.get(p),lines[p])

    def test_real_producer_restores_guard_closure_on_success_and_failures(self):
        # Run the unchanged protected guard, not a substitute origin predicate.
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve(); selected=root/'sglang'; selected.mkdir()
            modules={}; files={}
            for i,name in enumerate(source_loader.ModuleScope.NAMES):
                path=selected/(str(i)+'.py'); raw=b'# pinned selected fixture\n';path.write_bytes(raw)
                module=types.ModuleType(name);module.__file__=str(path)
                modules[name]=module;files[path.name]=hashlib.sha256(raw).hexdigest()
            inventory={'files':files}
            def closure():
                return guard.consumed_sources(inventory,modules={n:sys.modules[n] for n in modules},source_root=selected)
            with mock.patch.dict(sys.modules,modules), mock.patch.dict(linecache.cache):
                expected=closure()
                original_run=stage_probe.run
                def checked_run(*args,**kwargs):
                    value=original_run(*args,**kwargs)
                    # Check before fake_payload's patch.dict can restore modules.
                    self.assertEqual(closure(),expected)
                    for name in modules:self.assertIs(sys.modules[name],modules[name])
                    return value
                for i,variant in enumerate((None,'pin-mismatch','projection','clear-fails')):
                    with mock.patch.object(stage_probe,'run',side_effect=checked_run):
                        value,_=fake_payload.run(root/('run-'+str(i)),variant)
                    self.assertEqual(value['complete'],variant is None,value.get('failure'))
                    self.assertEqual(closure(),expected)
                    for name in modules:self.assertIs(sys.modules[name],modules[name])
                # The original leaked composition-path alias is still rejected.
                leaked=types.ModuleType(source_loader.ModuleScope.NAMES[0]);leaked.__file__=source_loader.SOURCE_PATH
                with mock.patch.dict(sys.modules,{leaked.__name__:leaked}):
                    with self.assertRaisesRegex(guard.ProbeFailure,'consumed source origin'):closure()

if __name__=='__main__':unittest.main(verbosity=2)
