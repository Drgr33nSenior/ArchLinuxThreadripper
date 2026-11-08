"""Future component launch boundary; checks run before any target launch.

No owner, automatic execution, graph capture or serving installation is present.
The warmup API mirrors the existing F1 audit (kernel.run with warmup=True).
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARTS = ('ttir', 'ttgir', 'llir', 'amdgcn', 'hsaco')
FIELDS = ('vgpr_count', 'sgpr_count', 'vgpr_spill_count', 'sgpr_spill_count',
          'private_segment_fixed_size', 'group_segment_fixed_size',
          'wavefront_size', 'max_flat_workgroup_size')
PART_LIMIT = 8 << 20
CASES = ('down-m16', 'gate_up-m16', 'gate_up-m8')
DATA_PINS_SHA256 = '7de14007975723bf4ee3d08fcf8d1b258ccfc5735d214f70db6701b0aace1d06'


def equal(actual, expected):
    """JSON-like equality with exact scalar types; bool is never an integer."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(equal(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def load_contract():
    raw_pins = (ROOT / 'DATA-PINS.json').read_bytes()
    if hashlib.sha256(raw_pins).hexdigest() != DATA_PINS_SHA256:
        raise ValueError('contract manifest identity')
    pins = json.loads(raw_pins)
    values = {}
    for name in ('LAUNCHES.json', 'COMPILER-PINS.json'):
        raw = (ROOT / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != pins[name]:
            raise ValueError('bound contract data: ' + name)
        values[name] = json.loads(raw)
    return values['LAUNCHES.json'], values['COMPILER-PINS.json']


def facts(isa):
    result = {}
    for key in FIELDS:
        matches = re.findall(r'^\s*\.' + key + r':\s*(\d+)\s*$', isa, re.M)
        if len(matches) != 1:
            raise ValueError('one resource field: ' + key)
        result[key] = int(matches[0])
    return result


def verify_compiled(compiled, expected):
    hashes = {}
    for name in PARTS:
        raw = compiled.asm[name]
        raw = raw.encode() if type(raw) is str else raw
        if type(raw) is not bytes or not 0 < len(raw) <= PART_LIMIT:
            raise ValueError('bounded compiler artifact: ' + name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
        if name == 'amdgcn':
            resources = facts(raw.decode())
    if not equal(hashes, expected['hashes']) or not equal(resources, expected['resources']):
        raise ValueError('exact five-part compiler/resource identity')
    for field, value in expected['metadata'].items():
        if not equal(getattr(compiled.metadata, field), value):
            raise ValueError('exact compiler metadata: ' + field)
    return hashes


def check_launch(args, kwargs, grid, expected, card):
    if type(card) is not int or card not in (0, 1):
        raise ValueError('declared card')
    if not equal(list(grid), expected['grid']) or not equal(kwargs, expected['kwargs']) or len(args) != len(expected['args']):
        raise ValueError('exact launch grid/options/arity')
    pointers = []
    for value, want in zip(args, expected['args']):
        if type(want) is dict:
            actual = dict(shape=list(value.shape), stride=list(value.stride()),
                          dtype=str(value.dtype).removeprefix('torch.'))
            if not equal(actual, want) or value.device.type != 'cuda' or type(value.device.index) is not int or value.device.index != card:
                raise ValueError('tensor metadata/card identity')
            storage = value.untyped_storage()
            pointer = storage.data_ptr()
            if type(pointer) is not int or pointer <= 0 or pointer % 16 or pointer in pointers:
                raise ValueError('distinct nonempty operand storage')
            offset, extent = value.storage_offset(), storage.nbytes()
            elements = 1
            for dimension in want['shape']:
                elements *= dimension
            required_bytes = elements * (2 if want['dtype'] == 'bfloat16' else 1)
            if type(offset) is not int or offset != 0 or type(extent) is not int or extent != required_bytes:
                raise ValueError('exact standalone operand allocation; no offset views')
            pointers.append(pointer)
        elif not equal(value, want):
            raise ValueError('launch scalar identity')


class AuditedKernel:
    """One audit instance per case/arm/card; holds the verified JIT object.

    Compile-only mode returns the object without a target launch. Live mode
    requires the kernel invocation to return that very object, as in the donor.
    Device identity/current stream, poisoning, oracles and ownership belong to
    the future component driver; they are not simulated by this boundary.
    """
    def __init__(self, kernel, case, arm, card, *, launch=False):
        if case not in CASES or arm not in ('baseline', 'candidate'):
            raise ValueError('frozen case/arm')
        if type(card) is not int or card not in (0, 1) or type(launch) is not bool:
            raise ValueError('explicit card/launch mode')
        launches, pins = load_contract()
        self.expected = launches[case][arm]
        self.pins = pins[case][arm]
        self.kernel, self.card, self.launch = kernel, card, launch
        self.role = 'negative_control' if case == 'down-m16' else 'mechanism_candidate'
        self.route = 'negative' if case == 'down-m16' and arm == 'candidate' else arm
        self.identity = dict(case=case, arm=arm, card=card, role=self.role, route=self.route)
        self.compiled = None
        self.calls = 0

    def __getitem__(self, grid):
        def call(*args, **kwargs):
            check_launch(args, kwargs, grid, self.expected, self.card)
            compiled = self.kernel.run(*args, grid=grid, warmup=True, **kwargs)
            verify_compiled(compiled, self.pins)
            if self.compiled is not None and self.compiled is not compiled:
                raise ValueError('compiled object changed within audit')
            self.compiled = compiled
            if self.launch and self.kernel[grid](*args, **kwargs) is not compiled:
                raise ValueError('launched object differs from audited object')
            self.calls += 1
            return compiled
        return call
