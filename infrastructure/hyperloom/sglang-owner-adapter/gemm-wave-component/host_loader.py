"""Scoped loading of complete host wrappers with their relative import intact.

The caller supplies an authenticated kernels module and runtime. This CPU stage
does not import Torch/Triton or assert real graph lifetime. The original source
is compiled whole, with no AST edits or replacement import statements.
"""
import hashlib
import json
import linecache
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parent
PREFIX = '_gemm_wave_host'
LEASE = '_gemm_wave_host_scope_lease'
SOURCE_PINS_SHA256 = '620d57bb3a00d3f2aee043a59fa0aa7b204d4ab540a3a4283042c3a12cdd2243'
FILES = {'geometry': 'prefill_reference.py', 'baseline': 'ops_baseline.py',
         'candidate': 'ops_candidate.py', 'negative': 'ops_negative_control.py'}


class HostScope:
    def __init__(self, runtime):
        self.runtime = runtime
        self.active = False

    def __enter__(self):
        if self.active:
            raise ValueError('host scope already active')
        self.absent = object()
        names = [PREFIX, PREFIX + '.prefill_reference'] + [PREFIX + '.' + v for v in ('baseline', 'candidate', 'negative')]
        self.old_modules = {k: sys.modules.get(k, self.absent) for k in names}
        self.paths = {key: '/mnt/review/gemm_wave_host/' + name for key, name in FILES.items()}
        self.old_lines = {p: linecache.cache.get(p, self.absent) for p in self.paths.values()}
        self.lease = types.ModuleType(LEASE)
        # One shared name also excludes a second copy of this loader module.
        if sys.modules.setdefault(LEASE, self.lease) is not self.lease:
            raise ValueError('one process-wide host scope')
        self.active = True
        try:
            raw_pins = (ROOT / 'SOURCE-PINS.json').read_bytes()
            if hashlib.sha256(raw_pins).hexdigest() != SOURCE_PINS_SHA256:
                raise ValueError('host source manifest identity')
            pins = json.loads(raw_pins)
            package = types.ModuleType(PREFIX)
            package.__path__ = []
            sys.modules[PREFIX] = package
            self.modules = {}
            for label, name in FILES.items():
                raw = (ROOT / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != pins[name]:
                    raise ValueError('host source identity: ' + name)
                suffix = 'prefill_reference' if label == 'geometry' else label
                mod = types.ModuleType(PREFIX + '.' + suffix)
                path = self.paths[label]
                mod.__file__, mod.__package__ = path, PREFIX
                linecache.cache[path] = (len(raw), None, raw.decode().splitlines(True), path)
                sys.modules[mod.__name__] = mod
                exec(compile(raw, path, 'exec'), mod.__dict__)
                if label != 'geometry':
                    # The complete wrapper's runtime normally imports frameworks.
                    # The component supplies its selected, pinned kernel module.
                    mod.runtime = self.runtime
                self.modules[label] = mod
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *_):
        if self.active:
            for mapping, values in ((sys.modules, self.old_modules), (linecache.cache, self.old_lines)):
                for key, value in values.items():
                    if value is self.absent:
                        mapping.pop(key, None)
                    else:
                        mapping[key] = value
            self.active = False
            if sys.modules.get(LEASE) is self.lease:
                del sys.modules[LEASE]
            else:
                error = RuntimeError('foreign host-scope lease replacement')
                if len(_) > 1 and _[1] is not None:
                    _[1].add_note(str(error))
                else:
                    raise error
        return False

    def call(self, arm, *args, card):
        if not self.active or arm not in ('baseline', 'candidate', 'negative'):
            raise ValueError('active host scope and declared route')
        return self.modules[arm].gemm_preallocated(*args, expected_device=card)
