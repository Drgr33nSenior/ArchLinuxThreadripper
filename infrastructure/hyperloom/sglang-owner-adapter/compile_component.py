"""Prepare, run and authenticate one device-free C4 candidate compile.

Native compilation is an explicit action. Import and preparation neither SSH
nor import a framework. The unchanged CPU sandbox reproduces the selected C4
binary before the candidate is considered. No GPU owner or authority is made.
"""

import argparse
import ast
import base64
import json
from pathlib import Path
import sys
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owner_operation as owner
import build_owner
import component_scope
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, decoded_json, require

COMPILE_SECONDS = 240
BASELINE_PARTS = dict(hsaco="ea0fefe1a40ef378d03dea47adf3d6c828f03dd2fab5c35db0200d1161019e9f",
                      amdgcn="10c924989dbf0534078c0959e6ac2b7ad472bcc3cc8a7c9fe0bdb9e090829218")
PINS = {
    "recipe": "5c7888aa34f2887bb1e77d2168317ae4f6511099a4c32c1a6da9d4125cca3b98",
    "compiler": "dc645438d509a52e0d8d3f20e973f0922fae3bcdad1c64676c91b0e5b05538c8",
    "launch": "1efe43dc7aa3e5d90d480d36d476f2d8fa02c957a6f1c979136c836067e3847b",
    "fixture": "1ef1ec1c3fc10cc09568645238cc6c3d0a6811e238cfd7214b4f117d51ac85e6",
    "gate": "780de3054ebb45b54cd90e2f898dd53e443da0ba81e9ecc6649aa9e62262fe02",
    "support": "c78bc4d1468b4759a76ed4b448c4ad01f7950c50b78545ee73d1246e44bfb9cb",
    "baseline": build_owner.C4_SHA,
    "connection": "1d885651f2d93b9f019bcce04c2022726a9c8af5676e475dc92035047b947dde",
    "commands": owner.COMMANDS_SHA,
}


def _literal(tree, name):
    found = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    require(len(found) == 1, "single donor literal: " + name)
    return ast.literal_eval(found[0].value)


def _module_lines(name, raw):
    return ("raw=base64.b64decode(" + repr(base64.b64encode(raw)) + ",validate=True)\n"
            "m=types.ModuleType(" + repr(name) + ");m.__file__='/tmp/' + " + repr(name + ".py") + "\n"
            "sys.modules[m.__name__]=m;exec(compile(raw,m.__file__,'exec'),m.__dict__)\n")


def prepare(inputs, destination):
    root = owner._fresh(destination, "CPU compiler preparation")
    require(set(inputs) == set(PINS) | {"candidate", "candidate_sha256"}, "closed compiler inputs required")
    sources = {name: checked_bytes(Path(inputs[name]), pin, 4 << 20) for name, pin in PINS.items()}
    candidate = checked_bytes(Path(inputs["candidate"]), inputs["candidate_sha256"], 4 << 20)
    changed = build_owner.source_scope(sources["baseline"], candidate)
    scope = component_scope.validate(sources["baseline"], candidate)
    donor = ast.parse(sources["recipe"])
    inner = _literal(donor, "source").decode()
    compiler = sources["compiler"].decode()
    require(inner.count(compiler) == 1, "original compile recipe boundary changed")
    prefix = inner.split(compiler)[0]
    require("att_io.install_capture_guard()" in prefix and "selected.load_runtime()" in prefix,
            "existing audit and authenticated runtime setup required")
    # Reuse compile_arm byte for byte. The orchestration below supplies the real
    # host wrapper's metadata rather than assuming every candidate uses int64.
    compiler = compiler[:compiler.index("def main(payload):")]
    payload = dict(baseline=sources["baseline"].decode(), candidate=candidate.decode(),
                   baseline_sha256=build_owner.C4_SHA, candidate_sha256=digest(candidate),
                   launch=decoded_json(sources["launch"]), **{k + "_sha256": v for k, v in BASELINE_PARTS.items()})
    dispatch = (HERE / "component_dispatch.py").read_bytes()
    inner = prefix + _module_lines("component_fixture", sources["fixture"])
    inner += _module_lines("component_dispatch", dispatch) + compiler
    inner += "\npayload=json.loads(base64.b64decode(" + repr(base64.b64encode(canonical(payload))) + ",validate=True))\n"
    inner += '''import component_fixture,component_dispatch
report=dict(gpu_launches=0,model_starts=0,device_nodes=False,arms={},dispatch={})
for arm,module_name in (('baseline','c4_exp2_B'),('candidate','c4_accumulator_C')):
 raw=payload[arm]
 assert hashlib.sha256(raw.encode()).hexdigest()==payload[arm+'_sha256']
 captured=component_dispatch.capture(component_fixture,raw,arm,payload['launch'])
 report['dispatch'][arm]=captured
 result=compile_arm(raw,'/mnt/review/extend_attention_fwd_'+arm+'.py',module_name,captured['launch'])
 report['arms'][arm]=result
 if arm=='baseline':
  report['baseline_reproduced']=all(result['artifacts'][part]['sha256']==payload[part+'_sha256'] for part in ('hsaco','amdgcn'))
  if not report['baseline_reproduced']:
   report['status']='STOP_BASELINE_IDENTITY_MISMATCH'
   break
else:
 report['status']='CPU_COMPILED'
assert not torch.cuda.is_initialized()
print(json.dumps(report,sort_keys=True,default=lambda obj:obj.__dict__))
'''
    compile(inner, "native-component-compile", "exec", dont_inherit=True)
    # Retain all original mounts, limits, boot check and finite subprocess call.
    lines = sources["recipe"].decode().splitlines(keepends=True)
    node = next(n for n in donor.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "source" for t in n.targets))
    lines[node.lineno - 1:node.end_lineno] = ["source=" + repr(inner.encode()) + "\n"]
    recipe = "".join(lines).encode()
    compile(recipe, "component-compile-recipe", "exec", dont_inherit=True)
    root.mkdir(mode=0o700)
    for name, raw in {"RECIPE.py": recipe, "TARGET.py": inner.encode(), "component_dispatch.py": dispatch,
                      "extend_attention_fwd_baseline.py": sources["baseline"],
                      "extend_attention_fwd_candidate.py": candidate}.items():
        (root / name).write_bytes(raw)
    metadata = dict(schema=1, status="PREPARED_CPU_ONLY", inputs=inputs, changed_functions=changed, scope=scope,
        sources={name: digest(raw) for name, raw in sources.items()}, candidate_sha256=digest(candidate),
        recipe_sha256=digest(recipe), target_sha256=digest(inner.encode()),
        dispatch_sha256=digest(dispatch), caller_source_sha256=digest(Path(__file__).read_bytes()),
        scope_source_sha256=digest((HERE / "component_scope.py").read_bytes()),
        work_seconds=COMPILE_SECONDS, term_seconds=5, reap_seconds=5, gpu_launches=0,
        model_starts=0, registration=False, automatic_retry=False, adoption_authorized=False)
    owner._write(root / "INPUTS.json", metadata)
    return metadata


def _decompress(encoded, expected):
    require(isinstance(encoded, str) and len(encoded) <= 4 << 20, "bounded compiler artifact")
    raw = base64.b64decode(encoded, validate=True)
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    result = d.decompress(raw, (8 << 20) + 1)
    require(len(result) <= 8 << 20 and d.eof and not d.unused_data and not d.unconsumed_tail,
            "closed compiler artifact compression")
    require(expected["bytes"] == len(result) and expected["sha256"] == digest(result), "compiler artifact identity")
    return result


def readout(root, metadata, receipt):
    stdout = checked_bytes(root / "target-compile.stdout", receipt["stdout"]["sha256"], 16 << 20)
    stderr = checked_bytes(root / "target-compile.stderr", receipt["stderr"]["sha256"], 16 << 20)
    require(receipt["exit_code"] == 0 and receipt["child_reaped"] is receipt["owned_group_absent"] is True
            and not receipt.get("failure") and not receipt.get("signals")
            and receipt["stdin_sha256"] == metadata["recipe_sha256"]
            and receipt["work_seconds"] == COMPILE_SECONDS
            and receipt["term_seconds"] == receipt["reap_seconds"] == 5
            and len(stdout) == receipt["stdout"]["bytes"] and len(stderr) == receipt["stderr"]["bytes"],
            "closed native CPU compile receipt required")
    outer = decoded_json(stdout)
    require(outer["returncode"] == 0, "native compiler child failed")
    report = decoded_json(outer["stdout"])
    require(report.get("status") == "CPU_COMPILED" and report.get("baseline_reproduced") is True
            and report.get("gpu_launches") == report.get("model_starts") == 0 and report.get("device_nodes") is False,
            "faithful device-free baseline compile required")
    require(set(report["arms"]) == set(report["dispatch"]) == {"baseline", "candidate"}, "both compiled arms required")
    owner._source(Path(metadata["inputs"]["support"]), PINS["support"], "support")
    gate = owner._source(Path(metadata["inputs"]["gate"]), PINS["gate"], "_hyperloom_component_compiler_gate")
    facts, pins = {}, {}
    artifacts = root / "artifacts"
    artifacts.mkdir()
    for arm in ("baseline", "candidate"):
        row = report["arms"][arm]
        require(row["source_sha256"] == (build_owner.C4_SHA if arm == "baseline" else metadata["candidate_sha256"])
                and row["source_path"] == "/mnt/review/extend_attention_fwd_" + arm + ".py",
                "compiled source identity/path mismatch")
        require(set(row["artifacts"]) == {"ttir", "ttgir", "llir", "amdgcn", "hsaco"}, "compiler artifact closure")
        raw_parts = {}
        for part, item in row["artifacts"].items():
            raw = _decompress(item.pop("gzip_base64"), item)
            (artifacts / (arm + "." + part)).write_bytes(raw)
            raw_parts[part] = raw
        pins[arm] = {part: digest(raw_parts[part]) for part in BASELINE_PARTS}
        facts[arm] = gate.inspect_isa(raw_parts["amdgcn"], "_fwd_kernel")
    require(pins["baseline"] == BASELINE_PARTS, "selected C4 executable did not reproduce")
    verdict = gate.compare(facts["baseline"], facts["candidate"])
    report.update(compiler_gate=verdict, facts=facts, compiler_pins={"B": pins["baseline"], "C": pins["candidate"]},
                  gpu_eligible=verdict == "DISTINCT_CODE", gpu_launch_authorized=False, qualification=False,
                  adoption_authorized=False)
    owner._write(root / "COMPILE-RESULT.json", report)
    return report


def execute(root, metadata_sha256):
    root = owner._canonical_dir(root, "CPU compile")
    metadata = checked_json(root / "INPUTS.json", metadata_sha256)
    checked_bytes(Path(__file__), metadata["caller_source_sha256"])
    checked_bytes(HERE / "component_scope.py", metadata["scope_source_sha256"])
    recipe = checked_bytes(root / "RECIPE.py", metadata["recipe_sha256"], 4 << 20)
    require(not any((root / name).exists() for name in ("INVOKED.json", "target-compile.json", "COMPILE-RESULT.json")),
            "CPU compiler preparation is single use")
    connection = owner._source(Path(metadata["inputs"]["connection"]), PINS["connection"], "_component_compile_connection")
    commands = owner.commands(metadata["inputs"]["commands"], root)
    connection.check()
    owner._write(root / "INVOKED.json", dict(metadata_sha256=metadata_sha256, gpu_work=False))
    receipt = commands.run("target-compile", connection.SSH + ["/usr/bin/python3", "-I", "-B", "-S", "-"],
                           COMPILE_SECONDS, stdin=recipe, cwd=root, output_limit=16 << 20)
    return readout(root, metadata, receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--inputs", type=Path, required=True)
    prep.add_argument("--inputs-sha256", required=True)
    prep.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("execute")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--metadata-sha256", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(checked_json(args.inputs, args.inputs_sha256), args.output)
    else:
        result = execute(args.root, args.metadata_sha256)
    print(json.dumps({k: v for k, v in result.items() if k not in ("arms", "dispatch", "inputs", "sources")}, sort_keys=True))
