"""Closed MXFP4 N32 experiment and source-derived metadata-only launch capture.

This module never imports a device framework. The original launcher executes
against inert tensor metadata after its entire source is authenticated.
"""
import hashlib
import types

OPS_SHA256 = "66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776"
KERNEL_SHA256 = "5571b10654697561455081455b6cd28d43314702f56c80160ef2e33e535b3693"
ANCHOR = "block_m, block_n, warps = (16, 64, 4) if m <= 16 else (128, 128, 8) if m >= 1024 else (64, 64, 4)"
CANDIDATE = ANCHOR.replace("(16, 64, 4)", "(16, 32, 4)")
SHAPES = (("qkvz_ba", 8240, 5120), ("qkv", 7168, 5120),
          ("out", 5120, 3072), ("gate_up", 17408, 5120), ("down", 5120, 8704))
CASES = tuple((f"{name}-m{m}", m, n, k) for name, n, k in SHAPES for m in (8, 16))


def require(value, reason):
    if not value:
        raise ValueError(reason)


def derive(baseline):
    require(type(baseline) is bytes and hashlib.sha256(baseline).hexdigest() == OPS_SHA256,
            "selected MXFP4 launcher source pin")
    require(baseline.count(ANCHOR.encode()) == 1, "single selected tile anchor")
    return baseline.replace(ANCHOR.encode(), CANDIDATE.encode())


def validate(baseline, candidate):
    require(candidate == derive(baseline), "only the M<=16 N64-to-N32 launcher edit is admitted")
    return dict(changed_function="gemm_preallocated", kernel_body_changed=False,
                baseline_tile=[16, 64, 4], candidate_tile=[16, 32, 4],
                affected_rows="1..16", measured_rows=[8, 16],
                adoption_authorized=False)


class Tensor:
    def __init__(self, shape, dtype, stride, card, pointer):
        self.shape, self.dtype, self._stride = tuple(shape), dtype, tuple(stride)
        self.device = types.SimpleNamespace(type="cuda", index=card)
        self.pointer = pointer

    def stride(self, dim=None):
        return self._stride if dim is None else self._stride[dim]

    def untyped_storage(self):
        return types.SimpleNamespace(data_ptr=lambda: self.pointer)

    def record(self):
        return dict(dtype=self.dtype, shape=list(self.shape), stride=list(self._stride))


class Kernel:
    def __init__(self):
        self.calls = []

    def __getitem__(self, grid):
        def call(*args, **kwargs):
            result = dict(grid=list(grid), args=[v.record() if isinstance(v, Tensor) else v for v in args],
                          kwargs=kwargs)
            self.calls.append(result)
            return result
        return call


def capture(baseline, candidate, *, arm, m, n, k, card):
    validate(baseline, candidate)
    require(arm in ("baseline", "candidate") and type(card) is int and card in (0, 1), "declared arm/card")
    raw = baseline if arm == "baseline" else candidate
    # The sole relative import is authenticated and unused by gemm_preallocated.
    # Supply its name without changing the function being exercised.
    prefix = b"from .prefill_reference import quantizer_geometry"
    require(raw.count(prefix) == 1, "frozen relative-import boundary")
    module = types.ModuleType("metadata_only_mxfp4_ops")
    exec(compile(raw.replace(prefix, b"quantizer_geometry = None"), "/mnt/review/ops.py", "exec"), module.__dict__)
    kernel = Kernel()
    torch = types.SimpleNamespace(uint8="uint8", bfloat16="bfloat16")
    triton = types.SimpleNamespace(cdiv=lambda a, b: (a + b - 1) // b)
    def runtime(expected_device):
        require(expected_device == card, "fixture current card")
        return torch, triton, types.SimpleNamespace(_gemm_afp4wfp4_kernel=kernel)
    module.runtime = runtime
    values = [Tensor((m, k // 2), "uint8", (k // 2, 1), card, 16),
              Tensor((n, k // 2), "uint8", (k // 2, 1), card, 32),
              Tensor((m, k // 32), "uint8", (1, m), card, 48),
              Tensor((n, k // 32), "uint8", (k // 32, 1), card, 64),
              Tensor((m, n), "bfloat16", (n, 1), card, 80)]
    result = module.gemm_preallocated(*values, expected_device=card)
    require(len(kernel.calls) == 1 and kernel.calls[0] is result, "one captured GEMM call")
    return result


def capture_cases(baseline, candidate):
    result = {}
    for name, m, n, k in CASES:
        arms = {}
        for arm in ("baseline", "candidate"):
            values = [capture(baseline, candidate, arm=arm, m=m, n=n, k=k, card=card) for card in (0, 1)]
            require(values[0] == values[1], "card-invariant launch metadata")
            arms[arm] = values[0]
        b, c = arms["baseline"], arms["candidate"]
        require(c["args"] == b["args"] and c["grid"][0] == b["grid"][0] * 2,
                "same ABI and doubled program count")
        require(c["kwargs"] == dict(b["kwargs"], BLOCK_SIZE_N=32), "sole changed launch constant")
        result[name] = arms
    return result
