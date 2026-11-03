"""Actual generated caller, real input seals, inert driver and no external actions."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(sys.argv.pop(1)).resolve()
MATERIAL = json.loads((ROOT / 'CALLER-MATERIALIZATION.json').read_bytes())
RAW = Path(MATERIAL['caller']['path']).read_bytes()
assert hashlib.sha256(RAW).hexdigest() == MATERIAL['caller']['sha256']
PROPOSAL = json.loads(Path(MATERIAL['proposal']['path']).read_bytes())


def refuse_external(event, args):
    if event in ('subprocess.Popen', 'os.fork', 'os.posix_spawn', 'os.system') or event.startswith(('os.exec', 'socket.')):
        raise RuntimeError('caller test forbids external actions')


sys.addaudithook(refuse_external)


class CallerTests(unittest.TestCase):
    def invoke(self, root, grant, *, bad_hash=False):
        script = root / 'execute_once.py'; script.write_bytes(RAW)
        if grant is not None:
            (root / 'REGISTRATION.json').write_text(json.dumps(grant, sort_keys=True))
            pin = hashlib.sha256((root / 'REGISTRATION.json').read_bytes()).hexdigest()
        else: pin = '0' * 64
        calls = []

        def driver(*args):
            calls.append(args)
            return dict(status='INERT_DRIVER_BOUNDARY_ONLY')

        with patch.dict(sys.modules, {'operator_driver': types.SimpleNamespace(execute_once=driver)}), \
             patch.object(sys, 'argv', [str(script), '--registration-sha256', '0' * 64 if bad_hash else pin]), \
             patch.object(sys, 'path', list(sys.path)), patch('sys.stdout', new_callable=io.StringIO):
            exec(compile(RAW, str(script), 'exec'), {'__file__': str(script), '__name__': '__main__'})
        return calls

    def grant(self):
        return dict(copy.deepcopy(PROPOSAL), registered=True, proposal_only=False, global_attempt_index=999999)

    def test_registration_absent_proposed_wrong_digest_and_missing_index_refuse(self):
        for grant, bad_hash in ((None, False), (PROPOSAL, False), (self.grant(), True),
                                (dict(self.grant(), global_attempt_index=None), False),
                                (dict(self.grant(), global_attempt_index=True), False)):
            with self.subTest(grant=grant is not None, bad_hash=bad_hash), tempfile.TemporaryDirectory(prefix='verify-caller-') as td:
                root = Path(td).resolve()
                with self.assertRaises(AssertionError): self.invoke(root, grant, bad_hash=bad_hash)
                self.assertFalse((root / 'CALLER-INVOKED.json').exists())

    def test_foreign_seal_refuses_before_invocation_marker(self):
        grant = self.grant(); grant['seals']['owner']['manifest_sha256'] = '0' * 64
        with tempfile.TemporaryDirectory(prefix='verify-caller-') as td:
            root = Path(td).resolve()
            with self.assertRaises(AssertionError): self.invoke(root, grant)
            self.assertFalse((root / 'CALLER-INVOKED.json').exists())

    def test_exact_driver_binding_and_one_use_marker(self):
        with tempfile.TemporaryDirectory(prefix='verify-caller-') as td:
            root = Path(td).resolve(); calls = self.invoke(root, self.grant())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][:4], (MATERIAL['context']['path'], MATERIAL['context']['sha256'],
                                          MATERIAL['readiness']['path'], MATERIAL['readiness']['sha256']))
            self.assertEqual(calls[0][-1], MATERIAL['native_root'])
            self.assertTrue((root / 'CALLER-INVOKED.json').is_file())
            with self.assertRaises(FileExistsError): self.invoke(root, self.grant())


if __name__ == '__main__':
    unittest.main(verbosity=2)
