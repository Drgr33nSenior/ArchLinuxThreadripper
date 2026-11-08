"""One read-only post-component power observation; never an admission condition."""
import hashlib
import math
import os
from pathlib import Path
import stat
import time

BDFS=('0000:23:00.0','0000:c3:00.0')

def observe():
    rows=[]
    for bdf in BDFS:
        device=Path('/sys/bus/pci/devices')/bdf
        root=device.resolve(strict=True)
        paths=sorted((device/'hwmon').glob('hwmon*/power1_cap'))
        if len(paths)!=1:raise ValueError('unique current power-cap readback required')
        path=paths[0].resolve(strict=True)
        if not path.is_relative_to(root) or path.name!='power1_cap':raise ValueError('power source/device identity')
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('regular sysfs power source')
            raw=stream.read(33)
        if not 0<len(raw)<=32 or not raw.strip().isdigit():raise ValueError('bounded power integer readback')
        rows.append(dict(bdf=bdf,path=str(path),raw=raw.decode('ascii'),raw_sha256=hashlib.sha256(raw).hexdigest(),microwatts=int(raw)))
    return dict(observed_monotonic=time.monotonic(),devices=rows)

def optional_observation():
    value=dict(schema=1,mode='read-only-post-component-power-comparability-v1',required_for_component_acceptance=False,cap_changed=False)
    try:
        observation=observe()
        value.update(status='OBSERVED',observation=observation,all_300w=all(r['microwatts']==300000000 for r in observation['devices']))
    except (OSError,ValueError) as error:
        value.update(status='UNVERIFIED',observation=None,all_300w=None,reason=(type(error).__name__+': '+str(error))[:500])
    validate_optional(value)
    return value

def validate_optional(value):
    if type(value)is not dict or value.get('schema')!=1 or type(value['schema'])is not int or value.get('mode')!='read-only-post-component-power-comparability-v1' or value.get('required_for_component_acceptance')is not False or value.get('cap_changed')is not False:raise ValueError('power observation scope')
    keys={'schema','mode','required_for_component_acceptance','cap_changed','status','observation','all_300w'}
    if value['status']=='UNVERIFIED':
        if set(value)!=keys|{'reason'} or value['observation']is not None or value['all_300w']is not None or type(value['reason'])is not str or not 0<len(value['reason'])<=500:raise ValueError('honest unverified power observation')
        return value
    if value['status']!='OBSERVED' or set(value)!=keys:raise ValueError('power observation status')
    observation=value['observation']
    if type(observation)is not dict or set(observation)!={'observed_monotonic','devices'}:raise ValueError('power observation shape')
    stamp=observation['observed_monotonic']
    if type(stamp)not in(int,float)or not math.isfinite(stamp)or stamp<=0:raise ValueError('power clock')
    rows=observation['devices']
    if type(rows)is not list or len(rows)!=2:raise ValueError('both power devices')
    for row,bdf in zip(rows,BDFS,strict=True):
        if type(row)is not dict or set(row)!={'bdf','path','raw','raw_sha256','microwatts'} or row['bdf']!=bdf:raise ValueError('power device record')
        path=Path(row['path'])
        if not path.is_absolute()or str(path)!=row['path']or '..'in path.parts or path.name!='power1_cap'or bdf not in path.parts:raise ValueError('power path')
        raw=row['raw'].encode('ascii')
        if not 0<len(raw)<=32 or not raw.strip().isdigit()or hashlib.sha256(raw).hexdigest()!=row['raw_sha256']or type(row['microwatts'])is not int or int(raw)!=row['microwatts']:raise ValueError('power raw integer identity')
    expected=all(r['microwatts']==300000000 for r in rows)
    if value['all_300w']is not expected:raise ValueError('power comparability conclusion')
    return value
