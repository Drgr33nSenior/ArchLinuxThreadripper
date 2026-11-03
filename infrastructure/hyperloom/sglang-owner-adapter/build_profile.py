"""Add the existing Kineto twin route with explicit concurrent-pair coverage."""

import ast
import json
from pathlib import Path

from hyperloom_exchange import canonical, digest
from hyperloom_sglang import checked_bytes, require


def apply(out, menu, inputs, donor):
    source = donor.c.R2 / "profiling-c4-serving-preparation-20261101-v6/serving_profile.py"
    profile = checked_bytes(source, inputs["profile_source_sha256"], 1 << 20).decode()
    inventory_path = donor.c.R2 / "profiling-smoke-revision-20261101-v2/build-evidence/TWIN-INVENTORY.json"
    inventory = checked_bytes(inventory_path, inputs["twin_inventory_sha256"], 2 << 20)
    twin = json.loads(inventory)
    require(len(twin["torch_files"]) == 12189, "pinned profiling twin inventory required")
    requests = [dict(rid=row["body"]["rid"], cache_salt=row["body"]["cache_salt"],
                     input_tokens=row["input_tokens"], input_ids_sha256=row["expected"]["input_ids_sha256"],
                     body_sha256=row["body_sha256"]) for row in menu["bound"][3:]]
    require(len(requests) == 2 and sum(r["input_tokens"] for r in requests) == 119973,
            "frozen long C2 request pair required")
    request = {"scope": "concurrent-request-pair", "requests": requests}

    def replace(text, before, after, count=1):
        require(text.count(before) == count, "profiling derivation drift: " + before[:75])
        return text.replace(before, after)

    profile = replace(profile, "PREFILL_INDICES = (0, 29, 58)", "PREFILL_INDICES = (0, 1, 2)")
    profile = replace(profile,
        "            captures=[], errors=[], numerical_acceptance=False, timing_claim=False)\n        write_report",
        "            captures=[], errors=[], numerical_acceptance=False, timing_claim=False)\n"
        "        c2_initialize(self)\n        write_report")
    capture = next(n for n in ast.parse(profile).body if isinstance(n, ast.ClassDef) and n.name == "Capture")
    select = next(n for n in capture.body if isinstance(n, ast.FunctionDef) and n.name == "select")
    profile = replace(profile, ast.get_source_segment(profile, select),
                      "def select(self, batch):\n        return c2_select(self, batch, REQUEST)")
    profile = replace(profile, "row = dict(phase=phase, index=index, file=name, status='INCOMPLETE')",
                      "row = dict(phase=phase, index=index, file=name, status='INCOMPLETE', batch=self.c2_selected_batch)")
    profile = replace(profile,
        "                           and row['prefill_batches'] == 59 and row['prefill_tokens'] == 119990\n"
        "                           and 4 <= row['decode_batches'] <= 4096)",
        "                           and c2_complete(row, REQUEST))")
    profile = replace(profile,
        "    return dict(status='COMPLETE' if all(r['complete'] for r in reports) else 'INCOMPLETE', ranks=reports)",
        "    if all(r['complete'] for r in reports):\n"
        "        need(len(reports) == 2 and reports[0]['batch_coverage'] == reports[1]['batch_coverage']\n"
        "             and reports[0]['request_progress'] == reports[1]['request_progress'],\n"
        "             'both TP ranks must describe the same captured request batches')\n"
        "    return dict(status='COMPLETE' if all(r['complete'] for r in reports) else 'INCOMPLETE', ranks=reports)")
    helper = Path(__file__).with_name("profile_c2.py").read_text()
    profile += "\n\n" + helper
    profile += "\nTWIN_ROOT=" + repr(twin["sitepackages"]) + "\nTWIN_SHA256=" + repr(digest(inventory))
    profile += "\nREQUEST=" + repr(request) + "\n"
    compile(profile, "serving_profile.py", "exec", dont_inherit=True)
    raw_profile = profile.encode()
    out["serving_profile.py"], out["PROFILING-TWIN.json"] = raw_profile, inventory

    def edit(name, before, after, count=1):
        out[name] = replace(out[name].decode(), before, after, count).encode()

    for arm in ("control", "mtp"):
        name = "HTTP-BINDING-" + arm + ".json"
        binding = json.loads(out[name])
        binding["binding_template"]["sources"]["serving_profile"] = dict(
            path="/mnt/review/serving_profile.py", sha256=digest(raw_profile), module="serving_profile")
        out[name] = canonical(binding)
    edit("worker_entry.py", "    restore_mxfp4 = None\n", "    restore_mxfp4 = None\n    restore_profile = None\n")
    edit("worker_entry.py", "        return target(\n",
         "        profile = _source_module(manifest['sources']['serving_profile'])\n"
         "        restore_profile = profile.install(manifest, record, target)\n        return target(\n")
    edit("worker_entry.py", "        actions = ([adapter.restore] if adapter is not None else [])",
         "        actions = ([restore_profile] if restore_profile is not None else [])\n"
         "        actions += ([adapter.restore] if adapter is not None else [])")
    edit("benchmark_physical_controller.py", "LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')",
         "LIVE_PACKAGE_NAMES.update({'review/serving_profile.py', 'review/PROFILING-TWIN.json'})\n"
         "LIVE_PACKAGE_NAMES.add('review/DRAFT-MODEL.json')")
    edit("benchmark_physical_controller.py",
         "        stack=mtp_contract.serving_stack();guard=_adjacent('serving_stack_guard','benchmark_gpu_identity.py')",
         "        profile=_adjacent('bound_serving_profile','serving_profile.py')\n"
         "        stamps.update(_tree(_canonical(profile.TWIN_ROOT) / 'torch', profile.runtime_inventory(), deadline, clock))\n"
         "        stack=mtp_contract.serving_stack();guard=_adjacent('serving_stack_guard','benchmark_gpu_identity.py')")
    config = {}
    # Evaluate the pinned, standard-library-only loader configuration without
    # importing the generated capture module or any framework.
    exec(compile(profile[:profile.index("\ndef canonical(")], "profile-environment", "exec"), config)
    env_args = [item for key, value in config["TOOL_ENV"].items() for item in ("--setenv", key, value)]
    edit("benchmark_owner_adapter.py", "                before_toolchain={'ROCM_HOME':",
         "                if arm == 'kv4':\n"
         "                    projected[position:position] = ['--ro-bind', " + repr(twin["sitepackages"] + "/torch") +
         ", '/usr/local/lib/python3.14/dist-packages/torch'] + " + repr(env_args) + "\n"
         "                    position=projected.index('/mnt/work/build-venv/bin/python3')\n"
         "                before_toolchain={'ROCM_HOME':")
    edit("benchmark_host_transport.py", "        return 81\n",
         "        need(expected.get('review/serving_profile.py') == " + repr(digest(raw_profile)) +
         " and sha(members.get('review/serving_profile.py', b'')) == " + repr(digest(raw_profile)) +
         ", 'exact C2 profiling source')\n"
         "        need(expected.get('review/PROFILING-TWIN.json') == " + repr(digest(inventory)) +
         " and sha(members.get('review/PROFILING-TWIN.json', b'')) == " + repr(digest(inventory)) +
         ", 'exact profiling twin inventory')\n        return 83\n")
    edit("benchmark_host_transport.py", "len(members) < 81", "len(members) < 83")
    # Retain the already-tested profiling redistribution of the same session
    # ceiling; the five-request diagnostic fits the smaller client allocation.
    for name, old, new in (
        ("benchmark_artifacts.py", "'http-client-bundle.json': 33 << 20", "'http-client-bundle.json': 20 << 20"),
        ("benchmark_artifacts.py", "'native-http-bundle.json': 6 << 20", "'native-http-bundle.json': 17 << 20"),
        ("benchmark_artifacts.py", "(24 << 20) + (4 << 20) + (1 << 20)", "(14 << 20) + (12 << 20) + (1 << 20)"),
        ("benchmark_artifacts.py", "files=_http_tree(output/'client', 24 << 20, files=512)", "files=_http_tree(output/'client', 14 << 20, files=512)"),
        ("benchmark_artifacts.py", "native = _http_tree(output/'native', 4 << 20, files=256)", "native = _http_tree(output/'native', 12 << 20, files=256)"),
    ):
        edit(name, old, new)
    edit("serving_integration.py",
         "    _need(set(observations) == PRODUCTION_OBSERVATIONS, 'production native observations are the readiness callback and startup bindings only')",
         "    profile_module = _load_serving_profile()\n"
         "    profile_names = {n for n in observations if n.startswith('PROFILE-')}\n"
         "    _need(set(observations) - profile_names == PRODUCTION_OBSERVATIONS, 'exact production observations plus bound profiles')\n"
         "    try:\n"
         "        profile_result = profile_module.validate_observations(observations, full_workers, expected)\n"
         "    except (ValueError, KeyError, TypeError) as error:\n"
         "        profile_result = dict(status='INVALID', error_type=type(error).__name__)\n")
    out["serving_integration.py"] += b"\n\ndef _load_serving_profile():\n    path = Path(__file__).with_name('serving_profile.py')\n    spec = importlib.util.spec_from_file_location('read_serving_profile',path)\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    return module\n"
    edit("serving_integration.py",
         "result_sha256=hashlib.sha256(result_raw).hexdigest(), instrumentation='production')",
         "result_sha256=hashlib.sha256(result_raw).hexdigest(), instrumentation='production', profile=profile_result)")
    return request
