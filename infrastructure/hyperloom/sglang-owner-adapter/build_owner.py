"""Build an inert selected-C4/candidate serving packet through the existing owner.

Run in a fresh CPU process. Inputs bind the selected package and all donor
helpers; output is a new reviewable package, never a registration or invocation.
"""

import argparse
import ast
import difflib
import importlib.util
import json
from pathlib import Path
import re
import secrets
import sys

LIB = Path(__file__).resolve().parents[3] / "lib/workstation"
sys.path.insert(0, str(LIB))
import hyperloom_workload
from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, checked_json, require

C4_SHA = "b5d145e55f391de7e515cfe164b6011444fe1696188e2c0f900e338ce36c22f0"
STOCK_SHA = "f061599bb2c9243d50b565281c47de077ccc90135b9b1e5320a07425bedac112"
BASELINE_FILE = "extend_attention_fwd_baseline.py"
CANDIDATE_FILE = "extend_attention_fwd_candidate.py"
# Reviewed configuration experiment (CLAUDE-WARM-C2-REVIEW-v1): bounded per-path
# recurrent checkpoint trimming. Opt-in only; the selected runtime default is -1.
MAMBA_TRIM_INPUT = "mamba_max_states_per_path"
MAMBA_TRIM_FLAG = "--mamba-max-states-per-path"
MAMBA_TRIM_CAP = 3


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def source_scope(baseline, candidate):
    """Keep imports, module statements, signatures and unrelated functions fixed."""
    left, right = ast.parse(baseline), ast.parse(candidate)
    allowed = {"_fwd_kernel", "extend_attention_fwd"}
    require(len(left.body) == len(right.body), "candidate changed module structure")
    changed = []
    for before, after in zip(left.body, right.body, strict=True):
        if ast.dump(before) == ast.dump(after):
            continue
        require(isinstance(before, ast.FunctionDef) and isinstance(after, ast.FunctionDef)
                and before.name == after.name and before.name in allowed,
                "candidate changed an undeclared source surface")
        old_body, new_body = before.body, after.body
        before.body, after.body = [], []
        require(ast.dump(before) == ast.dump(after), "candidate changed function signature or decorators")
        before.body, after.body = old_body, new_body
        changed.append(before.name)
    return changed


def mamba_trim_cap(inputs):
    """Absent input keeps the selected contract byte-for-byte; only the reviewed 3 opts in."""
    if MAMBA_TRIM_INPUT not in inputs:
        return None
    cap = inputs[MAMBA_TRIM_INPUT]
    require(type(cap) is int and cap == MAMBA_TRIM_CAP,
            "mamba_max_states_per_path opt-in accepts only the reviewed integer 3")
    return cap


def build(inputs, destination):
    destination = Path(destination)
    require(destination.is_absolute() and destination.resolve() == destination
            and not destination.exists(), "fresh absolute packet destination required")
    trim = mamba_trim_cap(inputs)
    frozen = inputs.get("frozen_packet")
    if "frozen_packet" in inputs:
        require(type(frozen) is dict and set(frozen) == {"nonce", "pins"}
                and type(frozen["nonce"]) is str and re.fullmatch(r"[0-9a-f]{16}", frozen["nonce"])
                and type(frozen["pins"]) is dict and set(frozen["pins"]) == {
                    "archive_sha256", "manifest_sha256", "package_sha256", "source_manifest_sha256", "session_sha256"}
                and all(type(pin) is str and re.fullmatch(r"[0-9a-f]{64}", pin) for pin in frozen["pins"].values()),
                "frozen packet requires its nonce and all five exact pins")
    donor_root = Path(inputs["donor_root"])
    require(donor_root.is_absolute() and donor_root.resolve() == donor_root, "canonical donor directory required")
    pins = inputs["donor_inputs"]
    require(set(pins) == {"build_c4ab.py", "w5_common.py", "production_mode.py", "BASE-128K.json", "BOOT-ID.json"},
            "complete donor helper closure required")
    for name, pin in pins.items():
        checked_bytes(donor_root / name, pin, 2 << 20)
    inspect_path = donor_root.parent / "normal-stop-workload-preparation-v1/inspect_donor.py"
    checked_bytes(inspect_path, inputs["inspect_donor_sha256"], 2 << 20)
    base_info = json.loads((donor_root / "BASE-128K.json").read_bytes())
    base = donor_root.parent / base_info["package"]
    checked_bytes(base / "MANIFEST.json", inputs["base_manifest_sha256"], 2 << 20)
    selected_root = donor_root / "author/package-v2"
    checked_bytes(selected_root / "MANIFEST.json", inputs["selected_manifest_sha256"], 2 << 20)
    boot = inputs["boot_id"]
    require(isinstance(boot, str) and re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot),
            "explicit native boot identity required")
    candidate = checked_bytes(Path(inputs["candidate"]), inputs["candidate_sha256"], 4 << 20)
    sys.path.insert(0, str(donor_root))
    donor = load(donor_root / "build_c4ab.py", "_hyperloom_serving_donor")
    c = donor.c
    files, original_manifest = c.package()
    selected, selected_manifest = c.package(selected_root)
    baseline = selected[CANDIDATE_FILE]
    require(digest(baseline) == C4_SHA, "selected C4 source changed")
    family = inputs.get("source_family", "extend_attention")
    require(family in ("extend_attention", "verify_prefetch", "four_member"), "unknown serving source family")
    experiment_candidate = candidate
    verification = None
    if family in ("verify_prefetch", "four_member"):
        verification = load(Path(__file__).with_name("verify_serving.py"), "_hyperloom_verify_serving")
        require(digest(candidate) == verification.CANDIDATE_SHA
                and inputs.get("kind", "serving") == "serving", "exact verified serving candidate required")
        # The selected C4 extend module stays fixed in both starts.
        candidate = baseline
        changed = ["_fwd_grouped_kernel_stage1"]
    else:
        changed = source_scope(baseline, candidate)
    kind = inputs.get("kind", "serving")
    require(kind in ("serving", "profile"), "bounded serving or profiling preparation required")
    m = (hyperloom_workload.derive_profile if kind == "profile" else hyperloom_workload.derive)(selected)
    count = len(m["rows"])
    donor.N = donor.W1N = len(m["rows"])
    out = donor.transform(files, m)
    donor.repin_backend(files, out)
    donor.c4_overlay(files, out)

    def edit(name, old, new, count=1):
        text = out[name].decode()
        require(text.count(old) == count, "owner source drift: " + name + ": " + old[:70])
        out[name] = text.replace(old, new).encode()

    # Both starts serve the selected stack. The only experimental source is the
    # declared attention module; the control always receives its immutable C4.
    out[BASELINE_FILE], out[CANDIDATE_FILE] = baseline, candidate
    overlay = json.loads(out["ALLOCATION-OVERLAY.json"])
    row = next(r for r in overlay["files"] if r["key"] == "extend_attention_fwd")
    require(row["sha256"] == C4_SHA and row["original_sha256"] == STOCK_SHA, "C4 overlay donor mismatch")
    row["sha256"] = digest(candidate)
    out["ALLOCATION-OVERLAY.json"] = canonical(overlay)
    edit("mtp_contract.py",
         "stripped = ('triton_backend', 'decode_attention_fwd') + (() if arm == 'mtp' else ('extend_attention_fwd',))",
         "stripped = ('triton_backend', 'decode_attention_fwd')")
    edit("mtp_contract.py",
         "        rows = [row for row in rows if row['key'] not in stripped]\n    return rows",
         "        rows = [row for row in rows if row['key'] not in stripped]\n"
         "        if arm == 'control':\n"
         "            baseline = Path(__file__).with_name('extend_attention_fwd_baseline.py')\n"
         "            _require(baseline.is_file() and not baseline.is_symlink() and\n"
         "                     hashlib.sha256(baseline.read_bytes()).hexdigest() == " + repr(C4_SHA) + ",\n"
         "                     'immutable selected C4 baseline identity')\n"
         "            rows = [dict(row, file=baseline.name, sha256=" + repr(C4_SHA) + ")\n"
         "                    if row['key'] == 'extend_attention_fwd' else row for row in rows]\n"
         "    return rows")
    edit("mtp_contract.py",
         "    names.difference_update('review/' + name for name in PACKED_REQUEST_NAMES",
         "    names.add('review/extend_attention_fwd_baseline.py')\n"
         "    names.difference_update('review/' + name for name in PACKED_REQUEST_NAMES")
    for arm, source in (("control", baseline), ("mtp", candidate)):
        name = "HTTP-BINDING-" + arm + ".json"
        binding = json.loads(out[name])
        source_row = binding["binding_template"]["sources"]["extend_attention_fwd"]
        source_row.update(sha256=digest(source), stock_sha256=STOCK_SHA)
        if trim is not None:
            # Both starts carry the same explicit trimming binding: CLI and resolved contract.
            template = binding["binding_template"]
            cli = template["canonical_server_cli"]
            require(cli.count("--max-mamba-cache-size") == 1 and MAMBA_TRIM_FLAG not in cli
                    and MAMBA_TRIM_INPUT not in template["resolved_contract"]["prefix_cache"],
                    "selected binding already carries recurrent trimming")
            at = cli.index("--max-mamba-cache-size") + 2
            cli[at:at] = [MAMBA_TRIM_FLAG, str(trim)]
            template["resolved_contract"]["prefix_cache"][MAMBA_TRIM_INPUT] = trim
        out[name] = canonical(binding)
    if trim is not None:
        edit("mtp_contract.py",
             "                enable_int8_mamba_checkpoint=False, uses_mamba_radix_cache=True)\n",
             "                enable_int8_mamba_checkpoint=False, uses_mamba_radix_cache=True,\n"
             "                " + MAMBA_TRIM_INPUT + "=" + repr(trim) + ")\n")
        flag = repr(MAMBA_TRIM_FLAG)
        edit("mtp_contract.py",
             "def validate_memory_pool_config(value, arm):\n",
             "def validate_mamba_trim_cli(cli):\n"
             "    _require(type(cli) is list and cli.count(" + flag + ") == 1 and\n"
             "             cli[cli.index(" + flag + ")+1:cli.index(" + flag + ")+2] == [" + repr(str(trim)) + "],\n"
             "             'exact recurrent checkpoint trimming CLI differs')\n\n\n"
             "def validate_memory_pool_config(value, arm):\n")
        edit("serving_entry.py",
             "    mtp_contract.validate_recurrent_capacity_cli(value['canonical_server_cli'])\n",
             "    mtp_contract.validate_recurrent_capacity_cli(value['canonical_server_cli'])\n"
             "    mtp_contract.validate_mamba_trim_cli(value['canonical_server_cli'])\n")
        edit("serving_integration.py",
             "    _mtp_contract().validate_recurrent_capacity_cli(binding.get('canonical_server_cli'))\n",
             "    _mtp_contract().validate_recurrent_capacity_cli(binding.get('canonical_server_cli'))\n"
             "    _mtp_contract().validate_mamba_trim_cli(binding.get('canonical_server_cli'))\n")
    edit("benchmark_host_transport.py", "        return 80\n",
         "        need(expected.get('review/extend_attention_fwd_baseline.py') == " + repr(C4_SHA) +
         " and sha(members.get('review/extend_attention_fwd_baseline.py', b'')) == " + repr(C4_SHA) +
         ", 'immutable selected C4 baseline member')\n        return 81\n")
    edit("benchmark_host_transport.py", "len(members) < 80", "len(members) < 81")
    verification_contract = verification.apply(out, experiment_candidate) if verification else None
    bundle_contract = None
    if family == "four_member":
        peer_root = str(Path(__file__).resolve().parent)
        sys.path.insert(0, peer_root)
        try:
            bundle_builder = load(Path(__file__).with_name("build_bundle.py"), "_hyperloom_build_bundle")
            bundle_contract = bundle_builder.apply(out, m, inputs, donor)
        finally:
            sys.path.remove(peer_root)
    profile_request = None
    if kind == "profile":
        profiler_builder = load(Path(__file__).with_name("build_profile.py"), "_hyperloom_build_profile")
        profile_request = profiler_builder.apply(out, m, inputs, donor)
    shared = None
    if "shared_diagnostic" in inputs:
        shared_builder = load(Path(__file__).with_name("build_shared.py"), "_hyperloom_build_shared")
        shared = shared_builder.apply(out, m, inputs, donor)
    matched = None
    if "matched_profile" in inputs:
        # Matched candidate/control profile composed on the reviewed bundle comparison.
        require(bundle_contract is not None and shared is None, "matched profile composes the bundle only")
        matched_builder = load(Path(__file__).with_name("build_matched_profile.py"), "_hyperloom_build_matched_profile")
        matched = matched_builder.apply(out, m, inputs, donor, bundle_contract)
    for name, raw in out.items():
        if name.endswith(".py"):
            compile(raw, name, "exec", dont_inherit=True)
    destination.mkdir(mode=0o700)
    (destination / "INPUTS.json").write_bytes(canonical(inputs))
    (destination / "WORKLOAD.json").write_bytes(canonical({"schema": 1, "cases": m["cases"],
        "groups": m["groups"], "requests": count, "samples_per_case": 1 if kind == "profile" else 3,
        "diagnostic_only": kind == "profile" or shared is not None or matched is not None,
        "unchanged_prompt_tokens_and_oracles": True, "launch_authorized": False}))
    review = destination / "assembled-review"
    review.mkdir()
    contract, spec, sizes = (bundle_builder.regenerate_pins(c, out, review) if bundle_contract is not None
                             else c.regenerate_pins(out, review))
    sys.path.insert(0, str(review))
    try:
        for name in ("mtp_contract", "serving_contract", "benchmark_artifacts"):
            sys.modules.pop(name, None)
        contract = c.load(review / "mtp_contract.py", "mtp_contract")
        serving = c.load(review / "serving_contract.py", "serving_contract")
        require(contract.phase_config()[:3] == (131040, 2040, count) and len(serving.requests()) == count,
                "generated owner rejected the frozen request menu")
        overlays = {a: {r["key"]: r for r in contract.runtime_overlays(a)} for a in ("control", "mtp")}
        require(set(overlays["control"]) == set(overlays["mtp"]), "per-arm overlay roles changed")
        for key in overlays["control"]:
            if key != "extend_attention_fwd":
                require(overlays["control"][key] == overlays["mtp"][key], "unrelated per-arm difference")
        require(overlays["control"]["extend_attention_fwd"]["sha256"] == C4_SHA
                and overlays["mtp"]["extend_attention_fwd"]["sha256"] == digest(candidate), "attention arm identity")
        factory = c.load(review / "prepare_benchmark_package.py", "_hyperloom_serving_factory")
        controller = factory.controller_module()
        require({"review/" + n for n in out} == controller.LIVE_PACKAGE_NAMES, "owner package closure mismatch")
        target = "/home/developer/fp8-kv-benchmark." + (frozen["nonce"] if frozen is not None else secrets.token_hex(8))
        session = digest(("native-http-serving:" + target).encode())
        source_pins = {"review/" + n: digest(raw) for n, raw in out.items()}
        resource = factory.make_resource_manifest(target_root=target, session_sha256=session,
            retained_root=original_manifest["retained_root"], runtime_root=original_manifest["runtime_root"],
            model_root=original_manifest["model_root"], package_files=source_pins,
            bwrap_sha256=json.loads(files["RESOURCE-MANIFEST.json"])["bwrap_sha256"],
            graph_contract=spec["graph_projection"], http_contract=spec["http_serving"])
        out["RESOURCE-MANIFEST.json"] = canonical(resource)
        (review / "RESOURCE-MANIFEST.json").write_bytes(out["RESOURCE-MANIFEST.json"])
        source_pins["review/RESOURCE-MANIFEST.json"] = digest(out["RESOURCE-MANIFEST.json"])
        manifest = factory.make_manifest(target_root=target, boot_id=boot, session_sha256=session,
            retained_root=original_manifest["retained_root"], runtime_root=original_manifest["runtime_root"],
            model_root=original_manifest["model_root"], package_files=source_pins,
            resource_manifest_sha256=source_pins["review/RESOURCE-MANIFEST.json"],
            facts=json.loads(hyperloom_workload._unpack(out["COMPOSITION-FACTS.zlib"])),
            proof_sha256=spec["build_proof_sha256"], retained_base=original_manifest["retained_base"],
            experiment_mode=spec["experiment_mode"])
        require(manifest["runtime_files"] == selected_manifest["runtime_files"], "selected runtime changed")
        result = factory.create_payload(destination / "package-v2",
            sources={n: (review / n, digest(raw)) for n, raw in out.items()}, manifest=manifest)
        transport = c.load(review / "benchmark_host_transport.py", "_hyperloom_serving_transport")
        observed, members = transport.inspect_archive(Path(result["archive"]), result["archive_sha256"],
                                                       result["manifest_sha256"])
        expected_members = (matched['package_members'] if matched is not None
                            else 81 + len(bundle_contract['additional_package_members']) if bundle_contract is not None
                            else 89 if shared is not None else 83 if kind == "profile" else 81)
        require(observed == manifest and len(members) == expected_members,
                "generated archive failed existing transport")
        staged = Path(result["archive"]).stat().st_size + sum(map(len, members.values()))
        staged += sum(len(out[n]) for n in donor.PROTECTED[:5])
        require(staged <= 32 << 20, "established protected staging bound exceeded")
        result.update(schema=1, status="INERT_HYPERLOOM_SERVING_PACKET", launch_authorized=False,
            qualification=False, selected=False, adoption_authorized=False, automatic_retry=False,
            nonce=target.rsplit(".", 1)[1], target_root=target, session_sha256=session,
            requests_per_start=count, starts=2, caller_seconds=7160, owner_seconds=6360,
            kind=kind, diagnostic_only=kind == "profile" or shared is not None or matched is not None,
            profile_request=profile_request,
            baseline_sha256=verification.BASELINE_SHA if verification else C4_SHA,
            candidate_sha256=digest(experiment_candidate), changed_functions=changed,
            workload_sha256=digest((destination / "WORKLOAD.json").read_bytes()),
            http_bindings=sizes, staged_bytes=staged)
        if verification is not None:
            result.update(source_family=family, verification_attention=verification_contract)
        if bundle_contract is not None:
            result.update(four_member=bundle_contract)
        if shared is not None:
            result.update(shared_diagnostic=shared)
        if matched is not None:
            result.update(matched_profile=matched)
        if trim is not None:
            # Declared runtime-configuration deviation: same selected model, precision,
            # context, pool, power cap and owner limits, but not unchanged selected behavior.
            result.update(mamba_max_states_per_path=trim, runtime_config_deviation=dict(
                option=MAMBA_TRIM_FLAG, value=trim, selected_native_default=-1,
                arms=["control", "mtp"], selected_runtime_behavior_unchanged=False,
                proposed_configuration_experiment=True, adopted_runtime=False,
                requires_owner_decision_and_fresh_gpu_qualification=True,
                generated_sources=["mtp_contract.py", "serving_entry.py", "serving_integration.py"],
                bindings={arm: dict(file="HTTP-BINDING-" + arm + ".json",
                                    sha256=source_pins["review/HTTP-BINDING-" + arm + ".json"])
                          for arm in ("control", "mtp")}))
        if frozen is not None:
            require(all(result.get(key) == pin for key, pin in frozen["pins"].items()),
                    "rebuilt packet differs from the reviewed frozen packet")
        (destination / "PACKAGE-IDENTITIES.json").write_bytes(canonical(result))
    finally:
        sys.path.remove(str(review))
    changes = {}
    diffdir = destination / "diffs"
    diffdir.mkdir()
    for name, raw in out.items():
        before = selected.get(name)
        if raw != before:
            changes[name] = dict(before_sha256=digest(before) if before is not None else None,
                                 after_sha256=digest(raw), protected=name in donor.PROTECTED)
            if name.endswith(".py"):
                old = c.expand(before).decode() if before is not None else ""
                (diffdir / (name + ".diff")).write_text("".join(difflib.unified_diff(
                    old.splitlines(True), raw.decode().splitlines(True), fromfile="selected/" + name,
                    tofile="hyperloom/" + name)))
    (destination / "PACKET-DELTA.json").write_bytes(canonical(changes))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--inputs-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    # This preparation imports only authenticated CPU owner code. Candidate
    # kernel bytes are parsed and packaged, never imported or compiled natively.
    def audit(event, args):
        if event == "import" and args[0].split(".")[0] in {"torch", "triton", "sglang", "numpy"}:
            raise RuntimeError("framework import forbidden in owner preparation")
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn", "socket.__new__"} or event.startswith("os.exec"):
            raise RuntimeError("child or network execution forbidden in owner preparation")
    sys.addaudithook(audit)
    inputs = checked_json(Path(args.inputs), args.inputs_sha256, 1 << 20)
    result = build(inputs, args.output)
    print(json.dumps({k: result[k] for k in ("status", "nonce", "package_sha256", "manifest_sha256",
                                           "archive_sha256", "staged_bytes")}, sort_keys=True))


if __name__ == "__main__":
    main()
