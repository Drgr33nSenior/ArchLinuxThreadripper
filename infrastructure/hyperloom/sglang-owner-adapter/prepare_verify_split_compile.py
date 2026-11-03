"""Pin a finite device-free compile through the actual split component loader."""
import ast,hashlib,importlib.util,json,shutil
from pathlib import Path
from build_verify_split_source import N,SCREEN,SIGNOFF

SOURCE=N/'verify-split-component-preparation-v1/author-v3'
SOURCE_SEAL='7edeac175d647bfe9e98f9adc97f9623ed4f8d5261855522427dd1a87b32e9c0'
DONOR=N/'verify-loader-compile-v3'
OUT=N/'verify-split-component-preparation-v1/native-loader-proof-v1'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def prepare():
    sealer=SCREEN/'seal_evidence.py'
    assert sha(sealer)=='082835dabf0bf1e96d9bba6f08918914b01a1306a5a2cd939a0c9309ad334e18'
    s=importlib.util.spec_from_file_location('sealer',sealer);seal=importlib.util.module_from_spec(s);s.loader.exec_module(seal)
    seal.verify(SOURCE,SOURCE_SEAL);seal.verify(DONOR,'2151a5f5bad14d5e42e93855bfc0bc90fad1286c540058bb44e0bce9f14f594d')
    raw=(DONOR/'TARGET.py').read_text();tree=ast.parse(raw)
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='compile_arm')
    prefix='\n'.join(raw.splitlines()[:node.lineno-1])+'\n'
    helper=ast.get_source_segment(raw,next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='compile_loaded'))
    old=json.loads((SCREEN/'compile-split32-cases/COMPILE-RESULT.json').read_bytes())['cases']
    payload=dict(source=(SOURCE/'decode_attention_baseline.py').read_text(),metadata=(SOURCE/'metadata.py').read_text(),
                 runtime={name:(SOURCE/(name+'.py')).read_text() for name in ('support','contract','source_loader')},
                 launches=json.loads((SOURCE/'LAUNCHES.json').read_bytes()),
                 expected={case:{arm:{stage:{part:v['sha256'] for part,v in data['artifacts'].items()} for stage,data in records.items()} for arm,records in [('B',row['baseline']),('C',row['candidate'])]} for case,row in old.items()})
    body=prefix+'\n'+helper+'\npayload=json.loads('+repr(json.dumps(payload))+')\n'+'''
import triton.language as tl
for name in ('support','contract','source_loader'):
 module=types.ModuleType(name);module.__file__='/mnt/review/'+name+'.py';sys.modules[name]=module
 exec(compile(payload['runtime'][name],module.__file__,'exec'),module.__dict__)
loader=sys.modules['source_loader'];before_closure=selected.consumed_sources(inventory)
absent=object();before_modules={n:sys.modules.get(n,absent) for n in loader.ModuleScope.NAMES}
before_lines={p:linecache.cache.get(p,absent) for p in loader.ModuleScope.PATHS}
report=dict(status='CPU_SPLIT_STARTED',gpu_launches=0,model_starts=0,device_nodes=False,cases={})
with loader.ModuleScope():
 modules=loader.load_pair(payload['source'].encode(),payload['source'].encode(),payload['metadata'].encode(),triton,tl)
 assert modules[0]._fwd_kernel_stage2 is modules[1]._fwd_kernel_stage2
 report['shared_stage2_jit_object']=True
 try:selected.consumed_sources(inventory)
 except selected.ProbeFailure as error:
  assert 'consumed source origin' in str(error);report['old_leak_rejected_by_unchanged_guard']=True
 else:raise AssertionError('temporary aliases unexpectedly admitted')
 for arm,index in [('B',0),('C',1)]:
  for case,row in payload['launches'].items():
   report['cases'].setdefault(case,{})[arm]={}
   for stage,fn in [('stage1',modules[index]._fwd_grouped_kernel_stage1),('stage2',modules[index]._fwd_kernel_stage2)]:
    result=compile_loaded(fn,payload['source'],row[arm][stage]);report['cases'][case][arm][stage]=result
    assert all(result['artifacts'][part]['sha256']==pin for part,pin in payload['expected'][case][arm][stage].items())
  if arm=='B':report['all_baselines_reproduced_before_candidates']=True
assert selected.consumed_sources(inventory)==before_closure
assert all(sys.modules.get(n,absent) is v for n,v in before_modules.items())
assert all(linecache.cache.get(p,absent) is v for p,v in before_lines.items())
report['selected_guard_restored_after_compile']=True
try:
 with loader.ModuleScope():
  loader.load_pair(payload['source'].encode(),payload['source'].encode(),payload['metadata'].encode(),triton,tl)
  raise RuntimeError('intentional CPU-only post-load failure')
except RuntimeError as error:assert str(error)=='intentional CPU-only post-load failure'
assert selected.consumed_sources(inventory)==before_closure
assert all(sys.modules.get(n,absent) is v for n,v in before_modules.items())
assert all(linecache.cache.get(p,absent) is v for p,v in before_lines.items())
report['selected_guard_restored_after_exception']=True
report['all_five_artifacts_reproduced']=True
report['status']='CPU_SPLIT_COMPILED'
assert not torch.cuda.is_initialized()
print(json.dumps(report,sort_keys=True,default=lambda obj:obj.__dict__))
'''
    original=(SCREEN/'compile-split32-cases/RECIPE.py').read_text();tree=ast.parse(original)
    node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='source' for t in n.targets))
    original_source=ast.get_source_segment(original,node);assert original.count(original_source)==1
    recipe=original.replace(original_source,'source='+repr(body.encode()))
    OUT.mkdir(mode=0o700)
    for name,text in [('TARGET.py',body),('RECIPE.py',recipe)]:
        compile(text,name,'exec',dont_inherit=True)
        with (OUT/name).open('x') as f:f.write(text)
    for name in ('connection.py','local_commands.py','run_native.py'):
        shutil.copyfile(SCREEN/'compile-split32-cases'/name,OUT/name)
    shutil.copyfile(SOURCE/'LAUNCHES.json',OUT/'LAUNCHES.json')
    files={p.name:sha(p) for p in OUT.iterdir() if p.is_file()}
    data=dict(files=files,source_root=str(SOURCE),source_seal=SOURCE_SEAL,expected=payload['expected'],
              runtime_pins={name:sha(SOURCE/(name+'.py')) for name in payload['runtime']},
              gpu_launches=0,model_starts=0,automatic_retry=False)
    with (OUT/'PREPARATION.json').open('x') as f:json.dump(data,f,indent=2,sort_keys=True)
    print(OUT)
if __name__=='__main__':prepare()
