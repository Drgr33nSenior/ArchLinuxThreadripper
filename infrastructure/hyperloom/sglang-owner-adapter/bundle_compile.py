"""Prepare a finite, device-free serving compiler proof from retained contexts."""
import ast
import base64
import gzip
import hashlib
import json
from pathlib import Path


def sha(raw): return hashlib.sha256(raw).hexdigest()


ABLATION = 'verify_cap32_single_variable'


def ablation_body(body):
    """Derive the cap-only ablation target without changing the legacy body file.

    The control arm compiles attention from the prefetch source at cap16 using the
    already reviewed stock cap16 stage-2 specialization; mtp is unchanged.
    """
    for old, new in (
            (b"        ('control','attention_baseline',native_name,native_path,'B'),\n",
             b"        ('control','attention_candidate','decode_attention_fwd_candidate','/mnt/review/decode_attention_fwd_candidate.py','C'),\n"),
            (b"    report.update(status='CPU_SERVING_COMPILERS_PROVEN',pins=pins,",
             b"    report.update(status='CPU_SERVING_COMPILERS_PROVEN',ablation='verify_cap32_single_variable',pins=pins,")):
        assert body.count(old) == 1, 'ablation compiler target anchor'
        body = body.replace(old, new)
    return body


COMPARISON = 'three_member_cap16_serving'


def comparison_body(body):
    """Derive the three-member cap16 comparison target; the legacy body file is unchanged.

    Control keeps the native attention source at cap16. The candidate keeps the
    prefetch source and compiles its stage 2 with the baseline cap16 row instead
    of the cap32 row. Every other compile, calibration and check is identical.
    """
    for old, new in (
            (b"            cap=16 if arm=='control' else 32\n",
             b"            cap=16\n"),
            (b"    report.update(status='CPU_SERVING_COMPILERS_PROVEN',pins=pins,",
             b"    report.update(status='CPU_SERVING_COMPILERS_PROVEN',comparison='three_member_cap16_serving',pins=pins,")):
        assert body.count(old) == 1, 'comparison compiler target anchor'
        body = body.replace(old, new)
    return body


def prepare(root, evidence, boot_id, ablation=None, comparison=None):
    assert ablation in (None, ABLATION), 'unknown compiler ablation'
    assert comparison in (None, COMPARISON), 'unknown compiler comparison'
    assert ablation is None or comparison is None, 'one closed compiler variant at most'
    root, evidence = Path(root), Path(evidence)
    root.mkdir(mode=0o700)
    f1 = evidence/'opt07-f1-component-preparation-v1/native-loader-proof-v2'
    attention = evidence/'opt07-attention-factorial-preparation-v1/native-loader-proof-v2'
    gemm = evidence/'gemm-wave-hint-approved-cpu-20261104-v1'
    g=json.loads((gemm/'COMPILE-RESULT.json').read_bytes())['cases']
    f=json.loads((f1/'COMPILE-RESULT.json').read_bytes())['cases']
    a=json.loads((attention/'COMPILE-RESULT.json').read_bytes())['cases']['p119990-rows8-sequential']
    baseline=evidence/'opt-00-baseline-and-route-witness-v1/native-source/author/package-v2/review'
    input_paths = dict(kernels=baseline/'kernels.py',
        attention_baseline=evidence/'opt07-attention-factorial-preparation-v1/packet-v3/source/decode_attention_baseline.py',
        attention_candidate=evidence/'opt07-attention-factorial-preparation-v1/packet-v3/source/decode_attention_candidate.py',
        fused=evidence/'opt07-f1-component-preparation-v1/packet-v3/source/norm_candidate.py')
    expected=dict(kernels='5571b10654697561455081455b6cd28d43314702f56c80160ef2e33e535b3693',
        attention_baseline='34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a',
        attention_candidate='ed26c67b69da358944999fdf9f769fafc4c2aef821335fbb777679b966e9e4e3',
        fused='3fe82935dd41b0647b1589dd5ff0e47542af1f58d76ff2f84b87509853ebbf9e')
    sources={key:path.read_bytes() for key,path in input_paths.items()}
    assert all(sha(raw)==expected[key] for key,raw in sources.items())
    payload=dict(sources={key:raw.decode() for key,raw in sources.items()},
        rows=dict(gemm={str(t):g['gate_up-m'+str(t)] for t in (8,16)},
                  f1={str(t):f[str(t)]['candidate'] for t in (8,16)},attention=a))
    # Reuse the already authenticated runtime/cache bootstrap, verbatim. It has
    # no GPU devices, no CUDA initialization and forbids child execution after
    # the approved compiler interception is installed.
    old=(f1/'native/TARGET.py').read_bytes()
    marker=b'\n\nimport ast,gzip,linecache\n'
    assert old.count(marker)==1
    bootstrap=old.split(marker)[0]+b'\n'
    body=Path(__file__).with_name('bundle_compile_target.py').read_bytes()
    if ablation is not None: body=ablation_body(body)
    if comparison is not None: body=comparison_body(body)
    packed=base64.b64encode(gzip.compress(json.dumps(payload,sort_keys=True).encode(),mtime=0))
    target=bootstrap+b'\nimport gzip\nPAYLOAD=json.loads(gzip.decompress(base64.b64decode('+repr(packed).encode()+b')))\n'+body
    old_recipe=(f1/'native/RECIPE.py').read_bytes(); tree=ast.parse(old_recipe)
    argv=ast.literal_eval(next(n.value for n in tree.body if isinstance(n,ast.Assign)
                              and any(isinstance(t,ast.Name) and t.id=='argv' for t in n.targets)))
    assert '/dev/kfd' not in argv and '--dev-bind' not in argv and '--unshare-all' in argv
    assert '--clearenv' in argv and argv[argv.index('LLVM_PATH')+1]=='/usr/local/lib/python3.14/dist-packages/_rocm_sdk_core/lib/llvm'
    recipe=('import json,subprocess,resource,time\nfrom pathlib import Path\n'
        'resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n'
        "assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()=="+repr(boot_id)+'\n'
        'started=time.monotonic()\nargv='+repr(argv)+'\nsource='+repr(target)+'\n'
        'result=subprocess.run(argv,input=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=225)\n'
        'assert len(result.stdout)+len(result.stderr)<=16<<20\n'
        "print(json.dumps(dict(returncode=result.returncode,elapsed_seconds=time.monotonic()-started,stdout=result.stdout.decode(),stderr=result.stderr.decode())))\n"
        'raise SystemExit(result.returncode)\n').encode()
    for name,raw in (('TARGET.py',target),('RECIPE.py',recipe)):
        compile(raw,name,'exec',dont_inherit=True)
        with (root/name).open('xb') as stream:stream.write(raw)
    pins={key:dict(path=str(path),sha256=expected[key],bytes=len(sources[key])) for key,path in input_paths.items()}
    pins.update(bootstrap=dict(path=str(f1/'native/TARGET.py'),sha256=sha(old)),
        recipe=dict(path=str(f1/'native/RECIPE.py'),sha256=sha(old_recipe)),
        gemm=dict(path=str(gemm/'COMPILE-RESULT.json'),sha256=sha((gemm/'COMPILE-RESULT.json').read_bytes())),
        attention_report=dict(path=str(attention/'COMPILE-RESULT.json'),sha256=sha((attention/'COMPILE-RESULT.json').read_bytes())),
        f1_report=dict(path=str(f1/'COMPILE-RESULT.json'),sha256=sha((f1/'COMPILE-RESULT.json').read_bytes())))
    with (root/'INPUTS.json').open('x') as stream:json.dump(pins,stream,indent=2,sort_keys=True)
    result=dict(target_sha256=sha(target),recipe_sha256=sha(recipe),gpu_launches=0,
                boot_id=boot_id,model_starts=0,seconds=225,output_bytes=16<<20)
    if ablation is not None: result.update(ablation=ablation)
    if comparison is not None: result.update(comparison=comparison)
    return result
