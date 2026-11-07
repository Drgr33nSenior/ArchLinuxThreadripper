"""Native CPU RAG fixture: loopback HTTP, synthetic identities, public documents.

No model generation, external endpoint, credential export or raw log publication.
"""
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import sqlite3
import statistics
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = 'http://127.0.0.1:8080'
DATA = Path('/app/backend/data')
INPUT = Path('/rag-fixture')
QUERY_ROUTE = '/api/v1/retrieval/query/collection'
CONFIG_ROUTE = '/api/v1/retrieval/config'
NOT_RUN = 'NOT RUN'
# Open WebUI 0.11.4 (8bd8b4fa) routers/retrieval.py:3152 takes the hybrid branch
# only when the global setting is true. Its hybrid=false branch (3179) calls
# retrieval/utils.py query_collection(request, ...), which re-reads
# rag.enable_hybrid_search (706-714) and runs hybrid search again. A request flag
# cannot select dense retrieval, so each arm runs under its own global profile.
ARMS = {
    'hybrid': {'profile': 'apps/overlays/rag', 'global_hybrid': True, 'request_hybrid': True},
    'dense': {'profile': 'apps/overlays/rag-dense', 'global_hybrid': False, 'request_hybrid': False},
}
PHASE_ARM = {'initial': 'hybrid', 'dense': 'dense', 'restart': 'hybrid'}
PILOT_CONFIG = {'TOP_K': 3, 'TOP_K_RERANKER': 3, 'CHUNK_SIZE': 192, 'CHUNK_OVERLAP': 32,
                'TEXT_SPLITTER': 'token_transformers', 'RAG_FULL_CONTEXT': False, 'HYBRID_BM25_WEIGHT': 0.5}
# Recorded, not asserted: they shape the hybrid path but are not pilot inputs.
RECORDED_CONFIG = ('ENABLE_RAG_HYBRID_SEARCH_ENRICHED_TEXTS', 'RELEVANCE_THRESHOLD', 'BYPASS_EMBEDDING_AND_RETRIEVAL')
# 22 setup/ingestion/access, 1+30 hybrid, 1+1+30 dense, 1+10 restart, 6 revocation.
REQUEST_COUNT = 102


class Failure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def request(method, route, *, token=None, body=None, raw=None, content_type=None, allowed=(200,)):
    require(route.startswith('/') and not route.startswith('//'), 'fixture route must be local')
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if body is not None:
        raw = json.dumps(body).encode()
        content_type = 'application/json'
    if content_type:
        headers['Content-Type'] = content_type
    started = time.monotonic()
    try:
        with urlopen(Request(BASE + route, data=raw, headers=headers, method=method), timeout=180) as response:
            code, payload = response.status, response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        code, payload = exc.code, exc.read(2048)
    require(len(payload) <= 2 * 1024 * 1024, 'bounded HTTP response required')
    with (DATA / '.rag-smoke-requests.jsonl').open('a') as stream:
        stream.write(json.dumps({'method': method, 'route': route, 'status': code,
                                 'seconds': time.monotonic() - started}) + '\n')
    require(code in allowed, f'HTTP {code} at {method} {route.split("?")[0]}; body_sha256={hashlib.sha256(payload).hexdigest()}')
    if code != 200:
        return None
    if route.split('?')[0].endswith('/content'):
        return payload
    return json.loads(payload) if payload else None


def upload(path, token):
    boundary = 'rag' + secrets.token_hex(16)
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
           'Content-Type: text/markdown\r\n\r\n').encode() + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    return request('POST', '/api/v1/files/?process=true&process_in_background=false', token=token,
                   raw=raw, content_type='multipart/form-data; boundary=' + boundary)


def metrics(expected, observed):
    expected, observed = set(expected), set(observed)
    if not expected:
        return {'hit_at_3': None, 'source_recall_at_3': None}
    return {'hit_at_3': bool(expected & observed),
            'source_recall_at_3': len(expected & observed) / len(expected)}


def arm_config(token, arm):
    """Assert one arm's process profile and effective global settings; return the evidence."""
    spec = ARMS[arm]
    profile = os.environ.get('ENABLE_RAG_HYBRID_SEARCH')
    require(profile == str(spec['global_hybrid']).lower(), f'{arm} arm started with another retrieval profile')
    config = request('GET', CONFIG_ROUTE, token=token)
    require(all(config.get(k) == v for k, v in PILOT_CONFIG.items())
            and config.get('ENABLE_RAG_HYBRID_SEARCH') is spec['global_hybrid'],
            f'actual parsed retrieval config differs from the {arm} profile')
    return {'arm': arm, 'profile': spec['profile'], 'profile_env': {'ENABLE_RAG_HYBRID_SEARCH': profile},
            'config_route': 'GET ' + CONFIG_ROUTE, 'query_route': 'POST ' + QUERY_ROUTE,
            'request_hybrid': spec['request_hybrid'], 'effective_mode': arm,
            'readback': {key: config.get(key) for key in ('ENABLE_RAG_HYBRID_SEARCH', *PILOT_CONFIG, *RECORDED_CONFIG)}}


def collection_dimensions(collection):
    # Read-only observation of the actual persisted collection dimensions.
    database = DATA / 'vector_db/chroma.sqlite3'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        return [row[0] for row in connection.execute('SELECT dimension FROM collections WHERE name = ?', (collection,))]


def query(state, question, arm, repetition, *, measured):
    effective = state.get('arms', {}).get(arm)
    require(effective and effective['effective_mode'] == arm
            and effective['readback']['ENABLE_RAG_HYBRID_SEARCH'] is ARMS[arm]['global_hybrid'],
            f'{arm} arm has no matching effective-config readback')
    started = time.monotonic()
    result = request('POST', QUERY_ROUTE, token=state['owner']['token'],
                     body={'collection_names': [state['shared']], 'query': question['question'],
                           'k': 3, 'k_reranker': 3, 'hybrid': ARMS[arm]['request_hybrid'],
                           'enable_enriched_texts': False})
    elapsed = time.monotonic() - started
    documents = result.get('documents', [[]])[0]
    metadata = result.get('metadatas', [[]])[0]
    scores = result.get('distances', [[]])[0]
    require(0 < len(documents) <= 3 and len(documents) == len(metadata) == len(scores), 'bounded matched retrieval hits required')
    # Route witness: only the hybrid RerankCompressor (utils.py:1842) adds
    # metadata['score']; the dense Chroma path returns stored chunk metadata.
    scored = [isinstance(item, dict) and 'score' in item for item in metadata]
    require(all(scored) if arm == 'hybrid' else not any(scored), f'{arm} response shape contradicts its effective route')
    file_ids = [item.get('file_id') for item in metadata]
    require(all(item in state['sources'] for item in file_ids), 'unknown source cannot become a citation')
    sources = [state['sources'][item] for item in file_ids]
    expected = [] if question.get('must_abstain') else question['sources']
    return {'question': question['id'], 'mode': arm, 'repetition': repetition,
            'measured': measured, 'route': 'POST ' + QUERY_ROUTE, 'request_hybrid': ARMS[arm]['request_hybrid'],
            'global_hybrid': effective['readback']['ENABLE_RAG_HYBRID_SEARCH'], 'rerank_score_present': all(scored),
            'retrieval_http_seconds': elapsed, 'file_ids': file_ids, 'sources': sources, 'scores': scores,
            'passage_sha256': [hashlib.sha256(text.encode()).hexdigest() for text in documents],
            **metrics(expected, sources),
            # No model generation occurs in this fixture.
            'supported_final_answer': NOT_RUN, 'abstained': NOT_RUN}


def summarize(rows, arm):
    selected = [r for r in rows if r['mode'] == arm and r['measured']]
    labelled = [r for r in selected if r['hit_at_3'] is not None]
    return {'samples': len(selected), 'labelled_samples': len(labelled),
            'hit_at_3': sum(r['hit_at_3'] for r in labelled) / len(labelled),
            'mean_source_recall_at_3': statistics.mean(r['source_recall_at_3'] for r in labelled),
            'supported_final_answer_rate': NOT_RUN, 'abstention_rate': NOT_RUN,
            'median_retrieval_http_seconds': statistics.median(r['retrieval_http_seconds'] for r in selected),
            'min_retrieval_http_seconds': min(r['retrieval_http_seconds'] for r in selected),
            'max_retrieval_http_seconds': max(r['retrieval_http_seconds'] for r in selected)}


def compare_arms(rows):
    """Observed ranking agreement only; not a quality or algorithm verdict."""
    measured = {(r['mode'], r['question'], r['repetition']): r for r in rows if r['measured']}
    pairs = [(measured[key], measured[('hybrid', *key[1:])]) for key in measured
             if key[0] == 'dense' and ('hybrid', *key[1:]) in measured]
    return {'pairs': len(pairs),
            'identical_ranked_passages': sum(d['passage_sha256'] == h['passage_sha256'] for d, h in pairs),
            'identical_ranked_sources': sum(d['file_ids'] == h['file_ids'] for d, h in pairs)}


def resources():
    result = {}
    for name in ('memory.peak', 'memory.current', 'memory.max', 'memory.swap.max'):
        path = Path('/sys/fs/cgroup') / name
        result[name] = path.read_text().strip() if path.exists() else None
    events = Path('/sys/fs/cgroup/memory.events')
    result['memory.events'] = dict(line.split() for line in events.read_text().splitlines()) if events.exists() else None
    if result['memory.events']:
        require(result['memory.events'].get('oom_kill') == '0', 'OOM kill observed')
    return result


def initialize(questions):
    credential = lambda: 'Fixture-' + secrets.token_urlsafe(24) + '-9!'
    admin = request('POST', '/api/v1/auths/signup', body={'name': 'RAG fixture admin',
                    'email': 'rag-admin@example.com', 'password': credential()})
    require(admin['role'] == 'admin', 'isolated first-user admin bootstrap required')
    users = {}
    for role in ('owner', 'outsider'):
        users[role] = request('POST', '/api/v1/auths/add', token=admin['token'],
            body={'name': 'RAG fixture ' + role, 'email': 'rag-' + role + '@example.com',
                  'password': credential(), 'role': 'user'})
        require(users[role]['role'] == 'user', 'ordinary test-user role required')
        require(request('GET', '/api/v1/auths/', token=users[role]['token'])['role'] == 'user', 'valid user required')
    # Signup remains disabled after the isolated bootstrap.
    request('POST', '/api/v1/auths/signup', body={'name': 'Refused', 'email': 'refused@example.com',
            'password': credential()}, allowed=(400, 401, 403))
    grants = [{'principal_type': 'user', 'principal_id': users['owner']['id'], 'permission': 'read'}]
    shared = request('POST', '/api/v1/knowledge/create', token=admin['token'],
                     body={'name': 'RAG fixture reviewed corpus', 'description': 'Public hashed documents', 'access_grants': grants})
    private = request('POST', '/api/v1/knowledge/create', token=admin['token'],
                      body={'name': 'RAG fixture private retention', 'description': 'ACL regression fixture', 'access_grants': []})
    hidden_dir = request('POST', f'/api/v1/knowledge/{private["id"]}/dirs/create', token=admin['token'],
                         body={'name': 'private-fixture-directory'})
    state = {'admin': admin, **users, 'shared': shared['id'], 'private': private['id'],
             'hidden_directory': hidden_dir['id'], 'sources': {}}
    state['arms'] = {'hybrid': arm_config(admin['token'], 'hybrid')}
    corpus = json.loads((INPUT / 'corpus/corpus.json').read_text())
    started = time.monotonic()
    for index, source in enumerate(corpus['sources']):
        document = INPUT / 'corpus' / source['file']
        require(hashlib.sha256(document.read_bytes()).hexdigest() == source['source_sha256'], 'corpus bytes changed')
        file = upload(document, admin['token'])
        if index == 0:
            request('POST', f'/api/v1/knowledge/{private["id"]}/file/add', token=admin['token'], body={'file_id': file['id']})
        request('POST', f'/api/v1/knowledge/{shared["id"]}/file/add', token=admin['token'], body={'file_id': file['id']})
        state['sources'][file['id']] = source['source_path']
    state['ingestion_seconds'] = time.monotonic() - started
    dimensions = collection_dimensions(state['shared'])
    require(dimensions == [1024], 'actual indexed collection must have 1024 dimensions')
    state['dimensions'] = dimensions[0]
    request('POST', QUERY_ROUTE, token=users['outsider']['token'],
            body={'collection_names': [shared['id']], 'query': questions[0]['question']}, allowed=(403, 404))
    request('GET', f'/api/v1/knowledge/{shared["id"]}/files?directory_id={hidden_dir["id"]}',
            token=users['owner']['token'], allowed=(403, 404))
    request('GET', '/api/v1/files/' + next(iter(state['sources'])) + '/content',
            token=users['outsider']['token'], allowed=(401, 403, 404))
    return state


def run(phase):
    os.umask(0o077)
    require(phase in PHASE_ARM, 'unknown phase')
    arm = PHASE_ARM[phase]
    questions = json.loads((INPUT / 'questions.json').read_text())
    require(len(questions) == 10, 'frozen ten-question pilot required')
    output = DATA / '.rag-smoke-output.json'
    auth = DATA / '.rag-smoke-auth.json'
    def evaluate(state, question, repetition, *, measured):
        row = query(state, question, arm, repetition, measured=measured)
        with (DATA / '.rag-smoke-rows.jsonl').open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        return row
    def measure(state):
        # One warmup, then three repetitions; both arms use the same question order.
        rows = [evaluate(state, questions[0], -1, measured=False)]
        for repetition in range(3):
            ordered = questions if repetition % 2 == 0 else questions[::-1]
            rows.extend(evaluate(state, question, repetition, measured=True) for question in ordered)
        return rows
    if phase == 'initial':
        state = initialize(questions)
        rows = measure(state)
        auth.write_text(json.dumps(state))  # Private fixture only; never exported.
        result = {'schema': 2, 'status': 'hybrid-arm-recorded', 'execution': 'disposable-linux-cpu',
                  'generation_quality': NOT_RUN, 'workstation_performance': NOT_RUN,
                  'image': os.environ['RAG_FIXTURE_IMAGE'], 'source_revision': os.environ['RAG_FIXTURE_REVISION'],
                  'corpus': json.loads((INPUT / 'corpus/corpus.json').read_text()),
                  'question_sha256': hashlib.sha256((INPUT / 'questions.json').read_bytes()).hexdigest(),
                  'rows': rows, 'arms': {'hybrid': state['arms']['hybrid']}, 'embedding_dimensions': state['dimensions'],
                  'ingestion_seconds': state['ingestion_seconds'], 'initial_resources': resources(),
                  'versions': {name: version(name) for name in ('torch', 'transformers', 'sentence-transformers', 'chromadb', 'rank-bm25')}}
        output.write_text(json.dumps(result, indent=2) + '\n')
        return
    state, result = json.loads(auth.read_text()), json.loads(output.read_text())
    if phase == 'dense':
        require(result['status'] == 'hybrid-arm-recorded', 'dense arm requires the recorded hybrid arm')
        state['arms']['dense'] = arm_config(state['admin']['token'], 'dense')
        # Same data volume and no reingestion: the dense arm queries the hybrid arm's index.
        require(collection_dimensions(state['shared']) == [state['dimensions']], 'dense arm must query the same index')
        result['rows'].extend(measure(state))
        result.update(status='both-arms-recorded', dense_resources=resources())
        result['arms']['dense'] = state['arms']['dense']
        auth.write_text(json.dumps(state))
        output.write_text(json.dumps(result, indent=2) + '\n')
        return
    require(result['status'] == 'both-arms-recorded', 'restart requires both recorded arms')
    state['arms']['hybrid'] = result['restart_arm'] = arm_config(state['admin']['token'], 'hybrid')
    rows = [evaluate(state, question, 3, measured=False) for question in questions]
    for row in rows:
        earlier = next(r for r in result['rows'] if r['question'] == row['question'] and r['mode'] == arm and r['repetition'] == 0)
        require(row['file_ids'] == earlier['file_ids'], 'same-volume restart changed ranked source IDs')
    first = next(iter(state['sources']))
    request('GET', f'/api/v1/files/{first}/content', token=state['owner']['token'])
    request('POST', f'/api/v1/knowledge/{state["shared"]}/file/remove?delete_file=false',
            token=state['admin']['token'], body={'file_id': first})
    request('GET', f'/api/v1/files/{first}/content', token=state['owner']['token'], allowed=(401, 403, 404))
    request('GET', f'/api/v1/files/{first}/content', token=state['admin']['token'])
    request('POST', f'/api/v1/knowledge/{state["shared"]}/access/update', token=state['admin']['token'], body={'access_grants': []})
    request('POST', QUERY_ROUTE, token=state['owner']['token'],
            body={'collection_names': [state['shared']], 'query': questions[0]['question']}, allowed=(403, 404))
    result.update(status='functional-pass-evaluation-recorded', restart_rankings_equal=True,
                  private_collection_denied=True, foreign_directory_denied=True,
                  removed_file_denied=True, revoked_collection_denied=True, signup_disabled=True,
                  restart_resources=resources())
    for name in ARMS:
        result[name] = summarize(result['rows'], name)
    result['arm_comparison'] = compare_arms(result['rows'])
    result['restart_queries'] = rows
    result['request_count'] = len((DATA / '.rag-smoke-requests.jsonl').read_text().splitlines())
    require(result['request_count'] == REQUEST_COUNT, 'frozen HTTP request accounting differs')
    output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    try:
        run(sys.argv[1])
        print(json.dumps({'phase': sys.argv[1], 'status': 'pass'}))
    except Exception as exc:
        # Fixed fixture failure text only; never publish HTTP bodies or tokens.
        message = str(exc) if isinstance(exc, Failure) else type(exc).__name__
        failure = {'schema': 2, 'status': 'failed', 'phase': sys.argv[1], 'reason': message,
                   'generation_quality': NOT_RUN, 'workstation_performance': NOT_RUN}
        (DATA / '.rag-smoke-failure.json').write_text(json.dumps(failure, indent=2) + '\n')
        print(json.dumps(failure))
        sys.exit(1)
