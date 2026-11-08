"""Pinned entry seam for the four-member bundle; inert until explicitly called."""
from contextlib import contextmanager
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import sys

MODULE_PINS = None


def modules():
    root = Path(__file__).resolve().parent
    for name, pin in MODULE_PINS.items():
        path = root / (name + '.py')
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != pin:
            raise ValueError('bundle dependency source: '+name)
        existing = sys.modules.get(name)
        if existing is not None:
            spec = getattr(existing, '__spec__', None)
            if (getattr(existing, '__file__', None) != str(path) or spec is None or
                    spec.origin != str(path) or type(spec.loader) is not SourceFileLoader):
                raise ValueError('foreign bundle dependency: '+name)
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules['bundle_runtime']


@contextmanager
def worker(manifest, record, target, gpu_id, native, private, scheme):
    runtime = modules()
    c = sys.modules['bundle_contract']
    c.need(record['rank'] == gpu_id and runtime.STATE is None, 'fresh bundle worker scope')
    pins = json.loads(c.checked_file(Path(__file__).with_name('BUNDLE-COMPILER-PINS.json'),
                                     256 << 10, c.CONFIG['compiler_pins_sha256']))
    c.need(pins == c.CONFIG['compiler_records'], 'embedded/file compiler contract equality')
    ops = private(manifest['sources']['mxfp4_ops'])
    def dependencies():
        norm = native(manifest['sources']['bundle_norm'])
        linear = native(manifest['sources']['head_linear'])
        allocator = native(manifest['sources']['bundle_allocator'])
        def allocation_is_symmetric():
            # Native DP eligibility is true even with DP disabled. The selected
            # RowParallelLinear actually enters that pool only when enabled.
            c.need(linear.use_symmetric_memory is allocator.use_symmetric_memory,
                   'native F1 allocator call path identity')
            return linear.is_allocation_symmetric() and allocator.is_symmetric_memory_enabled()
        return dict(norm=norm, linear=linear, scheme=scheme,
                    quark=native(manifest['sources']['quark_config']),
                    model=native(manifest['sources']['head_model']),
                    fused=private(manifest['sources']['bundle_fused']),
                    host=private(manifest['sources']['bundle_host']),
                    get_forward=linear.get_forward,
                    is_allocation_symmetric=allocation_is_symmetric)
    state = runtime.configure(manifest, record, sys.modules['torch'], ops, dependencies, pins[manifest['arm']])
    graph = native(manifest['sources']['head_decode_runner'])
    sources = {key: {k: manifest['sources'][role][k] for k in ('path', 'sha256')}
               for key, role in (('scheduler', 'scheduler'), ('stage', 'bundle_stage'), ('nvtx', 'bundle_nvtx'))}
    try:
        with sys.modules['bundle_hooks'].worker_scope(target, sources, graph,
                {k: manifest['sources']['head_decode_runner'][k] for k in ('path', 'sha256')}):
            yield state
    finally:
        runtime.STATE = None


def attach_runner(runner):
    runtime = modules()
    runtime.STATE.dependencies = runtime.STATE.dependencies()
    return runtime.attach_runner(runner)


def startup():
    runtime = modules()
    row = runtime.startup_record()
    c = sys.modules['bundle_contract']
    state = runtime.STATE
    return c.publish(state.directory / ('BUNDLE-STARTUP-rank-%d.json' % state.worker['rank']), row)
