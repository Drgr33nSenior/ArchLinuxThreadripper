"""Deterministic BF16 bit corpus; synthetic, not captured model activations."""
import hashlib
import struct
from functools import lru_cache

SPECIAL=(0,0x8000,1,0x8001,0x007F,0x807F,0x0080,0x8080,
         0x3F80,0xBF80,0x7F7F,0xFF7F,0x7F80,0xFF80,0x7FC1,0xFFC1,0x7F81,0xFF81)
ROUND=(0x3EFF,0x3F00,0x3F01,0x3F3F,0x3F40,0x3F41,0x3F7F,0x3F80,0x3F81,
       0x3FBF,0x3FC0,0x3FC1,0x401F,0x4020,0x4021,0x405F,0x4060,0x4061)

def finite(index,seed):
    value=(1664525*((index+seed)&0xFFFFFFFF)+1013904223)&0xFFFFFFFF
    return ((value>>16)&0x8000)|((121+(value%10))<<7)|((value>>8)&127)

def values(case,which):
    t=case['tokens'];kind=case['kind'];count=128 if which=='w' else t*24*128
    if t not in (8,16) or which not in ('x','z','w'):raise ValueError('fixed BF16 corpus geometry')
    for i in range(count):
        value=finite(i,20261103+(0 if which=='x' else 997 if which=='z' else 127))
        if which=='w':value=(0x3F00+((i*13)%256)) | (0x8000 if i%11==0 else 0)
        if kind=='zeros' and which!='w':value=(i%2)<<15
        if kind=='subnormal' and which!='w':value=(i%128)|((i%2)<<15)
        if kind=='rounding' and which!='w':value=ROUND[i%len(ROUND)]|((i%2)<<15)
        if kind==which+'-special':value=SPECIAL[i%len(SPECIAL)]
        if kind.startswith(which+'-all-'):value=(i+int(kind[-1])*24576)&0xFFFF
        yield value

@lru_cache(maxsize=78)
def _raw(tokens,kind,which):
    return b''.join(struct.pack('<H',v) for v in values(dict(tokens=tokens,kind=kind),which))

def raw(case,which):
    return _raw(case['tokens'],case['kind'],which)

def identity(case):
    return {k:dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
            for k in ('x','z','w') for data in (raw(case,k),)}
