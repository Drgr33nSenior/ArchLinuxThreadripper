"""Inert host-dispatch capture, executed only inside the native CPU sandbox.

The supplied fixture models tensor metadata, not tensor values. The target
compiler still uses Triton's real binder and an explicit gfx1201 target. A host
operation this fixture cannot represent is refused before GPU admission.
"""

import copy
import math
from pathlib import Path
import sys


def capture(fake_source, raw, role, frozen, *, root=None):
    class Tensor(fake_source.Tensor):
        @property
        def ndim(self):
            return len(self.shape)

        def numel(self):
            return math.prod(self.shape)

        def is_contiguous(self):
            return True

        def stride(self, index=None):
            if index is None:
                return tuple(super(Tensor, self).stride(i) for i in range(self.ndim))
            return super().stride(index)

        def to(self, dtype):
            if self.dtype not in ("int32", "int64") or dtype not in ("int32", "int64"):
                raise ValueError("only metadata-only integer index casts are supported")
            return Tensor(self.shape, dtype=dtype, card=self.device.index, device_type=self.device.type)

    root = Path("/tmp/hyperloom-component-dispatch") if root is None else root
    root.mkdir(exist_ok=True)
    filename = root / ("extend_attention_fwd_" + role + ".py")
    with filename.open("x") as stream:
        stream.write(raw)
    launches = []
    dtype_names = {"bf16": "bfloat16", "fp32": "float32", "int32": "int32", "int64": "int64"}
    for card in (0, 1):
        for prefix in (0, 16384):
            args = fake_source.arguments(card=card)
            for name, value in tuple(args.items()):
                if isinstance(value, fake_source.Tensor):
                    args[name] = Tensor(value.shape, dtype=value.dtype, card=card)
            args["qo_indptr"] = Tensor((2,), dtype="int32", card=card)
            args["kv_indptr"] = Tensor((2,), dtype="int32", card=card)
            args["kv_indices"] = Tensor((prefix,), dtype="int64", card=card)
            with fake_source.module(filename) as module:
                torch = sys.modules["torch"]
                torch.int32, torch.int64 = "int32", "int64"
                module.extend_attention_fwd(**args)
                if len(module._fwd_kernel.calls) != 1:
                    raise ValueError("one target attention dispatch required")
                grid, positional, kwargs = module._fwd_kernel.calls[0]
                converted = [dict(dtype=dtype_names[x.dtype], shape=list(x.shape))
                             if isinstance(x, fake_source.Tensor) else x for x in positional]
                launch = dict(grid=list(grid), args=converted, kwargs=kwargs)
                expected = copy.deepcopy(frozen)
                expected["args"][9]["shape"] = [prefix]
                # The only permitted ABI variation is the gathered-index width.
                # Every scalar, tile, launch option, and other pointer stays fixed.
                index_type = launch["args"][9]["dtype"]
                if index_type not in ("int32", "int64") or role == "baseline" and index_type != "int64":
                    raise ValueError("bounded index pointer type required")
                expected["args"][9]["dtype"] = index_type
                import json
                if json.dumps(launch, sort_keys=True) != json.dumps(expected, sort_keys=True):
                    raise ValueError("host dispatch changed the frozen component geometry or ABI")
                launches.append(dict(card=card, prefix=prefix, launch=launch))
    signatures = []
    for row in launches:
        normalized = copy.deepcopy(row["launch"])
        normalized["args"][9]["shape"] = [16384]
        signatures.append(normalized)
    if not all(item == signatures[0] for item in signatures):
        raise ValueError("both cards and prefix paths must share one specialization")
    return dict(launch=signatures[0], witnesses=launches)
