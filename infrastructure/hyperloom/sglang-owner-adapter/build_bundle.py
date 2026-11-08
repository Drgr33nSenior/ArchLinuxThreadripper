"""Inert, closed derivation of the four reviewed cumulative members.

Called only by build_owner. Every generated input is pinned before import into
the existing owner; no registration, remote access or GPU launch occurs here.
"""
import ast
import json
from pathlib import Path
import textwrap
from types import FunctionType

from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, require

import bundle_client
import combined_serving_surfaces

FAMILY = 'four_member'
MODULES = ('bundle_contract', 'bundle_auth', 'bundle_kernels', 'bundle_runtime',
           'bundle_hooks', 'bundle_client', 'bundle_readout')
FIXED = dict(
    f1_model='4b08207146f354d2fdfd56def029d06c46c2a1947fcaa8c0f14e16d8b1e9bb10',
    f1_scheme='705cfe6c38d61462de303b742a996b468eb82c6279bab0edc090fbb457eeef00',
    f1_host='8c1dfba85b8bdd544417e8421b5ef18bd84cb55dcb100a07f1e2233686b01d27',
    f1_fused='3fe82935dd41b0647b1589dd5ff0e47542af1f58d76ff2f84b87509853ebbf9e',
    runner='da81d4c393cf5910753c48036bedbe593e851ffe0985997f4eca02d79737df76',
    backend='9924224f0dd4150fe1fc9ef7de6a556c49857608792479379179a973436762c1',
    config='4a139d3e01df039e17b4c8b8362f7944674d76051a8cd96f213c6bc332d69f55')
# The controller accepts exactly two SPRY_RESOURCE_V1 records of at most 64 KiB
# each on the same stdout stream as the bundle line; count prefix and newline.
RESOURCE_RECORD_BYTES = len(b'SPRY_RESOURCE_V1 ') + (64 << 10) + 1
# Explicit opt-in mechanism ablation. Both arms run prefetch, F1 and gate/up
# wave0; only the private verification split cap differs. Absent this input the
# legacy control/four-member derivation is unchanged byte for byte.
ABLATION = 'verify_cap32_single_variable'
ABLATION_MEMBERS = {'control': ['verify_prefetch', 'gatednorm_quant', 'gate_up_wave'],
                    'mtp': ['verify_prefetch', 'verify_cap32', 'gatednorm_quant', 'gate_up_wave']}
CANDIDATE_ATTENTION = '/mnt/review/decode_attention_fwd_candidate.py'
SHARED_RECORDS = ('attention-stage1', 'f1-t8', 'f1-t16', 'gate-up-m8-wave0', 'gate-up-m8-wave1',
                  'gate-up-m16-wave0', 'gate-up-m16-wave1')
# Explicit opt-in serving comparison, closed to exactly these two arms. The control
# label is the selected baseline (native verification attention, original
# norm/quantizer, gate/up wave1); the mtp label adds verification prefetch, F1
# and gate/up wave0. Both keep private verification cap16; cap32 is not a member.
COMPARISON = 'three_member_cap16_serving'
COMPARISON_MEMBERS = {'control': [], 'mtp': ['verify_prefetch', 'gatednorm_quant', 'gate_up_wave']}
NATIVE_ATTENTION = '/mnt/composition/image-source/python/sglang/kernels/ops/attention/decode_attention.py'
# Explicit opt-in final confirmation of exactly the three-member cap16 comparison with the
# logical start order reversed: candidate (mtp) first, control second. The physical owner
# labels and order stay bf16 then kv4; only the frozen FINAL-SETTINGS.execution_order, which
# mtp_contract.owner_arm_map() already consumes, changes. Arm members, compiler records,
# caps, menu and limits are those of the comparison. Absent this input every byte of the
# comparison derivation is unchanged. The controller line below is a PROPOSED protected-helper
# change: it needs an exact owner exception before any execution; nothing here approves it.
CONFIRMATION = 'three_member_cap16_reversed_confirmation'
CONFIRMATION_ORDER = ['mtp', 'control']
CONFIRMATION_ROLE_MAP = {'bf16': 'mtp', 'kv4': 'control'}
PAIRED_ORDER_OLD = "len(rows)==30 and value['execution_order']==['control','mtp'],"
PAIRED_ORDER_NEW = "len(rows)==30 and value['execution_order']==['mtp','control'],"
WIRE_ROLE_OLD = "value.get('arm') != {'bf16':'control','kv4':'mtp'}[owner_arm]):"
WIRE_ROLE_NEW = "value.get('arm') != mtp_contract.owner_arm_map()[owner_arm]):"


def regenerate_pins(common, files, review):
    """Keep the donor's exact equality guard, bound to the declared new runner.

    The donor module and helper bytes remain unchanged. The one data binding
    differs because this experiment explicitly adds the before-capture attach.
    No ceiling, validator, helper code or other function global is replaced.
    """
    require(common.COMBINED_RUNNER == FIXED['runner'], 'selected donor runner constant')
    function = common.regenerate_pins
    namespace = dict(function.__globals__)
    namespace['COMBINED_RUNNER'] = digest(files['d3b_combined_model_runner.py'])
    require(namespace['COMBINED_RUNNER'] != common.COMBINED_RUNNER, 'declared runner attachment delta')
    scoped = FunctionType(function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__)
    require({key for key in namespace if namespace[key] is not function.__globals__[key]} == {'COMBINED_RUNNER'},
            'only the exact declared runner identity may differ')
    return scoped(files, review)


def edit(files, name, old, new, count=1):
    text = files[name].decode()
    require(text.count(old) == count, 'four-member source drift: '+name+': '+old[:90])
    files[name] = text.replace(old, new).encode()


def derive_ops(raw):
    require(digest(raw) == '66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776',
            'selected packed GEMM baseline')
    files = {'ops.py': raw}
    edit(files, 'ops.py', 'from .prefill_reference import quantizer_geometry\n',
         'from .prefill_reference import quantizer_geometry\nimport bundle_runtime\n')
    edit(files, 'ops.py', '    return torch, triton, kernels\n',
         '    return torch, triton, bundle_runtime.kernel_view(kernels)\n')
    edit(files, 'ops.py', '    return kernels._gemm_afp4wfp4_kernel[',
         '    config["waves_per_eu"] = bundle_runtime.wave_hint(m, n, k, config["waves_per_eu"])\n'
         '    compiled = kernels._gemm_afp4wfp4_kernel[')
    suffix = '        **config,\n    )\n'
    edit(files, 'ops.py', suffix, suffix+
         '    bundle_runtime.observe_gemm(x, weight, x_scales, weight_scales, y, expected_device=expected_device)\n'
         '    return compiled\n')
    return files['ops.py']


def ablation_contract(value, pins):
    """Bind the cap-only ablation to its native compiler proof and op224's candidate.

    The control label then carries prefetch, F1 and wave0; the mtp label is the
    unchanged four-member candidate. Every non-cap compiler record must be shared.
    """
    require(type(value) is dict and set(value) == {'kind', 'compiler_report', 'legacy_compiler_pins'} and
            value['kind'] == ABLATION, 'closed single-variable ablation input')
    report = json.loads(checked_bytes(Path(value['compiler_report']['path']),
                                      value['compiler_report']['sha256'], 16 << 20))
    legacy = json.loads(checked_bytes(Path(value['legacy_compiler_pins']['path']),
                                      value['legacy_compiler_pins']['sha256'], 256 << 10))
    require(type(report) is dict and report.get('status') == 'CPU_SERVING_COMPILERS_PROVEN' and
            report.get('ablation') == ABLATION and report.get('gpu_launches') == report.get('model_starts') == 0 and
            canonical(report.get('pins')) == canonical(pins), 'native ablation compiler proof binds these pins')
    control, mtp = pins['control'], pins['mtp']
    require(set(control) == set(SHARED_RECORDS) | {'attention-stage2-cap16'} and
            set(mtp) == set(SHARED_RECORDS) | {'attention-stage2-cap32'}, 'ablation compiler record sets')
    require(all(canonical(control[key]) == canonical(mtp[key]) for key in SHARED_RECORDS),
            'ablation arms differ outside the verification cap')
    require(canonical(control['attention-stage2-cap16']) != canonical(mtp['attention-stage2-cap32']),
            'cap16 and cap32 stage2 records must differ')
    require(type(legacy) is dict and canonical(legacy.get('mtp')) == canonical(mtp),
            'cap32 arm must reproduce the executed four-member candidate records')
    serving = report.get('serving')
    require(type(serving) is dict and all(
        type(serving.get(arm+'-'+key)) is dict and serving[arm+'-'+key].get('source_path') == CANDIDATE_ATTENTION
        for arm, key in (('control', 'attention-stage1'), ('control', 'attention-stage2-cap16'),
                         ('mtp', 'attention-stage1'), ('mtp', 'attention-stage2-cap32'))),
            'both ablation arms compile attention from the prefetch source')
    return dict(kind=ABLATION, arm_members=ABLATION_MEMBERS, only_difference=['verify_cap32'],
                control_verification_cap=16, candidate_verification_cap=32,
                compiler_report_sha256=value['compiler_report']['sha256'],
                legacy_compiler_pins_sha256=value['legacy_compiler_pins']['sha256'])


def comparison_contract(value, pins):
    """Bind the three-member cap16 comparison to its native proof and op224's records.

    Every control record must equal op224's control record. Every candidate record
    except stage 2 must equal op224's candidate record; candidate stage 2 is the
    one new specialization, compiled from the prefetch source with the baseline
    cap16 specialization row. Nothing else is accepted.
    """
    require(type(value) is dict and set(value) == {'kind', 'compiler_report', 'legacy_compiler_pins'} and
            value['kind'] == COMPARISON, 'closed three-member comparison input')
    report = json.loads(checked_bytes(Path(value['compiler_report']['path']),
                                      value['compiler_report']['sha256'], 16 << 20))
    legacy = json.loads(checked_bytes(Path(value['legacy_compiler_pins']['path']),
                                      value['legacy_compiler_pins']['sha256'], 256 << 10))
    require(type(report) is dict and report.get('status') == 'CPU_SERVING_COMPILERS_PROVEN' and
            report.get('comparison') == COMPARISON and 'ablation' not in report and
            report.get('gpu_launches') == report.get('model_starts') == 0 and
            report.get('fresh_codegen_for_every_compile') is True and
            report.get('artifact_source_paths_verified') is True and
            canonical(report.get('pins')) == canonical(pins), 'native comparison compiler proof binds these pins')
    control, mtp = pins['control'], pins['mtp']
    records = set(SHARED_RECORDS) | {'attention-stage2-cap16'}
    require(set(control) == set(mtp) == records, 'comparison compiler record sets')
    require(type(legacy) is dict and set(legacy) == {'control', 'mtp'} and
            canonical(control) == canonical(legacy['control']),
            'control must reproduce the selected baseline records exactly')
    require(all(canonical(mtp[key]) == canonical(legacy['mtp'][key]) for key in SHARED_RECORDS),
            'candidate records other than cap16 stage 2 must reproduce op224')
    new = mtp['attention-stage2-cap16']
    require(canonical(new) != canonical(control['attention-stage2-cap16']) and
            canonical(new) != canonical(legacy['mtp']['attention-stage2-cap32']) and
            canonical(new['metadata']) == canonical(control['attention-stage2-cap16']['metadata']),
            'candidate cap16 stage 2 must be a distinct prefetch-source binary of the baseline specialization')
    serving = report.get('serving')
    paths = {'control-attention-stage1': NATIVE_ATTENTION, 'control-attention-stage2-cap16': NATIVE_ATTENTION,
             'mtp-attention-stage1': CANDIDATE_ATTENTION, 'mtp-attention-stage2-cap16': CANDIDATE_ATTENTION}
    require(type(serving) is dict and all(type(serving.get(key)) is dict and serving[key].get('source_path') == path
                                          for key, path in paths.items()) and
            'mtp-attention-stage2-cap32' not in serving, 'per-arm attention sources')
    fields = ('signature', 'constants', 'attrs', 'options')
    require(all(serving['mtp-attention-stage2-cap16'].get(f) == serving['control-attention-stage2-cap16'].get(f)
                for f in fields), 'candidate stage 2 must use the baseline cap16 specialization row')
    return dict(kind=COMPARISON, arm_members=COMPARISON_MEMBERS, cap32_member=False,
                control='selected baseline: native verification attention, original norm/quantizer, gate/up wave1',
                control_verification_cap=16, candidate_verification_cap=16,
                new_compiler_records=['mtp/attention-stage2-cap16'],
                unique_member_causality=False,
                compiler_report_sha256=value['compiler_report']['sha256'],
                legacy_compiler_pins_sha256=value['legacy_compiler_pins']['sha256'])


def confirmation_contract(value, comparison):
    """Admit only the closed reversed-order confirmation of the bound comparison."""
    require(comparison is not None and comparison['kind'] == COMPARISON,
            'reversed confirmation requires the three-member cap16 comparison')
    require(type(value) is dict and set(value) == {'kind', 'execution_order'} and
            value['kind'] == CONFIRMATION and type(value['execution_order']) is list and
            value['execution_order'] == CONFIRMATION_ORDER, 'closed reversed-order confirmation input')
    return dict(kind=CONFIRMATION, comparison=COMPARISON, execution_order=list(CONFIRMATION_ORDER),
                physical_owner_order=['bf16', 'kv4'], owner_arm_map=dict(CONFIRMATION_ROLE_MAP),
                arm_members=COMPARISON_MEMBERS, control_verification_cap=16, candidate_verification_cap=16,
                generated_changes=['FINAL-SETTINGS.json execution_order', 'mtp_contract.validate_paired_settings order',
                                   'benchmark_physical_controller._http_wire_reference role map'],
                protected_helper_change='benchmark_physical_controller._http_wire_reference: owner_arm_map() role',
                protected_helper_exception_approved=False, pooling_with_prior_order=False)


def reverse_order(out):
    """Reverse the logical start order through the existing settings field and its two readers."""
    raw = out['FINAL-SETTINGS.json']
    settings = json.loads(raw)
    require(canonical(settings) == raw and settings.get('phase') == 'sprint01-paired-serving' and
            settings.get('execution_order') == ['control', 'mtp'], 'selected control-first paired settings')
    settings['execution_order'] = list(CONFIRMATION_ORDER)
    out['FINAL-SETTINGS.json'] = canonical(settings)
    edit(out, 'mtp_contract.py', PAIRED_ORDER_OLD, PAIRED_ORDER_NEW)
    edit(out, 'benchmark_physical_controller.py', WIRE_ROLE_OLD, WIRE_ROLE_NEW)


def ablate_modules(out):
    """Replace arm-label member predicates with the pinned per-arm member sets."""
    edit(out, 'bundle_contract.py', '\n\ndef canonical(value):\n',
         "\n\ndef enabled(arm, member):\n"
         "    need(arm in ('control', 'mtp') and member in MEMBERS, 'bundle arm/member identity')\n"
         "    return member in CONFIG['arm_members'][arm]\n"
         '\n\ndef canonical(value):\n')
    edit(out, 'bundle_runtime.py', "    candidate = STATE.manifest['arm'] == 'mtp'\n",
         "    candidate = c.enabled(STATE.manifest['arm'], 'gate_up_wave')\n")
    edit(out, 'bundle_runtime.py', "            if state.manifest['arm'] == 'control' and not (testing and wanted):\n",
         "            if not c.enabled(state.manifest['arm'], 'gatednorm_quant') and not (testing and wanted):\n")
    edit(out, 'bundle_runtime.py', "            return result if state.manifest['arm'] == 'mtp' else None\n",
         "            return result if c.enabled(state.manifest['arm'], 'gatednorm_quant') else None\n")
    edit(out, 'bundle_runtime.py',
         "    selected = {'attention-stage1', 'attention-stage2-cap'+('32' if arm == 'mtp' else '16'),\n"
         "                'gate-up-m8-wave'+('0' if arm == 'mtp' else '1'),\n"
         "                'gate-up-m16-wave'+('0' if arm == 'mtp' else '1')}\n"
         "    if arm == 'mtp':\n",
         "    wave = '0' if c.enabled(arm, 'gate_up_wave') else '1'\n"
         "    selected = {'attention-stage1', 'attention-stage2-cap'+('32' if c.enabled(arm, 'verify_cap32') else '16'),\n"
         "                'gate-up-m8-wave'+wave, 'gate-up-m16-wave'+wave}\n"
         "    if c.enabled(arm, 'gatednorm_quant'):\n")
    edit(out, 'bundle_readout.py',
         "    arm=ident['arm']; wave='0' if arm=='mtp' else '1'\n"
         "    keys={'attention-stage1','attention-stage2-cap'+('32' if arm=='mtp' else '16'),\n"
         "          'gate-up-m8-wave'+wave,'gate-up-m16-wave'+wave}\n"
         "    if arm=='mtp':\n",
         "    arm=ident['arm']; wave='0' if c.enabled(arm,'gate_up_wave') else '1'\n"
         "    keys={'attention-stage1','attention-stage2-cap'+('32' if c.enabled(arm,'verify_cap32') else '16'),\n"
         "          'gate-up-m8-wave'+wave,'gate-up-m16-wave'+wave}\n"
         "    if c.enabled(arm,'gatednorm_quant'):\n")


def ablate_adapter(out, binder='The single-variable ablation'):
    """Select prefetch and the split cap from the pinned member sets."""
    name = 'verify_decode_adapter.py'
    edit(out, name, 'CURRENT = None\n',
         'CURRENT = None\n\n\n'
         'def member(manifest, name):\n'
         '    # '+binder+' binds members to the pinned bundle contract.\n'
         '    import bundle_contract\n'
         "    return bundle_contract.enabled(manifest['arm'], name)\n")
    edit(out, name, "                               if manifest['arm'] == 'mtp' else None)\n",
         "                               if member(manifest, 'verify_cap32') else None)\n")
    edit(out, name, "        if manifest['arm'] == 'mtp':\n            candidate = source_module(dict(\n",
         "        if member(manifest, 'verify_prefetch'):\n            candidate = source_module(dict(\n")
    edit(out, name, "            if manifest['arm'] == 'control' else candidate)\n",
         "            if not member(manifest, 'verify_prefetch') else candidate)\n")
    edit(out, name, "        if self.manifest['arm'] == 'mtp':\n            need(self.prefetch_decode is not None, 'prefetch function absent')\n",
         "        if member(self.manifest, 'verify_prefetch'):\n            need(self.prefetch_decode is not None, 'prefetch function absent')\n")
    edit(out, name, "        need(self.manifest['arm'] == 'control' and self.prefetch_decode is None,\n",
         "        need(not member(self.manifest, 'verify_prefetch') and self.prefetch_decode is None,\n")
    edit(out, name, "        return (32 if self.manifest['arm'] == 'mtp' and\n",
         "        return (32 if member(self.manifest, 'verify_cap32') and\n")


def ablate_owner(out):
    """Both ablation arms use prefetch; only the checked verification cap differs."""
    prefetch = "'ed26c67b69da358944999fdf9f769fafc4c2aef821335fbb777679b966e9e4e3'"
    native = "'34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a'"
    # Report the adapter's observed module selection rather than the arm label.
    edit(out, 'worker_entry.py', "        if manifest['arm'] == 'mtp' else "+native+")\n",
         "        if adapter.prefetch_decode is not None else "+native+")\n")
    edit(out, 'serving_integration.py',
         "          ({'8': 16, '16': 16} if binding['arm'] == 'mtp' else {'8': 0, '16': 0}),\n"
         "          'candidate graph route differs or used in control')\n",
         "          {'8': 16, '16': 16}, 'prefetch graph route absent from an ablation arm')\n")
    edit(out, 'serving_integration.py',
         "    expected_kernel = ("+prefetch+" if binding['arm'] == 'mtp'\n"
         "                       else "+native+")\n",
         "    expected_kernel = "+prefetch+"\n")
    edit(out, 'serving_integration.py',
         "    _need((all(v >= 16 for v in calls.values()) and sum(calls.values()) == record['eligible_host_calls'])\n"
         "          if binding['arm'] == 'mtp' else all(v == 0 for v in calls.values()),\n"
         "          'prefetch absent from an eligible verification graph or used in control')\n",
         "    _need(all(v >= 16 for v in calls.values()) and sum(calls.values()) == record['eligible_host_calls'],\n"
         "          'prefetch absent from an eligible verification graph')\n")


def compare_owner(out):
    """Candidate-only prefetch, both arms at cap16; witnesses report observed selection.

    The legacy route predicates (prefetch only in mtp, native kernel in control)
    already describe this comparison and stay byte-identical.
    """
    native = "'34ca0066e2716e1ffad048ec7cbb3a25af13eeb61e99034a663fed6277bf691a'"
    edit(out, 'worker_entry.py', "        if manifest['arm'] == 'mtp' else "+native+")\n",
         "        if adapter.prefetch_decode is not None else "+native+")\n")
    edit(out, 'serving_integration.py',
         "    _need(record['verification_caps'] == ([32] if binding['arm'] == 'mtp' else [16]) and\n",
         "    _need(record['verification_caps'] == [16] and\n")


def apply(out, menu, inputs, donor):
    config = inputs['four_member']
    base = {'files', 'compiler_pins', 'probe_nonce', 'scheduler_pins'}
    require(set(config) in (base, base | {'ablation'}, base | {'comparison'}, base | {'comparison', 'confirmation'}) and
            set(config['files']) == set(FIXED) and inputs['mamba_max_states_per_path'] == 3 and
            inputs['kind'] == 'serving', 'closed four-member build inputs')
    raw = {key: checked_bytes(Path(row['path']), FIXED[key], 4 << 20)
           for key, row in config['files'].items()}
    require(all(config['files'][key]['sha256'] == FIXED[key] for key in FIXED), 'fixed bundle source pins')
    compiled_raw = checked_bytes(Path(config['compiler_pins']['path']),
                                 config['compiler_pins']['sha256'], 256 << 10)
    compiler_pins = json.loads(compiled_raw)
    require(set(compiler_pins) == {'control', 'mtp'} and
            all(type(row) is dict and row for row in compiler_pins.values()), 'both compiler arm contracts')
    ablation = ablation_contract(config['ablation'], compiler_pins) if 'ablation' in config else None
    comparison = comparison_contract(config['comparison'], compiler_pins) if 'comparison' in config else None
    confirmation = confirmation_contract(config['confirmation'], comparison) if 'confirmation' in config else None
    probes = bundle_client.derive(menu, config['probe_nonce'])
    probe_raw = canonical(probes)
    require(len(probe_raw) <= 4 << 20, 'bounded frozen unscored probe bodies')
    model_config = json.loads(raw['config'])
    gdn_ids, full_ids = bundle_client.c.layer_ids(model_config)
    bound = dict(members=list(bundle_client.c.MEMBERS), gdn_ids=gdn_ids, full_ids=full_ids,
        probe_ids={p['body']['rid']: p['kind'] for p in probes}, probes_sha256=digest(probe_raw),
        compiler_pins_sha256=digest(compiled_raw), compiler_records=compiler_pins)
    if ablation is not None:
        bound.update(ablation=ABLATION, arm_members=ABLATION_MEMBERS)
    if comparison is not None:
        bound.update(comparison=COMPARISON, arm_members=COMPARISON_MEMBERS)
    source = Path(__file__).resolve().parent
    for name in MODULES:
        data = (source/(name+'.py')).read_bytes()
        if name == 'bundle_contract':
            require(data.count(b'CONFIG = None') == 1, 'one bundle contract substitution')
            data = data.replace(b'CONFIG = None', ('CONFIG = '+repr(bound)).encode())
        out[name+'.py'] = data
    if ablation is not None or comparison is not None:
        ablate_modules(out)
    out['BUNDLE-COMPILER-PINS.json'], out['BUNDLE-PROBES.json'] = compiled_raw, probe_raw
    out['bundle_host.py'], out['opt04_gatednorm_quant.py'] = raw['f1_host'], raw['f1_fused']
    entry = (source/'bundle_entry.py').read_bytes()
    require(entry.count(b'MODULE_PINS = None') == 1, 'one bundle loader substitution')
    entry = entry.replace(b'MODULE_PINS = None', ('MODULE_PINS = '+repr(
        {name: digest(out[name+'.py']) for name in MODULES})).encode())
    out['bundle_entry.py'] = entry
    # verify_serving.apply has already inserted the reviewed prefetch-only route.
    out['verify_decode_adapter.py'] = combined_serving_surfaces.derive(out['verify_decode_adapter.py'], raw['backend'])
    edit(out,'verify_decode_adapter.py','        self.buffers = {}\n',
         '        self.bundle_caps = set()\n        self.native_decode_caps = set()\n        self.buffers = {}\n')
    edit(out,'verify_decode_adapter.py','        cap = self.verification_cap(backend)\n',
         '        cap = self.verification_cap(backend)\n'
         '        self.bundle_caps.add(cap)\n        self.native_decode_caps.add(backend.max_kv_splits)\n')
    edit(out, 'verify_decode_adapter.py', '        self.buffers = {}\n',
         '        import bundle_runtime\n'
         "        audited_source = (source_module(dict(path=manifest['sources']['decode_attention_fwd']['path'],\n"
         "            module=manifest['sources']['decode_attention_fwd']['module'],\n"
         "            sha256=manifest['sources']['decode_attention_fwd']['sha256']))\n"
         "            if manifest['arm'] == 'control' else candidate)\n"
         '        self.bundle_attention = bundle_runtime.attention_function(audited_source)\n'
         '        self.buffers = {}\n')
    # Both views preserve the original host function body and JIT functions.
    edit(out, 'verify_decode_adapter.py', '            return self.prefetch_decode(q, *args, **kwargs)\n',
         '            return self.bundle_attention(q, *args, **kwargs)\n')
    edit(out, 'verify_decode_adapter.py', '        return backend.decode_attention_fwd(q, *args, **kwargs)\n',
         '        return self.bundle_attention(q, *args, **kwargs)\n')
    if ablation is not None:
        ablate_adapter(out)
    if comparison is not None:
        ablate_adapter(out, 'The three-member cap16 comparison')
    out['ops.py'] = derive_ops(out['ops.py'])
    out['isolated_scheme.py'] = raw['f1_scheme']
    # Native model and runner instrumentation is identical in both arms. Only
    # the explicitly attached candidate Route returns a fused result.
    require(digest(out['qwen3_5_candidate.py']) == '2a76ddea9a380dd8e8fd71a8919fc4aadae702ec5c5f35bd052c614ed07a78e8',
            'selected existing GDN model overlay')
    out['qwen3_5_candidate.py'] = raw['f1_model']
    runner = {'runner.py': raw['runner']}
    edit(runner, 'runner.py', '            logger.info("Packed MXFP4 GDN input projection enabled for %d layers", len(gdn_layers))\n',
         '            logger.info("Packed MXFP4 GDN input projection enabled for %d layers", len(gdn_layers))\n'
         '            from bundle_entry import attach_runner as attach_bundle_runner\n'
         '            attach_bundle_runner(self)\n')
    require(digest(out['d3b_combined_model_runner.py']) == FIXED['runner'], 'selected existing target runner overlay')
    out['d3b_combined_model_runner.py'] = runner['runner.py']
    edit(out,'mtp_contract.py',repr(FIXED['runner']),repr(digest(out['d3b_combined_model_runner.py'])))
    edit(out,'mtp_contract.py',repr('2a76ddea9a380dd8e8fd71a8919fc4aadae702ec5c5f35bd052c614ed07a78e8'),
         repr(digest(out['qwen3_5_candidate.py'])),count=2)
    edit(out, 'mtp_contract.py', " 'mxfp4_ops': ('ops.py',\n               'mxfp4_component.ops',\n               '66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776')",
         " 'mxfp4_ops': ('ops.py',\n               'mxfp4_component.ops',\n               "+repr(digest(out['ops.py']))+')')
    edit(out, 'mtp_contract.py', "'f396186d36fa83812d7d3d4ec538e47588d3513a06e9996539e1481bebe8c185'",
         repr(digest(out['isolated_scheme.py'])))
    for arm in ('control', 'mtp'):
        name='HTTP-BINDING-'+arm+'.json'; binding=json.loads(out[name]); sources=binding['binding_template']['sources']
        for role, filename in (('head_model','qwen3_5_candidate.py'), ('model_runner','d3b_combined_model_runner.py'),
                               ('mxfp4_ops','ops.py'), ('mxfp4_scheme','isolated_scheme.py')):
            sources[role]['sha256']=digest(out[filename])
        for role, filename, module in (('bundle_entry','bundle_entry.py','bundle_entry'),
                                     ('bundle_host','bundle_host.py','bundle_host'),
                                     ('bundle_fused','opt04_gatednorm_quant.py','opt04_candidate')):
            sources[role]=dict(path='/mnt/review/'+filename, module=module, sha256=digest(out[filename]))
        sources['bundle_norm']=dict(path='/mnt/composition/image-source/python/sglang/kernels/ops/attention/fla/layernorm_gated.py',
            module='sglang.kernels.ops.attention.fla.layernorm_gated', sha256='27f1c3d591b1a8cf393d5665d5418f830b46a64897be338ecfbf262cba18a2ad')
        sources['bundle_allocator']=dict(path='/mnt/composition/image-source/python/sglang/srt/distributed/device_communicators/pynccl_allocator.py',
            module='sglang.srt.distributed.device_communicators.pynccl_allocator',
            sha256='cec2f1df527cc2c5569bcd555734bc1b97f7ce2280c33b099e1a6b5accb93c22')
        require(set(config['scheduler_pins'])=={'stage','nvtx'}, 'scheduler decorator pins')
        for key, relative in (('stage','observability/scheduler_stage_metrics.py'),('nvtx','utils/nvtx_utils.py')):
            sources['bundle_'+key]=dict(path='/mnt/composition/image-source/python/sglang/srt/'+relative,
                module='sglang.srt.'+relative[:-3].replace('/','.'),sha256=config['scheduler_pins'][key])
        out[name]=canonical(binding)
    worker = out['worker_entry.py'].decode()
    begin = worker.index('        if not instrumented:\n')
    end = worker.index('    finally:\n        primary = sys.exc_info()[1]',begin)
    block = worker[begin:end]
    require(block.count('        return target(\n') == 1, 'one original target invocation')
    edit(out,'worker_entry.py',block,
         "        bundle = _source_module(manifest['sources']['bundle_entry'])\n"
         '        with bundle.worker(manifest, record, target, gpu_id, _native_module, _source_module, _MXFP4_SCHEME):\n'
         + textwrap.indent(block,'    '))
    edit(out, 'worker_entry.py', "    proof['indices_elements'] = sorted",
         "    proof['verification_caps'] = sorted(adapter.bundle_caps)\n"
         "    proof['native_decode_caps'] = sorted(adapter.native_decode_caps)\n"
         "    _source_module(manifest['sources']['bundle_entry']).startup()\n"
         "    proof['indices_elements'] = sorted")
    edit(out,'serving_integration.py',"'bf16_capture_calls', 'prefetch_capture_calls', 'start_ticks', 'boot_id'))",
         "'bf16_capture_calls', 'prefetch_capture_calls', 'start_ticks', 'boot_id',\n"
         "                   'verification_caps', 'native_decode_caps'))")
    edit(out,'serving_integration.py',"    _need(all(record[k] == worker[k] for k in ('start_ticks', 'boot_id')), 'route worker lifecycle')\n",
         "    _need(record['verification_caps'] == ([32] if binding['arm'] == 'mtp' else [16]) and\n"
         "          record['native_decode_caps'] == [16], 'private verification and unchanged ordinary decode caps')\n"
         "    _need(all(record[k] == worker[k] for k in ('start_ticks', 'boot_id')), 'route worker lifecycle')\n")
    loader = '''\n\ndef _bundle_entry():
    import importlib.util
    path = Path(__file__).with_name('bundle_entry.py')
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != ENTRY_PIN:
        raise ValueError('bundle entry source identity')
    spec = importlib.util.spec_from_file_location('bound_bundle_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
'''.replace('ENTRY_PIN',repr(digest(entry)))
    for name in ('serving_runtime.py','serving_integration.py'):
        out[name] += loader.encode()
    edit(out, 'serving_runtime.py', '        client_deadline = min(deadline, time.monotonic() + mtp_contract.phase_config()[1])\n',
         '        client_deadline = min(deadline, time.monotonic() + mtp_contract.phase_config()[1])\n'
         "        bundle = _bundle_entry(); bundle.modules()\n"
         '        import bundle_readout\n'
         '        bundle_readout.ready(material, serving_client.validate_gate(gate, expected))\n'
         '        import bundle_client\n'
         '        bundle_client.run(material, port, client_deadline, serving_client.validate_gate(gate, expected))\n')
    if ablation is not None:
        ablate_owner(out)
    if comparison is not None:
        compare_owner(out)
    if confirmation is not None:
        reverse_order(out)
    _reader(out)
    # Explicitly charge the bounded new receipts, including their encoded tree,
    # within the existing 512 MiB session cap. The stream allowance still covers
    # base64 expansion of all maximum-size member files and fixed framing.
    edit(out,'benchmark_artifacts.py',"'native-http-bundle.json': 6 << 20}","'native-http-bundle.json': 7 << 20}")
    edit(out,'benchmark_artifacts.py','HTTP_STREAM_LIMIT = 64 << 20','HTTP_STREAM_LIMIT = 65 << 20')
    edit(out,'benchmark_artifacts.py',"native = _http_tree(output/'native', 4 << 20, files=256)",
         "native = _http_tree(output/'native', 5 << 20, files=256)")
    edit(out,'benchmark_artifacts.py','(24 << 20) + (4 << 20) + (1 << 20)) +',
         '(24 << 20) + (5 << 20) + (1 << 20)) +')
    # The receiver's line bound must cover the raised encoded bound. Derive it
    # from the same member limits instead of keeping the donor's 63 MiB datum.
    edit(out,'benchmark_artifacts.py',"    if binding.get('experiment_mode') == HTTP_MODE:\n        return 63 << 20\n",
         "    if binding.get('experiment_mode') == HTTP_MODE:\n        return encoded_limit(binding)\n")
    bounds = export_bounds(out['benchmark_artifacts.py'])
    added = [n+'.py' for n in MODULES]+['bundle_entry.py','bundle_host.py','opt04_gatednorm_quant.py',
        'BUNDLE-COMPILER-PINS.json','BUNDLE-PROBES.json']
    edit(out,'benchmark_physical_controller.py',"LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')",
         'LIVE_PACKAGE_NAMES.update('+repr(sorted('review/'+name for name in added))+')\n'
         "LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')")
    edit(out,'benchmark_host_transport.py','        return 81\n','        return '+str(81+len(added))+'\n')
    edit(out,'benchmark_host_transport.py','len(members) < 81','len(members) < '+str(81+len(added)))
    for old,new,count in (
        ('66d25968516c6c9509923ad363a291d57d192918f1e7121f96481202391c3776',digest(out['ops.py']),1),
        ('f396186d36fa83812d7d3d4ec538e47588d3513a06e9996539e1481bebe8c185',digest(out['isolated_scheme.py']),1),
        ('2a76ddea9a380dd8e8fd71a8919fc4aadae702ec5c5f35bd052c614ed07a78e8',digest(out['qwen3_5_candidate.py']),2),
        (FIXED['runner'],digest(out['d3b_combined_model_runner.py']),2)):
        edit(out,'benchmark_host_transport.py',repr(old),repr(new),count=count)
    edit(out,'benchmark_owner_adapter.py',"                before_toolchain={'ROCM_HOME':",
         "                llvm=[i for i in range(len(projected)-2) if projected[i:i+2]==['--setenv','LLVM_PATH']]\n"
         "                _need(len(llvm)<=1, 'one source-bound LLVM_PATH')\n"
         "                if llvm:\n"
         "                    _need(projected[llvm[0]+2]=='/usr/local/lib/python3.14/dist-packages/_rocm_sdk_core/lib/llvm', 'source-bound LLVM_PATH')\n"
         "                else:\n"
         "                    projected[position:position]=['--setenv','LLVM_PATH','/usr/local/lib/python3.14/dist-packages/_rocm_sdk_core/lib/llvm']\n"
         "                before_toolchain={'ROCM_HOME':")
    for name, data in out.items():
        if name.endswith('.py'): compile(data,name,'exec',dont_inherit=True)
    result = dict(members=list(bundle_client.c.MEMBERS), source_family=FAMILY, model_starts=2,
        auxiliary_requests_per_start=3, benchmark_menu_requests_per_start=30,
        scored_requests_per_start=27, ordinary_decode_cap=16,
        candidate_verification_cap=32, kv_capacity_unchanged=True, selected=False,
        gpu_qualified=False, adoption_authorized=False, no_shadow_in_scored_graphs=True,
        additional_package_members=added, native_tree_bytes=5<<20, encoded_native_bytes=7<<20,
        session_max_bytes=529006592, http_export_bounds=bounds)
    if ablation is not None:
        result.update(ablation=ablation)
    if comparison is not None:
        result.update(comparison=comparison, candidate_verification_cap=16)
    if confirmation is not None:
        result.update(confirmation=confirmation)
    return result


def export_bounds(source):
    """Refuse an HTTP export chain that the generated stdlib module cannot satisfy.

    export_bundle requires len(line) <= encoded_limit <= bundle_limit. The
    receiver applies bundle_limit to the same line inside one retained stdout
    stream, which must also hold the two bounded resource records.
    """
    space = {'__name__': '_four_member_export_bounds'}
    exec(compile(source, 'benchmark_artifacts.py', 'exec', dont_inherit=True), space)
    mode = space['HTTP_MODE']
    row = dict(encoded_line_bytes=space['encoded_limit']({'experiment_mode': mode}),
               receiver_line_bytes=space['bundle_limit']({'experiment_mode': mode}),
               stream_bytes=space['stream_limit'](mode), resource_record_bytes=2 * RESOURCE_RECORD_BYTES,
               session_max_bytes=space['HTTP_SESSION_MAX_BYTES'])
    require(row['encoded_line_bytes'] <= row['receiver_line_bytes'] and
            row['receiver_line_bytes'] + row['resource_record_bytes'] <= row['stream_bytes'] == 65 << 20 and
            row['session_max_bytes'] == 529006592 < 512 << 20, 'coherent HTTP export bound chain')
    return row


def _reader(out):
    """Replay both source-bound worker receipts and the unscored HTTP bytes."""
    names={f'BUNDLE-{kind}-rank-{rank}.json' for kind in ('STARTUP','NUMERICS') for rank in (0,1)}|{'BUNDLE-HTTP.json'}
    edit(out,'serving_integration.py',
         "    'WORKER-MANIFEST.json', 'FULL-ENABLED-rank-0.json', 'FULL-ENABLED-rank-1.json'})",
         "    'WORKER-MANIFEST.json', 'FULL-ENABLED-rank-0.json', 'FULL-ENABLED-rank-1.json'} | frozenset("+
         repr(tuple(sorted(names)))+'))')
    # Both this readiness-time check and offline readout consume the same decoder.
    code = out['bundle_readout.py']
    # Included explicitly rather than relying on a plain import from sys.path.
    for arm in ('control','mtp'):
        name='HTTP-BINDING-'+arm+'.json'; binding=json.loads(out[name]); binding['binding_template']['sources']['bundle_readout']=dict(
            path='/mnt/review/bundle_readout.py',module='bundle_readout',sha256=digest(code))
        out[name]=canonical(binding)
    edit(out,'serving_integration.py','    wire = reader.read_client(client_root, expected=expected, native_gate_raw=gate_raw)\n    frozen = reader.contract.requests()',
         '    bundle = _bundle_entry(); bundle.modules()\n'
         '    from bundle_readout import read\n'
         '    read(observations, binding, full_workers)\n'
         '    wire = reader.read_client(client_root, expected=expected, native_gate_raw=gate_raw)\n    frozen = reader.contract.requests()')
