"""Four-member worker state, explicit attachment and real-input qualification.

The owner configures this once with source-authenticated dependencies. Numerical
shadow operations run only in the private eager probe context, never in capture
or scored graph replay. This module starts no process and grants no authority.
"""
from pathlib import Path
import time

import bundle_contract as c
import bundle_kernels as k

STATE = None


class State:
    def __init__(self, manifest, worker, torch, ops, dependencies, pins):
        self.manifest, self.worker = manifest, worker
        self.identity = c.worker_identity(manifest, worker)
        self.torch, self.ops, self.dependencies, self.pins = torch, ops, dependencies, pins
        self.records, self.routes, self.gate_up = {}, {}, {}
        self.gemm_capture = {str(i): {'8': 0, '16': 0} for i in range(64)}
        self.gemm_exact = {str(i): {'8': 0, '16': 0} for i in range(64)}
        self.f1_capture = {str(i): {'8': 0, '16': 0} for i in c.CONFIG['gdn_ids']}
        self.f1_exact = {str(i): {} for i in c.CONFIG['gdn_ids']}
        self.shadow_calls = 0
        self.numerical_receipt = None
        self.attached = False
        self.views = {}
        self.directory = Path(manifest['evidence_dir'])
        self.device_count = None
        self.fallback8 = None

    def view(self, kernels):
        key = id(kernels)
        if key not in self.views:
            self.views[key] = k.kernel_view(kernels, self.pins, self.capturing, self.records)
        return self.views[key]

    def capturing(self):
        return self.torch.cuda.is_current_stream_capturing()

    def exact(self, first, second, label):
        c.need(not self.capturing() and c.PHASE is not None and c.SHADOW,
               'numerical check outside eager qualification')
        c.need(first.shape == second.shape and first.dtype == second.dtype and
               self.torch.equal(first.contiguous().view(self.torch.uint8),
                                second.contiguous().view(self.torch.uint8)), label)
        self.shadow_calls += 1

    def maybe_publish(self):
        if self.numerical_receipt is not None:
            return
        tokens = (16,) if self.fallback8 else (8, 16)
        if (not all(all(v > 0 for v in row.values()) for row in self.gemm_exact.values()) or
                not all(all(row.get(str(t), 0) > 0 for t in tokens) for row in self.f1_exact.values())):
            return
        row = dict(schema=1, identity=self.identity, status='F1_GEMM_REAL_INPUT_EXACT',
            members=list(c.MEMBERS), f1=self.f1_exact, gemm=self.gemm_exact,
            f1_fallback8=self.fallback8, device_api_count=self.device_count,
            probe_ids=c.CONFIG['probe_ids'], scored=False, shadow_calls=self.shadow_calls,
            capture_shadow_calls=0, compilers=self.records, completed_monotonic=time.monotonic(),
            exact_members=['gatednorm_quant','gate_up_wave'])
        c.validate_numerics(row, self.identity, c.CONFIG)
        self.numerical_receipt = c.publish(self.directory /
            ('BUNDLE-NUMERICS-rank-%d.json' % self.worker['rank']), row)


def configure(manifest, worker, torch, ops, dependencies, pins):
    global STATE
    c.need(STATE is None and c.CONFIG is not None, 'single configured bundle worker')
    STATE = State(manifest, worker, torch, ops, dependencies, pins)
    return STATE


def kernel_view(kernels):
    # The selected scheme can load before the runner is attached. Configuration
    # itself is installed before Scheduler construction and any model execution.
    c.need(STATE is not None, 'bundle runtime not configured')
    return STATE.view(kernels)


def wave_hint(m, n, kdim, original):
    c.need(original == 1 and STATE is not None, 'selected GEMM wave default')
    changed = m in (8, 16) and (n, kdim) == (17408, 5120)
    candidate = STATE.manifest['arm'] == 'mtp'
    if c.SHADOW:
        candidate = not candidate
    return 0 if changed and candidate else original


def observe_gemm(x, weight, xs, ws, output, *, expected_device):
    state = STATE
    c.need(state is not None, 'GEMM observer configured')
    if c.SHADOW or not state.attached or id(weight) not in state.gate_up:
        return
    layer, bound_weight, bound_scale = state.gate_up[id(weight)]
    c.need(weight is bound_weight and ws is bound_scale and expected_device == state.worker['rank'],
           'bound gate/up tensor identity')
    m = x.shape[0]
    if m not in (8, 16):
        return
    if state.capturing():
        state.gemm_capture[str(layer)][str(m)] += 1
        return
    if c.PHASE is None or state.gemm_exact[str(layer)][str(m)] > 0:
        return
    t = state.torch
    with c.shadow():
        alternate = t.empty_like(output)
        alternate.view(t.uint8).fill_(0xA5)
        state.ops.gemm_preallocated(x, weight, xs, ws, alternate, expected_device=expected_device)
        state.exact(output, alternate, 'gate/up real-input byte mismatch')
    state.gemm_exact[str(layer)][str(m)] += 1
    state.maybe_publish()


def route_class(base):
    class QualifiedRoute(base):
        def try_project(self, model, batch, x, z, original_shape, strided):
            state = STATE
            c.need(state is not None and self.bundle_layer in state.routes, 'attached F1 route')
            tokens = original_shape[0] if len(original_shape) == 3 else None
            eligible = self.eligible(model, batch, x, z, original_shape, strided)
            if tokens in (8, 16) and batch.forward_mode.is_target_verify():
                expected = tokens == 16 or not state.fallback8
                if eligible != expected:
                    # Metadata only: preserve enough context to diagnose a
                    # refusal without copying values or launching diagnostics.
                    detail = dict(tokens=tokens, expected=expected, eligible=eligible,
                                  strided=bool(strided), fallback8=state.fallback8,
                                  effective_symmetric=getattr(self,'is_allocation_symmetric',lambda:None)())
                    for name, value in (('x',x),('z',z),('norm_weight',getattr(self,'norm_weight',None)),
                                        ('weight',getattr(self,'weight',None)),
                                        ('weight_scale',getattr(self,'weight_scale',None))):
                        if value is not None and hasattr(value,'stride'):
                            detail[name] = dict(shape=list(value.shape),stride=list(value.stride()),
                                                dtype=str(value.dtype),device=str(value.device))
                    c.need(False, 'F1 selected real/captured geometry refused: '+c.canonical(detail).decode())
                if state.capturing() and eligible:
                    state.f1_capture[str(self.bundle_layer)][str(tokens)] += 1
            if not eligible:
                return None
            # The control executes the original source path except for one eager
            # shadow per layer/geometry during the unscored numerical requests.
            testing = c.PHASE is not None and not c.SHADOW and not state.capturing()
            wanted = str(tokens) not in state.f1_exact[str(self.bundle_layer)]
            if state.manifest['arm'] == 'control' and not (testing and wanted):
                return None
            result = super().try_project(model, batch, x, z, original_shape, strided)
            return result if state.manifest['arm'] == 'mtp' else None

        def consume(self, scheme, layer, value, bias):
            output = super().consume(scheme, layer, value, bias)
            state = STATE
            tokens = value.packed.shape[0]
            if (c.PHASE is None or c.SHADOW or state.capturing() or
                    str(tokens) in state.f1_exact[str(self.bundle_layer)]):
                return output
            t = self.torch
            x, z, _ = value.inputs
            with c.shadow():
                # The production norm includes the BF16 rounding boundary.
                normed = self.norm(x, z).reshape(tokens, 3072)
                packed = t.empty_like(value.packed)
                scales = t.empty_strided((tokens, 96), (1, tokens), dtype=t.uint8, device=x.device)
                baseline = t.empty_like(output)
                packed.fill_(0xA5); scales.fill_(0x5A); baseline.view(t.uint8).fill_(0xA5)
                self.ops.quantize_preallocated(normed, packed, scales, expected_device=x.device.index)
                state.exact(value.packed, packed, 'F1 real-input packed byte mismatch')
                state.exact(value.scales, scales, 'F1 real-input scale byte mismatch')
                self.ops.gemm_preallocated(packed, self.weight, scales, self.weight_scale,
                                          baseline, expected_device=x.device.index)
                state.exact(output, baseline, 'F1 real-input projection byte mismatch')
            state.f1_exact[str(self.bundle_layer)][str(tokens)] = 1
            state.maybe_publish()
            return output
    return QualifiedRoute


def attach_runner(runner):
    """Called by the derived target runner after weight finalization, before capture."""
    state = STATE
    c.need(state is not None and not state.attached and runner.is_draft_worker is False and
           runner.tp_rank == runner.gpu_id == state.worker['rank'] and
           runner.startup_weight_load is None and not state.capturing(),
           'fresh target runner attachment boundary')
    d, t = state.dependencies, state.torch
    layers = runner.model.model.layers
    c.need(len(layers) == 64, 'complete target decoder inventory')
    state.device_count = t.cuda.get_device_properties(runner.gpu_id).multi_processor_count
    c.need(type(state.device_count) is int and state.device_count > 0, 'device API processor count')
    count8 = d['norm'].calc_rows_per_block(192, t.device('cuda', runner.gpu_id))
    count16 = d['norm'].calc_rows_per_block(384, t.device('cuda', runner.gpu_id))
    c.need(count8 in (2, 4) and count16 == 4, 'fixed F1 row geometry')
    state.fallback8 = count8 != 4
    route = route_class(d['host'].Route)
    fused = k.Audited(d['fused']._gated_norm_mxfp4_kernel, state.pins, state.capturing,
                     lambda args, kw: 'f1-t'+str(args[3]), state.records)
    for i, layer in enumerate(layers):
        projection = layer.mlp.gate_up_proj
        c.need(type(projection) is d['linear'].MergedColumnParallelLinear and
               type(projection.scheme) is d['scheme'] and projection.bias is None and
               projection._isolated_mxfp4_loaded is True and
               projection.scheme.input_size_per_partition == 5120 and
               projection.scheme.output_size_per_partition == 17408 and
               tuple(projection.weight.shape) == (17408, 2560) and
               tuple(projection.weight_scale.shape) == (17408, 160), 'exact gate/up layer')
        state.gate_up[id(projection.weight)] = (i, projection.weight, projection.weight_scale)
        if i not in c.CONFIG['gdn_ids']:
            continue
        model = layer.linear_attn
        item = route(model, torch=t, ops=state.ops, norm_module=d['norm'], kernel=fused,
            classes=(d['model'].Qwen3_5GatedDeltaNet, d['norm'].RMSNorm,
                     d['linear'].RowParallelLinear, d['scheme'], d['quark'].QuarkLinearMethod),
            get_forward=d['get_forward'], is_allocation_symmetric=d['is_allocation_symmetric'])
        item.bundle_layer = i
        item.attach()
        state.routes[i] = item
    c.need(len(state.gate_up) == 64 and len(state.routes) == 48, 'complete bundle attachment')
    state.attached = True


def attention_function(module):
    c.need(STATE is not None, 'attention audit configured')
    return k.attention_view(module, STATE.pins, STATE.capturing, STATE.records)


def startup_record():
    state = STATE
    c.need(state is not None and state.attached and not state.capturing() and
           state.torch.cuda.current_device() == state.worker['rank'], 'startup bundle boundary')
    c.complete_counts(state.gemm_capture, list(range(64)), (8, 16))
    expected_f1 = {str(i): {'8': 0 if state.fallback8 else 1, '16': 1} for i in c.CONFIG['gdn_ids']}
    for key, expected in expected_f1.items():
        c.need(all(state.f1_capture[key][t] >= n if n else state.f1_capture[key][t] == 0
                   for t, n in expected.items()), 'both F1 capture geometries')
    arm = state.manifest['arm']
    selected = {'attention-stage1', 'attention-stage2-cap'+('32' if arm == 'mtp' else '16'),
                'gate-up-m8-wave'+('0' if arm == 'mtp' else '1'),
                'gate-up-m16-wave'+('0' if arm == 'mtp' else '1')}
    if arm == 'mtp':
        selected.add('f1-t16')
        if not state.fallback8: selected.add('f1-t8')
    c.need(selected <= set(state.records), 'all selected bundle compiler witnesses before HTTP')
    return dict(schema=1, identity=state.identity, attached=True, members=list(c.MEMBERS),
        f1_capture=state.f1_capture, gemm_capture=state.gemm_capture,
        f1_fallback8=state.fallback8, device_api_count=state.device_count,
        selected_compilers={key: state.records[key] for key in sorted(selected)},
        numerical_qualified=False, scored_shadow_calls=0, before_external_generations=True)
