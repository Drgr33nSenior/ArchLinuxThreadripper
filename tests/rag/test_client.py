"""CPU checks for the fixture; real embeddings and ACLs require native-smoke.sh."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('rag_client', ROOT / 'tests/rag/client.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class FixtureTests(unittest.TestCase):
    def test_metrics_are_source_level_and_exclude_abstention(self):
        self.assertEqual(client.metrics([], ['doc']), {'hit_at_3': None, 'source_recall_at_3': None})
        self.assertEqual(client.metrics(['a', 'b'], ['a', 'a']), {'hit_at_3': True, 'source_recall_at_3': 0.5})
        self.assertEqual(client.metrics(['a'], ['b']), {'hit_at_3': False, 'source_recall_at_3': 0.0})

    def test_real_questions_and_modes(self):
        questions = json.loads((ROOT / 'tests/fixtures/rag/questions.json').read_text())
        self.assertEqual(len(questions), 10)
        self.assertEqual(len({q['id'] for q in questions}), 10)
        self.assertEqual(sum(bool(q.get('must_abstain')) for q in questions), 2)
        state = {'owner': {'token': 'private-fixture-value'}, 'shared': 'collection', 'sources': {'file': 'docs/AI-PERFORMANCE.md'}}
        for mode in ('dense', 'hybrid'):
            with patch.object(client, 'request', return_value={'documents': [['public passage']], 'metadatas': [[{'file_id': 'file'}]]}) as http:
                row = client.query(state, questions[0], mode, 0, measured=True)
            self.assertEqual(http.call_args.kwargs['body']['hybrid'], mode == 'hybrid')
            self.assertEqual(http.call_args.kwargs['body']['k'], 3)
            self.assertNotIn('private-fixture-value', json.dumps(row))
            self.assertEqual(len(row['passage_sha256'][0]), 64)

    def test_unknown_citations_and_unbounded_hits_refused(self):
        state = {'owner': {'token': 'fixture'}, 'shared': 'collection', 'sources': {}}
        for count in (1, 4):
            with patch.object(client, 'request', return_value={'documents': [['text'] * count], 'metadatas': [[{'file_id': 'unknown'}] * count]}):
                with self.assertRaises(client.Failure):
                    client.query(state, {'id': 'q', 'sources': ['doc'], 'question': 'query'}, 'dense', 0, measured=True)

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
