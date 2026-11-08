"""Audited JIT views for the private bundle; original module globals stay fixed."""
from types import FunctionType, SimpleNamespace

from bundle_contract import need, sha, same, canonical

PARTS = ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')


def compiler_record(compiled):
    need(set(PARTS) <= set(compiled.asm), 'five compiler artifacts required')
    parts = {}
    for name in PARTS:
        raw = compiled.asm[name]
        raw = raw.encode() if type(raw) is str else raw
        need(type(raw) is bytes and 0 < len(raw) <= 8 << 20, 'bounded compiler artifact')
        parts[name] = dict(bytes=len(raw), sha256=sha(raw))
    metadata = {k: getattr(compiled.metadata, k) for k in
                ('arch', 'global_scratch_size', 'profile_scratch_size', 'num_stages',
                 'num_warps', 'shared', 'warp_size', 'waves_per_eu')}
    return dict(parts=parts, metadata=metadata)


class Audited:
    """Warm outside capture, check exact pins, then enforce compiled-object identity."""
    def __init__(self, jit, expected, capturing, key, records):
        self.jit, self.expected, self.capturing = jit, expected, capturing
        self.key, self.records, self.cached = key, records, {}

    def warm(self, grid, args, kwargs):
        warmup = getattr(self.jit, 'warmup', None)
        if warmup is not None:
            return warmup(*args, grid=grid, **kwargs)
        # Triton's selected GEMM is wrapped in Heuristics, whose run() applies
        # EVEN_K before delegating to JITFunction.run(warmup=True).
        return self.jit.run(*args, grid=grid, warmup=True, **kwargs)

    def __getitem__(self, grid):
        def launch(*args, **kwargs):
            key = self.key(args, kwargs)
            if key is None:
                return self.jit[grid](*args, **kwargs)
            need(key in self.expected, 'undeclared bundle compiler specialization: '+key)
            if key not in self.cached:
                need(not self.capturing(), 'bundle specialization first seen inside capture')
                compiled = self.warm(grid, args, kwargs)
                observed = compiler_record(compiled)
                # Keep both records in the existing bounded stderr evidence.
                # Refusal must not discard the only observed compiler hashes.
                if not same(observed, self.expected[key]):
                    detail = canonical(dict(expected=self.expected[key], observed=observed)).decode()
                    need(False, 'bundle compiler drift: '+key+'; records='+detail)
                self.cached[key] = compiled
                self.records[key] = observed
            compiled = self.cached[key]
            # Recheck the cached artifact view before every host-side launch,
            # including graph construction. Graph replay never calls Python.
            need(same(compiler_record(compiled), self.expected[key]), 'cached bundle compiler changed')
            candidate = self.warm(grid, args, kwargs)
            need(candidate is compiled and same(compiler_record(candidate), self.expected[key]),
                 'launch arguments select a foreign bundle specialization')
            launched = self.jit[grid](*args, **kwargs)
            need(launched is compiled, 'bundle launched object differs from audited compile')
            return launched
        return launch


def function_view(function, replacements):
    need(type(function) is FunctionType and replacements and
         all(name in function.__globals__ for name in replacements), 'closed function view overrides')
    namespace = dict(function.__globals__)
    namespace.update(replacements)
    value = FunctionType(function.__code__, namespace, function.__name__,
                         function.__defaults__, function.__closure__)
    value.__kwdefaults__ = function.__kwdefaults__
    value.__annotations__ = function.__annotations__
    need(value.__code__ is function.__code__ and value.__closure__ is function.__closure__ and
         {k for k in namespace if namespace[k] is not function.__globals__[k]} == set(replacements),
         'only declared function globals may differ')
    return value


def attention_view(module, expected, capturing, records):
    """Clone only the grouped call chain. No native module or JIT is rebound."""
    stage1 = Audited(module._fwd_grouped_kernel_stage1, expected, capturing,
                     lambda args, kw: 'attention-stage1', records)
    stage2 = Audited(module._fwd_kernel_stage2, expected, capturing,
                     lambda args, kw: 'attention-stage2-cap'+str(kw['MAX_KV_SPLITS']), records)
    grouped = function_view(module._decode_grouped_att_m_fwd, {'_fwd_grouped_kernel_stage1': stage1})
    reduce = function_view(module._decode_softmax_reducev_fwd, {'_fwd_kernel_stage2': stage2})
    host = function_view(module.decode_attention_fwd_grouped,
                         {'_decode_grouped_att_m_fwd': grouped, '_decode_softmax_reducev_fwd': reduce})
    return function_view(module.decode_attention_fwd, {'decode_attention_fwd_grouped': host})


def gemm_key(args, kwargs):
    m, n, packed_k = args[5:8]
    if m not in (8, 16) or (n, packed_k) != (17408, 2560):
        return None
    return 'gate-up-m'+str(m)+'-wave'+str(kwargs['waves_per_eu'])


def kernel_view(kernels, expected, capturing, records):
    return SimpleNamespace(_dynamic_mxfp4_quant_kernel=kernels._dynamic_mxfp4_quant_kernel,
        _gemm_afp4wfp4_kernel=Audited(kernels._gemm_afp4wfp4_kernel, expected, capturing, gemm_key, records))
