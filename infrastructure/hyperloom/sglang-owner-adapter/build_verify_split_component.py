"""Derive the one frozen verification cap16/cap32 component; no GPU invocation.

Source and owner checks use the established finite CPU-only native gate.
Finalization builds an inert owner packet. Registration and execution are absent.
"""
import argparse
import ast
import difflib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_component as base
import compile_component
import owner_operation as op
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, require

N = op.R2 / 'hyperloom-sglang-implementation-20261102-v1/optimization-continuation-v1'
SOURCE = N / 'verify-split-component-preparation-v1/author-v3'
SOURCE_SEAL = '7edeac175d647bfe9e98f9adc97f9623ed4f8d5261855522427dd1a87b32e9c0'
COMPILE = N / 'verify-split-component-preparation-v1/native-loader-proof-v1'
COMPILE_SEAL = 'e3b6eaf29ebbe877568caacf21cfdcd05d21908ea12834effc16b976831b078a'
DONOR = op.R2 / 'phase2-attn-c4-accumulator-preparation-v2'
MODE = 'isolated-gfx1201-verify-split32-component-v1'
PROVENANCE = 'seeded-bf16-grouped-verification-cap16-cap32-two-cards-v1'
TESTS = 31
OLD_BOOT = 'ff396f8f-e5c3-4ae5-86c1-b986bc483185'
BOOT = 'cf30344d-a1d7-4350-825b-e097c8e60f2b'
INFRA = base.INFRA
OLD_KERNELS = ('extend_attention_fwd_baseline.py', 'extend_attention_fwd_candidate.py')
KERNELS = ('decode_attention_baseline.py', 'decode_attention_candidate.py')
read, put, write, replace = base.read, base.put, base.write, base.replace


def assignment(path, name, value):
    if not isinstance(value, set):
        return base.assignment(path, name, value)
    # Stable generated source bytes across hash seeds and native/local Python.
    lines = path.read_text().splitlines(keepends=True)
    nodes = [n for n in ast.parse(''.join(lines)).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    require(len(nodes) == 1 and value and all(type(v) is str for v in value), 'closed string set assignment')
    node = nodes[0]
    lines[node.lineno - 1:node.end_lineno] = [name + ' = {' + ', '.join(repr(v) for v in sorted(value)) + '}\n']
    path.write_text(''.join(lines))


def authenticated_inputs():
    seal = op.sealer()
    seal.verify(SOURCE, SOURCE_SEAL)
    seal.verify(COMPILE, COMPILE_SEAL)
    base.verify_donor(DONOR)
    result = read(COMPILE / 'COMPILE-RESULT.json')
    require(result['status'] == 'CPU_SPLIT_COMPILED'
            and result['gpu_launches'] == result['model_starts'] == 0
            and result['device_nodes'] is False, 'device-free native loader proof')
    require(all(result.get(k) is True for k in ('all_five_artifacts_reproduced',
            'all_baselines_reproduced_before_candidates', 'shared_stage2_jit_object',
            'old_leak_rejected_by_unchanged_guard', 'selected_guard_restored_after_compile',
            'selected_guard_restored_after_exception')), 'native loader identity and lifetime proof')
    contract = op._source(SOURCE / 'contract.py', digest((SOURCE / 'contract.py').read_bytes()), '_split_frozen_contract')
    require(contract.MODE == MODE and contract.PROVENANCE == PROVENANCE
            and contract.CAPS == {'B': 16, 'C': 32}, 'frozen component identity')
    proof = read(COMPILE / 'INPUTS.json')
    require(proof['source_root'] == str(SOURCE) and proof['source_seal'] == SOURCE_SEAL,
            'proof bound to the exact harness')
    for name, pin in proof['harness_runtime_pins'].items():
        checked_bytes(SOURCE / (name + '.py'), pin)
    require(set(result['cases']) == {c['name'] for c in contract.CASES}, 'every frozen case compiled')
    for case, arms in result['cases'].items():
        require(set(arms) == {'B', 'C'}, 'both compiled arms')
        for arm, stages in arms.items():
            for stage, pins in (('stage1', contract.COMPILER_PINS[arm]), ('stage2', contract.STAGE2_PINS[arm])):
                for kind, artifact in stages[stage]['artifacts'].items():
                    require(artifact['sha256'] == proof['expected'][case][arm][stage][kind],
                            'all five artifacts match original CPU screen')
                    checked_bytes(COMPILE / artifact['path'], artifact['sha256'])
                require(all(stages[stage]['artifacts'][part]['sha256'] == pin for part, pin in pins.items()),
                        'live compiler pins equal the actual-loader compile')
    require((SOURCE / KERNELS[0]).read_bytes() == (SOURCE / KERNELS[1]).read_bytes(),
            'configuration experiment keeps source identical')
    return contract


def source(destination):
    root = op._fresh(destination, 'verification component packet')
    require(root.is_relative_to(INFRA), 'native gate paths within private evidence workspace')
    contract = authenticated_inputs()
    root.mkdir(mode=0o700)
    write(root / 'INPUTS.json', dict(source_root=str(SOURCE), source_seal=SOURCE_SEAL,
          compile_root=str(COMPILE), compile_seal=COMPILE_SEAL, donor_root=str(DONOR), tests=TESTS))
    out = root / 'source'
    pins = read(SOURCE / 'RUNTIME-SOURCE-PINS.json')
    for name in (*pins, 'RUNTIME-SOURCE-PINS.json', 'fake_payload.py', 'test_payload.py', 'test_reference.py',
                 'test_loader_scope.py', 'test_selected_imports_fixture.py'):
        put(out / name, (SOURCE / name).read_bytes())
    put(out / 'native_source_gate.py', (DONOR / 'source/native_source_gate.py').read_bytes())
    assignment(out / 'native_source_gate.py', 'TESTS', TESTS)
    files = {p.name: dict(bytes=p.stat().st_size, sha256=digest(p.read_bytes())) for p in sorted(out.iterdir())}
    write(out / 'NATIVE-SOURCE-INPUTS.json', dict(schema=1, expected_tests=TESTS,
          compiled_modules=sum(n.endswith('.py') for n in files), files=files))
    native = root / 'native-component'
    raw = (DONOR / 'native-component-v2/prepare.py').read_text()
    require(raw.count('ROOT.parents[4]') == 1, 'native source preparation root anchor')
    depth = len(native.relative_to(INFRA).parts) - 1
    require(raw.count(OLD_BOOT) == 1, 'donor source-check boot anchor')
    raw = raw.replace('ROOT.parents[4]', 'ROOT.parents[' + str(depth) + ']').replace(OLD_BOOT, BOOT)
    put(native / 'prepare.py', raw.encode())
    write(root / 'SOURCE-PREPARATION.json', dict(status='INERT_SOURCE_REQUIRES_NATIVE_CHECK', tests=TESTS,
          cases=contract.CASES, compiler_pins=contract.COMPILER_PINS, per_arm_stage2_pins=contract.STAGE2_PINS,
          gpu_work=False, registration=False, independent_review_complete=False))
    return read(root / 'SOURCE-PREPARATION.json')


def local_run(root, script, args, label, seconds=40):
    receipts = root / 'local-checks'
    receipts.mkdir(exist_ok=True)
    transport = read(COMPILE / 'INPUTS.json')
    commands = op.commands(transport['commands'], receipts)
    return commands.run(label, [op.PYTHON, '-I', '-B', '-S', str(script), *args], seconds,
                        cwd=script.parent, output_limit=2 << 20)


def verify_native_source(root):
    out, native = root / 'source', root / 'native-component'
    metadata = read(native / 'INPUTS.json')
    result = read(native / 'native-source-check.stdout')
    receipt = read(native / 'native-source-check.json')
    require(result['status'] == 'PASS' and result['tests'] == metadata['expected_tests'] == TESTS
            and result['compiled_modules'] == metadata['expected_modules']
            and result['errors'] == result['failures'] == result['skips'] == 0
            and result['python'] == '3.14.4' and result['machine'] == 'x86_64'
            and result['native_model_or_device_operations'] is False
            and result['input_inventory_sha256'] == metadata['input_inventory_sha256'], 'actual native source gate')
    checked_bytes(out / 'NATIVE-SOURCE-INPUTS.json', metadata['input_inventory_sha256'])
    require(receipt['exit_code'] == 0 and receipt['child_reaped'] is receipt['owned_group_absent'] is True
            and receipt['stdin_sha256'] == metadata['recipe_sha256'] and receipt['work_seconds'] == 45
            and not receipt.get('failure') and not receipt.get('signals'), 'clean finite source receipt')
    for name in ('stdout', 'stderr'):
        raw = checked_bytes(native / ('native-source-check.' + name), receipt[name]['sha256'], 2 << 20)
        require(len(raw) == receipt[name]['bytes'], 'native source stream length')
    for name, row in read(out / 'NATIVE-SOURCE-INPUTS.json')['files'].items():
        require(len(checked_bytes(out / name, row['sha256'])) == row['bytes'], 'native-tested source unchanged')
    return result


def native_source(root):
    native = root / 'native-component'
    local_run(root, native / 'prepare.py', [], 'prepare-native-component')
    metadata = read(native / 'INPUTS.json')
    require(metadata['expected_tests'] == TESTS and metadata['work_seconds'] == 45, 'fixed source gate')
    transport = read(COMPILE / 'INPUTS.json')
    connection = op._source(Path(transport['connection']), compile_component.PINS['connection'], '_verify_source_connection')
    commands = op.commands(transport['commands'], native)
    recipe = checked_bytes(native / 'RECIPE.py', metadata['recipe_sha256'], 12 << 20)
    require(not (native / 'INVOKED.json').exists(), 'single native source check')
    connection.check()
    write(native / 'INVOKED.json', dict(recipe_sha256=metadata['recipe_sha256'], gpu_work=False))
    commands.run('native-source-check', connection.SSH + [base.BASE_PYTHON, '-I', '-B', '-S', '-'],
                 45, stdin=recipe, cwd=native, output_limit=2 << 20)
    return verify_native_source(root)


def owner(root):
    source, native, out = root / 'source', root / 'native-component', root / 'owner'
    require(not out.exists(), 'fresh owner')
    result = verify_native_source(root)
    contract = authenticated_inputs()
    excluded = ('native-preparation', 'NATIVE-SOURCE-GATE-INPUTS.json', 'MATERIALIZATION.json',
                'component/PACKAGE.json', 'component/PACKAGE-IDENTITY.json', 'driver/OPERATOR-CONTEXT.json',
                'fake_source.py', 'test_component.py',
                *('component/' + n for n in OLD_KERNELS),
                *('component/ABC-OWNER-SOURCE-' + n + '.json' for n in ('INVENTORY', 'RESULT', 'CALLER')))
    base.copy_files(DONOR / 'owner', out, exclude=excluded)
    pins = read(source / 'RUNTIME-SOURCE-PINS.json')
    for name in (*pins, 'RUNTIME-SOURCE-PINS.json'):
        (out / 'component' / name).write_bytes((source / name).read_bytes())
    (out / 'fake_payload.py').write_bytes((source / 'fake_payload.py').read_bytes())
    replace(out / 'fixture_component.py', 'value = fake_payload.run(root)', 'value, _observations = fake_payload.run(root)')
    modified = ('component/common.py', 'component/run_experiment.py', 'component/root_observe.py',
                'driver/operator_driver.py', 'driver/driver_contract.py', 'test_owner.py')
    for name in modified:
        p = out / name
        p.write_text(p.read_text().replace(base.OLD_MODE, MODE).replace(base.OLD_PROVENANCE, PROVENANCE)
            .replace('C4-ACCUMULATOR', 'VERIFY-SPLIT32')
            .replace('REPEATED_C4_BITWISE_ACCUMULATOR_FP32_REFERENCE_BF16_TOLERANCE',
                     'REPEATED_CAP16_BITWISE_ALL16_FP32_REFERENCE_MEAN102_BF16_TOLERANCE'))
    common = out / 'component/common.py'
    descriptor = compile_component._literal(ast.parse(common.read_bytes()), 'COMPONENT')
    descriptor.update(cases=4, timed_cases=4)
    assignment(common, 'COMPONENT', descriptor)
    names = compile_component._literal(ast.parse(common.read_bytes()), 'REQUIRED_SOURCES')
    names.difference_update(OLD_KERNELS)
    names.update(KERNELS + ('source_loader.py', 'metadata.py', 'LAUNCHES.json'))
    assignment(common, 'REQUIRED_SOURCES', names)
    assignment(out / 'component/run_experiment.py', 'EXPECTED_SOURCES', names)
    replace(out / 'component/run_experiment.py', 'Current C4 versus ACCUMULATOR', 'Selected verification attention cap16 versus cap32')
    replace(out / 'driver/operator_driver.py', "'cases': 3", "'cases': 4")
    replace(out / 'driver/operator_driver.py', "'timed_cases': 2", "'timed_cases': 4")
    replace(out / 'component/component_entry.py',
            'compiler gate and3 correctness cases before6 alternating AB/BA rounds on2 cases; current C4 versus ACCUMULATOR',
            'compiler gate and4 correctness cases before6 alternating AB/BA rounds on4 cases; selected verification attention cap16 versus cap32')
    replace(out / 'component/component_entry.py', 'accumulator_live_reader', 'verify_split32_live_reader')
    replace(out / 'component/mx_readout.py', 'serving prefill time', 'serving latency')
    export = out / 'driver/sealed_export.py'
    replace(export, "'extend_attention_fwd_baseline': 'extend_attention_fwd_baseline.py', 'extend_attention_fwd_candidate': 'extend_attention_fwd_candidate.py',",
            "'source_loader': 'source_loader.py', 'decode_attention_baseline': 'decode_attention_baseline.py', 'decode_attention_candidate': 'decode_attention_candidate.py',")
    # Source-loader imports are stdlib only; the offline reader never invokes its JIT functions.
    text = export.read_text()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'STDLIB' for t in n.targets))
    allowed = ast.literal_eval(node.value); allowed.add('linecache')
    assignment(export, 'STDLIB', allowed)
    replace(out / 'test_owner.py', "len(value['sources']),40", "len(value['sources']),43")
    replace(out / 'test_owner.py', "('cases',4)", "('cases',5)")
    replace(out / 'test_owner.py', "package['component']['cases']=4", "package['component']['cases']=5")
    depth = len(out.relative_to(INFRA).parts) - 1
    for name in ('native_source_gate.py', 'run_native_source_checks.py', 'prepare_packet.py'):
        replace(out / name, 'parents[4]', 'parents[' + str(depth) + ']')
    replace(out / 'run_native_source_checks.py', OLD_BOOT, BOOT)
    template = out / 'component/MX-COMPOSE-TEMPLATE.json'
    before_template = digest(template.read_bytes())
    replace(template, OLD_BOOT, BOOT)
    replace(out / 'component/run_experiment.py', before_template, digest(template.read_bytes()))
    replace(out / 'prepare_packet.py', 'Exact frozen extend-attention component sources', 'Exact frozen verification-attention component sources')
    proof = [(source / 'NATIVE-SOURCE-INPUTS.json').read_bytes(),
             (native / 'native-source-check.stdout').read_bytes(), (native / 'native-source-check.json').read_bytes()]
    for part, raw in zip(('INVENTORY', 'RESULT', 'CALLER'), proof):
        (out / 'component' / ('ABC-COMPONENT-SOURCE-' + part + '.json')).write_bytes(raw)
    (out / 'component/assets/ABC-NATIVE-SOURCE-SUMMARY.json').write_bytes(canonical(dict(result=result,
        receipt=dict(sha256=digest(proof[2])), source_inventory=dict(sha256=digest(proof[0])))))
    manifest = read(out / 'component/OPERAND-MANIFEST.json')
    manifest.update(cases=4, chunk=contract.CHUNK, case_menu=contract.CASES)
    (out / 'component/OPERAND-MANIFEST.json').write_bytes(canonical(manifest))
    binding = read(out / 'component/ASSET-BINDING.json')
    binding.update(operand_manifest_sha256=digest(canonical(manifest)), source_pins={'attention_ops.py': pins['attention_ops.py']})
    (out / 'component/ASSET-BINDING.json').write_bytes(canonical(binding))
    config = read(out / 'component/ABC-OWNER-CONFIG.json')
    config.update(capture_provenance=PROVENANCE, component=descriptor, component_sources=pins,
                  component_native_expected_tests=TESTS, component_native_expected_modules=result['compiled_modules'],
                  payload_sha256=pins['stage_probe.py'], reader_sha256=pins['read_component.py'])
    native_names = set(config['owner_native_gate_sources']) - set(OLD_KERNELS)
    native_names.update(KERNELS + ('source_loader.py', 'metadata.py', 'LAUNCHES.json'))
    config['owner_native_gate_sources'] = {n: (out / 'component' / n).relative_to(INFRA).as_posix() for n in sorted(native_names)}
    config['owner_native_expected_modules'] = len(list(out.glob('*.py'))) + sum(len(list((out / n).rglob('*.py'))) for n in ('component', 'driver', 'fixtures'))
    (out / 'component/ABC-OWNER-CONFIG.json').write_bytes(canonical(config))
    (out / 'ADDITIONAL-SOURCE-BINDINGS.json').write_bytes(canonical(dict(schema=1, component_root=str(source),
        component_native_root=str(native), component_inventory_sha256=digest(proof[0]), source_pins=pins,
        gpu_work=False, model_starts=0, protected_helpers_changed=False)))
    protected = read(out / 'PROTECTED-UNCHANGED.json')['source_pins']
    require(len(protected) == 13, 'exact protected helper closure')
    for name, pin in protected.items(): checked_bytes(out / 'component' / name, pin)
    for p in out.rglob('*.py'): compile(p.read_bytes(), str(p), 'exec', dont_inherit=True)
    return dict(status='INERT_OWNER_REQUIRES_NATIVE_CHECK', protected_helpers_unchanged=13,
                source_tests=TESTS, owner_tests=10, source_members=len(names))


def native_owner(root):
    script = root / 'owner/run_native_source_checks.py'
    local_run(root, script, ['--prepare-only'], 'prepare-native-owner')
    local_run(root, script, ['--execute-once'], 'native-owner', seconds=60)
    return read(root / 'owner/native-preparation/native-source-check.stdout')


def finalize(root):
    out = root / 'owner'
    verify_native_source(root)
    local_run(root, out / 'prepare_packet.py', [], 'materialize-component')
    context = read(out / 'driver/OPERATOR-CONTEXT.json'); package = context['package']
    require(package['mode'] == MODE and package['capture_provenance'] == PROVENANCE, 'exact verification mode')
    for name, pin in read(out / 'PROTECTED-UNCHANGED.json')['source_pins'].items(): checked_bytes(out / 'component' / name, pin)
    pins = read(root / 'source/RUNTIME-SOURCE-PINS.json')
    for name, pin in pins.items():
        require(checked_bytes(out / 'component' / name, pin) == checked_bytes(root / 'source' / name, pin), 'native-tested runtime bytes')
    require(not any('fake' in n or 'test_' in n for n in package['sources']), 'no fixture in live package')
    changes = []
    for directory in ('source', 'owner'):
        for path in sorted((root / directory).rglob('*.py')):
            relative = path.relative_to(root); original = DONOR / relative
            before = original.read_bytes() if original.is_file() else b''
            if before != path.read_bytes(): changes.extend(difflib.unified_diff(before.decode().splitlines(True), path.read_text().splitlines(True), str(original), str(path)))
    put(root / 'DERIVATION.diff', ''.join(changes).encode())
    value = dict(schema=1, status='CPU_COMPONENT_PACKET_READY_NOT_REGISTERED', mode=MODE,
        package=op._pin(out / 'component/PACKAGE.json'), context=op._pin(out / 'driver/OPERATOR-CONTEXT.json'),
        candidate_sha256=pins[KERNELS[1]], baseline_sha256=pins[KERNELS[0]], protected_helpers_unchanged=13,
        source_tests=TESTS, owner_tests=10, owner_seconds=1500, caller_seconds=2190, worker_seconds=600,
        card_seconds=280, model_starts=0, gpu_work=False, registration=False, adoption_authorized=False)
    write(root / 'COMPONENT-PACKET.json', value)
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('source', 'native-source', 'owner', 'native-owner', 'finalize'))
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    if args.action == 'source': value = source(args.root)
    else:
        root = op._canonical_dir(args.root, 'verification preparation')
        value = {'native-source': native_source, 'owner': owner, 'native-owner': native_owner, 'finalize': finalize}[args.action](root)
    print(json.dumps(value, sort_keys=True))
