"""Regression evidence for the final-round contract defects; authored fixtures only."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import test_task_acceptance as fixtures
from test_task_acceptance import (Case, REPAIR, REPAIR_SEAL, PUBLICATION,
                                  PUBLICATION_SEAL, FIXTURES, checked_task, host_run)
import task_acceptance


class LifetimePublicationTests(Case):
    inputs = fixtures.FinalCheckTests.inputs

    def setUp(self):
        super().setUp()
        fixtures.Records.seen = []
        self.server = fixtures.http.server.ThreadingHTTPServer(("127.0.0.1", 0), fixtures.Records)
        fixtures.threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_writable_state_is_not_authoritative(self):
        workspace, options = self.inputs("wrong-state")
        state = self.fresh("observation.json")
        state.write_bytes(options["state"].read_bytes())
        options["state"] = state
        # Actual fixture tool policy: the agent can open this observation for
        # writing, despite being denied all source/configuration paths.
        def route(operation, name):
            target = Path(name).resolve()
            if workspace not in target.parents and target != state:
                return "denied"
            return task_acceptance.perform_probe(operation, name)
        state.write_bytes((FIXTURES / 'cases/record-publication/valid/state.json').read_bytes())
        result = checked_task(PUBLICATION, workspace, PUBLICATION_SEAL, agent_routes={"state-writer": route}, **options)
        self.assertNotEqual("accepted", result["verdict"])
        self.assertEqual("failed", result["components"]["protection"]["status"])


class LifetimeTests(Case):
    def test_read_only_route_has_explicit_unexposed_operations(self):
        workspace = self.repair_workspace('valid-repair')
        def read_only(operation, name):
            if operation != 'read':
                return 'unsupported'
            return task_acceptance.perform_probe(operation, name) if workspace in Path(name).resolve().parents else 'denied'
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run, agent_routes={'read-only-fixture':read_only})
        self.assertEqual('accepted', result['verdict'])
        controls = result['components']['protection']['controls']
        self.assertTrue(any(row['operation']=='rename' and row['status']=='not-applicable' for row in controls))

    def test_directory_mutation_route_cannot_establish_protection(self):
        workspace = self.repair_workspace("valid-repair")
        def route(operation, name):
            if workspace in Path(name).resolve().parents or operation in ("create", "rename", "unlink"):
                return task_acceptance.perform_probe(operation, name)
            return "denied"
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run,
                              agent_routes={"data-denied-directory-writable": route})
        self.assertNotEqual("accepted", result["verdict"])
        self.assertEqual("failed", result["components"]["protection"]["status"])

    def session(self, source, workspace, state=None):
        pin = self.fresh("pin")
        sealed = task_acceptance.seal(source)
        pin.write_text(sealed)
        def route(operation, name):
            if workspace not in Path(name).resolve().parents:
                return "denied"
            return task_acceptance.perform_probe(operation, name)
        attempt = task_acceptance.begin_attempt(source, workspace, sealed,
                                                agent_routes={"fixture-only": route},
                                                protected_configuration=[pin], state=state,
                                                expected_checker=task_acceptance.checker_identity())
        self.assertEqual("passed", attempt.before["status"])
        return attempt, pin

    def test_source_and_pin_swap_cannot_replace_evaluator_anchors(self):
        source = self.fresh("source")
        shutil.copytree(REPAIR, source)
        workspace = self.repair_workspace("hardcoded-answers")
        attempt, pin = self.session(source, workspace)
        original = attempt.expected_seal
        # Owned fixture directory only. Emulates the reviewer's successful
        # directory replacement after the evaluator has prepared the attempt.
        source.rename(self.fresh("old-source"))
        shutil.copytree(REPAIR, source)
        observations = source / 'oracle/observations.json'
        value = json.loads(observations.read_text())
        value['cases'] = [{'args':[[1, 9, 10], 10], 'expected':{'value':[[0, 2], [10, 1]]}}]
        observations.write_text(json.dumps(value))
        spec_path = source / 'acceptance.json'
        spec = json.loads(spec_path.read_text())
        spec['oracle']['references'] = [row for row in spec['oracle']['references'] if row['id'] != 'unvalidated-width']
        spec_path.write_text(json.dumps(spec))
        pin.write_text(task_acceptance.seal(source))
        result = task_acceptance.check_task(source, workspace, original, attempt=attempt, run=host_run)
        self.assertNotEqual('accepted', result['verdict'])
        self.assertEqual('attempt-trust-input-changed', result['components']['protection']['reason'])
        self.assertEqual('failed', result['components']['oracle']['status'])

    def test_registered_observation_is_an_immutable_snapshot(self):
        source = self.fresh('source')
        shutil.copytree(PUBLICATION, source)
        workspace = self.fresh('workspace')
        shutil.copytree(FIXTURES / 'cases/record-publication/wrong-state/workspace', workspace)
        state = self.fresh('state.json')
        state.write_bytes((FIXTURES / 'cases/record-publication/wrong-state/state.json').read_bytes())
        attempt, pin = self.session(source, workspace, state)
        state.write_bytes((FIXTURES / 'cases/record-publication/valid/state.json').read_bytes())
        result = task_acceptance.check_task(source, workspace, attempt.expected_seal, attempt=attempt)
        self.assertEqual('failed', result['components']['external_state']['status'])
        self.assertEqual('attempt-trust-input-changed', result['components']['protection']['reason'])
        self.assertNotEqual('accepted', result['verdict'])

    def test_parent_observation_has_no_mutable_dictionary_alias(self):
        workspace = self.fresh('workspace'); workspace.mkdir()
        attempt, pin = self.session(PUBLICATION, workspace)
        value = json.loads((FIXTURES / 'cases/record-publication/wrong-state/state.json').read_text())
        good = json.loads((FIXTURES / 'cases/record-publication/valid/state.json').read_text())
        attempt.observe(value)
        value.clear(); value.update(good)
        returned = attempt.snapshot
        returned.clear(); returned.update(good)
        result = task_acceptance.check_task(PUBLICATION, workspace, PUBLICATION_SEAL, attempt=attempt)
        self.assertEqual('failed', result['components']['external_state']['status'])
        self.assertNotEqual('accepted', result['verdict'])

    def test_full_checker_module_tree_is_probed(self):
        workspace = self.repair_workspace('valid-repair')
        seen = set()
        def route(operation, name):
            path = Path(name).resolve()
            seen.add(path.name)
            if workspace in path.parents:
                return task_acceptance.perform_probe(operation, name)
            if path.name == 'assistant_api.py' and operation == 'write':
                return 'allowed'  # Permission-only stand-in; never mutate the installed module.
            return 'denied'
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run, agent_routes={'module-writer':route})
        self.assertEqual('failed', result['components']['protection']['status'])
        self.assertTrue({'assistant_api.py', 'run_record.py', 'telemetry_power.py'} <= seen)

    def test_origin_normalization_and_failed_answer_are_preserved(self):
        self.assertEqual('https://example.com', task_acceptance.normalized_origin('HTTPS://EXAMPLE.COM:443/'))
        for value in ('https://example.com/path', 'https://user@example.com', 'https://example.com?x=1'):
            with self.assertRaises(ValueError):
                task_acceptance.normalized_origin(value)
        spec = {'oracle': {'applicable': False, 'reason': 'none'},
                'external_state': {'applicable': False, 'reason': 'none'},
                'linkage': {'applicable': False, 'reason': 'none'},
                'deliverable': {'answer': [{'id':'done', 'path':['done'], 'equals':True}]}}
        answer = self.fresh('answer.json'); answer.write_text('{"done":false}')
        result = task_acceptance.final_components(spec, self.temporary, answer, None, ())
        self.assertEqual('failed', result['deliverable']['status'])

    def test_unreadable_sibling_retains_manipulated_test_evidence(self):
        workspace = self.repair_workspace("rewritten-test")
        (workspace / 'unreadable').mkdir()
        (workspace / 'unreadable').chmod(0)
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run)
        self.assertEqual("changed", result["evidence"]["test_diff"]["status"])
        self.assertIn("test_windows.py", [row['path'] for row in result['evidence']['test_diff']['entries']])
        self.assertEqual("invalid", result["evidence"]["workspace_suite"]["status"])
        self.assertEqual("invalid", result["evidence"]["generated_tests"]["status"])

    def test_absolute_oracle_reader_really_steals_expectations_without_isolation(self):
        workspace = self.repair_workspace('reads-oracle')
        app = workspace / 'windows.py'
        app.write_text('OBSERVATIONS_PATH = ' + repr(str(REPAIR / 'oracle/observations.json')) + '\n' + app.read_text())
        result = checked_task(REPAIR, workspace, REPAIR_SEAL, run=host_run)
        self.assertEqual('accepted', result['verdict'])  # authored HOST control, deliberately not isolation proof
        self.assertFalse(result['components']['oracle']['run']['isolated'])

    def test_unreadable_editable_does_not_erase_a_known_invalid_editable(self):
        source = self.fresh('source')
        shutil.copytree(REPAIR, source)
        spec_path = source / 'acceptance.json'
        spec = json.loads(spec_path.read_text())
        spec['editable'].append('additional.py')
        spec_path.write_text(json.dumps(spec))
        workspace = self.repair_workspace('valid-repair')
        (workspace / 'windows.py').chmod(0)
        (workspace / 'additional.py').mkdir()
        result = checked_task(source, workspace, task_acceptance.seal(source), run=host_run)
        self.assertEqual('rejected', result['verdict'])
        self.assertEqual('failed', result['components']['oracle']['status'])
        self.assertEqual('editable-not-a-regular-file', result['components']['oracle']['reason'])

    def test_stderr_is_diagnostic_and_fixture_helpers_are_available(self):
        source = self.fresh('source')
        shutil.copytree(REPAIR, source)
        helper = source / 'fixture/helper.py'
        helper.write_text('value = 1\n')
        workspace = self.repair_workspace("valid-repair")
        program = workspace / 'windows.py'
        program.write_text('import helper\nimport sys\nprint("diagnostic", file=sys.stderr)\n' + program.read_text())
        result = checked_task(source, workspace, task_acceptance.seal(source), run=host_run)
        self.assertEqual('accepted', result['verdict'])
        self.assertNotEqual(result['components']['oracle']['run']['stdout_sha256'],
                            result['components']['oracle']['run']['output_sha256'])


class PinnedRuntimeTests(Case):
    def test_late_import_uses_verified_bytes_after_filesystem_module_swap(self):
        import importlib
        import sys
        import task_acceptance_session
        root = self.fresh('runtime')
        root.mkdir()
        name = 'acceptance_owned_late_module'
        path = root / (name + '.py')
        trusted = b'value = "trusted"\n'
        path.write_bytes(trusted)
        loader = task_acceptance_session.PinnedModules(root, {path.name: trusted})
        path.write_text('raise RuntimeError("swapped module executed")\n')
        sys.meta_path.insert(0, loader)
        try:
            module = importlib.import_module(name)
            self.assertEqual('trusted', module.value)
        finally:
            sys.meta_path.remove(loader)
            sys.modules.pop(name, None)

    def test_parent_session_protocol_preserves_receipt_and_detects_module_swap(self):
        import subprocess
        import sys
        for swap in (False, True):
            with self.subTest(swap=swap):
                runtime = self.fresh('runtime')
                runtime.mkdir()
                original = Path(task_acceptance.__file__).parent
                for path in original.glob('*.py'):
                    (runtime / path.name).write_bytes(path.read_bytes())
                identities = {path.name: task_acceptance.digest(path.read_bytes()) for path in runtime.glob('*.py')}
                workspace = self.fresh('workspace')
                workspace.mkdir()
                # Parent-side observation + artifact linkage, no executable oracle.
                source = self.fresh('source')
                source.mkdir()
                spec = {'schema':1,'kind':task_acceptance.SPEC_KIND,'task_id':'session-fixture','version':'1',
                        'oracle':{'applicable':False,'reason':'parent observation'},
                        'external_state':{'checks':[{'id':'observed','path':['ok'],'equals':True}]},
                        'deliverable':{'artifacts':[{'id':'entry','path':'entry.txt','format':'bytes'}]},
                        'linkage':{'checks':[{'id':'same','left':{'from':'state','path':['sha']},
                                             'right':{'from':'artifact-sha256','name':'entry'}}]}}
                (source / 'acceptance.json').write_text(json.dumps(spec))
                pin = self.fresh('pin'); pin.write_text(task_acceptance.seal(source))
                process = subprocess.Popen([sys.executable, '-I', '-B', str(runtime / 'task_acceptance_session.py')],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                begin = {'source':str(source),'workspace':str(workspace),'seal':task_acceptance.seal(source),
                         'checker':identities,'configuration':[str(pin)], 'routes':['fixture-only'],
                         'capability_lease':'fixed-fixture-policy','run_id':'session-run','attempt_id':'1'}
                process.stdin.write(json.dumps(begin)+'\n'); process.stdin.flush()
                result = None
                while True:
                    line = process.stdout.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    if 'probe' in message:
                        path = Path(message['path']).resolve()
                        observed = (task_acceptance.perform_probe(message['probe'], str(path))
                                    if workspace in path.parents else 'denied')
                        response = {'outcome':observed,'capability_lease':'fixed-fixture-policy'}
                    elif 'ready' in message:
                        self.assertTrue(message['ready'])
                        (workspace / 'entry.txt').write_bytes(b'produced artifact')
                        if swap:
                            (runtime / 'run_record.py').write_text('raise RuntimeError("swapped")\n')
                        response = {'capability_lease':'fixed-fixture-policy','observation':{
                            'ok':True,'sha':task_acceptance.digest(b'produced artifact')}}
                    else:
                        result = message['result']
                        break
                    process.stdin.write(json.dumps(response)+'\n'); process.stdin.flush()
                process.stdin.close()
                errors = process.stderr.read()
                process.wait(timeout=10)
                process.stdout.close(); process.stderr.close()
                self.assertIsNotNone(result, errors)
                task_acceptance.validate_result(result)
                self.assertEqual('unavailable' if swap else 'accepted', result['verdict'])
                self.assertEqual(3 if swap else 0, process.returncode)
                self.assertEqual(identities['task_acceptance.py'], result['checker_sha256'])
                if swap:
                    self.assertEqual('attempt-trust-input-changed', result['components']['protection']['reason'])

if __name__ == '__main__':
    unittest.main()
