"""Exact host-only wave-hint derivation; no imports of device frameworks."""
import hashlib

OPS_SHA256 = '66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776'
KERNEL_SHA256 = '5571b10654697561455081455b6cd28d43314702f56c80160ef2e33e535b3693'
ANCHOR = '    config = dict(CONFIG, BLOCK_SIZE_M=block_m, BLOCK_SIZE_N=block_n, num_warps=warps, SPLITK_BLOCK_SIZE=k)\n'
GATE_UP = '    if m in (8, 16) and (n, k) == (17408, 5120):\n        config["waves_per_eu"] = 0\n'
NEGATIVE = '    if (m, n, k) == (16, 5120, 8704):\n        config["waves_per_eu"] = 0\n'


def derive(raw, *, negative_control=False):
    if type(raw) is not bytes or hashlib.sha256(raw).hexdigest() != OPS_SHA256:
        raise ValueError('selected launcher identity')
    if type(negative_control) is not bool or raw.count(ANCHOR.encode()) != 1:
        raise ValueError('single host boundary and explicit control role')
    addition = NEGATIVE if negative_control else GATE_UP
    return raw.replace(ANCHOR.encode(), (ANCHOR + addition).encode())


def validate(baseline, candidate, negative):
    if candidate != derive(baseline) or negative != derive(baseline, negative_control=True):
        raise ValueError('exact separately scoped derivations')
    return {'production_cases': [[8, 17408, 5120], [16, 17408, 5120]],
            'negative_control': [16, 5120, 8704], 'kernel_body_changed': False,
            'quantizer_changed': False, 'other_shapes_keep_hint': 1,
            'negative_control_adoptable': False}
