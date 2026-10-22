"""Native-reduction RMSNorm suffix; inserted only into the pinned source."""
if _workstation_no_residual_rms_enabled and _is_hip:
    import triton as _workstation_triton
    import triton.language as _workstation_tl

    @_workstation_triton.jit
    def _workstation_rms_suffix(X, SCALE, WEIGHT, OUT, N: _workstation_tl.constexpr,
                               NUMEL: _workstation_tl.constexpr, BLOCK: _workstation_tl.constexpr):
        offsets = _workstation_tl.program_id(0) * BLOCK + _workstation_tl.arange(0, BLOCK)
        active = offsets < NUMEL
        x = _workstation_tl.load(X + offsets, active, other=0)
        scale = _workstation_tl.load(SCALE + offsets // N, active, other=0)
        weight = _workstation_tl.load(WEIGHT + offsets % N, active, other=0).to(_workstation_tl.float32)
        normalized = x * scale
        weighted = normalized * weight
        _workstation_tl.store(OUT + offsets, weighted, active)


def _workstation_rms_epilogue(x, weight):
    working = x.contiguous().float()
    variance = working.pow(2).mean(-1, keepdim=True)
    scale = torch.rsqrt(variance + 1e-6)
    out = torch.empty_like(x, memory_format=torch.contiguous_format)
    _workstation_rms_suffix[(_workstation_triton.cdiv(x.numel(), 256),)](
        working, scale, weight, out, x.shape[-1], x.numel(), 256,
        num_warps=4, num_stages=1, enable_fp_fusion=False)
    return out
