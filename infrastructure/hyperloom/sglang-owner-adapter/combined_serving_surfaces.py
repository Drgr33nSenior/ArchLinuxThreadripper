"""Derive the inactive combined attention surface from the reviewed producer.

This preparation tool does not build an owner packet or install a serving route.
F1's separately reviewed model/scheme additions and host route are carried by the
preparation evidence without modification. Its production attachment is a gate.
"""
import ast
import hashlib

ADAPTER_SHA = 'f03083142af33c287432e12050b31b07e4b26b03a6e3007ae77d992fdd268ca5'
BACKEND_SHA = '9924224f0dd4150fe1fc9ef7de6a556c49857608792479379179a973436762c1'

HELPERS = '''    def verification_cap(self, backend):
        # A private verification buffer width; never mutate backend.max_kv_splits.
        need(self.manifest['arm'] in ('control', 'mtp'), 'combined arm identity')
        return (32 if self.manifest['arm'] == 'mtp' and
                getattr(backend, 'bf16_query_fp8_target_kv', False) is not True else 16)

    def select_verification_splits(self, backend, splits, lengths, cap):
        need(cap == self.verification_cap(backend), 'combined split cap binding')
        if cap == 16:
            return backend.get_num_kv_splits(splits, lengths)
        need(backend.max_kv_splits == 16 and backend.static_kv_splits is False and
             backend.enable_deterministic is False and backend.split_tile_size is None and
             type(backend.device_core_count) is int and backend.device_core_count > 0 and
             backend.num_head == 12 and backend.num_kv_head == 2 and
             tuple(splits.shape) == tuple(lengths.shape) == (1,),
             'combined singleton split geometry')
        need(self.split_selector is not None, 'combined split helper missing')
        # The same dynamic singleton helper/geometry as the selected backend,
        # with its maximum supplied explicitly for this private path only.
        self.split_selector[(1,)](
            splits, lengths, 1, 1, backend.num_head, backend.num_kv_head,
            cap, backend.device_core_count, MAX_NUM_SEQ=256)

'''


def derive(adapter, backend):
    for raw, pin in ((adapter, ADAPTER_SHA), (backend, BACKEND_SHA)):
        if hashlib.sha256(raw).hexdigest() != pin:
            raise ValueError('combined serving source pin differs')
    text = adapter.decode()

    def edit(before, after, count=1):
        nonlocal text
        if text.count(before) != count:
            raise ValueError('combined serving source anchor differs')
        text = text.replace(before, after)

    edit('    def decode(self, backend, batch, args, layer):\n',
         HELPERS + '    def decode(self, backend, batch, args, layer):\n')
    edit('        self.prefetch_decode = None\n',
         '        self.prefetch_decode = None\n'
         "        self.split_selector = (source_module(manifest['sources']['triton_backend']).get_num_kv_splits_triton\n"
         "                               if manifest['arm'] == 'mtp' else None)\n")
    edit('        rows = 8 * bs\n        key = (id(backend), rows)\n',
         '        rows = 8 * bs\n        cap = self.verification_cap(backend)\n'
         '        key = (id(backend), rows)\n')
    edit("partial=empty((rows, 12, 16, 256), torch.float32), lse=empty((rows, 12, 16), torch.float32))",
         "partial=empty((rows, 12, cap, 256), torch.float32), lse=empty((rows, 12, cap), torch.float32))")
    edit("        b = self.buffers[key]\n", "        b = self.buffers[key]\n"
         "        need(tuple(b['partial'].shape) == (rows, 12, cap, 256) and\n"
         "             tuple(b['lse'].shape) == (rows, 12, cap), 'combined buffer policy changed')\n")
    edit('torch.cuda.is_current_stream_capturing(), rows)',
         'torch.cuda.is_current_stream_capturing(), rows, cap)')
    edit("backend.get_num_kv_splits(b['splits'][row:row+1], b['lengths'][row:row+1])",
         "self.select_verification_splits(backend, b['splits'][row:row+1], b['lengths'][row:row+1], cap)")
    edit("self.bf16_attention(backend, q, kb, vb, out, b['indptr'], b['indices'],\n"
         "                b['partial'], b['lse'], b['splits'], 16, scale, ks, vs, enable_lean=False, page_size=1)",
         "self.bf16_attention(backend, q, kb, vb, out, b['indptr'], b['indices'],\n"
         "                b['partial'], b['lse'], b['splits'], cap, scale, ks, vs, enable_lean=False, page_size=1)")
    ast.parse(text)
    return text.encode()
