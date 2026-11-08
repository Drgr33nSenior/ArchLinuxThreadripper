"""Prepare one exact four-arm, device-free compiler proof; never launch it."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

N=Path("/Users/uk-gr9yjx0l0y/Projects/ArchLinuxThreadripperAI/scratchpad/mxfp4-ultraquant-feasibility-20261026-v1/fp8-fastpath-research-v2/hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1")
DONOR=N/'opt07-attention-composed-compile-v1'
SOURCE=N/'opt07-attention-factorial-preparation-v1/author-v3'
SOURCE_SEAL='435159c1770b15f361e4ee96089092aadaa1987b8f72b778d95f3c3e11feeb7b'
DEST=N/'opt07-attention-factorial-preparation-v1/native-loader-proof-v2'
SEALER=N/'host-copy-and-gemm-triage-v1/seal_evidence.py'
SEALER_PIN='082835dabf0bf1e96d9bba6f08918914b01a1306a5a2cd939a0c9309ad334e18'


def sha(raw):return hashlib.sha256(raw).hexdigest()


def assignment(source,name,value):
    nodes=[n for n in ast.parse(source).body if isinstance(n,ast.Assign)
           and any(isinstance(t,ast.Name) and t.id==name for t in n.targets)]
    assert len(nodes)==1,name
    node=nodes[0];lines=source.splitlines(keepends=True)
    return ''.join(lines[:node.lineno-1])+name+'='+value+'\n'+''.join(lines[node.end_lineno:])


def exact(source,old,new):
    assert source.count(old)==1,old
    return source.replace(old,new)


def derive():
    target=(DONOR/'TARGET.py').read_text()
    node=next(n for n in ast.parse(target).body if isinstance(n,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=='payload' for t in n.targets))
    payload=json.loads(ast.literal_eval(node.value.args[0]))
    for name in ('contract','support','source_loader'):
        payload['runtime'][name]=(SOURCE/(name+'.py')).read_text()
    assert payload['source']==(SOURCE/'decode_attention_baseline.py').read_text()
    assert payload['candidate']==(SOURCE/'decode_attention_candidate.py').read_text()
    assert payload['metadata']==(SOURCE/'metadata.py').read_text()
    payload['launches']=json.loads((SOURCE/'LAUNCHES.json').read_bytes())
    for case,arms in payload['expected'].items():
        arms['P']=dict(stage1=copy.deepcopy(arms['C']['stage1']),stage2=copy.deepcopy(arms['B']['stage2']))
        arms['S']=dict(stage1=copy.deepcopy(arms['B']['stage1']),stage2=copy.deepcopy(arms['C']['stage2']))
        assert set(payload['launches'][case])==set(arms)=={'B','P','S','C'}
    target=assignment(target,'payload','json.loads('+repr(json.dumps(payload))+')')
    target=exact(target,"for arm,index in [('B',0),('C',1)]:",
                 "for arm,index in [('B',0),('S',0),('P',1),('C',1)]:")
    target=exact(target,"payload['candidate'] if arm=='C' and stage=='stage1' else payload['source']",
                 "payload['candidate'] if index==1 and stage=='stage1' else payload['source']")
    target=exact(target,"if arm=='B':report['all_baselines_reproduced_before_candidates']=True",
                 "if arm=='S':report['all_baselines_reproduced_before_candidates']=True")
    target=exact(target,"source_path='/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py'",
                 "source_path=fn.fn.__code__.co_filename")
    target=target.replace('CPU_COMPOSED_', 'CPU_FACTORIAL_')
    recipe=assignment((DONOR/'RECIPE.py').read_text(),'source',repr(target.encode()))
    compile(target,'TARGET.py','exec');compile(recipe,'RECIPE.py','exec')
    return target,recipe,payload


def main():
    assert sha(SEALER.read_bytes())==SEALER_PIN
    spec=importlib.util.spec_from_file_location('sealer',SEALER)
    seal=importlib.util.module_from_spec(spec);spec.loader.exec_module(seal)
    roots=[seal.verify(DONOR,'116927b6a3e7457753d5baed6e19b53af141a3f6bfc2097eb7b8d7096b02d1ac'),
           seal.verify(SOURCE,SOURCE_SEAL)]
    target,recipe,payload=derive()
    DEST.mkdir(mode=0o700)
    def put(name,raw):
        with (DEST/name).open('xb') as f:f.write(raw)
    for name in ('connection.py','local_commands.py','run_native.py'):
        put(name,(DONOR/name).read_bytes())
    retain=(DONOR/'retain_compile.py').read_text().replace("CPU_COMPOSED_COMPILED","CPU_FACTORIAL_COMPILED")
    retain=exact(retain,"set(arms)=={'B','C'}","set(arms)=={'B','P','S','C'}")
    retain=exact(retain,"ROOT.parent/'host-copy-and-gemm-triage-v1/seal_evidence.py'",
                 "ROOT.parents[1]/'host-copy-and-gemm-triage-v1/seal_evidence.py'")
    put('retain_compile.py',retain.encode())
    put('TARGET.py',target.encode());put('RECIPE.py',recipe.encode())
    put('LAUNCHES.json',(SOURCE/'LAUNCHES.json').read_bytes())
    for name,text in payload['runtime'].items():put('runtime-'+name+'.py',text.encode())
    prep=dict(automatic_retry=False,gpu_launches=0,model_starts=0,
        source_root=str(SOURCE),source_seal=SOURCE_SEAL,expected=payload['expected'],
        input_roots=roots,runtime_pins={k:sha(v.encode()) for k,v in payload['runtime'].items()},
        files={p.name:sha(p.read_bytes()) for p in DEST.iterdir()},
        producer=dict(path=str(Path(__file__)),sha256=sha(Path(__file__).read_bytes())))
    put('PREPARATION.json',(json.dumps(prep,sort_keys=True,indent=2)+'\n').encode())
    print(json.dumps(dict(root=str(DEST),gpu_launches=0,arms=4)))


if __name__=='__main__':main()
