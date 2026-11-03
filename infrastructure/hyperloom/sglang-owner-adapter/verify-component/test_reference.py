"""Small scalar arithmetic fixture for the real reference's indexing and formula.

This checks CPU arithmetic and causality, not device BF16/FP32 rounding.
"""
import itertools
import math
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import attention_ops as ops
import contract
import source_loader


class Array:
    def __init__(self,shape,values=None,*,data=None,indices=None):
        self.shape=tuple(shape);n=math.prod(shape)
        self.data=list(values) if data is None and values is not None else [0.0]*n if data is None else data
        self.indices=list(range(n)) if indices is None else indices
    def float(self):return self
    def __getitem__(self,key):
        key=key if isinstance(key,tuple) else (key,)
        key=(*key,*([slice(None)]*(len(self.shape)-len(key))))
        ranges=[];shape=[]
        for size,k in zip(self.shape,key):
            if isinstance(k,int):ranges.append([k])
            else:
                seq=list(range(*k.indices(size)));ranges.append(seq);shape.append(len(seq))
        strides=[math.prod(self.shape[i+1:]) for i in range(len(self.shape))]
        ids=[self.indices[sum(i*s for i,s in zip(position,strides))] for position in itertools.product(*ranges)]
        return Array(shape,data=self.data,indices=ids)
    def __setitem__(self,key,other):
        view=self[key]
        assert view.shape==other.shape
        for dst,src in zip(view.indices,other.indices):self.data[dst]=other.data[src]
    def values(self):return [self.data[i] for i in self.indices]
    def index_select(self,axis,indices):
        assert axis==0
        values=[]
        for row in indices.values():values.extend(self[int(row)].values())
        return Array((len(indices.indices),*self.shape[1:]),values)
    @property
    def T(self):
        assert len(self.shape)==2
        rows,cols=self.shape;values=self.values()
        return Array((cols,rows),[values[r*cols+c] for c in range(cols) for r in range(rows)])
    def __matmul__(self,other):
        m,k=self.shape;k2,n=other.shape;assert k==k2
        a,b=self.values(),other.values()
        return Array((m,n),[sum(a[r*k+i]*b[i*n+c] for i in range(k)) for r in range(m) for c in range(n)])
    def __mul__(self,scalar):return Array(self.shape,[x*scalar for x in self.values()])


def softmax(a,dim):
    assert dim==-1 and len(a.shape)==2
    rows,cols=a.shape;values=a.values();result=[]
    for row in range(rows):
        v=values[row*cols:(row+1)*cols];e=[math.exp(x-max(v)) for x in v];total=sum(e)
        result.extend(x/total for x in e)
    return Array(a.shape,result)


class ReferenceTests(unittest.TestCase):
    def test_real_reference_uses_each_causal_stream_and_head_group(self):
        q=Array((2,12,256));k=Array((4,2,256));v=Array((4,2,256))
        for row in range(2):
            for head in range(12):q.data[(row*12+head)*256]=1.0
        for slot,key in enumerate((1600,16,-16,0)):
            for group in range(2):
                k.data[(slot*2+group)*256]=key
                for d in range(256):v.data[(slot*2+group)*256+d]=slot*10+group*100+d/10
        # Physical slot0 is a poison future token; row0 cannot see slot2 yet.
        idx=dict(queries=2,kv_indices=Array((5,),[3,1,3,1,2]),witness=dict(indptr=[0,2,5]))
        torch=types.SimpleNamespace(float32='float32',empty_like=lambda a,**kw:Array(a.shape),softmax=softmax)
        actual=ops.reference(torch,dict(q=q,k_buf=k,v_buf=v),idx)
        for row in range(2):
            expected=(30+10*math.e+(20/math.e if row else 0))/(1+math.e+(1/math.e if row else 0))
            for head in range(12):
                for d in (0,127,255):
                    self.assertAlmostEqual(actual.data[(row*12+head)*256+d],expected+(head//6)*100+d/10,places=11)

    def test_loader_refuses_source_changes_before_any_jit(self):
        root=ops.ROOT
        raws=[(root/n).read_bytes() for n in ('decode_attention_baseline.py','decode_attention_candidate.py','metadata.py')]
        for i in range(3):
            values=list(raws);values[i]+=b'\n'
            with self.assertRaisesRegex(ValueError,'source identity'):
                source_loader.load_pair(*values,None,None)

    def test_inherited_tolerance_is_not_replaced_by_bitwise_claim(self):
        b=dict(max_abs=.01,mean_abs=.001,finite=True)
        c=dict(max_abs=.01,mean_abs=.01,finite=True)
        self.assertFalse(contract.within_tolerance(c,b,1.0))


if __name__=='__main__':unittest.main(verbosity=2)
