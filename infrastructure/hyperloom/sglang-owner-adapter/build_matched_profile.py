"""Opt-in derivation of the matched candidate/control profile on the bundle comparison.

Called by build_owner only after build_bundle derived the reviewed three-member cap16
comparison with the reversed confirmation order. It composes the existing shared
diagnostic derivation (build_shared) on top of that output instead of the control-only
C4 donor: the shared scope nests inside the bundle scope in worker_entry, both starts
are profiled, and a capture-time graph-inventory producer is installed before the
target builds graphs. Every limit is derived from the bundle's already-declared
raises plus the unchanged shared start bound; nothing is silently raised.
The generated payload still requires review, native proof and an exact owner decision.
"""
import ast
import json
from pathlib import Path

from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, require

MODULES = ("shared_diagnostic", "shared_trace_io", "shared_runtime_binding",
           "shared_capture", "shared_readout", "shared_owner", "graph_inventory")
ORDER = ["candidate", "control"]
TWIN_TORCH_VERSION = "2.14.0+git2e66fe81.rocm10.2.0"
TWIN_GRAPHS_SHA256 = "2ce9b12b17d39bba15e4f36dac0a6f2153f8ed40dd6464d0e188053732b7dbdf"
INVENTORY_NAMES = tuple(f"GRAPH-INVENTORY-rank-{rank}.json" for rank in (0, 1))
# Unchanged inherited ceilings (op217 shared derivation and op227 bundle derivation).
SHARED_START_BYTES = 15 << 20
OWNER_PARTITIONS = 92798976          # 88.5 MiB owner/source/observer/closeout partitions
SESSION_CAP = 512 << 20


def apply(out, menu, inputs, donor, bundle):
    config = inputs["matched_profile"]
    require(set(config) == {"nonces", "source_pins", "twin_inventory_sha256", "profile_source_sha256", "abi_manifest"}
            and set(config["nonces"]) == {"control", "mtp"} and len(set(config["nonces"].values())) == 2
            and all(type(n) is str and len(n) == 16 and all(c in "0123456789abcdef" for c in n)
                    for n in config["nonces"].values()), "fixed matched profile inputs")
    require("shared_diagnostic" not in inputs and inputs.get("source_family") == "four_member"
            and bundle.get("comparison", {}).get("kind") == "three_member_cap16_serving"
            and bundle.get("confirmation", {}).get("execution_order") == ["mtp", "control"]
            and bundle.get("candidate_verification_cap") == 16 and bundle.get("ordinary_decode_cap") == 16,
            "matched profile composes only the reversed three-member cap16 comparison")
    require(set(config["abi_manifest"]) == {"path", "sha256"}, "pinned HIP graph ABI manifest")
    abi_raw = checked_bytes(Path(config["abi_manifest"]["path"]), config["abi_manifest"]["sha256"], 64 << 10)
    abi = json.loads(abi_raw)
    require(type(abi) is dict and abi.get("schema") == 1 and abi.get("status") in ("QUALIFIED", "UNQUALIFIED"),
            "HIP graph ABI manifest schema")
    gpu_admissible = abi["status"] == "QUALIFIED"
    require(gpu_admissible or inputs.get("inert_preview") is True,
            "an unqualified HIP graph ABI is accepted only for an inert preview build")
    source = Path(__file__).resolve().parent
    modules = {name: (source / (name + ".py")).read_bytes() for name in MODULES}
    for name, raw in modules.items():
        require(name + ".py" not in out, "matched module collides with an existing member: " + name)
        out[name + ".py"] = raw
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
    abi_value = json.loads(abi_raw)
    inventory_config = dict(abi=abi_value, abi_sha256=digest(canonical(abi_value)),
                            producer_sha256=digest(modules["graph_inventory"]),
                            twin_graphs_sha256=TWIN_GRAPHS_SHA256, torch_version=TWIN_TORCH_VERSION)
    entry = (source / "matched_entry.py").read_text()
    require(entry.count("MODULE_PINS = None") == 1 and entry.count("CONFIG = None") == 1, "one entry substitution each")
    entry = entry.replace("MODULE_PINS = None", "MODULE_PINS = " + repr({n: digest(r) for n, r in modules.items()}))
    entry = entry.replace("CONFIG = None", "CONFIG = " + repr(dict(menu=plans, nonces=config["nonces"], order=ORDER,
                                                                    graph_inventory=inventory_config)))
    require("shared_entry.py" not in out, "one shared entry")
    out["shared_entry.py"] = entry.encode()
    inventory_path = donor.c.R2 / "profiling-smoke-revision-20261101-v2/build-evidence/TWIN-INVENTORY.json"
    inventory = checked_bytes(inventory_path, config["twin_inventory_sha256"], 2 << 20)
    twin = json.loads(inventory)
    require(len(twin["torch_files"]) == 12189, "pinned profiling twin inventory")
    require(twin["torch_files"].get("cuda/graphs.py", {}).get("sha256") == TWIN_GRAPHS_SHA256,
            "twin graph lifecycle source pinned in the inventory")
    out["PROFILING-TWIN.json"] = inventory
    require(set(config["source_pins"]) == {"scheduler", "stage", "nvtx"}, "installed callable pins")
    require({k: config["source_pins"][k] for k in ("stage", "nvtx")} == inputs["four_member"]["scheduler_pins"],
            "shared and bundle scheduler decorator pins agree")
    paths = {"stage": "observability/scheduler_stage_metrics.py", "nvtx": "utils/nvtx_utils.py"}
    for arm in ("control", "mtp"):
        name = "HTTP-BINDING-" + arm + ".json"; binding = json.loads(out[name])
        sources = binding["binding_template"]["sources"]
        require(sources["scheduler"]["sha256"] == config["source_pins"]["scheduler"], "installed Scheduler pin")
        require(sources["backend"]["module"] == "sglang.srt.model_executor.runner_backend.full_cuda_graph_backend",
                "graph backend descriptor for capture identity")
        for key in ("stage", "nvtx"):
            require(sources["bundle_" + key]["sha256"] == config["source_pins"][key], "bundle decorator descriptor")
            sources["shared_" + key] = dict(path="/mnt/composition/image-source/python/sglang/srt/" + paths[key],
                sha256=config["source_pins"][key], module="sglang.srt." + paths[key][:-3].replace("/", "."))
        sources["shared_entry"] = dict(path="/mnt/review/shared_entry.py", sha256=digest(out["shared_entry.py"]), module="shared_entry")
        # The shared observer authenticates the bundle hook it nests inside (G6).
        sources["shared_bundle_hooks"] = dict(path="/mnt/review/bundle_hooks.py", sha256=digest(out["bundle_hooks.py"]),
                                              module="bundle_hooks")
        out[name] = canonical(binding)

    def edit(name, before, after, count=1):
        text = out[name].decode()
        require(text.count(before) == count, "matched derivation drift: " + name + ": " + before[:90])
        out[name] = text.replace(before, after).encode()

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
        require(b"def _shared_entry():" not in out[name], "one shared entry loader")
        out[name] += loader.encode()
    # Shared scope nests inside the bundle scope around the single target call.
    worker = out["worker_entry.py"].decode()
    require(worker.count("        bundle = _source_module(manifest['sources']['bundle_entry'])\n") == 1
            and worker.count("return target(") == 1, "one bundle-scoped target invocation")
    edit("worker_entry.py", "\n            return target(\n",
         "\n            shared = _source_module(manifest['sources']['shared_entry'])\n"
         "            with shared.worker(manifest, record, target, gpu_id, _source_module):\n"
         "                return target(\n")
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
    edit("model_benchmark.py", "raw_transport = serving_runtime.mtp_contract.raw_attention_capture_enabled()",
         "raw_transport = serving_runtime.mtp_contract.shared_diagnostic_enabled()")
    edit("benchmark_physical_controller.py", "http_integration._mtp_contract().raw_attention_capture_enabled())",
         "http_integration._mtp_contract().shared_diagnostic_enabled())")
    require(b"def shared_diagnostic_enabled" not in out["mtp_contract.py"], "one diagnostic switch")
    out["mtp_contract.py"] += b"\n\ndef shared_diagnostic_enabled():\n    return True\n"
    added = [n + ".py" for n in MODULES] + ["shared_entry.py", "PROFILING-TWIN.json"]
    edit("benchmark_physical_controller.py", "LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')",
         "LIVE_PACKAGE_NAMES.update(" + repr(sorted("review/" + n for n in added)) + ")\nLIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')")
    bundle_members = 81 + len(bundle["additional_package_members"])
    members = bundle_members + len(added)
    edit("benchmark_host_transport.py", "        return %d\n" % bundle_members, "        return %d\n" % members)
    edit("benchmark_host_transport.py", "len(members) < %d" % bundle_members, "len(members) < %d" % members)
    old_profile = checked_bytes(donor.c.R2 / "profiling-c4-serving-preparation-20261101-v6/serving_profile.py",
                                config["profile_source_sha256"], 1 << 20).decode()
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
    session = _transport(out, edit)
    for name, raw in out.items():
        if name.endswith(".py"): compile(raw, name, "exec", dont_inherit=True)
    return dict(kind="matched_candidate_control_profile", roles=dict(candidate="mtp", control="control"),
                order=ORDER, both_starts_profiled=True, menu=plans, added_members=added,
                package_members=members, gpu_authority=False, gpu_admissible=gpu_admissible,
                abi_status=abi["status"], abi_manifest_sha256=config["abi_manifest"]["sha256"],
                graph_inventory_producer_sha256=digest(modules["graph_inventory"]),
                session_bound_bytes=session["raw_session_limit"], transport=session,
                instrumented_latency_diagnostic_only=True, selection_input=False)


def _transport(out, edit):
    """One exact frame vocabulary plus the two inventory files; derived session bound."""
    import shared_owner as owner
    from shared_trace_io import WINDOWS
    names = ["SHARED-HTTP-START.json"]
    for rank in (0, 1):
        names += [f"OBS-rank-{rank}.json", f"CAPTURE-rank-{rank}.json",
                  f"SHARED-CLOSE-rank-{rank}.json", f"SHARED-DEVICE-rank-{rank}.json",
                  f"GRAPH-INVENTORY-rank-{rank}.json"]
        names += [f"OBS-rank-{rank}-chunk-{i:03d}.jsonl" for i in range(4)]
        names += [f"TRACE-rank-{rank}-{w}{s}" for w in WINDOWS for s in (".attempt.json", ".export.json", ".json.gz")]
    require(len(names) == 61 and len(names) <= owner.MAX_MEMBERS, "fixed matched frame vocabulary")
    limits = {"shared-diagnostic/" + n: owner._classify(n)[2] for n in names}
    text = out["benchmark_artifacts.py"].decode()
    # The bundle raised the native tree partition to 5 MiB; carry that same raise here.
    require(text.count("(24 << 20) + (5 << 20) + (1 << 20)) +") == 1, "bundle native-tree partition")
    begin = text.index("RAW_TRANSPORT ="); end = text.index("# Count both stdio streams", begin)
    replacement = "RAW_TRANSPORT = 'shared-diagnostic-frames-v1'\nRAW_PREFIX = b'SPRY_RAW_FRAME_V1 '\nRAW_LOG_LIMIT_BYTES = 16 << 20\n"
    replacement += "RAW_MEMBER_LIMITS = dict(HTTP_MEMBER_LIMITS, **" + repr(limits) + ")\n"
    replacement += "RAW_PAYLOAD_LIMIT = sum(HTTP_MEMBER_LIMITS.values()) + (15 << 20)\nRAW_WIRE_LIMIT = RAW_PAYLOAD_LIMIT + RAW_LOG_LIMIT_BYTES + 65536\n"
    replacement += ("RAW_SESSION_LIMIT = 2 * (RAW_PAYLOAD_LIMIT + 2 * RAW_LOG_LIMIT_BYTES + (30 << 20)) + %d\n"
                    "assert RAW_SESSION_LIMIT < (512 << 20)\n" % OWNER_PARTITIONS)
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
         "                need(self.shared_partitions[key] <= {'journal': owner.core.RANK_BYTES, 'other': owner.OTHER_BYTES, 'trace': owner.RANK_TRACE_LIMIT, 'inventory': owner.INVENTORY_BYTES}[kind], 'shared received partition')\n"
         "        self.header = header\n")
    edit("benchmark_artifacts.py", "        required = [name for name in self.names if not name.endswith('/INCOMPLETE.json')]", "        required = list(HTTP_MEMBER_LIMITS)")
    space = {"__name__": "_matched_profile_bounds"}
    exec(compile(out["benchmark_artifacts.py"], "benchmark_artifacts.py", "exec", dont_inherit=True), space)
    row = dict(raw_payload_limit=space["RAW_PAYLOAD_LIMIT"], raw_wire_limit=space["RAW_WIRE_LIMIT"],
               raw_session_limit=space["RAW_SESSION_LIMIT"], http_session_max_bytes=space["HTTP_SESSION_MAX_BYTES"],
               http_member_limits_sum=sum(space["HTTP_MEMBER_LIMITS"].values()), shared_start_bytes=owner.START_BYTES,
               frame_names=len(names), shared_member_limits_sum=sum(limits.values()),
               inventory_bytes_per_rank=owner.INVENTORY_BYTES)
    require(row["raw_payload_limit"] == row["http_member_limits_sum"] + SHARED_START_BYTES
            and owner.START_BYTES == SHARED_START_BYTES and row["raw_session_limit"] < SESSION_CAP
            and row["http_session_max_bytes"] < SESSION_CAP, "derived matched transport bounds")
    return row
