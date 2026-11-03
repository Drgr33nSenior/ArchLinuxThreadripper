"""Load only the frozen grouped route; both arms share the unchanged stage2 JIT."""
import ast
import linecache
import sys
import types
from typing import Tuple

import contract
import support as s

SOURCE_PATH = '/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py'
METADATA_PATH = '/mnt/composition/image-source/python/sglang/kernels/ops/attention/metadata.py'
HOST_NAMES = {'decode_attention_fwd', 'decode_attention_fwd_grouped', '_decode_grouped_att_m_fwd',
              '_decode_softmax_reducev_fwd', '_grouped_head_tiles', '_extract_kv_strides',
              '_mla_launch_plan', '_mla_tuning_applies', '_lean_head_dim_ok'}


class ModuleScope:
    """Keep compiler aliases for the component lifetime, then restore exact priors.

    This component is isolated and sequential. Canonical names and source paths
    are required by the pinned binaries, but must not escape into the owner's
    selected-source inventory check, including on partial load or launch failure.
    """
    NAMES = ('sglang.kernels.ops.attention.decode_attention',
             'sglang.kernels.ops.attention.metadata')
    PATHS = (SOURCE_PATH, METADATA_PATH)

    def __enter__(self):
        self.absent = object()
        self.modules = {name: sys.modules.get(name, self.absent) for name in self.NAMES}
        self.lines = {path: linecache.cache.get(path, self.absent) for path in self.PATHS}
        return self

    def __exit__(self, kind, error, trace):
        for mapping, saved in ((sys.modules, self.modules), (linecache.cache, self.lines)):
            for key, previous in saved.items():
                if previous is self.absent:
                    mapping.pop(key, None)
                else:
                    mapping[key] = previous
        return False


def load_pair(baseline, candidate, metadata, triton, tl):
    for raw, pin in ((baseline, contract.BASELINE_SHA256), (candidate, contract.CANDIDATE_SHA256),
                     (metadata, contract.METADATA_SHA256)):
        if type(raw) is not bytes or s.sha(raw) != pin:
            raise ValueError('frozen kernel/metadata source identity')

    def unpack(score_mod, auxiliary):
        if score_mod is not None or auxiliary is not None:
            raise ValueError('no score modification or auxiliary tensors')
        return None, 0, 0, 0

    modules = []
    for raw, arm in ((baseline, 'B'), (candidate, 'C')):
        text = raw.decode()
        tree = ast.parse(text, filename=SOURCE_PATH)
        names = HOST_NAMES | {'_fwd_grouped_kernel_stage1'}
        if arm == 'B':
            names |= {'tanh', '_fwd_kernel_stage2'}
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        if {n.name for n in tree.body} != names:
            raise ValueError('frozen public decode function closure')
        module = types.ModuleType('sglang.kernels.ops.attention.decode_attention')
        module.__file__ = SOURCE_PATH
        module.__dict__.update(triton=triton, tl=tl, Tuple=Tuple, _is_hip=True, _is_gfx1250=False,
                               _GROUPED_BLOCK_H=16, _MIN_BLOCK_KV=32, unpack_aux_tensors=unpack)
        if arm == 'C':
            module.tanh = modules[0].tanh
            module._fwd_kernel_stage2 = modules[0]._fwd_kernel_stage2
        linecache.cache[SOURCE_PATH] = (len(text), None, text.splitlines(True), SOURCE_PATH)
        sys.modules[module.__name__] = module
        exec(compile(tree, SOURCE_PATH, 'exec'), module.__dict__)
        modules.append(module)
    tree = ast.parse(metadata, filename=METADATA_PATH)
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_num_kv_splits_triton']
    if len(tree.body) != 1:
        raise ValueError('one pinned singleton metadata kernel')
    module = types.ModuleType('sglang.kernels.ops.attention.metadata')
    module.__file__ = METADATA_PATH
    module.__dict__.update(triton=triton, tl=tl)
    text = metadata.decode()
    linecache.cache[METADATA_PATH] = (len(text), None, text.splitlines(True), METADATA_PATH)
    sys.modules[module.__name__] = module
    exec(compile(tree, METADATA_PATH, 'exec'), module.__dict__)
    for arm in modules:
        arm._split_helper = module.get_num_kv_splits_triton
    if modules[0]._fwd_kernel_stage2 is not modules[1]._fwd_kernel_stage2:
        raise ValueError('shared stage2 identity')
    return tuple(modules)
