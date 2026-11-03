"""Build the inert split-cap component from authenticated CPU/signoff evidence."""
import hashlib,importlib.util,json
from pathlib import Path

HERE=Path(__file__).resolve().parent
N=HERE.parents[2]/'scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1'
SCREEN=N/'opt-03-attention-cpu-v1';SIGNOFF=N/'opt-03-signoff-v1'
RUNTIME=('attention_ops.py','compiler_gate.py','contract.py','source_loader.py','read_component.py','stage_probe.py','support.py')
def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(v):return (json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
def prepare(destination):
    root=Path(destination).absolute()
    if root.exists() or root.resolve()!=root:raise ValueError('fresh canonical source root')
    sealer=SCREEN/'seal_evidence.py'
    if digest(sealer.read_bytes())!='082835dabf0bf1e96d9bba6f08918914b01a1306a5a2cd939a0c9309ad334e18':raise ValueError('sealer identity')
    spec=importlib.util.spec_from_file_location('split_sealer',sealer);seal=importlib.util.module_from_spec(spec);spec.loader.exec_module(seal)
    inputs=[]
    for path,pin in ((SCREEN,'f5da71da960528525b5e2c15888c675a759098c05279d1c664f712d49a92b363'),
                     (SIGNOFF,'34ddfb4096b4641a87ffceb2e5e5dff3e251b2f38db760bdb75b3256527bae3c'),
                     (N/'verify-prefetch-campaign-983ff3bdf1d8de53','9abb0e815ea26b3fd7a0dcc0a77054f22b709899ce24768e24b5ce26a6213e51')):
        inputs.append(seal.verify(path,pin))
    template=HERE/'verify-split-component'
    files={name:(template/name).read_bytes() for name in RUNTIME}
    source=(SCREEN/'source/decode_attention.py').read_bytes()
    launches=json.loads((SCREEN/'WRAPPER-PROOF.json').read_bytes())['cases']
    files.update({'decode_attention_baseline.py':source,'decode_attention_candidate.py':source,
                  'metadata.py':(SCREEN/'source/metadata.py').read_bytes(),
                  'LAUNCHES.json':canonical({name:{'B':row['baseline'],'C':row['candidate']} for name,row in launches.items()})})
    root.mkdir(mode=0o700)
    for name,raw in files.items():
        with (root/name).open('xb') as f:f.write(raw)
    with (root/'RUNTIME-SOURCE-PINS.json').open('xb') as f:f.write(canonical({n:digest(raw) for n,raw in files.items()}))
    for path in template.glob('*.py'):
        if path.name not in files:
            with (root/path.name).open('xb') as f:f.write(path.read_bytes())
    with (root/'PREPARATION.json').open('xb') as f:f.write(canonical(dict(status='INERT_SOURCE_ONLY',inputs=inputs,gpu_authorized=False,owner_route_connected=False,independently_reviewed=False)))
    return root
