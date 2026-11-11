"""UQ3 Phase 0a capture hook, installed as sitecustomize.py in a capture Job only.

Python imports sitecustomize at start-up from PYTHONPATH, in the launcher and in every
spawned scheduler (TP rank) process. When UQ3_CAPTURE_TOKENS is set, this wraps
sglang.srt.layers.radix_attention.RadixAttention.forward as soon as SGLang imports it, and
on TP rank 0 records, for each full-attention layer of the target (head_dim 256; the DFlash
draft's head_dim is 128), what the KV cache would store:

  k, v  the first N prompt tokens  [N, KV heads per rank, 256]  (after QK-norm and RoPE)
  q     the 32 query rows ending at token N  [32, Q heads per rank, 256]

from the first request's extend (prefill) chunks only; prefill runs without CUDA graphs in
this profile, so the hook sees every chunk. It writes UQ3_CAPTURE_DIR/capture-rank0.pt once
all layers reach N, then a .done marker, and never touches decode or later requests.
The format is what serving tools/uq3_fakequant.py --capture reads.
"""
import os

TOKENS = int(os.environ.get("UQ3_CAPTURE_TOKENS", "0") or 0)
QUERY_ROWS = 32


class _Capture:
    def __init__(self, tokens, layers):
        self.tokens, self.layers = tokens, set(layers)
        self.k, self.v, self.q_tail, self.count = {}, {}, {}, {}
        self.done = False

    def take(self, layer_id, q, k, v):
        """Record one extend chunk for one layer; returns True when every layer reached N."""
        import torch
        have = self.count.get(layer_id, 0)
        if have >= self.tokens:
            return False
        rows = min(k.shape[0], self.tokens - have)
        self.k.setdefault(layer_id, []).append(k[:rows].detach().to("cpu", torch.bfloat16))
        self.v.setdefault(layer_id, []).append(v[:rows].detach().to("cpu", torch.bfloat16))
        tail = q[:rows].detach().to("cpu", torch.bfloat16)
        previous = self.q_tail.get(layer_id)
        self.q_tail[layer_id] = (tail if previous is None else torch.cat([previous, tail]))[-QUERY_ROWS:]
        self.count[layer_id] = have + rows
        return self.layers <= {lid for lid, n in self.count.items() if n >= self.tokens}

    def result(self):
        import torch
        return {lid: {"q": self.q_tail[lid], "k": torch.cat(self.k[lid]), "v": torch.cat(self.v[lid])}
                for lid in sorted(self.layers)}


def _rank():
    try:
        from sglang.srt.distributed import get_tensor_model_parallel_rank
        return get_tensor_model_parallel_rank()
    except Exception:  # noqa: BLE001 - not initialised yet (launcher process)
        return -1


def install(radix_module, layers, out_dir, tokens=TOKENS):
    """Wrap RadixAttention.forward; returns the capture state (exposed for tests)."""
    state = _Capture(tokens, layers)
    original = radix_module.RadixAttention.forward

    def forward(self, q, k, v, forward_batch, *args, **kwargs):
        try:
            mode = forward_batch.forward_mode
            if (not state.done and k is not None and self.head_dim == 256 and self.layer_id in state.layers
                    and mode.is_extend() and not getattr(mode, "is_target_verify", lambda: False)()
                    and getattr(forward_batch, "batch_size", 1) == 1 and _rank() == 0):
                complete = state.take(self.layer_id, q.view(-1, self.tp_q_head_num, self.head_dim),
                                      k.view(-1, self.tp_k_head_num, self.head_dim),
                                      v.view(-1, self.tp_v_head_num, self.v_head_dim))
                if complete:
                    import torch
                    os.makedirs(out_dir, exist_ok=True)
                    path = os.path.join(out_dir, "capture-rank0.pt")
                    torch.save(state.result(), path + ".tmp")
                    os.replace(path + ".tmp", path)
                    open(path + ".done", "w").close()
                    state.done = True
        except Exception as error:  # noqa: BLE001 - never break serving for a capture
            state.done = True
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "capture-error.txt"), "w") as handle:
                handle.write(repr(error))
        return original(self, q, k, v, forward_batch, *args, **kwargs)

    radix_module.RadixAttention.forward = forward
    return state


if TOKENS > 0:
    import importlib.abc
    import sys

    _TARGET = "sglang.srt.layers.radix_attention"
    _LAYERS = [int(x) for x in os.environ["UQ3_CAPTURE_LAYERS"].split(",")]
    _OUT = os.environ.get("UQ3_CAPTURE_DIR", "/cache/uq3")

    class _Finder(importlib.abc.MetaPathFinder):
        """Patch RadixAttention right after SGLang executes its module."""

        def find_spec(self, fullname, path, target=None):
            if fullname != _TARGET:
                return None
            sys.meta_path.remove(self)
            try:
                import importlib.util
                spec = importlib.util.find_spec(fullname)
            finally:
                sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader
            run = loader.exec_module

            def exec_module(module):
                run(module)
                install(module, _LAYERS, _OUT)
            loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _Finder())
