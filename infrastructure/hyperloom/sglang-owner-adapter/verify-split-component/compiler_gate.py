"""Retain bounded real ISA/binary artifacts; compare code and compiler resources."""
import gzip
import re
import zlib

import support as s

RAW_LIMIT=2<<20
STORED_LIMIT=1<<20
RESOURCE_FIELDS=('group_segment_fixed_size','private_segment_fixed_size','sgpr_count',
                 'sgpr_spill_count','vgpr_count','vgpr_spill_count','wavefront_size','max_flat_workgroup_size')


def code_body(isa,name):
    start=re.search(r'^'+re.escape(name)+r':\s*(?:;[^\n]*)?$',isa,re.M)
    if start is None:raise ValueError('compiler function label absent')
    end=re.search(r'^\.Lfunc_end[0-9]+:',isa[start.end():],re.M)
    if end is None:raise ValueError('compiler function end absent')
    lines=[]
    for line in isa[start.end():start.end()+end.start()].splitlines():
        line=line.split(';',1)[0].strip()
        if line and not line.startswith(('.loc ','.file ','.cfi_')):lines.append(line)
    if not lines:raise ValueError('empty compiler instruction body')
    return '\n'.join(lines)+'\n'


def inspect_isa(raw,name):
    if not 0<len(raw)<=RAW_LIMIT:raise ValueError('bounded compiler ISA')
    isa=raw.decode();resources={}
    for field in RESOURCE_FIELDS:
        vals=re.findall(r'^\s*\.'+field+r':\s*(\d+)\s*$',isa,re.M)
        if len(vals)!=1:raise ValueError('one compiler resource field: '+field)
        resources[field]=int(vals[0])
    if resources['wavefront_size']!=32 or resources['max_flat_workgroup_size']!=128:
        raise ValueError('four wave32 warps')
    if resources['group_segment_fixed_size']>65536:raise ValueError('LDS exceeds 64 KiB')
    body=code_body(isa,name).encode()
    return dict(resources=resources,code_body_sha256=s.sha(body),instruction_body_bytes=len(body))


def store(compiled,card,arm,artifacts):
    asm=compiled.asm;name=str(compiled.name)
    parts={}
    for part in ('amdgcn','hsaco'):
        raw=asm[part];raw=raw.encode() if type(raw) is str else raw
        if type(raw) is not bytes or not 0<len(raw)<=RAW_LIMIT:raise ValueError('bounded compiler part')
        packed=gzip.compress(raw,compresslevel=9,mtime=0)
        if len(packed)>STORED_LIMIT:raise ValueError('compressed compiler part cap')
        art=artifacts.write(f'compiler-card{card}-{arm}-{part}.gz',packed)
        parts[part]=dict(raw_bytes=len(raw),raw_sha256=s.sha(raw),artifact=art)
    facts=inspect_isa(asm['amdgcn'].encode(),name)
    shared=compiled.metadata.shared
    if type(shared) is not int or not 0<=shared<=65536:raise ValueError('bounded dynamic LDS')
    return dict(name=name,parts=parts,dynamic_lds_bytes=shared,**facts)


def decompress(raw):
    if len(raw)>STORED_LIMIT:raise ValueError('compressed compiler cap')
    d=zlib.decompressobj(16+zlib.MAX_WBITS);value=d.decompress(raw,RAW_LIMIT+1)
    if len(value)>RAW_LIMIT or not d.eof or d.unused_data or d.unconsumed_tail:
        raise ValueError('closed bounded compiler compression')
    return value


def replay(record,blobs):
    if type(record['dynamic_lds_bytes']) is not int or not 0<=record['dynamic_lds_bytes']<=65536:
        raise ValueError('bounded recorded dynamic LDS')
    if set(record['parts'])!={'amdgcn','hsaco'}:raise ValueError('compiler stage membership')
    decoded={}
    for part,row in record['parts'].items():
        raw=decompress(blobs[row['artifact']['path']])
        if len(raw)!=row['raw_bytes'] or s.sha(raw)!=row['raw_sha256']:raise ValueError('compiler raw identity')
        decoded[part]=raw
    facts=inspect_isa(decoded['amdgcn'],record['name'])
    if any(record[k]!=v for k,v in facts.items()):raise ValueError('compiler facts recomputation')
    return facts


def compare(records):
    if set(records)!={'B','C','B2','C2'}:raise ValueError('two stages per arm')
    for key,record in records.items():
        reduction=key.endswith('2');r=record['resources']
        if any(r[k]!=0 for k in ('private_segment_fixed_size','sgpr_spill_count','vgpr_spill_count')):
            return 'RESOURCE_REGRESSION'
        if r['vgpr_count']>(13 if reduction else 245) or r['sgpr_count']>(33 if reduction else 77):return 'RESOURCE_REGRESSION'
        if record['dynamic_lds_bytes']!=(0 if reduction else 16384) or r['group_segment_fixed_size']!=0:return 'RESOURCE_REGRESSION'
    for part in ('amdgcn','hsaco'):
        if records['B']['parts'][part]['raw_sha256']!=records['C']['parts'][part]['raw_sha256']:
            return 'UNEXPECTED_STAGE1_CHANGE'
    if records['B2']['code_body_sha256']==records['C2']['code_body_sha256']:return 'NO_DISTINCT_REDUCTION'
    return 'CONFIGURATION_READY'
