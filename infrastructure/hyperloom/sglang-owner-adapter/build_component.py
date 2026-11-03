"""Derive a fresh C4 component packet with genuine native CPU source gates.

Each action is explicit. Source and owner preparation are inert; the two native
checks use the retained CPU-only guard. Finalization does not register or invoke
a GPU owner. The protected observer, resource gate and finite owner are retained.
"""

import argparse
import ast
import difflib
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owner_operation as operation
import compile_component
import component_scope
import build_owner
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, require

INFRA = HERE.parents[2]
MODE = "isolated-gfx1201-hyperloom-c4-component-v1"
PROVENANCE = "seeded-bf16-current-c4-versus-hyperloom-candidate-two-cards-sequential-v1"
OLD_MODE = "isolated-gfx1201-c4-accumulator-component-v1"
OLD_PROVENANCE = "seeded-bf16-current-c4-versus-accumulator-two-cards-sequential-v1"
TESTS = 37
SOURCE_SEAL = "79de5992ee89b9f5cc5269b37aa450e96e271bf23f5a306de25096ae0913d885"
OWNER_SEAL = "39b4b297d825c939622bf440a86deced5bd9cb75a626cf07dc4de834362efae0"
NATIVE_SEAL = "4c5a033dd4dfcb84246773305985f0ea3d47f7345c1911c64413b2ecca8035a9"
BASE_PYTHON = "/home/developer/kv4-serving-integration-20261014.M4uQHI-base2/base-rootfs/usr/bin/python3.14"


def read(path):
    return json.loads(Path(path).read_bytes())


def put(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)


def write(path, value):
    put(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def replace(path, old, new, count=1):
    raw = path.read_text()
    require(raw.count(old) == count, "component donor changed: " + path.name + ": " + old[:60])
    path.write_text(raw.replace(old, new))


def assignment(path, name, value):
    lines = path.read_text().splitlines(keepends=True)
    nodes = [n for n in ast.parse("".join(lines)).body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    require(len(nodes) == 1, "single component assignment: " + name)
    n = nodes[0]
    lines[n.lineno - 1:n.end_lineno] = [name + " = " + repr(value) + "\n"]
    path.write_text("".join(lines))


def verify_donor(root):
    root = operation._canonical_dir(root, "component donor")
    seals = operation.sealer()
    for name, pin in (("source", SOURCE_SEAL), ("owner", OWNER_SEAL), ("native-component-v2", NATIVE_SEAL)):
        seals.verify(root / name, pin)
    return root


def copy_files(source, destination, *, exclude=()):
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative == "FINAL-ARTIFACTS.sha256" or any(relative == n or relative.startswith(n + "/") for n in exclude):
            continue
        require(path.resolve() == path and not path.is_symlink(), "canonical donor member")
        put(destination / relative, path.read_bytes())


def _paths(root):
    return root / "source", root / "native-component", root / "owner"


def source(inputs, destination):
    root = operation._fresh(destination, "component packet")
    require(root.is_relative_to(INFRA), "component gates retain paths relative to the private infrastructure workspace")
    donor = verify_donor(inputs["donor_root"])
    compiled_root = operation._canonical_dir(inputs["compile_root"], "native CPU compile")
    metadata = checked_json(compiled_root / "INPUTS.json", inputs["compile_inputs_sha256"])
    result = checked_json(compiled_root / "COMPILE-RESULT.json", inputs["compile_result_sha256"])
    receipt = checked_json(compiled_root / "target-compile.json", inputs["compile_receipt_sha256"])
    baseline = checked_bytes(donor / "source/extend_attention_fwd_baseline.py", build_owner.C4_SHA)
    candidate = checked_bytes(Path(inputs["candidate"]), inputs["candidate_sha256"])
    require(metadata["candidate_sha256"] == digest(candidate), "CPU compile belongs to another candidate")
    build_owner.source_scope(baseline, candidate)
    scope = component_scope.validate(baseline, candidate)
    root.mkdir(mode=0o700)
    validation = root / "compile-validation"
    validation.mkdir()
    for name in ("target-compile.stdout", "target-compile.stderr"):
        put(validation / name, (compiled_root / name).read_bytes())
    replayed = compile_component.readout(validation, metadata, receipt)
    require(replayed == result and result["gpu_eligible"] is True, "fresh distinct compiler/resource gate required")
    write(root / "INPUTS.json", inputs)
    source, native, _ = _paths(root)
    copy_files(donor / "source", source, exclude=("NATIVE-SOURCE-INPUTS.json",))
    (source / "extend_attention_fwd_candidate.py").write_bytes(candidate)
    for name in ("component_scope.py", "component_dispatch.py"):
        put(source / name, (HERE / name).read_bytes())
    put(source / "assets/LAUNCH.json", checked_bytes(Path(metadata["inputs"]["launch"]), compile_component.PINS["launch"]))
    contract = source / "contract.py"
    assignment(contract, "MODE", MODE)
    assignment(contract, "PROVENANCE", PROVENANCE)
    assignment(contract, "CANDIDATE_SHA256", digest(candidate))
    assignment(contract, "COMPILER_PINS", result["compiler_pins"])
    replace(contract, "prefix PV accumulator association with post-prefix V scale",
            "pinned Hyperloom candidate at frozen C4 geometry and precision")
    for name in ("read_component.py", "test_payload.py"):
        p = source / name
        p.write_text(p.read_text().replace("C4-ACCUMULATOR", "C4-CANDIDATE"))
    # The exact-block assertion was specific to the historical accumulator edit.
    # Replace it with a source-bound contract and real host-dispatch witnesses;
    # retain all 36 other oracle, audit, payload and cleanup tests unchanged.
    test = source / "test_component.py"
    text = test.read_text()
    tree = ast.parse(text)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "test_only_reviewed_accumulator_block_changes")
    lines = text.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = ['''    def test_source_bound_candidate_and_actual_dispatch(self):
        import component_scope, component_dispatch, tempfile
        baseline=(ROOT/'extend_attention_fwd_baseline.py').read_bytes()
        candidate=(ROOT/'extend_attention_fwd_candidate.py').read_bytes()
        self.assertEqual(hashlib.sha256(baseline).hexdigest(),contract.BASELINE_SHA256)
        self.assertEqual(hashlib.sha256(candidate).hexdigest(),contract.CANDIDATE_SHA256)
        scope=component_scope.validate(baseline,candidate)
        frozen=json.loads((ROOT/'assets/LAUNCH.json').read_bytes())
        with tempfile.TemporaryDirectory(prefix='component-dispatch-') as temporary:
            for role,raw in (('baseline',baseline),('candidate',candidate)):
                captured=component_dispatch.capture(fake_source,raw.decode(),role,frozen,root=Path(temporary)/role)
                self.assertEqual(len(captured['witnesses']),4)
                wanted='int32' if role=='candidate' and scope['host_index_cast'] else 'int64'
                self.assertEqual(captured['launch']['args'][9]['dtype'],wanted)
''']
    test.write_text("".join(lines))
    fixture = source / "fake_source.py"
    replace(fixture, "    def dim(self):return len(self.shape)",
            "    def dim(self):return len(self.shape)\n"
            "    def to(self,dtype):\n"
            "        if self.dtype not in ('int32','int64') or dtype not in ('int32','int64'):raise ValueError('integer metadata cast only')\n"
            "        return Tensor(self.shape,dtype,self.device.index,self.device.type)")
    replace(fixture, "    torch.cuda=", "    torch.int32='int32';torch.int64='int64'\n    torch.cuda=")
    replace(fixture, "qo_indptr=t((2,)),kv_indptr=t((2,)),kv_indices=t((118784,)),",
            "qo_indptr=Tensor((2,),'int32',card),kv_indptr=Tensor((2,),'int32',card),kv_indices=Tensor((118784,),'int64',card),")
    runtime = read(source / "RUNTIME-SOURCE-PINS.json")
    (source / "RUNTIME-SOURCE-PINS.json").write_bytes(canonical({name: digest((source / name).read_bytes()) for name in runtime}))
    files = {p.relative_to(source).as_posix(): dict(bytes=p.stat().st_size, sha256=digest(p.read_bytes()))
             for p in sorted(source.rglob("*")) if p.is_file()}
    write(source / "NATIVE-SOURCE-INPUTS.json", dict(schema=1, expected_tests=TESTS,
          compiled_modules=sum(n.endswith(".py") for n in files), files=files))
    native.mkdir()
    raw = (donor / "native-component-v2/prepare.py").read_text()
    depth = len(native.relative_to(INFRA).parts) - 1
    require(raw.count("ROOT.parents[4]") == 1, "native preparation infrastructure root")
    put(native / "prepare.py", raw.replace("ROOT.parents[4]", "ROOT.parents[" + str(depth) + "]").encode())
    write(root / "SOURCE-PREPARATION.json", dict(schema=1, status="INERT_SOURCE_REQUIRES_NATIVE_CHECK",
        scope=scope, compiler_result=operation._pin(compiled_root / "COMPILE-RESULT.json"),
        runtime_pins=read(source / "RUNTIME-SOURCE-PINS.json"), tests=TESTS, compiled_modules=len([n for n in files if n.endswith('.py')]),
        gpu_work=False, registration=False, adoption_authorized=False))
    return read(root / "SOURCE-PREPARATION.json")


def _run(root, script, args, label, seconds=40):
    receipts = root / "local-checks"
    receipts.mkdir(exist_ok=True)
    inputs = read(root / "INPUTS.json")
    metadata = read(Path(inputs["compile_root"]) / "INPUTS.json")
    commands = operation.commands(metadata["inputs"]["commands"], receipts)
    return commands.run(label, [operation.PYTHON, "-I", "-B", "-S", str(script), *args], seconds,
                        cwd=script.parent, output_limit=2 << 20)


def native_source(root):
    source, native, _ = _paths(root)
    _run(root, native / "prepare.py", [], "prepare-native-component")
    metadata = read(native / "INPUTS.json")
    require(metadata["expected_tests"] == TESTS and metadata["work_seconds"] == 45, "fixed component source gate")
    inputs = read(root / "INPUTS.json")
    compiler_inputs = read(Path(inputs["compile_root"]) / "INPUTS.json")["inputs"]
    connection = operation._source(Path(compiler_inputs["connection"]), compile_component.PINS["connection"], "_component_source_connection")
    commands = operation.commands(compiler_inputs["commands"], native)
    recipe = checked_bytes(native / "RECIPE.py", metadata["recipe_sha256"], 12 << 20)
    require(not (native / "INVOKED.json").exists(), "single native component source invocation")
    connection.check()
    write(native / "INVOKED.json", dict(recipe_sha256=metadata["recipe_sha256"], gpu_work=False))
    commands.run("native-source-check", connection.SSH + [BASE_PYTHON, "-I", "-B", "-S", "-"],
                 45, stdin=recipe, cwd=native, output_limit=2 << 20)
    return verify_native_source(root)


def verify_native_source(root):
    source, native, _ = _paths(root)
    metadata = read(native / "INPUTS.json")
    result = read(native / "native-source-check.stdout")
    receipt = read(native / "native-source-check.json")
    require(result["status"] == "PASS" and result["tests"] == TESTS and result["compiled_modules"] == metadata["expected_modules"]
            and result["errors"] == result["failures"] == result["skips"] == 0
            and result["python"] == "3.14.4" and result["native_model_or_device_operations"] is False
            and result["input_inventory_sha256"] == metadata["input_inventory_sha256"], "actual native component source gate required")
    checked_bytes(source / "NATIVE-SOURCE-INPUTS.json", metadata["input_inventory_sha256"])
    require(receipt["exit_code"] == 0 and receipt["child_reaped"] is receipt["owned_group_absent"] is True
            and receipt["stdin_sha256"] == metadata["recipe_sha256"] and receipt["work_seconds"] == 45
            and not receipt.get("failure") and not receipt.get("signals"), "clean finite native component source receipt")
    for name in ("stdout", "stderr"):
        raw = checked_bytes(native / ("native-source-check." + name), receipt[name]["sha256"], 2 << 20)
        require(len(raw) == receipt[name]["bytes"], "native source stream length")
    for name, row in read(source / "NATIVE-SOURCE-INPUTS.json")["files"].items():
        require(len(checked_bytes(source / name, row["sha256"])) == row["bytes"], "native source changed after test")
    return result


def owner(root):
    source, native, out = _paths(root)
    require(not out.exists(), "fresh owner preparation required")
    result = verify_native_source(root)
    donor = verify_donor(read(root / "INPUTS.json")["donor_root"])
    exclude = ("native-preparation", "NATIVE-SOURCE-GATE-INPUTS.json", "MATERIALIZATION.json",
               "component/PACKAGE.json", "component/PACKAGE-IDENTITY.json", "driver/OPERATOR-CONTEXT.json",
               *("component/ABC-OWNER-SOURCE-" + name + ".json" for name in ("INVENTORY", "RESULT", "CALLER")))
    copy_files(donor / "owner", out, exclude=exclude)
    pins = read(source / "RUNTIME-SOURCE-PINS.json")
    for name in (*pins, "RUNTIME-SOURCE-PINS.json"):
        (out / "component" / name).write_bytes((source / name).read_bytes())
    for name in ("fake_payload.py", "fake_source.py", "test_component.py"):
        (out / name).write_bytes((source / name).read_bytes())
    for name in ("component_scope.py", "component_dispatch.py"):
        put(out / name, (source / name).read_bytes())
    for name in ("component/common.py", "component/run_experiment.py", "component/root_observe.py",
                 "driver/operator_driver.py", "driver/driver_contract.py", "test_owner.py"):
        path = out / name
        path.write_text(path.read_text().replace(OLD_MODE, MODE).replace(OLD_PROVENANCE, PROVENANCE)
                        .replace("C4-ACCUMULATOR", "C4-CANDIDATE")
                        .replace("REPEATED_C4_BITWISE_ACCUMULATOR_FP32_REFERENCE_BF16_TOLERANCE",
                                 "REPEATED_C4_BITWISE_CANDIDATE_FP32_REFERENCE_BF16_TOLERANCE"))
    replace(out / "component/run_experiment.py", "Current C4 versus ACCUMULATOR", "Current C4 versus pinned Hyperloom candidate")
    depth = len(out.relative_to(INFRA).parts) - 1
    for name in ("native_source_gate.py", "run_native_source_checks.py", "prepare_packet.py"):
        replace(out / name, "parents[4]", "parents[" + str(depth) + "]")
    proof = [(source / "NATIVE-SOURCE-INPUTS.json").read_bytes(),
             (native / "native-source-check.stdout").read_bytes(), (native / "native-source-check.json").read_bytes()]
    for part, raw in zip(("INVENTORY", "RESULT", "CALLER"), proof):
        (out / "component" / ("ABC-COMPONENT-SOURCE-" + part + ".json")).write_bytes(raw)
    (out / "component/assets/ABC-NATIVE-SOURCE-SUMMARY.json").write_bytes(canonical(dict(result=result,
        receipt=dict(sha256=digest(proof[2])), source_inventory=dict(sha256=digest(proof[0])))))
    config = read(out / "component/ABC-OWNER-CONFIG.json")
    descriptor = compile_component._literal(ast.parse((out / "component/common.py").read_bytes()), "COMPONENT")
    config.update(capture_provenance=PROVENANCE, component=descriptor, component_sources=pins,
                  component_native_expected_tests=TESTS, component_native_expected_modules=result["compiled_modules"],
                  payload_sha256=pins["stage_probe.py"], reader_sha256=pins["read_component.py"])
    config["owner_native_gate_sources"] = {n: (out / "component" / n).relative_to(INFRA).as_posix()
                                           for n in config["owner_native_gate_sources"]}
    config["owner_native_expected_modules"] = len(list(out.glob("*.py"))) + sum(len(list((out / name).rglob("*.py")))
                                                                               for name in ("component", "driver", "fixtures"))
    (out / "component/ABC-OWNER-CONFIG.json").write_bytes(canonical(config))
    (out / "ADDITIONAL-SOURCE-BINDINGS.json").write_bytes(canonical(dict(schema=1, component_root=str(source),
        component_native_root=str(native), component_inventory_sha256=digest(proof[0]), source_pins=pins,
        gpu_work=False, model_starts=0, protected_helpers_changed=False)))
    protected = read(out / "PROTECTED-UNCHANGED.json")["source_pins"]
    require(len(protected) == 13, "exact protected helper closure")
    for name, pin in protected.items():
        checked_bytes(out / "component" / name, pin)
    for path in out.rglob("*.py"):
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    return dict(status="INERT_OWNER_REQUIRES_NATIVE_CHECK", protected_helpers_unchanged=13,
                source_tests=TESTS, source_modules=result["compiled_modules"], owner_modules=config["owner_native_expected_modules"])


def native_owner(root):
    out = root / "owner"
    _run(root, out / "run_native_source_checks.py", ["--prepare-only"], "prepare-native-owner")
    # The inner SSH helper already has work 45 + TERM 5 + reap 5. This enclosing
    # local command reserves its own ten-second cleanup without shortening it.
    _run(root, out / "run_native_source_checks.py", ["--execute-once"], "native-owner", seconds=60)
    return read(out / "native-preparation/native-source-check.stdout")


def finalize(root):
    out = root / "owner"
    verify_native_source(root)
    _run(root, out / "prepare_packet.py", [], "materialize-component")
    context = read(out / "driver/OPERATOR-CONTEXT.json")
    package = context["package"]
    require(package["mode"] == MODE and package["capture_provenance"] == PROVENANCE,
            "fresh Hyperloom component descriptor")
    for name, pin in read(out / "PROTECTED-UNCHANGED.json")["source_pins"].items():
        checked_bytes(out / "component" / name, pin)
    pins = read(root / "source/RUNTIME-SOURCE-PINS.json")
    for name, pin in pins.items():
        require(checked_bytes(out / "component" / name, pin) == checked_bytes(root / "source" / name, pin),
                "packaged component differs from native-tested source")
    changes = []
    donor = Path(read(root / "INPUTS.json")["donor_root"])
    for directory in ("source", "owner"):
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root)
            original = donor / relative
            before = original.read_bytes() if original.is_file() else b""
            after = path.read_bytes()
            if before != after:
                changes.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
                                                   str(original), str(path)))
    put(root / "DERIVATION.diff", "".join(changes).encode())
    value = dict(schema=1, status="CPU_COMPONENT_PACKET_READY_NOT_REGISTERED", mode=MODE,
        package=operation._pin(out / "component/PACKAGE.json"), context=operation._pin(out / "driver/OPERATOR-CONTEXT.json"),
        candidate_sha256=pins[build_owner.CANDIDATE_FILE], baseline_sha256=build_owner.C4_SHA,
        protected_helpers_unchanged=13, source_tests=TESTS, owner_tests=10,
        owner_seconds=1500, caller_seconds=2190, worker_seconds=600, card_seconds=280,
        model_starts=0, gpu_work=False, registration=False, adoption_authorized=False)
    write(root / "COMPONENT-PACKET.json", value)
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("source", "native-source", "owner", "native-owner", "finalize"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--inputs-sha256")
    args = parser.parse_args()
    if args.action == "source":
        value = source(checked_json(args.inputs, args.inputs_sha256), args.root)
    else:
        root = operation._canonical_dir(args.root, "component preparation")
        value = {"native-source": native_source, "owner": owner, "native-owner": native_owner, "finalize": finalize}[args.action](root)
    print(json.dumps(value, sort_keys=True))
