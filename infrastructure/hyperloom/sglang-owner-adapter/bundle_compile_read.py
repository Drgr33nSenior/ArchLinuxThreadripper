"""Decode and check the finite CPU compiler receipt; never import frameworks."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import re

META = ('arch','global_scratch_size','profile_scratch_size','num_stages','num_warps','shared','warp_size','waves_per_eu')
PARTS = ('ttir','ttgir','llir','amdgcn','hsaco')


def sha(raw): return hashlib.sha256(raw).hexdigest()


def extract(root,label):
    root=Path(root)
    receipt=json.loads((root/(label+'.json')).read_bytes())
    assert receipt['exit_code']==0 and receipt['child_reaped'] and receipt['owned_group_absent']
    assert receipt['stdin_sha256']==sha((root/'proof/RECIPE.py').read_bytes())
    raw=(root/(label+'.stdout')).read_bytes()
    assert len(raw)==receipt['stdout']['bytes'] and sha(raw)==receipt['stdout']['sha256']
    wrapper=json.loads(raw);assert wrapper['returncode']==0 and not wrapper['stderr']
    report=json.loads(wrapper['stdout'])
    assert report['status']=='CPU_SERVING_COMPILERS_PROVEN' and report['gpu_launches']==report['model_starts']==0
    assert report['device_nodes'] is False and report['calibration_complete_before_serving'] is True
    assert report['compiled_objects_unloaded'] and report['original_modules_and_linecache_restored']
    assert report['fresh_codegen_for_every_compile'] is True and report['artifact_source_paths_verified'] is True
    assert len(report['artifacts'])==10
    # The cap-only ablation compiles control attention from the prefetch source.
    assert report.get('ablation',None) in (None,'verify_cap32_single_variable')
    prefetch_stage1=('mtp-attention-stage1','control-attention-stage1') if report.get('ablation') else ('mtp-attention-stage1',)
    # The three-member comparison keeps native control attention; only candidate
    # stage 2 moves from cap32 to the cap16 specialization of the prefetch source.
    assert report.get('comparison',None) in (None,'three_member_cap16_serving')
    assert not (report.get('ablation') and report.get('comparison')), 'one closed compiler variant'
    if report.get('comparison'):
        native='/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py'
        candidate='/mnt/review/decode_attention_fwd_candidate.py'
        assert set(report['artifacts'])=={'gate-up-m8-wave0','gate-up-m8-wave1','gate-up-m16-wave0','gate-up-m16-wave1',
            'f1-t8','f1-t16','control-attention-stage1','control-attention-stage2-cap16','mtp-attention-stage1',
            'mtp-attention-stage2-cap16'},'comparison artifact set'
        assert all(report['serving'][key]['source_path']==(native if key.startswith('control-') else candidate)
                   for key in report['artifacts'] if 'attention' in key),'comparison attention sources'
    directory=root/'artifacts';directory.mkdir(mode=0o700)
    resources={}
    for key,parts in report['artifacts'].items():
        assert set(parts)==set(PARTS)
        if key.startswith('mtp-'):arm='mtp';name=key[4:]
        elif key.startswith('control-'):arm='control';name=key[8:]
        else:arm='mtp';name=key
        expected=report['pins'][arm][name]
        decoded={}
        for part,blob in parts.items():
            raw=gzip.decompress(base64.b64decode(blob,validate=True))
            assert len(raw)==expected['parts'][part]['bytes'] and sha(raw)==expected['parts'][part]['sha256']
            assert 0<len(raw)<=8<<20
            if part=='ttir':
                assert ('loc('+json.dumps(report['serving'][key]['source_path'])+':') in raw.decode(), (key,'source origin')
            filename=key+'.'+part
            with (directory/filename).open('xb') as stream:stream.write(raw)
            decoded[part]=dict(path='artifacts/'+filename,bytes=len(raw),sha256=sha(raw))
            if part=='amdgcn':isa=raw.decode()
        facts={}
        for field in ('sgpr_count','vgpr_count','sgpr_spill_count','vgpr_spill_count',
                      'private_segment_fixed_size','group_segment_fixed_size','wavefront_size','max_flat_workgroup_size'):
            values=re.findall(r'^\s*\.'+field+r':\s*(\d+)\s*$',isa,re.M)
            assert len(values)==1,(key,field,values)
            facts[field]=int(values[0])
        assert facts['sgpr_spill_count']==facts['vgpr_spill_count']==facts['private_segment_fixed_size']==0
        assert facts['wavefront_size']==32
        want=(90,41,4096) if key.startswith('gate-up-') and key.endswith('wave0') else \
             (109,41,4096) if key.startswith('gate-up-') else \
             (62,31,0) if key.startswith('f1-') else \
             (236,100,16384) if key in prefetch_stage1 else \
             (245,77,16384) if key=='control-attention-stage1' else (13,33,0)
        assert (facts['vgpr_count'],facts['sgpr_count'],expected['metadata']['shared'])==want,(key,facts,expected['metadata'])
        resources[key]=facts
        report['artifacts'][key]=decoded
    pins_raw=(json.dumps(report['pins'],sort_keys=True,separators=(',',':'))+'\n').encode()
    for filename,data in (('BUNDLE-COMPILER-PINS.json',pins_raw),
            ('COMPILE-RESULT.json',(json.dumps(report,sort_keys=True,indent=2)+'\n').encode()),
            ('RESOURCE-RESULT.json',(json.dumps(resources,sort_keys=True,indent=2)+'\n').encode())):
        with (root/filename).open('xb') as stream:stream.write(data)
    result=dict(status=report['status'],compiler_pins_sha256=sha(pins_raw),binaries=10,artifacts=50,
                resources_exact=True,gpu_launches=0,model_starts=0)
    if report.get('comparison'):result.update(comparison=report['comparison'])
    return result
