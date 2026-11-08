"""Inert source/launch preparation; never invokes SSH, a compiler or a GPU."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import dispatch
import launch_gate

N = Path("/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1")
CPU = N / 'gemm-wave-hint-approved-cpu-20261104-v1'
OLD = N / 'gemm-wave-hint-cpu-20261104-v1'
DONOR = Path("/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/infrastructure/hyperloom/sglang-owner-adapter") / 'norm-quant-component'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def put(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)


def write(path, value):
    put(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())


def main():
    inputs = {'/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1/gemm-wave-hint-approved-cpu-20261104-v1/LAUNCHES.json': 'f37df50c2e5879d6857da07deeed73af7840a4cc724ca628857653df4a263963', '/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1/gemm-wave-hint-approved-cpu-20261104-v1/COMPILE-RESULT.json': '4e35f2156a3f94ab1fd512327ee3ddc48ddb9e1ca4a18f9ab848635372be6c78', '/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/infrastructure/hyperloom/sglang-owner-adapter/norm-quant-component/prefill_reference.py': 'cd5841650fc59da7f1537e82ffebf16fb07ca0db7d191cdfec59540575bbd887'}
    for path, pin in inputs.items():
        if sha(Path(path).read_bytes()) != pin:
            raise ValueError("preparation input changed: " + path)
    baseline = (OLD / 'source/ops_baseline.py').read_bytes()
    kernel = (CPU / 'source/kernels.py').read_bytes()
    if sha(kernel) != dispatch.KERNEL_SHA256:
        raise ValueError('unchanged kernel source')
    copies = {'ops_baseline.py': baseline, 'ops_candidate.py': dispatch.derive(baseline),
              'ops_negative_control.py': dispatch.derive(baseline, negative_control=True),
              'kernels.py': kernel}
    original_pins = json.loads((DONOR / 'INPUT-PINS.json').read_bytes())
    geometry = (DONOR / 'prefill_reference.py').read_bytes()
    if sha(geometry) != original_pins['prefill_reference.py']:
        raise ValueError('unchanged geometry helper')
    copies['prefill_reference.py'] = geometry
    launches = json.loads((CPU / 'LAUNCHES.json').read_bytes())
    report = json.loads((CPU / 'COMPILE-RESULT.json').read_bytes())
    if report['status'] != 'CPU_THREE_CASES_PASS' or set(report['cases']) != set(launch_gate.CASES):
        raise ValueError('three passing CPU cases')
    variants, pins = {}, {}
    fixture = ROOT / 'test_artifacts'
    fixture.mkdir(mode=0o700)
    for case in launch_gate.CASES:
        variants[case], pins[case] = {}, {}
        for arm in ('baseline', 'candidate'):
            row = report['cases'][case][arm]
            call = copy.deepcopy(launches[case])
            call['kwargs']['waves_per_eu'] = int(arm == 'baseline')
            variants[case][arm] = call
            artifacts = {}
            for part in launch_gate.PARTS:
                raw = (CPU / 'artifacts' / (case + '-' + arm + '.' + part)).read_bytes()
                if sha(raw) != row['artifacts'][part]['sha256'] or len(raw) != row['artifacts'][part]['bytes']:
                    raise ValueError('retained compiler artifact')
                artifacts[part] = sha(raw)
                put(fixture / (case + '-' + arm + '.' + part), raw)
            metadata_fields = ('shared', 'num_warps', 'num_stages', 'waves_per_eu', 'warp_size',
                               'global_scratch_size', 'profile_scratch_size', 'arch', 'name')
            isa = (fixture / (case + '-' + arm + '.amdgcn')).read_text()
            pins[case][arm] = dict(hashes=artifacts, resources=launch_gate.facts(isa),
                                  metadata={k: row['metadata'][k] for k in metadata_fields})
    for name, raw in copies.items():
        put(ROOT / name, raw)
    write(ROOT / 'SOURCE-PINS.json', {k: sha(v) for k, v in copies.items()})
    write(ROOT / 'LAUNCHES.json', variants)
    write(ROOT / 'COMPILER-PINS.json', pins)
    write(ROOT / 'DATA-PINS.json', {name: sha((ROOT / name).read_bytes()) for name in ('LAUNCHES.json', 'COMPILER-PINS.json')})
    print(json.dumps(dict(status='HOST_GATE_PREPARED', gpu_launches=0, native_invocations=0,
                         scope=dispatch.validate(copies['ops_baseline.py'], copies['ops_candidate.py'], copies['ops_negative_control.py']))))


if __name__ == '__main__':
    main()
