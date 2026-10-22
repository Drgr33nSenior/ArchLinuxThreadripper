def _workstation_no_residual_rms_supported(self, x, residual, post, quant):
    """Narrow inference-only path; unsupported semantics keep original dispatch."""
    return (
        _workstation_no_residual_rms_enabled
        and _is_hip
        and not _use_aiter
        and not _has_vllm_rms_norm
        and not is_batch_invariant_mode_enabled()
        and not torch.is_grad_enabled()
        and type(self) is RMSNorm
        and residual is None and post is None and quant is None
        and not self.cast_x_before_out_mul and not self.fp32_residual
        and self.override_orig_dtype is None
        and self.variance_size_override is None
        and getattr(self, "x_pad_to_multiple", 0) == 0
        and self.variance_epsilon == 1e-6
        and self.hidden_size in (128, 2048)
        and x.dim() in (2, 3) and x.shape[-1] == self.hidden_size
        and 0 < x.numel() // self.hidden_size <= 512
        and x.is_cuda and x.dtype == torch.bfloat16
        and self.weight.dtype == x.dtype and self.weight.device == x.device
        and tuple(self.weight.shape) == (self.hidden_size,)
        and self.weight.is_contiguous()
    )
