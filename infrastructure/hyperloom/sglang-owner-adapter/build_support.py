"""Derive the finite caller and complete answer-grading template for an inert packet."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys

LIB = Path(__file__).resolve().parents[3] / "lib/workstation"
sys.path.insert(0, str(LIB))
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, require
from hyperloom_workload import _unpack
import base64


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def replace(raw, before, after, count=1):
    require(raw.count(before) == count, "support derivation drift: " + str(before)[:90])
    return raw.replace(before, after)


def build(inputs, root):
    root = Path(root)
    require(root.is_absolute() and root.resolve() == root and root.is_dir()
            and not (root / "SUPPORT-RESULT.json").exists(), "fresh canonical preparation root required")
    author = root / "author"
    checked_bytes(author / "package-v2/MANIFEST.json", inputs["manifest_sha256"], 2 << 20)
    ids = checked_json(author / "PACKAGE-IDENTITIES.json", inputs["identities_sha256"], 2 << 20)
    count = ids["requests_per_start"]
    require(count in (5, 30) and ids["launch_authorized"] is False
            and ids["starts"] == 2 and ids["owner_seconds"] == 6360 and ids["caller_seconds"] == 7160,
            "exact inert serving/profile owner required")
    donor = Path(inputs["donor_root"])
    require(donor.resolve() == donor and donor.is_absolute(), "canonical support donor required")
    r2 = donor.parent
    required = {
        "phase2-128k-c4ab-ff396f8f-preparation-v2/" + n for n in
        ("w5_common.py", "build_c4ab.py", "production_mode.py", "BASE-128K.json", "BOOT-ID.json",
         "prepare_launch.py", "derive_template.py", "POWER-CAP.json",
         "caller-sources/host_sample.py", "caller-sources/host_faults.py",
         "quality-template-c4ab/PROJECTION-INVENTORY.json")
    }
    required.update("mxfp4-quality-gate-p3-preparation-v1/" + n for n in
        ("author/LAUNCHER-BINDINGS.json", "author/owner-upload/RUN-ON-WORKSTATION.sh", "CALLER-PLAN.json"))
    required.update("mxfp4-quality-gate-p3-preparation-v1/caller-preview/" + n for n in
        ("run_native.py", "connection.py", "local_commands.py", "seal_evidence.py", "collect_results.py",
         "observe_closeout.py", "observe_failed_closeout.py"))
    required.update(("p1-quality-preparation-v1/caller-preview/power_observe.py",
                     "p1-quality-campaign-06c3aa52bd210a31/execute_once.py"))
    require(set(inputs["pins"]) == required, "complete support helper input closure required")
    for name, pin in inputs["pins"].items():
        checked_bytes(r2 / name, pin, 2 << 20)
    (root / "SUPPORT-INPUTS.json").write_bytes(canonical(inputs))
    manifest = json.loads((author / "package-v2/MANIFEST.json").read_bytes())
    files = {}
    for name, pin in manifest["package_files"].items():
        files[name.removeprefix("review/")] = checked_bytes(author / "package-v2" / name, pin, 32 << 20)
    logical = {k: base64.b64decode(v, validate=True) for k, v in
               json.loads(_unpack(files["FINAL-REQUESTS.zlib"])).items()}
    bound = json.loads(_unpack(logical["CLIENT-BINDING.zlib"]))["rows"]
    require(len(bound) == count, "complete owner request binding required")
    sys.path.insert(0, str(donor))
    c = load(donor / "w5_common.py", "w5_common")
    old_boot = c.BOOT
    c.HERE, c.BOOT = root, inputs["boot_id"]
    require(c.BOOT == manifest["boot_id"], "caller and owner boot identity differ")
    builder = load(donor / "build_c4ab.py", "build_c4ab")
    builder.N = builder.W1N = count
    template_builder = load(donor / "derive_template.py", "_hyperloom_quality_derivation")
    source = donor / "quality-template-c4ab"
    inventory = json.loads((source / "PROJECTION-INVENTORY.json").read_bytes())
    require(inventory["rows"] == 32, "selected C4 quality donor required")
    out = {}
    for name, row in inventory["files"].items():
        raw = checked_bytes(source / name, row["sha256"], 16 << 20)
        require(len(raw) == row["bytes"], "quality source size mismatch")
        out[name] = raw
    for name, edits in template_builder.EDITS.items():
        for before, after, occurrences in edits:
            # The same reviewed count transformation, applied to C4's 32-row
            # descendant rather than the original 352-row endurance template.
            before = before.replace("352", "32").replace("176", "16")
            out[name] = replace(out[name], before.encode(), after.encode(), occurrences)
    rows = []
    for row in bound:
        oracle = row["expected"]["task_grade"]
        require(oracle["kind"] == "exact_json", "frozen exact JSON answer required")
        rows.append(dict(case=row["case"], expected_answer=json.dumps(oracle["expected"], sort_keys=True, separators=(",", ":")),
                         grade_kind=oracle["kind"], maximum_output_tokens=row["maximum_output_tokens"],
                         minimum_final_tokens=row["expected"]["minimum_final_tokens"], ordinal=row["ordinal"],
                         request_body_sha256=row["body_sha256"], scope="natural-oracle"))
    out["producer/inputs/QUALITY-MENU.json"] = canonical({"rows": rows})
    out["source/EXPECTED-QUALITY.json"] = canonical([dict(copy.deepcopy(r), arm=arm)
                                                   for arm in ("bf16", "kv4") for r in rows])
    name = "producer/final_quality_inputs.py"
    menu_name = "producer/inputs/QUALITY-MENU.json"
    out[name] = replace(out[name], digest((source / menu_name).read_bytes()).encode(), digest(out[menu_name]).encode())
    for name in ("producer/final_quality_inputs.py", menu_name, "source/quality_contract.py", "source/EXPECTED-QUALITY.json"):
        out["bind_future.py"] = replace(out["bind_future.py"], digest((source / name).read_bytes()).encode(),
                                        digest(out[name]).encode())
    if old_boot != c.BOOT:
        for name in ("host_cpu.py", "workstation.py", "donor/PACKAGE.json"):
            out[name] = replace(out[name], old_boot.encode(), c.BOOT.encode())
    destination = root / "quality-template-c4ab"
    destination.mkdir(mode=0o700)
    for name, raw in out.items():
        if name.endswith(".py"):
            compile(raw, name, "exec", dont_inherit=True)
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
    updated = dict(inventory, rows=2 * count, total_bytes=sum(map(len, out.values())),
        files={name: dict(inventory["files"][name], sha256=digest(raw), bytes=len(raw)) for name, raw in out.items()},
        authority="Inert Hyperloom owner grading template; no execution authority",
        derived_from={"path": str(source), "inventory_sha256": digest((source / "PROJECTION-INVENTORY.json").read_bytes())})
    (destination / "PROJECTION-INVENTORY.json").write_bytes(canonical(updated))
    shutil.copytree(donor / "caller-sources", root / "caller-sources")
    require({p.name for p in (root / "caller-sources").iterdir()} == {"host_sample.py", "host_faults.py"},
            "unexpected caller source")
    if "observed_power_cap_microwatts" in inputs:
        cap = inputs["observed_power_cap_microwatts"]
        require(type(cap) is int and cap == 300000000, "only the observed 300 W override is supported")
        (root / "POWER-CAP.json").write_bytes(canonical(dict(watts=cap // 1000000,
            power_microwatts=cap, note="Pinned observation for both starts; no power-setting change")))
    else:
        shutil.copyfile(donor / "POWER-CAP.json", root / "POWER-CAP.json")
    helper = (donor / "prepare_launch.py").read_bytes()
    helper = replace(helper, b"    subprocess.run(['test', '!', '-e', str(path)], check=True)",
                     b"    assert not path.exists() and not path.is_symlink(), 'fresh caller path required'")
    helper = replace(helper,
        b"# One-use phase-2 128K C4 serving A/B, production mode: promoted candidate incl. packed QKVZ+BA; start 1 stock extend attention, start 2 the C4 overlay (8 warps, gfx1201 head-dim-256 extend shape); two starts x 16 natural-stop requests (10 verbatim B1 + 6 W1, ~6 min each); BF16 target and draft KV. No qualification.",
        ("# One-use Hyperloom " + ids["kind"] + " experiment: selected C4 then the source-bound candidate; two starts x " +
         str(count) + " natural-stop requests; fixed model, precision and context. No adoption.").encode())
    helper_path = root / "prepare_launch_derived.py"
    helper_path.write_bytes(helper)
    launcher = load(helper_path, "_hyperloom_prepare_launch")
    launcher.main()
    plan_path = root / "launcher/CALLER-PLAN.json"
    plan = json.loads(plan_path.read_bytes())
    require(plan["limits"]["requests"] == plan["limits"]["natural_grades"] == 2 * count
            and plan["limits"]["cpu_grading_seconds"] == 320, "finite caller grading bounds changed")
    plan["label"] = "hyperloom-" + ids["kind"]
    plan_path.write_bytes(canonical(plan))
    caller = root / "launcher/execute_once.py"
    raw = caller.read_bytes()
    raw = replace(raw, b"REGISTERED_ONE_PHASE2_128K_C4_SERVING_AB", b"REGISTERED_ONE_HYPERLOOM_SERVING")
    raw = replace(raw, b"Run the exact phase-2 C4 serving A/B operation", b"Run one registered Hyperloom serving experiment")
    caller.write_bytes(raw)
    result = dict(schema=1, status="INERT_OWNER_CALLER_AND_GRADING_TEMPLATE", execution_authorized=False,
        caller_sha256=digest(raw), caller_plan_sha256=digest(plan_path.read_bytes()),
        quality_inventory_sha256=digest((destination / "PROJECTION-INVENTORY.json").read_bytes()),
        natural_grades=2 * count, grading_seconds=320, owner_seconds=6360, caller_seconds=7160,
        automatic_retry=False, adoption_authorized=False)
    (root / "SUPPORT-RESULT.json").write_bytes(canonical(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    def audit(event, args):
        if event == "import" and args[0].split(".")[0] in {"torch", "triton", "sglang", "numpy"}:
            raise RuntimeError("framework import forbidden during caller preparation")
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "socket.__new__"} or event.startswith("os.exec"):
            raise RuntimeError("child or network execution forbidden during caller preparation")
    sys.addaudithook(audit)
    print(json.dumps(build(checked_json(Path(args.inputs), args.inputs_sha256, 1 << 20), args.root), sort_keys=True))


if __name__ == "__main__":
    main()
