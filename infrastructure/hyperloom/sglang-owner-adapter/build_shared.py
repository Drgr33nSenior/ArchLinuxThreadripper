"""Opt-in derivation of the two-start, control-only shared diagnostic owner.

Called by build_owner before its existing pin regeneration and archive checks.
The generated payload still requires a separate exact execution decision.
"""
import ast
import json
from pathlib import Path

from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, require

MODULES = ("shared_diagnostic", "shared_trace_io", "shared_runtime_binding",
           "shared_capture", "shared_readout", "shared_owner")


def apply(out, menu, inputs, donor):
    config = inputs["shared_diagnostic"]
    require(set(config) == {"nonces", "source_pins", "twin_inventory_sha256"}
            and set(config["nonces"]) == {"control", "mtp"}
            and len(set(config["nonces"].values())) == 2
            and all(type(n) is str and len(n) == 16 and all(c in "0123456789abcdef" for c in n)
                    for n in config["nonces"].values()), "fixed shared diagnostic inputs")
    require(inputs.get("kind", "serving") == "serving" and inputs.get("source_family", "extend_attention") == "extend_attention"
            and out["extend_attention_fwd_candidate.py"] == out["extend_attention_fwd_baseline.py"],
            "control-only unchanged selected C4 source")
    source = Path(__file__).resolve().parent
    modules = {name: (source / (name + ".py")).read_bytes() for name in MODULES}
    for name, raw in modules.items(): out[name + ".py"] = raw
    plans = []
    for group in menu["groups"]:
        warmup = group["kind"] == "warmup"
        case, repetition = (group["id"], 0) if warmup else (group["id"].rsplit("-r", 1)[0], int(group["id"].rsplit("-r", 1)[1]))
        for ordinal in group["ordinals"]:
            row = menu["bound"][ordinal]
            plans.append(dict(ordinal=ordinal, rid=row["body"]["rid"], cache_salt=row["body"]["cache_salt"],
                input_tokens=row["input_tokens"], input_ids_sha256=row["expected"]["input_ids_sha256"],
                body_sha256=row["body_sha256"], case=case, repetition=repetition, concurrency=group["concurrency"]))
    require([p["ordinal"] for p in plans] == list(range(30)), "fixed shared menu order")
    entry = (source / "shared_entry.py").read_text()
    entry = entry.replace("MODULE_PINS = None", "MODULE_PINS = " + repr({n: digest(r) for n, r in modules.items()}))
    entry = entry.replace("CONFIG = None", "CONFIG = " + repr(dict(menu=plans, nonces=config["nonces"])))
    out["shared_entry.py"] = entry.encode()
    inventory_path = donor.c.R2 / "profiling-smoke-revision-20261101-v2/build-evidence/TWIN-INVENTORY.json"
    inventory = checked_bytes(inventory_path, config["twin_inventory_sha256"], 2 << 20)
    twin = json.loads(inventory)
    require(len(twin["torch_files"]) == 12189, "pinned profiling twin inventory")
    out["PROFILING-TWIN.json"] = inventory
    require(set(config["source_pins"]) == {"scheduler", "stage", "nvtx"}, "installed callable pins")
    paths = {"scheduler": "managers/scheduler.py", "stage": "observability/scheduler_stage_metrics.py", "nvtx": "utils/nvtx_utils.py"}
    for arm in ("control", "mtp"):
        name = "HTTP-BINDING-" + arm + ".json"; binding = json.loads(out[name])
        sources = binding["binding_template"]["sources"]
        require(sources["scheduler"]["sha256"] == config["source_pins"]["scheduler"], "installed Scheduler pin")
        for key in ("stage", "nvtx"):
            sources["shared_" + key] = dict(path="/mnt/composition/image-source/python/sglang/srt/" + paths[key],
                sha256=config["source_pins"][key], module="sglang.srt." + paths[key][:-3].replace("/", "."))
        sources["shared_entry"] = dict(path="/mnt/review/shared_entry.py", sha256=digest(out["shared_entry.py"]), module="shared_entry")
        out[name] = canonical(binding)

    def edit(name, before, after, count=1):
        text = out[name].decode()
        require(text.count(before) == count, "shared derivation drift: " + name + ": " + before[:90])
        out[name] = text.replace(before, after).encode()

    # This helper's own pin is carried by each containing generated source.
    loader = '''\n\ndef _shared_entry():
    import importlib.util
    path = Path(__file__).with_name('shared_entry.py')
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != ENTRY_PIN:
        raise ValueError('shared entry source identity')
    spec = importlib.util.spec_from_file_location('bound_shared_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
'''.replace("ENTRY_PIN", repr(digest(out["shared_entry.py"])))
    for name in ("serving_runtime.py", "serving_integration.py", "benchmark_artifacts.py"):
        out[name] += loader.encode()
    edit("worker_entry.py", "        return target(\n",
         "        shared = _source_module(manifest['sources']['shared_entry'])\n"
         "        with shared.worker(manifest, record, target, gpu_id, _source_module):\n"
         "            return target(\n")
    # Parentheses make the original call indentation valid within the with block.
    edit("serving_runtime.py", "        if benchmark_mode:betterbench_owner.preweight_preflight(material,deadline)",
         "        _shared_entry().before_start(material)\n        if benchmark_mode:betterbench_owner.preweight_preflight(material,deadline)")
    edit("serving_runtime.py", '        holder["summary"] = client.run(gate, hashlib.sha256(raw_gate).hexdigest())\n',
         '        holder["summary"] = client.run(gate, hashlib.sha256(raw_gate).hexdigest())\n'
         '        _complete_normal_stop_summary(holder["summary"], paired_raw=(material["client_dir"]/"paired"/"RUN-SUMMARY.json").read_bytes())\n'
         '        material["shared_receipt"] = _shared_entry().parent_barrier(material, serving_client.validate_gate(gate, expected), holder["summary"], deadline)\n')
    edit("serving_runtime.py", 'parent_error: str | None,\n', 'parent_error: str | None, shared_diagnostic: Any = None,\n')
    edit("serving_runtime.py", '    _write_exclusive(output / "result.json", value)',
         '    value["shared_diagnostic"] = shared_diagnostic\n    _write_exclusive(output / "result.json", value)')
    edit("serving_runtime.py", 'client_summary=summary, native_gate=gate, parent_error=None)',
         'client_summary=summary, native_gate=gate, parent_error=None, shared_diagnostic=material["shared_receipt"])')
    edit("serving_runtime.py", '        _result(output, status="HTTP_ARM_FAILED",',
         '        retained_shared = None\n'
         '        try: retained_shared = _shared_entry().modules().failed_start(output / "shared-diagnostic")\n'
         '        except BaseException: pass  # Preserve the native error; export still enforces strict bounds.\n'
         '        _result(output, status="HTTP_ARM_FAILED",')
    edit("serving_runtime.py", 'parent_error=type(error).__name__ + ": " + str(error)[:1024])',
         'parent_error=type(error).__name__ + ": " + str(error)[:1024], shared_diagnostic=retained_shared)')
    edit("serving_integration.py", "'parent_error', 'qualification'}", "'parent_error', 'qualification', 'shared_diagnostic'}", 2)
    integration = out["serving_integration.py"].decode()
    function = next(n for n in ast.parse(integration).body if isinstance(n, ast.FunctionDef) and n.name == "_production_readback")
    original = ast.get_source_segment(integration, function)
    require(original.count("    return dict(") == 1, "one production reader return")
    edit("serving_integration.py", original, original.replace("    return dict(",
         "    shared = _shared_entry().replay(directory, binding, full_workers, result, client_root)\n    return dict("))
    edit("serving_integration.py", "instrumentation='production')", "instrumentation='production', shared_diagnostic=shared)")

    # Explicit diagnostic framing; raw-attention capture remains disabled.
    edit("model_benchmark.py", "raw_transport = serving_runtime.mtp_contract.raw_attention_capture_enabled()",
         "raw_transport = serving_runtime.mtp_contract.shared_diagnostic_enabled()")
    edit("benchmark_physical_controller.py", "http_integration._mtp_contract().raw_attention_capture_enabled())",
         "http_integration._mtp_contract().shared_diagnostic_enabled())")
    out["mtp_contract.py"] += b"\n\ndef shared_diagnostic_enabled():\n    return True\n"
    added = [n + ".py" for n in MODULES] + ["shared_entry.py", "PROFILING-TWIN.json"]
    edit("benchmark_physical_controller.py", "LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')",
         "LIVE_PACKAGE_NAMES.update(" + repr(sorted("review/" + n for n in added)) + ")\nLIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')")
    edit("benchmark_host_transport.py", "        return 81\n", "        return 89\n")
    edit("benchmark_host_transport.py", "len(members) < 81", "len(members) < 89")
    # Retained profile tool environment and full twin inventory remain source-bound.
    old_profile = checked_bytes(donor.c.R2 / "profiling-c4-serving-preparation-20261101-v6/serving_profile.py",
                                inputs["profile_source_sha256"], 1 << 20).decode()
    env = {}
    exec(compile(old_profile[:old_profile.index("\ndef canonical(")], "pinned-profile-env", "exec"), env)
    env_args = [v for pair in env["TOOL_ENV"].items() for v in ("--setenv", *pair)]
    mounts = [v for rank in (0, 1) for v in ("--size", str(32 << 20), "--tmpfs", f"/mnt/shared-trace-raw-rank-{rank}")]
    edit("benchmark_owner_adapter.py", "                before_toolchain={'ROCM_HOME':",
         "                projected[position:position] = " + repr(["--ro-bind", twin["sitepackages"] + "/torch",
            "/usr/local/lib/python3.14/dist-packages/torch"] + env_args + mounts) + "\n"
         "                position=projected.index('/mnt/work/build-venv/bin/python3')\n"
         "                before_toolchain={'ROCM_HOME':")
    edit("benchmark_owner_adapter.py", "projected[output_index-2]=str(int(projected[output_index-2])-stack['scratch_transfer_bytes'])",
         "projected[output_index-2]=str(int(projected[output_index-2])-stack['scratch_transfer_bytes']-(64 << 20))\n"
         "            _need(int(projected[output_index-2]) > 0, 'shared raw mounts charged to existing scratch')")
    edit("benchmark_physical_controller.py", "        stack=mtp_contract.serving_stack();guard=_adjacent('serving_stack_guard','benchmark_gpu_identity.py')",
         "        twin = json.loads(_canonical(__file__).parent.joinpath('PROFILING-TWIN.json').read_bytes())\n"
         "        stamps.update(_tree(_canonical(twin['sitepackages']) / 'torch', {n: r['sha256'] for n, r in twin['torch_files'].items()}, deadline, clock))\n"
         "        stack=mtp_contract.serving_stack();guard=_adjacent('serving_stack_guard','benchmark_gpu_identity.py')")
    _transport(out, edit)
    return dict(control_only=True, menu=plans, added_members=added, gpu_authority=False,
                session_bound_bytes=350748672, live_48_layer_hooks=False)


def _transport(out, edit):
    # One exact vocabulary. It includes optional chunks/windows for failed starts;
    # the semantic reader separately requires the exact successful member set.
    import shared_owner as owner
    from shared_trace_io import WINDOWS
    names = ["SHARED-HTTP-START.json"]
    for rank in (0, 1):
        names += [f"OBS-rank-{rank}.json", f"CAPTURE-rank-{rank}.json",
                  f"SHARED-CLOSE-rank-{rank}.json", f"SHARED-DEVICE-rank-{rank}.json"]
        names += [f"OBS-rank-{rank}-chunk-{i:03d}.jsonl" for i in range(4)]
        names += [f"TRACE-rank-{rank}-{w}{s}" for w in WINDOWS for s in (".attempt.json", ".export.json", ".json.gz")]
    require(len(names) == 59, "fixed shared frame vocabulary")
    limits = {"shared-diagnostic/" + n: owner._classify(n)[2] for n in names}
    text = out["benchmark_artifacts.py"].decode()
    begin = text.index("RAW_TRANSPORT ="); end = text.index("# Count both stdio streams", begin)
    replacement = "RAW_TRANSPORT = 'shared-diagnostic-frames-v1'\nRAW_PREFIX = b'SPRY_RAW_FRAME_V1 '\nRAW_LOG_LIMIT_BYTES = 16 << 20\n"
    replacement += "RAW_MEMBER_LIMITS = dict(HTTP_MEMBER_LIMITS, **" + repr(limits) + ")\n"
    replacement += "RAW_PAYLOAD_LIMIT = sum(HTTP_MEMBER_LIMITS.values()) + (15 << 20)\nRAW_WIRE_LIMIT = RAW_PAYLOAD_LIMIT + RAW_LOG_LIMIT_BYTES + 65536\n"
    replacement += "RAW_SESSION_LIMIT = 2 * (RAW_PAYLOAD_LIMIT + 2 * RAW_LOG_LIMIT_BYTES + (29 << 20)) + 92798976\nassert RAW_SESSION_LIMIT == 350748672 < (512 << 20)\n"
    out["benchmark_artifacts.py"] = (text[:begin] + replacement + text[end:]).encode()
    text = out["benchmark_artifacts.py"].decode(); start = text.index("    for rank in (0, 1):", text.index("def export_raw_frames")); end = text.index("    for index, (name, limit)", start)
    out["benchmark_artifacts.py"] = (text[:start] + "    shared_root = root / 'shared-diagnostic'\n    if shared_root.exists(): _shared_entry().modules().collect(shared_root)\n" + text[end:]).encode()
    edit("benchmark_artifacts.py", "        for rank in (0, 1):\n            (self.destination / f'raw-rank-{rank}').mkdir(mode=0o700)",
         "        (self.destination / 'shared-diagnostic').mkdir(mode=0o700)\n        self.shared_bytes = 0\n        self.shared_partitions = {}")
    edit("benchmark_artifacts.py", "        self.header = header\n", "        if name.startswith('shared-diagnostic/'):\n"
         "            owner = _shared_entry().modules()\n"
         "            rank, kind, _ = owner._classify(name.split('/', 1)[1])\n"
         "            self.shared_bytes += header['bytes']\n"
         "            need(self.shared_bytes <= owner.START_BYTES, 'shared received total')\n"
         "            for r in ((0, 1) if rank is None else (rank,)):\n"
         "                key = (r, kind); self.shared_partitions[key] = self.shared_partitions.get(key, 0) + header['bytes']\n"
         "                need(self.shared_partitions[key] <= {'journal': owner.core.RANK_BYTES, 'other': owner.OTHER_BYTES, 'trace': owner.RANK_TRACE_LIMIT}[kind], 'shared received partition')\n"
         "        self.header = header\n")
    edit("benchmark_artifacts.py", "        required = [name for name in self.names if not name.endswith('/INCOMPLETE.json')]", "        required = list(HTTP_MEMBER_LIMITS)")
    # Missing diagnostic members remain explicit until the independent semantic
    # reader checks the start type. A complete frame stream proves only transport.
    for name, raw in out.items():
        if name.endswith(".py"): compile(raw, name, "exec", dont_inherit=True)
