"""Finite CPU-only resource census of the active grouped verification kernel."""
import ast
import base64
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compile_component as existing
import compile_gemm
import owner_operation as owner
import verify_dispatch as dispatch
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, require

SOURCE_ROOT = owner.INFRA / "scratchpad/fp8-verify-adapter._5rkpmzn"
SOURCE_SEAL = "47eb02d2d474980296b69f9c5025fdb0110c9d6fa8ee348f3a624459f1e397ca"
STOCK_PARTS = dict(hsaco="617bee9affff4921b8005c7d5f39587f3ab1197c58a2c11788391240251c88ed",
                   amdgcn="e1de5895628d1f4447d27904b1d0476e0397e1a7a1f085ea8c7f81831a8e7b09")


def prepare(destination, *, prefetch=False):
    require(type(prefetch) is bool, "explicit closed prefetch experiment flag")
    root = owner._fresh(destination, "verification CPU census")
    owner.sealer().verify(SOURCE_ROOT, SOURCE_SEAL)
    owner.sealer().verify(owner.R2 / "phase2-attn-c4-accumulator-compile-v1",
                          compile_gemm.SEALS["fp8-fastpath-research-v2/phase2-attn-c4-accumulator-compile-v1"])
    inputs = json.loads((compile_gemm.I / "COMPONENT-COMPILE-INPUTS-v1.json").read_bytes())
    sources = {name: checked_bytes(Path(inputs[name]), pin, 4 << 20) for name, pin in existing.PINS.items()}
    raw = checked_bytes(SOURCE_ROOT / "native-attention/kernels/ops/attention/decode_attention.py", dispatch.SOURCE_SHA256)
    launches = dispatch.capture_cases(raw)
    old = ast.parse(sources["recipe"])
    compiler = sources["compiler"].decode()
    original_inner = existing._literal(old, "source").decode()
    require(original_inner.count(compiler) == 1, "original compiler boundary")
    prefix = original_inner.split(compiler)[0]
    payload = dict(source=raw.decode(), launches=launches, c4=sources["baseline"].decode(),
                   c4_launch=json.loads(sources["launch"]), c4_pins=existing.BASELINE_PARTS,
                   prefetch=prefetch, candidate=dispatch.derive_prefetch(raw).decode() if prefetch else None,
                   stock_pins=STOCK_PARTS)
    inner = prefix + compiler[:compiler.index("def main(payload):")] + (HERE / "compile_verify_target.py").read_text()
    inner += "\npayload=json.loads(base64.b64decode(" + repr(base64.b64encode(canonical(payload))) + ",validate=True))\n"
    inner += '''report=dict(status='CPU_CENSUS_STARTED',gpu_launches=0,model_starts=0,device_nodes=False,cases={},candidates={})
c4=compile_arm(payload['c4'],'/mnt/review/extend_attention_fwd_baseline.py','c4_exp2_B',payload['c4_launch'])
report['c4_proxy_reproduced']=all(c4['artifacts'][p]['sha256']==v for p,v in payload['c4_pins'].items())
report['c4']=c4
if report['c4_proxy_reproduced']:
 for name,row in payload['launches'].items():
  report['cases'][name]=compile_verify(payload['source'],row['launch'])
 report['status']='CPU_CENSUS_COMPLETE'
 if payload['prefetch']:
  report['stock_cpu_reproduced']=all(value['artifacts'][p]['sha256']==v for value in report['cases'].values() for p,v in payload['stock_pins'].items())
  if report['stock_cpu_reproduced']:
   for name,row in payload['launches'].items():
    report['candidates'][name]=compile_verify(payload['candidate'],row['launch'])
   report['status']='CPU_PREFETCH_COMPILED'
  else:
   report['status']='STOP_STOCK_CPU_IDENTITY_MISMATCH'
else:
 report['status']='STOP_C4_PROXY_MISMATCH'
assert not torch.cuda.is_initialized()
print(json.dumps(report,sort_keys=True,default=lambda obj:obj.__dict__))
'''
    compile(inner, "verify-census-target", "exec", dont_inherit=True)
    lines = sources["recipe"].decode().splitlines(True)
    node = next(n for n in old.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "source" for t in n.targets))
    lines[node.lineno - 1:node.end_lineno] = ["source=" + repr(inner.encode()) + "\n"]
    recipe = "".join(lines).encode()
    compile(recipe, "verify-census-recipe", "exec", dont_inherit=True)
    require(existing._literal(ast.parse(recipe), "argv") == existing._literal(old, "argv"), "unchanged sandbox")
    files = {"RECIPE.py": recipe, "TARGET.py": inner.encode(), "decode_attention.py": raw,
             "LAUNCHES.json": canonical(launches)}
    if prefetch:
        files["decode_attention_candidate.py"] = payload["candidate"].encode()
    root.mkdir(mode=0o700)
    for name, value in files.items():
        (root / name).write_bytes(value)
    metadata = dict(schema=1, status="PREPARED_CPU_ONLY", files={k: digest(v) for k, v in files.items()},
                    sources={name: digest((HERE / name).read_bytes()) for name in
                             ("compile_verify.py", "compile_verify_target.py", "verify_dispatch.py", "gemm_dispatch.py",
                              "compile_gemm.py", "compile_component.py")},
                    source_seal=SOURCE_SEAL, source_sha256=dispatch.SOURCE_SHA256, prefetch=prefetch,
                    connection=inputs["connection"], commands=inputs["commands"], work_seconds=240,
                    gpu_launches=0, model_starts=0, automatic_retry=False, registration=False,
                    gpu_launch_authorized=False, adoption_authorized=False)
    owner._write(root / "INPUTS.json", metadata)
    return metadata


def readout(root, metadata, receipt):
    require(receipt["exit_code"] == 0 and receipt["child_reaped"] is receipt["owned_group_absent"] is True
            and not receipt.get("failure") and not receipt.get("signals")
            and receipt["stdin_sha256"] == metadata["files"]["RECIPE.py"]
            and receipt["work_seconds"] == 240 and receipt["term_seconds"] == receipt["reap_seconds"] == 5,
            "finite natural CPU census completion")
    for name in ("stdout", "stderr"):
        raw = checked_bytes(root / ("target-compile." + name), receipt[name]["sha256"], 16 << 20)
        require(len(raw) == receipt[name]["bytes"], "receipt stream extent")
    outer = json.loads((root / "target-compile.stdout").read_bytes())
    require(outer["returncode"] == 0, "CPU census child failure")
    result = json.loads(outer["stdout"])
    require(result["status"] in ("CPU_CENSUS_COMPLETE", "CPU_PREFETCH_COMPILED", "STOP_STOCK_CPU_IDENTITY_MISMATCH")
            and result["c4_proxy_reproduced"] is True
            and result["gpu_launches"] == result["model_starts"] == 0 and result["device_nodes"] is False,
            "device-free faithful compiler census")
    require(set(result["cases"]) == {name for name, _ in dispatch.CASES}, "all frozen census cases")
    output = root / "artifacts"
    output.mkdir()
    candidates = result.get("candidates", {})
    if metadata.get("prefetch") and result["status"] == "CPU_PREFETCH_COMPILED":
        require(set(candidates) == set(result["cases"]), "all prefetch compiler cases")
    for name, row in [("c4", result["c4"]), *result["cases"].items(),
                      *((name + "-candidate", row) for name, row in candidates.items())]:
        if name != "c4":
            expected = metadata["files"]["decode_attention_candidate.py"] if name.endswith("-candidate") else dispatch.SOURCE_SHA256
            require(row["source_sha256"] == expected and row["source_path"] == dispatch.SOURCE_PATH,
                    "stock source identity")
        for part, info in row["artifacts"].items():
            raw = existing._decompress(info.pop("gzip_base64"), info)
            (output / (name + "." + part)).write_bytes(raw)
            if part == "amdgcn" and name != "c4":
                row["resources"] = compile_gemm.inspect_isa(raw)
                row["dynamic_lds_bytes"] = row["metadata"]["shared"]
    if metadata.get("prefetch"):
        reproduced = all(value["artifacts"][p]["sha256"] == pin
                         for value in result["cases"].values() for p, pin in STOCK_PARTS.items())
        require(reproduced == result["stock_cpu_reproduced"], "stock binary comparison recomputation")
        resource = {}
        for name, row in candidates.items():
            b, c = result["cases"][name], row
            resource[name] = (c["resources"]["vgpr_count"] <= b["resources"]["vgpr_count"] + 2
                and all(c["resources"][k] <= b["resources"][k] for k in
                        ("private_segment_fixed_size", "vgpr_spill_count", "sgpr_spill_count"))
                and c["dynamic_lds_bytes"] == b["dynamic_lds_bytes"]
                and c["artifacts"]["hsaco"]["sha256"] != b["artifacts"]["hsaco"]["sha256"])
        result.update(resource_pass=resource, cpu_resource_gate_pass=reproduced and bool(resource) and all(resource.values()),
                      isa_prefetch_order_reviewed=False, cpu_acceptance_complete=False)
    result.update(gpu_launch_authorized=False, adoption_authorized=False, performance_measured=False,
                  numerical_correctness_measured=False, physical_split_values_observed=False,
                  stock_gpu_binary_reproduction=False,
                  limitation="C4 authenticates the native compiler proxy; no retained GPU stage-1 binary exists to reproduce.")
    owner._write(root / "COMPILE-RESULT.json", result)
    return result


def execute(root, metadata_sha256):
    root = owner._canonical_dir(root, "verification CPU census")
    metadata = checked_json(root / "INPUTS.json", metadata_sha256)
    for name, pin in metadata["sources"].items():
        checked_bytes(HERE / name, pin)
    for name, pin in metadata["files"].items():
        checked_bytes(root / name, pin, 4 << 20)
    require(not any((root / n).exists() for n in ("INVOKED.json", "target-compile.json", "COMPILE-RESULT.json")),
            "single-use native CPU census")
    connection = owner._source(Path(metadata["connection"]), existing.PINS["connection"], "_verify_compile_connection")
    commands = owner.commands(metadata["commands"], root)
    connection.check()
    owner._write(root / "INVOKED.json", dict(metadata_sha256=metadata_sha256, gpu_work=False))
    receipt = commands.run("target-compile", connection.SSH + ["/usr/bin/python3", "-I", "-B", "-S", "-"],
                           240, stdin=(root / "RECIPE.py").read_bytes(), cwd=root, output_limit=16 << 20)
    return readout(root, metadata, receipt)
