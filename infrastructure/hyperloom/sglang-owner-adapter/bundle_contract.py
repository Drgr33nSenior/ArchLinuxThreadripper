"""Closed metadata contract for four-member serving qualification.

No framework imports, GPU authority, timing policy or owner lifecycle lives here.
The builder fills CONFIG from authenticated inputs before packaging this source.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat

CONFIG = None
MEMBERS = ('verify_prefetch', 'verify_cap32', 'gatednorm_quant', 'gate_up_wave')
MAX_RECORD_BYTES = 256 << 10
PHASE = None
SHADOW = False


def need(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def same(first, second):
    return canonical(first) == canonical(second)


def checked_file(path, limit, pin=None):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path, 'canonical bundle path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        meta = os.fstat(fd)
        need(stat.S_ISREG(meta.st_mode) and meta.st_size <= limit, 'bounded regular bundle file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        need(len(raw) == meta.st_size and len(raw) <= limit, 'bundle file changed or too large')
    finally:
        os.close(fd)
    need(pin is None or sha(raw) == pin, 'bundle file pin')
    return raw


def publish(path, value):
    raw = canonical(value)
    need(len(raw) <= MAX_RECORD_BYTES, 'bundle record byte bound')
    path = Path(path)
    need(path.parent.is_dir() and path.parent.resolve() == path.parent, 'bundle output parent')
    # The parent reads only after publication. Rename never replaces an old record.
    temporary = path.with_name(path.name + '.writing')
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()
    return dict(file=path.name, bytes=len(raw), sha256=sha(raw))


@contextmanager
def phase(value):
    global PHASE
    need(PHASE is None and value in (None, 'c1', 'c2'), 'bundle phase cannot nest')
    PHASE = value
    try:
        yield
    finally:
        PHASE = None


@contextmanager
def shadow():
    global SHADOW
    need(not SHADOW, 'recursive bundle shadow execution')
    SHADOW = True
    try:
        yield
    finally:
        SHADOW = False


def batch_phase(batch, probe_ids):
    """Only exact private RIDs select qualification; no text or cache heuristic."""
    rids = [request.rid for request in batch.reqs]
    need(all(type(rid) is str for rid in rids), 'request RID types')
    selected = [probe_ids.get(rid) for rid in rids]
    if not any(selected):
        return None
    need(all(selected) and len(set(selected)) == 1 and len(rids) in (1, 2),
         'qualification requests mixed with foreign work')
    kind = selected[0]
    need(kind == 'c2' or len(rids) == 1, 'C1 qualification batch')
    # Prefill has no numerical shadow work. DFlash verification runs synchronously
    # inside the native decode Scheduler call with overlap disabled by the contract.
    return kind if batch.forward_mode.is_decode() or batch.forward_mode.is_target_verify() else None


def layer_ids(config):
    text = config['text_config']
    need(text['num_hidden_layers'] == 64 and text['hidden_size'] == 5120 and
         text['intermediate_size'] == 17408 and text['linear_num_value_heads'] == 48 and
         text['linear_value_head_dim'] == 128 and len(text['layer_types']) == 64,
         'selected bundle model geometry')
    recurrent = [i for i, kind in enumerate(text['layer_types']) if kind == 'linear_attention']
    full = [i for i, kind in enumerate(text['layer_types']) if kind == 'full_attention']
    need(recurrent == [i for i in range(64) if i % 4 != 3] and
         full == list(range(3, 64, 4)), 'selected sparse global layer identities')
    return recurrent, full


def complete_counts(records, ids, tokens):
    need(type(records) is dict and set(records) == {str(i) for i in ids}, 'exact layer counter set')
    for row in records.values():
        need(type(row) is dict and set(row) == {str(t) for t in tokens} and
             all(type(n) is int and n > 0 for n in row.values()), 'both geometries engaged per layer')


def validate_numerics(record, identity, config):
    need(type(record) is dict and set(record) == {
        'schema', 'identity', 'status', 'members', 'f1', 'gemm', 'f1_fallback8',
        'device_api_count', 'probe_ids', 'scored', 'shadow_calls', 'capture_shadow_calls', 'compilers',
        'completed_monotonic', 'exact_members'},
        'bundle numerical fields')
    need(type(record['schema']) is int and record['schema'] == 1 and record['identity'] == identity and
         record['status'] == 'F1_GEMM_REAL_INPUT_EXACT' and record['members'] == list(MEMBERS) and
         record['exact_members'] == ['gatednorm_quant','gate_up_wave'] and
         record['probe_ids'] == config['probe_ids'] and record['scored'] is False and
         type(record['device_api_count']) is int and record['device_api_count'] > 0 and
         type(record['shadow_calls']) is int and record['shadow_calls'] > 0 and
         type(record['capture_shadow_calls']) is int and record['capture_shadow_calls'] == 0,
         'bundle numerical identity and scope')
    need(type(record['completed_monotonic']) in (int,float) and
         0 <= record['completed_monotonic'] < float('inf'), 'numerical completion time')
    ids = config['gdn_ids']
    complete_counts(record['gemm'], list(range(64)), (8, 16))
    need(type(record['f1_fallback8']) is bool, 'F1 fallback decision')
    complete_counts(record['f1'], ids, (16,) if record['f1_fallback8'] else (8, 16))
    expected = config['compiler_records'][identity['arm']]
    required = {'gate-up-m'+str(t)+'-wave'+str(w) for t in (8, 16) for w in (0, 1)}
    required.add('f1-t16')
    if not record['f1_fallback8']: required.add('f1-t8')
    need(type(record['compilers']) is dict and required <= set(record['compilers']) <= set(expected) and
         all(same(value, expected[key]) for key, value in record['compilers'].items()),
         'real-input compiler witnesses')
    return record


def worker_identity(manifest, worker):
    keys = ('arm', 'session_sha256', 'source_manifest_sha256')
    need(all(manifest[k] == worker[k] for k in keys), 'bundle worker/manifest join')
    need(type(worker['rank']) is int and worker['rank'] in (0, 1), 'bundle TP rank')
    return dict(**{k: manifest[k] for k in keys}, rank=worker['rank'], pid=worker['pid'],
                boot_id=worker['boot_id'], start_ticks=worker['start_ticks'],
                worker_identity_sha256=worker['identity_sha256'])
