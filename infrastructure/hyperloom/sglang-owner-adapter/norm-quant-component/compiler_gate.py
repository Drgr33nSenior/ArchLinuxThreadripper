"""Exact compiler parts and resources; bounded retention and offline replay."""
import gzip
import re
import zlib
import contract
import support as s

PARTS=('ttir','ttgir','llir','amdgcn','hsaco')
RAW_LIMIT=2<<20
STORED_LIMIT=1<<20
FIELDS=('vgpr_count','sgpr_count','vgpr_spill_count','sgpr_spill_count',
        'private_segment_fixed_size','wavefront_size','max_flat_workgroup_size')

def facts(isa,shared):
    result={}
    for field in FIELDS:
        rows=re.findall(r'^\s*\.'+field+r':\s*(\d+)\s*$',isa,re.M)
        if len(rows)!=1:raise ValueError('one compiler field: '+field)
        result[field]=int(rows[0])
    if type(shared)is not int or not 0<=shared<=65536:raise ValueError('bounded shared memory')
    result['dynamic_lds_bytes']=shared
    return result

def raw_parts(compiled):
    parts={}
    for name in PARTS:
        raw=compiled.asm[name];raw=raw.encode() if isinstance(raw,str) else raw
        if type(raw)is not bytes or not 0<len(raw)<=RAW_LIMIT:raise ValueError('compiler part bound')
        parts[name]=raw
    return parts

def verify(compiled,tokens,stage):
    raw=raw_parts(compiled);expected=contract.COMPILER_PINS[str(tokens)][stage]
    if {k:s.sha(v) for k,v in raw.items()}!=expected['hashes']:raise ValueError('exact compiler identity '+stage)
    if facts(raw['amdgcn'].decode(),compiled.metadata.shared)!=expected['resources']:
        raise ValueError('exact compiler resource identity')
    return raw

def store(compiled,card,tokens,stage,artifacts):
    raw=verify(compiled,tokens,stage);parts={}
    for name,data in raw.items():
        packed=gzip.compress(data,mtime=0)
        if len(packed)>STORED_LIMIT:raise ValueError('stored compiler cap')
        art=artifacts.write(f'compiler-card{card}-{tokens}-{stage}-{name}.gz',packed)
        parts[name]=dict(raw_bytes=len(data),raw_sha256=s.sha(data),artifact=art)
    return dict(parts=parts,resources=facts(raw['amdgcn'].decode(),compiled.metadata.shared))

def replay(record,tokens,stage,blobs):
    expected=contract.COMPILER_PINS[str(tokens)][stage]
    if set(record['parts'])!=set(PARTS):raise ValueError('all five compiler parts')
    raw={}
    for name,row in record['parts'].items():
        packed=blobs[row['artifact']['path']]
        if len(packed)>STORED_LIMIT:raise ValueError('compiler compression cap')
        d=zlib.decompressobj(16+zlib.MAX_WBITS);data=d.decompress(packed,RAW_LIMIT+1)
        if len(data)>RAW_LIMIT or not d.eof or d.unused_data or d.unconsumed_tail:raise ValueError('closed compression')
        if len(data)!=row['raw_bytes'] or s.sha(data)!=row['raw_sha256'] or s.sha(data)!=expected['hashes'][name]:
            raise ValueError('compiler artifact identity')
        raw[name]=data
    if record['resources']!=expected['resources'] or facts(raw['amdgcn'].decode(),record['resources']['dynamic_lds_bytes'])!=record['resources']:
        raise ValueError('compiler resources replay')
