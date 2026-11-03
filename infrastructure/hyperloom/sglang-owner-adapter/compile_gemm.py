"""One finite, device-free native compile census for the fixed MXFP4 N32 edit.

Reuse the existing sandbox, authenticated runtime, audit hook and finite caller.
Reproduce C4 and the historical GPU GEMM binaries before judging the candidate.
No GPU admission, model start or optimizer activation is available here.
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
import compile_component as existing
import gemm_dispatch as dispatch
import owner_operation as owner
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, require

R2, MX = owner.R2, owner.MX
I = R2 / "hyperloom-sglang-implementation-20261102-v1"
SEALS = {
    "d1-layouts-run-a04f5783ef4e4e9a": "8b53228728d313cbd75968311fcc9c9210d6ee7779462953f9518203b07b0201",
    "fp8-fastpath-research-v2/phase2-128k-c4ab-ff396f8f-preparation-v2": "4a61f62b34e9ca35ef0b4d3ee981e910850dd74ec2a68095638b72afd6888d34",
    "fp8-fastpath-research-v2/phase2-attn-c4-accumulator-compile-v1": "0c2585f4c70e7e14ba51fac0d6708c5a04961336dff83beb41e794d838c7d494",
}
FIELDS = ("group_segment_fixed_size", "private_segment_fixed_size", "sgpr_count", "sgpr_spill_count",
          "vgpr_count", "vgpr_spill_count", "wavefront_size", "max_flat_workgroup_size")


def gpu_pins():
    root = MX / "d1-layouts-run-a04f5783ef4e4e9a/results/retained/workload/output/mxfp4"
    record = json.loads((root / "MXFP4-RESULT.json").read_bytes())
    result = {}
    for name, m, n, k in dispatch.CASES:
        # N is runtime i32 with the same divisibility on every declared shape.
        # QKVZ+BA has no historical combined-shape case; use the same-K GEMM,
        # then require exact native binary equality rather than assume it.
        row = next(r for r in record["cases"] if r["M"] == m and r["K"] == k
                   and r["card"] == 0 and r["arm"] == "B")
        item = row["compiler"]["artifact"]
        packed = checked_bytes(root / item["path"], item["sha256"], 16 << 20)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, (16 << 20) + 1)
        require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail and len(raw) <= 16 << 20,
                "bounded historical compiler record")
        require(digest(raw) == row["compiler"]["codec"]["decoded_sha256"], "historical compiler decoded digest")
        compiled = json.loads(raw)
        require(compiled["name"] == "_gemm_afp4wfp4_kernel", "historical GEMM symbol")
        result[name] = {part: compiled["artifacts"][part]["sha256"] for part in ("hsaco", "amdgcn")}
    return result


def prepare(destination):
    root = owner._fresh(destination, "GEMM CPU compile")
    seals = owner.sealer()
    for name, pin in SEALS.items():
        seals.verify(MX / name, pin)
    inputs = json.loads((I / "COMPONENT-COMPILE-INPUTS-v1.json").read_bytes())
    source = {name: checked_bytes(Path(inputs[name]), pin, 4 << 20) for name, pin in existing.PINS.items()}
    review = R2 / "phase2-128k-c4ab-ff396f8f-preparation-v2/author/package-v2/review"
    baseline = checked_bytes(review / "ops.py", dispatch.OPS_SHA256)
    kernels = checked_bytes(review / "kernels.py", dispatch.KERNEL_SHA256)
    candidate = dispatch.derive(baseline)
    launches = dispatch.capture_cases(baseline, candidate)
    donor = ast.parse(source["recipe"])
    original_inner = existing._literal(donor, "source").decode()
    old_compiler = source["compiler"].decode()
    require(original_inner.count(old_compiler) == 1, "frozen native compiler boundary")
    prefix = original_inner.split(old_compiler)[0]
    target = (HERE / "compile_gemm_target.py").read_bytes()
    payload = dict(c4=source["baseline"].decode(), c4_launch=json.loads(source["launch"]),
                   c4_pins=existing.BASELINE_PARTS, kernels=kernels.decode(), launches=launches,
                   gpu_pins=gpu_pins())
    inner = prefix + old_compiler[:old_compiler.index("def main(payload):")] + target.decode()
    inner += "\npayload=json.loads(base64.b64decode(" + repr(base64.b64encode(canonical(payload))) + ",validate=True))\n"
    inner += '''report=dict(status='CPU_COMPILE_STARTED',gpu_launches=0,model_starts=0,device_nodes=False,cases={})
c4=compile_arm(payload['c4'],'/mnt/review/extend_attention_fwd_baseline.py','c4_exp2_B',payload['c4_launch'])
report['c4_proxy_reproduced']=all(c4['artifacts'][p]['sha256']==v for p,v in payload['c4_pins'].items())
report['c4']=c4
if report['c4_proxy_reproduced']:
 for name,arms in payload['launches'].items():
  b=compile_gemm(payload['kernels'],arms['baseline'])
  reproduced=all(b['artifacts'][p]['sha256']==v for p,v in payload['gpu_pins'][name].items())
  report['cases'][name]=dict(baseline=b,baseline_reproduced=reproduced)
  if not reproduced:
   report['status']='STOP_GEMM_BASELINE_IDENTITY_MISMATCH'
   break
  report['cases'][name]['candidate']=compile_gemm(payload['kernels'],arms['candidate'])
 else:
  report['status']='CPU_COMPILED'
else:
 report['status']='STOP_C4_PROXY_MISMATCH'
assert not torch.cuda.is_initialized()
print(json.dumps(report,sort_keys=True,default=lambda obj:obj.__dict__))
'''
    compile(inner, "native-gemm-compile", "exec", dont_inherit=True)
    lines = source["recipe"].decode().splitlines(keepends=True)
    node = next(n for n in donor.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "source" for t in n.targets))
    lines[node.lineno - 1:node.end_lineno] = ["source=" + repr(inner.encode()) + "\n"]
    recipe = "".join(lines).encode()
    compile(recipe, "native-gemm-recipe", "exec", dont_inherit=True)
    require(existing._literal(ast.parse(recipe), "argv") == existing._literal(donor, "argv"), "unchanged sandbox")
    root.mkdir(mode=0o700)
    files = {"RECIPE.py": recipe, "TARGET.py": inner.encode(), "ops_baseline.py": baseline,
             "ops_candidate.py": candidate, "kernels.py": kernels, "LAUNCHES.json": canonical(launches),
             "GPU-BASELINE-PINS.json": canonical(payload["gpu_pins"])}
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    metadata = dict(schema=1, status="PREPARED_CPU_ONLY", files={k: digest(v) for k, v in files.items()},
                    sources={name: digest((HERE / name).read_bytes()) for name in
                             ("compile_gemm.py", "compile_gemm_target.py", "gemm_dispatch.py", "compile_component.py")},
                    input_seals=SEALS, scope=dispatch.validate(baseline, candidate),
                    connection=inputs["connection"], commands=inputs["commands"], work_seconds=240,
                    gpu_launches=0, model_starts=0, automatic_retry=False, registration=False,
                    gpu_launch_authorized=False, adoption_authorized=False)
    owner._write(root / "INPUTS.json", metadata)
    return metadata


def inspect_isa(raw):
    import re
    text = raw.decode()
    resources = {}
    for field in FIELDS:
        values = re.findall(r"^\s*\." + field + r":\s*(\d+)\s*$", text, re.M)
        require(len(values) == 1, "one GEMM compiler resource: " + field)
        resources[field] = int(values[0])
    require(resources["wavefront_size"] == 32 and resources["max_flat_workgroup_size"] == 128,
            "four wave32 warps")
    require(resources["group_segment_fixed_size"] <= 65536, "LDS exceeds 64 KiB")
    return resources


def readout(root, metadata, receipt):
    require(receipt["exit_code"] == 0 and receipt["child_reaped"] is receipt["owned_group_absent"] is True
            and not receipt.get("failure") and not receipt.get("signals")
            and receipt["stdin_sha256"] == metadata["files"]["RECIPE.py"]
            and receipt["work_seconds"] == 240 and receipt["term_seconds"] == receipt["reap_seconds"] == 5,
            "finite natural CPU compile completion")
    for stream in ("stdout", "stderr"):
        raw = checked_bytes(root / ("target-compile." + stream), receipt[stream]["sha256"], 16 << 20)
        require(len(raw) == receipt[stream]["bytes"], "receipt stream extent")
    outer = json.loads((root / "target-compile.stdout").read_bytes())
    require(outer["returncode"] == 0, "native CPU compile child failed")
    result = json.loads(outer["stdout"])
    require(result["gpu_launches"] == result["model_starts"] == 0 and result["device_nodes"] is False,
            "CPU-only native result")
    artifacts = root / "artifacts"
    artifacts.mkdir()
    rows = [("c4", result["c4"])]
    rows += [(name + "-" + arm, value[arm]) for name, value in result["cases"].items()
             for arm in ("baseline", "candidate") if arm in value]
    for name, row in rows:
        require(set(row["artifacts"]) == {"ttir", "ttgir", "llir", "amdgcn", "hsaco"}, "compiler part closure")
        for part, info in row["artifacts"].items():
            raw = existing._decompress(info.pop("gzip_base64"), info)
            (artifacts / (name + "." + part)).write_bytes(raw)
            if part == "amdgcn" and name != "c4":
                row["resources"] = inspect_isa(raw)
    eligible = result["status"] == "CPU_COMPILED" and result["c4_proxy_reproduced"] is True
    expected = json.loads((root / "GPU-BASELINE-PINS.json").read_bytes())
    eligible = eligible and set(result["cases"]) == set(expected)
    for name, row in result["cases"].items():
        require(all(row["baseline"]["artifacts"][p]["sha256"] == v for p, v in expected[name].items())
                == row["baseline_reproduced"], "baseline binary comparison recomputation")
        if "candidate" not in row:
            eligible = False
            continue
        b, c = row["baseline"]["resources"], row["candidate"]["resources"]
        row["dynamic_lds"] = {arm: row[arm]["metadata"]["shared"] for arm in ("baseline", "candidate")}
        row["resource_pass"] = (c["vgpr_count"] < b["vgpr_count"] <= 109
            and 0 <= row["dynamic_lds"]["candidate"] <= row["dynamic_lds"]["baseline"] <= 4096
            and all(c[k] == b[k] == 0 for k in ("group_segment_fixed_size", "private_segment_fixed_size",
                                               "sgpr_spill_count", "vgpr_spill_count"))
            and row["candidate"]["artifacts"]["hsaco"]["sha256"] != row["baseline"]["artifacts"]["hsaco"]["sha256"])
        eligible = eligible and row["resource_pass"] and row["baseline_reproduced"]
    result.update(cpu_gate_pass=eligible, gpu_launch_authorized=False, adoption_authorized=False,
                  numerical_correctness_measured=False, performance_measured=False)
    owner._write(root / "COMPILE-RESULT.json", result)
    return result


def execute(root, metadata_sha256):
    root = owner._canonical_dir(root, "GEMM CPU compile")
    metadata = checked_json(root / "INPUTS.json", metadata_sha256)
    for name, pin in metadata["sources"].items():
        checked_bytes(HERE / name, pin)
    for name, pin in metadata["files"].items():
        checked_bytes(root / name, pin, 4 << 20)
    require(not any((root / n).exists() for n in ("INVOKED.json", "target-compile.json", "COMPILE-RESULT.json")),
            "single-use native CPU compile")
    connection = owner._source(Path(metadata["connection"]), existing.PINS["connection"], "_gemm_compile_connection")
    commands = owner.commands(metadata["commands"], root)
    connection.check()
    owner._write(root / "INVOKED.json", dict(metadata_sha256=metadata_sha256, gpu_work=False))
    receipt = commands.run("target-compile", connection.SSH + ["/usr/bin/python3", "-I", "-B", "-S", "-"],
                           240, stdin=(root / "RECIPE.py").read_bytes(), cwd=root, output_limit=16 << 20)
    return readout(root, metadata, receipt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "execute"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--metadata-sha256")
    args = parser.parse_args()
    result = prepare(args.root) if args.action == "prepare" else execute(args.root, args.metadata_sha256)
    print(json.dumps({k: v for k, v in result.items() if k not in ("c4", "cases")}, sort_keys=True))
