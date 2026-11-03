"""Prepare a fixed, inert verification-attention harness; no owner or GPU route."""
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import owner_operation as owner
import verify_dispatch
from hyperloom_exchange import canonical,digest
from hyperloom_sglang import require

TEMPLATE=HERE/'verify-component'
COMPILE_ROOT=owner.R2/'hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1/verify-prefetch-compile-v1'
COMPILE_SEAL='7522e78968cab2eac47dd23d4d41bdac07a234a0f98fa26002072ea38c164428'
METADATA=owner.INFRA/'scratchpad/fp8-norm8-replay.7jdnvw_d/native-metadata/kernels/ops/attention/metadata.py'
RUNTIME=('attention_ops.py','compiler_gate.py','contract.py','source_loader.py','read_component.py','stage_probe.py','support.py')


def prepare(destination):
    root=owner._fresh(destination,'inert verification source packet')
    owner.sealer().verify(COMPILE_ROOT,COMPILE_SEAL)
    baseline=(COMPILE_ROOT/'decode_attention.py').read_bytes()
    candidate=(COMPILE_ROOT/'decode_attention_candidate.py').read_bytes()
    require(candidate==verify_dispatch.derive_prefetch(baseline),'exact prefetch derivation')
    launches={name:verify_dispatch.capture_full(baseline,prefixes) for name,prefixes in verify_dispatch.CASES}
    for row in launches.values():
        for stage in row.values():
            if isinstance(stage,dict) and 'args' in stage:
                for arg in stage['args']:
                    if isinstance(arg,dict) and arg.get('shape')==[262144,2,256]:arg['shape'][0]=120833
    files={name:(TEMPLATE/name).read_bytes() for name in RUNTIME}
    files.update({'decode_attention_baseline.py':baseline,'decode_attention_candidate.py':candidate,
                  'metadata.py':METADATA.read_bytes(),'LAUNCHES.json':canonical(launches)})
    require(digest(files['metadata.py'])=='10d4f1ac194e1fd999b58bbfcb2d1ad09bc25df289128925d2945108173afdc0',
            'pinned singleton metadata')
    root.mkdir(mode=0o700)
    for name,raw in files.items():
        with (root/name).open('xb') as stream:stream.write(raw)
    with (root/'RUNTIME-SOURCE-PINS.json').open('xb') as stream:
        stream.write(canonical({k:digest(v) for k,v in files.items()}))
    for path in TEMPLATE.glob('test_*.py'):
        (root/path.name).write_bytes(path.read_bytes())
    if (TEMPLATE/'fake_payload.py').exists():(root/'fake_payload.py').write_bytes((TEMPLATE/'fake_payload.py').read_bytes())
    owner._write(root/'PREPARATION.json',dict(status='INERT_SOURCE_ONLY',compiler_seal=COMPILE_SEAL,
        gpu_authorized=False,owner_route_connected=False,independently_reviewed=False))
    return root
