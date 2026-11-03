"""One finite device-free compiler proof for the actual shared-stage harness loader."""
import ast
import base64
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import build_verify_source as build
import compile_verify as existing
from hyperloom_exchange import canonical,digest
from hyperloom_sglang import require


def prepare(destination, source_root):
    source_root=existing.owner._canonical_dir(source_root,'inert harness sources')
    metadata=existing.prepare(destination,prefetch=True)
    root=Path(destination)
    prior=(root/'TARGET.py').read_text()
    prefix=prior[:prior.index('def compile_verify(raw, launch):')]
    inputs=json.loads((root/'INPUTS.json').read_bytes())
    payload=dict(source=(root/'decode_attention.py').read_text(),candidate=(root/'decode_attention_candidate.py').read_text(),
                 metadata=(source_root/'metadata.py').read_text(),launches=json.loads((source_root/'LAUNCHES.json').read_bytes()),
                 runtime={n:(source_root/(n+'.py')).read_text() for n in ('support','contract','source_loader')})
    original=ast.parse(prior)
    assignment=next(n for n in original.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='payload' for t in n.targets))
    encoded=ast.literal_eval(assignment.value.args[0].args[0])
    old_payload=json.loads(base64.b64decode(encoded,validate=True))
    payload.update({k:old_payload[k] for k in ('c4','c4_launch','c4_pins','stock_pins')})
    inner=prefix+(HERE/'compile_verify_loader_target.py').read_text()
    inner+='\npayload=json.loads(base64.b64decode('+repr(base64.b64encode(canonical(payload)))+',validate=True))\n'
    inner+='''report=dict(status='CPU_CENSUS_STARTED',gpu_launches=0,model_starts=0,device_nodes=False,cases={},candidates={})
c4=compile_arm(payload['c4'],'/mnt/review/extend_attention_fwd_baseline.py','c4_exp2_B',payload['c4_launch'])
report['c4_proxy_reproduced']=all(c4['artifacts'][p]['sha256']==v for p,v in payload['c4_pins'].items())
report['c4']=c4
if report['c4_proxy_reproduced']:
 import triton.language as tl
 for name in ('support','contract','source_loader'):
  module=types.ModuleType(name);module.__file__='/mnt/review/'+name+'.py';sys.modules[name]=module
  exec(compile(payload['runtime'][name],module.__file__,'exec'),module.__dict__)
 modules=sys.modules['source_loader'].load_pair(payload['source'].encode(),payload['candidate'].encode(),payload['metadata'].encode(),triton,tl)
 assert modules[0]._fwd_kernel_stage2 is modules[1]._fwd_kernel_stage2
 report['shared_stage2_object']=True
 for name,row in payload['launches'].items():
  report['cases'][name]=compile_loaded(modules[0]._fwd_grouped_kernel_stage1,payload['source'],row['stage1'])
 report['stock_cpu_reproduced']=all(value['artifacts'][p]['sha256']==v for value in report['cases'].values() for p,v in payload['stock_pins'].items())
 if report['stock_cpu_reproduced']:
  for name,row in payload['launches'].items():
   report['candidates'][name]=compile_loaded(modules[1]._fwd_grouped_kernel_stage1,payload['candidate'],row['stage1'])
  second=[compile_loaded(modules[0]._fwd_kernel_stage2,payload['source'],row['stage2']) for row in payload['launches'].values()]
  assert all(v['artifacts']==second[0]['artifacts'] for v in second)
  report['stage2']=second[0];report['stage2_case_identities_equal']=True
  report['status']='CPU_PREFETCH_COMPILED'
 else:report['status']='STOP_STOCK_CPU_IDENTITY_MISMATCH'
else:report['status']='STOP_C4_PROXY_MISMATCH'
assert not torch.cuda.is_initialized()
print(json.dumps(report,sort_keys=True,default=lambda obj:obj.__dict__))
'''
    compile(inner,'harness-loader-proof','exec',dont_inherit=True)
    recipe=(root/'RECIPE.py').read_text();tree=ast.parse(recipe);lines=recipe.splitlines(True)
    node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='source' for t in n.targets))
    lines[node.lineno-1:node.end_lineno]=['source='+repr(inner.encode())+'\n']
    modified=''.join(lines)
    require(existing.existing._literal(ast.parse(modified),'argv')==existing.existing._literal(tree,'argv'),'unchanged native sandbox')
    (root/'TARGET.py').write_text(inner);(root/'RECIPE.py').write_text(modified)
    for name in ('TARGET.py','RECIPE.py'):inputs['files'][name]=digest((root/name).read_bytes())
    for name in ('compile_verify_loader.py','compile_verify_loader_target.py',*[f'verify-component/{n}.py' for n in ('source_loader','contract','support')]):
        inputs['sources'][name]=digest((HERE/name).read_bytes())
    inputs['harness_runtime_pins']={n:digest(raw.encode()) for n,raw in payload['runtime'].items()}
    inputs['public_launches_sha256']=digest((source_root/'LAUNCHES.json').read_bytes())
    require(not (root/'INVOKED.json').exists(),'preparation before invocation')
    (root/'INPUTS.json').write_bytes(canonical(inputs))
    return inputs


def execute(root, pin):
    root=Path(root);result=existing.execute(root,pin)
    require(result.get('shared_stage2_object') and result.get('stage2_case_identities_equal'),'actual loader and shared reduction')
    expected=json.loads((build.COMPILE_ROOT/'COMPILE-RESULT.json').read_bytes())
    result['harness_stage1_reproduced']=all(result[k][n]['artifacts'][p]['sha256']==expected[k][n]['artifacts'][p]['sha256']
        for k in ('cases','candidates') for n in result[k] for p in ('amdgcn','hsaco'))
    require(result['harness_stage1_reproduced'],'actual harness loader must reproduce both arms')
    stage=result['stage2']
    for name,part in stage['artifacts'].items():
        raw=existing.existing._decompress(part.pop('gzip_base64'),part)
        (root/'artifacts'/('shared-stage2.'+name)).write_bytes(raw)
    result['stage2_pins']={p:stage['artifacts'][p]['sha256'] for p in ('amdgcn','hsaco')}
    (root/'COMPILE-RESULT.json').write_bytes(canonical(result))
    return result
