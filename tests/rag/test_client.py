"""CPU checks for the fixture; real embeddings and ACLs require native-smoke.sh."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('rag_client', ROOT / 'tests/rag/client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)
QUESTIONS = json.loads((ROOT / 'tests/fixtures/rag/questions.json').read_text())


def readback(global_hybrid, **changes):
    return {**client.PILOT_CONFIG, 'ENABLE_RAG_HYBRID_SEARCH': global_hybrid,
            'ENABLE_RAG_HYBRID_SEARCH_ENRICHED_TEXTS': False, 'RELEVANCE_THRESHOLD': 0.0, **changes}


def arm_state(sources=None):
    arms = {arm: {'effective_mode': arm, 'readback': {'ENABLE_RAG_HYBRID_SEARCH': spec['global_hybrid']}}
            for arm, spec in client.ARMS.items()}
    return {'owner': {'token': 'private-fixture-value'}, 'shared': 'collection',
            'sources': {'file': 'docs/AI-PERFORMANCE.md'} if sources is None else sources, 'arms': arms}


def response(scored, count=1, file_id='file'):
    metadata = [{'file_id': file_id, **({'score': 0.5} if scored else {})} for _ in range(count)]
    return {'documents': [['public passage'] * count], 'metadatas': [metadata], 'distances': [[0.5] * count]}


class FakeOpenWebUI:
    """Loopback stand-in that follows the traced 0.11.4 dispatch, not the request label."""

    def __init__(self, data):
        self.data = data

    def __call__(self, method, route, *, token=None, body=None, raw=None, content_type=None, allowed=(200,)):
        with (self.data / '.rag-smoke-requests.jsonl').open('a') as stream:
            stream.write(json.dumps({'method': method, 'route': route}) + '\n')
        if 200 not in allowed:
            return None
        global_hybrid = os.environ['ENABLE_RAG_HYBRID_SEARCH'] == 'true'
        path = route.split('?')[0]
        if path == '/api/v1/auths/signup':
            return {'role': 'admin', 'token': 'admin-token', 'id': 'admin'}
        if path == '/api/v1/auths/add':
            return {'role': 'user', 'token': body['email'], 'id': body['email']}
        if path == '/api/v1/auths/':
            return {'role': 'user'}
        if path == '/api/v1/knowledge/create':
            return {'id': 'collection-' + body['name']}
        if path.endswith('/dirs/create'):
            return {'id': 'directory'}
        if path == client.CONFIG_ROUTE:
            return readback(global_hybrid)
        if path == client.QUERY_ROUTE:
            # retrieval.py:3152 then utils.py:714: hybrid=false still runs hybrid
            # whenever the global setting is true.
            hybrid = global_hybrid and (body.get('hybrid') is None or body['hybrid'])
            hybrid = hybrid or global_hybrid
            files = ['file-0', 'file-1', 'file-2'] if hybrid else ['file-1', 'file-0', 'file-2']
            metadata = [{'file_id': item, **({'score': 0.4} if hybrid else {})} for item in files]
            return {'documents': [[f'passage {item}' for item in files]], 'metadatas': [metadata],
                    'distances': [[0.4, 0.3, 0.2]]}
        return {}


class FixtureTests(unittest.TestCase):
    def test_metrics_are_source_level_and_exclude_abstention(self):
        self.assertEqual(client.metrics([], ['doc']), {'hit_at_3': None, 'source_recall_at_3': None})
        self.assertEqual(client.metrics(['a', 'b'], ['a', 'a']), {'hit_at_3': True, 'source_recall_at_3': 0.5})
        self.assertEqual(client.metrics(['a'], ['b']), {'hit_at_3': False, 'source_recall_at_3': 0.0})

    def test_real_questions_and_route_evidence(self):
        self.assertEqual(len(QUESTIONS), 10)
        self.assertEqual(len({q['id'] for q in QUESTIONS}), 10)
        self.assertEqual(sum(bool(q.get('must_abstain')) for q in QUESTIONS), 2)
        for arm in ('dense', 'hybrid'):
            with patch.object(client, 'request', return_value=response(arm == 'hybrid')) as http:
                row = client.query(arm_state(), QUESTIONS[0], arm, 0, measured=True)
            self.assertEqual(http.call_args.args[1], client.QUERY_ROUTE)
            self.assertEqual(http.call_args.kwargs['body']['hybrid'], arm == 'hybrid')
            self.assertEqual(http.call_args.kwargs['body']['k'], 3)
            self.assertEqual((row['mode'], row['global_hybrid'], row['request_hybrid']), (arm, arm == 'hybrid', arm == 'hybrid'))
            self.assertEqual(row['route'], 'POST ' + client.QUERY_ROUTE)
            self.assertEqual(row['rerank_score_present'], arm == 'hybrid')
            self.assertEqual(row['scores'], [0.5])
            self.assertEqual((row['supported_final_answer'], row['abstained']), ('NOT RUN', 'NOT RUN'))
            self.assertNotIn('private-fixture-value', json.dumps(row))
            self.assertEqual(len(row['passage_sha256'][0]), 64)

    def test_response_shape_must_match_effective_route(self):
        # A dense label answered by the hybrid path (the old defect) or the reverse fails.
        for arm in ('dense', 'hybrid'):
            with patch.object(client, 'request', return_value=response(arm != 'hybrid')):
                with self.assertRaisesRegex(client.Failure, 'contradicts its effective route'):
                    client.query(arm_state(), QUESTIONS[0], arm, 0, measured=True)

    def test_query_requires_matching_effective_readback(self):
        state = arm_state()
        state['arms']['dense']['readback']['ENABLE_RAG_HYBRID_SEARCH'] = True
        for broken in (state, {**arm_state(), 'arms': {}}):
            with patch.object(client, 'request', return_value=response(False)) as http:
                with self.assertRaisesRegex(client.Failure, 'effective-config readback'):
                    client.query(broken, QUESTIONS[0], 'dense', 0, measured=True)
            http.assert_not_called()

    def test_each_arm_asserts_its_own_global_config(self):
        for arm, global_hybrid in (('hybrid', True), ('dense', False)):
            env = str(global_hybrid).lower()
            with patch.dict(os.environ, {'ENABLE_RAG_HYBRID_SEARCH': env}):
                with patch.object(client, 'request', return_value=readback(global_hybrid)) as http:
                    evidence = client.arm_config('admin-token', arm)
                self.assertEqual(http.call_args.args, ('GET', client.CONFIG_ROUTE))
                self.assertEqual(evidence['readback']['ENABLE_RAG_HYBRID_SEARCH'], global_hybrid)
                self.assertEqual(evidence['readback']['RELEVANCE_THRESHOLD'], 0.0)
                self.assertEqual(evidence['profile'], client.ARMS[arm]['profile'])
                self.assertEqual(evidence['profile_env'], {'ENABLE_RAG_HYBRID_SEARCH': env})
                self.assertEqual(evidence['query_route'], 'POST ' + client.QUERY_ROUTE)
                self.assertNotIn('admin-token', json.dumps(evidence))
                # The other arm's effective config is refused, as is a pilot drift.
                for wrong in (readback(not global_hybrid), readback(global_hybrid, TOP_K=4), readback(int(global_hybrid))):
                    with patch.object(client, 'request', return_value=wrong):
                        with self.assertRaisesRegex(client.Failure, 'retrieval config differs'):
                            client.arm_config('admin-token', arm)
            with patch.dict(os.environ, {'ENABLE_RAG_HYBRID_SEARCH': str(not global_hybrid).lower()}):
                with patch.object(client, 'request', return_value=readback(global_hybrid)) as http:
                    with self.assertRaisesRegex(client.Failure, 'another retrieval profile'):
                        client.arm_config('admin-token', arm)
                http.assert_not_called()

    def test_dense_profile_changes_only_global_hybrid(self):
        profile = (ROOT / 'apps/overlays/rag/rag.env').read_text().splitlines()
        overlay = (ROOT / 'apps/overlays/rag-dense/kustomization.yaml').read_text()
        self.assertEqual(profile.count('ENABLE_RAG_HYBRID_SEARCH=true'), 1)
        self.assertEqual(re.findall(r'^ {6}- (\S+)$', overlay, re.M), ['ENABLE_RAG_HYBRID_SEARCH=false'])
        self.assertEqual({spec['profile'] for spec in client.ARMS.values()}, {'apps/overlays/rag', 'apps/overlays/rag-dense'})
        smoke = (ROOT / 'tests/rag/native-smoke.sh').read_text()
        self.assertIn('app=$(create_app "$root/apps/overlays/rag/rag.env")', smoke)
        self.assertIn('dense=$(create_app "$scratch/rag-dense.env")', smoke)
        self.assertIn('"$dense" python -B /rag-fixture/client.py dense', smoke)
        self.assertLess(smoke.index('docker stop --timeout 30 "$app"'), smoke.index('docker start "$dense"'))

    def test_unknown_citations_and_unbounded_hits_refused(self):
        for count in (1, 4):
            with patch.object(client, 'request', return_value=response(False, count, 'unknown')):
                with self.assertRaises(client.Failure):
                    client.query(arm_state({}), {'id': 'q', 'sources': ['doc'], 'question': 'query'}, 'dense', 0, measured=True)

    def test_phases_run_each_arm_under_its_own_profile_and_retain_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            data, fixture = Path(directory, 'data'), Path(directory, 'fixture')
            (fixture / 'corpus').mkdir(parents=True)
            data.mkdir()
            sources = []
            for index in range(4):
                content = f'public document {index}\n'.encode()
                (fixture / 'corpus' / f'{index}.md').write_bytes(content)
                sources.append({'source_path': f'docs/{index}.md', 'file': f'{index}.md',
                                'source_sha256': hashlib.sha256(content).hexdigest()})
            (fixture / 'corpus/corpus.json').write_text(json.dumps({'sources': sources}))
            (fixture / 'questions.json').write_text(json.dumps(QUESTIONS))
            uploads = iter(f'file-{index}' for index in range(4))
            fake = FakeOpenWebUI(data)
            base = {'RAG_FIXTURE_IMAGE': 'image', 'RAG_FIXTURE_REVISION': 'revision'}
            with patch.object(client, 'DATA', data), patch.object(client, 'INPUT', fixture), \
                    patch.object(client, 'request', fake), patch.object(client, 'upload', lambda path, token: fake('POST', '/api/v1/files/', token=token) or {'id': next(uploads)}), \
                    patch.object(client, 'collection_dimensions', lambda name: [1024]), \
                    patch.object(client, 'resources', dict), patch.object(client, 'version', lambda name: '0'):
                with patch.dict(os.environ, {**base, 'ENABLE_RAG_HYBRID_SEARCH': 'true'}):
                    client.run('initial')
                    # A dense arm started on the hybrid profile is refused before querying.
                    with self.assertRaisesRegex(client.Failure, 'another retrieval profile'):
                        client.run('dense')
                with patch.dict(os.environ, {**base, 'ENABLE_RAG_HYBRID_SEARCH': 'false'}):
                    client.run('dense')
                with patch.dict(os.environ, {**base, 'ENABLE_RAG_HYBRID_SEARCH': 'true'}):
                    client.run('restart')
            result = json.loads((data / '.rag-smoke-output.json').read_text())
            exported = (data / '.rag-smoke-output.json').read_text() + (data / '.rag-smoke-rows.jsonl').read_text()
        self.assertEqual(result['status'], 'functional-pass-evaluation-recorded')
        self.assertEqual(result['request_count'], client.REQUEST_COUNT)
        self.assertEqual({arm: evidence['readback']['ENABLE_RAG_HYBRID_SEARCH'] for arm, evidence in result['arms'].items()},
                         {'hybrid': True, 'dense': False})
        self.assertEqual(result['arms']['dense']['profile'], 'apps/overlays/rag-dense')
        self.assertIs(result['restart_arm']['readback']['ENABLE_RAG_HYBRID_SEARCH'], True)
        for arm in ('dense', 'hybrid'):
            rows = [r for r in result['rows'] if r['mode'] == arm]
            self.assertEqual((len(rows), sum(r['measured'] for r in rows)), (31, 30))
            self.assertTrue(all(r['global_hybrid'] is (arm == 'hybrid') and r['request_hybrid'] is (arm == 'hybrid')
                                and r['rerank_score_present'] is (arm == 'hybrid') for r in rows))
            self.assertEqual((result[arm]['supported_final_answer_rate'], result[arm]['abstention_rate']), ('NOT RUN', 'NOT RUN'))
            self.assertEqual((result[arm]['samples'], result[arm]['labelled_samples']), (30, 24))
        self.assertTrue(all(r['mode'] == 'hybrid' for r in result['restart_queries']))
        self.assertEqual(result['arm_comparison'], {'pairs': 30, 'identical_ranked_passages': 0, 'identical_ranked_sources': 0})
        self.assertNotIn('admin-token', exported)
        self.assertNotIn('rag-owner@example.com', exported)

    def test_http_failure_does_not_publish_body_or_token(self):
        class Response:
            status = 500
            def read(self, limit):
                return b'sensitive-response-body'
        @contextmanager
        def response(*args, **kwargs):
            yield Response()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(client, 'DATA', Path(directory)), patch.object(client, 'urlopen', response):
                with self.assertRaises(client.Failure) as failure:
                    client.request('GET', '/fixture', token='sensitive-fixture-token')
            report = str(failure.exception) + (Path(directory) / '.rag-smoke-requests.jsonl').read_text()
            self.assertNotIn('sensitive-response-body', report)
            self.assertNotIn('sensitive-fixture-token', report)
            self.assertIn('HTTP 500', report)

    def test_network_route_escape_refused(self):
        for route in ('https://remote.invalid/', '//remote.invalid/'):
            with self.assertRaises(client.Failure):
                client.request('GET', route)


if __name__ == '__main__':
    unittest.main()
