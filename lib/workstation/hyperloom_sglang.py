"""Import retained native HTTP groups into Hyperloom without launching a server.

The native owner remains responsible for execution, quality and release. This
first bridge is diagnostic-only; importing a timing never qualifies a model.
"""

import base64
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import statistics


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked_bytes(path, expected, maximum=16 * 1024 * 1024):
    require(isinstance(expected, str) and len(expected) == 64
            and all(c in "0123456789abcdef" for c in expected), "explicit SHA256 required")
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
            "evidence must be a bounded regular file")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum and hashlib.sha256(raw).hexdigest() == expected, "evidence identity changed")
    return raw


def decoded_json(raw):

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite JSON constant: " + value)

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def checked_json(path, expected, maximum=16 * 1024 * 1024):
    return decoded_json(checked_bytes(path, expected, maximum))


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def measurements(summary):
    """Use native IDs/usage and group walls; never infer decode from chunks."""
    require(summary.get("schema") == 1 and summary.get("mode") == "native-http-fp8-sprint01-v1",
            "unsupported native summary contract")
    require(summary.get("stopped_early") is False and summary.get("paired_error") is None,
            "incomplete native run; retain evidence without importing timings")
    rows = summary.get("rows")
    groups = summary.get("groups")
    require(isinstance(rows, list) and isinstance(groups, list) and 0 < len(rows) <= 256
            and 0 < len(groups) <= 128, "bounded native rows/groups required")
    by_ordinal = {}
    for row in rows:
        ordinal = row.get("ordinal")
        require(type(ordinal) is int and ordinal >= 0 and ordinal not in by_ordinal, "duplicate/invalid ordinal")
        by_ordinal[ordinal] = row
    require(summary.get("executed_case_count") == summary.get("frozen_case_count") == len(rows),
            "native request accounting incomplete")
    result, used, group_names = [], set(), set()
    for group in groups:
        name = group.get("group_id")
        require(isinstance(name, str) and name and name not in group_names, "duplicate/invalid group")
        group_names.add(name)
        if group.get("measured") is not True or group.get("kind") != "timing":
            continue
        require(group.get("excluded_warmup") is False, "warmup cannot be measured")
        ordinals = group.get("ordinals")
        require(isinstance(ordinals, list) and ordinals and all(type(i) is int and i in by_ordinal for i in ordinals)
                and len(set(ordinals)) == len(ordinals) and not used.intersection(ordinals), "invalid/reused timing ordinals")
        used.update(ordinals)
        wall = group.get("makespan_seconds")
        start, end = group.get("prepared_monotonic"), group.get("received_monotonic")
        require(positive(wall) and positive(start) and positive(end)
                and math.isclose(end - start, wall, rel_tol=1e-8, abs_tol=1e-6), "invalid timing window")
        concurrency = group.get("concurrency")
        require(type(concurrency) is int and 1 <= concurrency <= len(ordinals), "invalid concurrency")
        output_tokens, input_tokens, latencies = 0, 0, []
        for ordinal in ordinals:
            row = by_ordinal[ordinal]
            contract, wire = row.get("contract", {}), row.get("wire", {})
            require(wire.get("status") == 200 and wire.get("primary_failure") is None
                    and wire.get("response_truncated") is False, "failed/truncated timing response")
            usage = contract.get("usage", {})
            for field, ids in (("completion_tokens", "response_token_ids"), ("prompt_tokens", "prompt_token_ids")):
                count, tokens = usage.get(field), contract.get(ids)
                require(type(count) is int and count > 0 and isinstance(tokens, list) and len(tokens) == count
                        and all(type(i) is int and i >= 0 for i in tokens), "native token count/IDs disagree")
            require(contract.get("id_count_equals_reported_count") is True, "native token agreement missing")
            latency = contract.get("client_elapsed_seconds")
            require(positive(latency) and latency <= wall + 1e-6, "invalid request latency")
            latencies.append(latency)
            output_tokens += usage["completion_tokens"]
            input_tokens += usage["prompt_tokens"]
        require(group.get("completion_tokens") == output_tokens, "group output count disagrees")
        rate = group.get("combined_tp2_tokens_per_second")
        require(positive(rate) and math.isclose(rate, output_tokens / wall, rel_tol=1e-8), "group throughput disagrees")
        result.append({"group_id": name, "concurrency": concurrency, "ordinals": ordinals,
                       "duration": wall, "completed": len(ordinals), "total_input_tokens": input_tokens,
                       "total_output_tokens": output_tokens, "output_throughput": output_tokens / wall,
                       "request_throughput": len(ordinals) / wall,
                       "total_token_throughput": (input_tokens + output_tokens) / wall,
                       "mean_e2el_ms": statistics.mean(latencies) * 1000,
                       "median_e2el_ms": statistics.median(latencies) * 1000})
    require(result, "no complete native timing groups")
    return result


def fixed_c1_projection(summary, settings, intents):
    """Project the existing fixed C1 format using its actual request clocks.

    Only the owner-bound path calls this, after authenticating the untouched
    summary, frozen settings, intent files and enclosing native bundle.
    """
    protocol = settings.get("performance_benchmark", {})
    require(protocol.get("mode") == "m2047-fixed1024-c1-v1"
            and protocol.get("warmup_requests") == 1 and protocol.get("measured_requests") == 3
            and len(settings.get("requests", [])) == len(summary.get("rows", [])) == len(intents) == 4
            and "groups" not in summary and "c2_clock" not in summary,
            "unsupported fixed C1 projection")
    result = copy.deepcopy(summary)
    groups = []
    previous_end = None
    for index, (request, row, intent) in enumerate(zip(settings["requests"], summary["rows"], intents)):
        case = "perf-warmup" if index == 0 else "perf-sample-" + str(index)
        require(request.get("ordinal") == row.get("ordinal") == intent.get("ordinal") == index
                and request.get("case") == row.get("case") == intent.get("case") == case
                and request.get("sha256") == row.get("body_sha256") == intent.get("body_sha256"),
                "fixed C1 intent/request identity mismatch")
        wire, contract = row["wire"], row["contract"]
        start, end = intent.get("prepared_monotonic"), wire.get("received_monotonic")
        elapsed = wire.get("client_elapsed_seconds")
        require(positive(start) and positive(end) and positive(elapsed)
                and math.isclose(end-start, elapsed, rel_tol=1e-8, abs_tol=1e-6)
                and (previous_end is None or start >= previous_end)
                and contract.get("client_elapsed_seconds") == elapsed,
                "fixed C1 clock mismatch or overlap")
        require(wire.get("status") == 200 and wire.get("response_observed") is True
                and wire.get("response_truncated") is False and wire.get("primary_failure") is None
                and contract.get("id_count_equals_reported_count") is True
                and contract["usage"].get("prompt_tokens") == len(contract["prompt_token_ids"]) == 2047
                and contract["usage"].get("completion_tokens") == len(contract["response_token_ids"]) == 1024,
                "fixed C1 response/count mismatch")
        groups.append({"group_id": case, "kind": "warmup" if index == 0 else "timing",
            "measured": index != 0, "excluded_warmup": index == 0, "ordinals": [index], "concurrency": 1,
            "prepared_monotonic": start, "received_monotonic": end, "makespan_seconds": elapsed,
            "completion_tokens": 1024, "combined_tp2_tokens_per_second": 1024 / elapsed})
        previous_end = end
    result["groups"] = groups
    return result


def fixed_c1_readout(rows, readout, responses):
    """Verify the older reader's two explicit enrichment differences."""
    require(len(rows) == len(readout) == len(responses) == 4, "fixed C1 readout coverage")
    for row, checked, raw in zip(rows, readout, responses):
        native = row["contract"]
        require(native.get("raw_http_response_sha256") == row["wire"].get("response_sha256")
                == hashlib.sha256(raw).hexdigest(), "fixed C1 raw response identity")
        decoded = decoded_json(raw)
        details = decoded.get("sglext", {}).get("spec_tokens_details")
        require(isinstance(details, dict) and checked.get("spec_tokens_details") == details,
                "fixed C1 speculative readout substitution")
        require({k:v for k,v in native.items() if k != "raw_http_response_sha256"}
                == {k:v for k,v in checked.items() if k != "spec_tokens_details"},
                "fixed C1 native/readout fields differ")


def export(profile, proof_path, proof_sha, summary_path, summary_sha, output, upstream):
    """Use the installed upstream report writer, with unavailable metrics null."""
    require(profile.get("schema") == 1 and profile.get("framework") == "sglang"
            and profile.get("standalone_launch") is False and profile.get("automatic_optimization") is False,
            "unsupported bridge configuration")
    proof = checked_json(proof_path, proof_sha)
    observed = proof.get("result", {})
    for key in ("sdk_version", "torch_version", "hip_version", "sdk_manifest_sha256"):
        require(observed.get(key) == profile[key], "unsupported/stale stack: " + key)
    summary = checked_json(summary_path, summary_sha)
    return write_reports(profile, summary, summary_sha, output, upstream, proof_sha=proof_sha)


def write_reports(profile, summary, summary_sha, output, upstream, proof_sha=None, binding=None):
    """Keep binding and model qualification separate in both import modes."""
    groups = measurements(summary)
    output = Path(output)
    require(output.is_absolute() and not output.exists() and not output.is_symlink(), "fresh absolute output required")
    output.mkdir(mode=0o700)
    index = {"schema": 1, "status": "diagnostic-import-only", "gpu_work": False,
             "provider_calls": False, "automatic_optimization": False,
             "stack_proof_sha256": proof_sha, "native_summary_sha256": summary_sha,
             "profile": profile, "model_identity_verified": False,
             "framework_source_identity_verified": False,
             "cross_run_stack_binding_verified": False,
             "native_qualification": summary.get("qualification"),
             "native_counts": summary.get("counts"),
             "native_clock": summary.get("c2_clock"),
             "excluded_groups": [{"group_id": g["group_id"], "kind": g.get("kind"),
                                  "ordinals": g.get("ordinals")}
                                 for g in summary["groups"]
                                 if g.get("measured") is not True or g.get("kind") != "timing"],
             "groups": []}
    if binding is not None:
        index.update(status="bound-owner-import", owner_run_binding_verified=True,
                     cross_run_stack_binding_verified=True, owner_binding=binding)
        if "external_quality" in binding:
            index["task_checks_passed"] = True
    # A stack proof and a summary can be authenticated independently without
    # proving they belong to the same model run. Never make this selectable.
    for ordinal, raw in enumerate(groups):
        workspace = output / f"group-{ordinal}"
        workspace.mkdir(mode=0o700)
        model = "owner-model@" + binding["model_revision"] if binding else "owner-selected; see native evidence"
        subject = upstream.ReportSubject("sglang", model, False)
        outcome = upstream.RunOutcome(True, raw_result=raw)
        report = upstream.build_report(subject, outcome, workspace_dir=str(workspace), execution_time=raw["duration"])
        report["latency"]["ttft"] = None
        report["latency"]["tpot"] = None
        report["latency"]["itl"] = None
        report["latency"]["e2el"]["p99_ms"] = None
        report["latency"]["e2el"]["std_ms"] = None
        report.update({"quality_gate": {"passed": False, "reason": "diagnostic import; native qualification unchanged"},
                       "diagnostic_only": True, "qualification": False,
                       "timing_scope": "delivered output tokens / native HTTP group wall, including prefill and instrumentation",
                       "decode_throughput": None, "energy_per_token": None,
                       "native_group": raw, "native_summary_sha256": summary_sha})
        if binding is not None:
            report["owner_binding"] = binding
            if "external_quality" in binding:
                report["task_checks_passed"] = True
                report["quality_gate"]["reason"] = "bounded task checks passed; full qualification remains incomplete"
        upstream.write_report(workspace, report)
        index["groups"].append({"group_id": raw["group_id"], "report": f"group-{ordinal}/benchmark_report.json"})
    (output / "IMPORT.json").write_text(json.dumps(index, indent=2, allow_nan=False) + "\n")
    return index


def owner_run(profile, root, seal_sha, arm):
    """Join existing sealed owner artifacts; never execute a supplied reader."""
    try:
        return _owner_run(profile, root, seal_sha, arm)
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("invalid owner artifact structure") from error


def _owner_run(profile, root, seal_sha, arm):
    require(arm in ("bf16", "kv4"), "unsupported owner arm")
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "real owner evidence directory required")
    raw_seal = checked_bytes(root / "FINAL-ARTIFACTS.sha256", seal_sha, 2 * 1024 * 1024)
    entries, consumed = {}, {}
    for line in raw_seal.decode().splitlines():
        sha, separator, name = line.partition("  ")
        parts = PurePosixPath(name).parts
        require(separator and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
                and name and not name.startswith("/") and ".." not in parts
                and str(PurePosixPath(name)) == name and name not in entries, "invalid owner seal")
        entries[name] = sha
    require(0 < len(entries) <= 10000, "owner seal size")

    def member(name):
        require(name in entries, "required owner member absent: " + name)
        target = root
        for part in PurePosixPath(name).parts:
            target = target / part
            require(not target.is_symlink(), "symlink in owner evidence")
        raw = checked_bytes(target, entries[name])
        consumed[name] = entries[name]
        return raw

    def load(name):
        return decoded_json(member(name))

    manifest = load("MANIFEST.json")
    controller = load("results/CONTROLLER-RESULT.json")
    spec_name = "review/BENCHMARK-SPEC2.json"
    spec = load(spec_name)
    session, spec_sha = manifest.get("session_sha256"), entries[spec_name]
    require(isinstance(session, str) and len(session) == 64 and manifest.get("spec_sha256") == spec_sha,
            "owner session/spec mismatch")
    for name in (spec_name, "review/SERVING-STACK.json", "review/FINAL-SETTINGS.json",
                 "review/COMMON-STACK-RECEIPT.json", "review/MODEL-INVENTORY.json"):
        require(manifest["package_files"].get(name) == entries.get(name), "prepared owner package substitution")
    require(controller.get("scope") == "matched-physical-model-benchmark-v2"
            and controller.get("status") == "MATCHED_PHYSICAL_BENCHMARK_COMPLETE"
            and controller.get("native_run") is True and controller.get("failure") is None,
            "owner execution incomplete or failed")
    receipt = controller["artifact_receipts"][arm]
    require(receipt.get("transport_valid") is True and receipt.get("all_members_present") is True,
            "owner artifact collection incomplete")
    records = {}
    for name in ("result.json", "native-http-bundle.json", "http-client-bundle.json"):
        relative = f"results/{arm}-artifacts/{name}"
        require(receipt["members"][name].get("exists") is True
                and receipt["members"][name].get("sha256") == entries.get(relative), "owner artifact substitution")
        records[name] = load(relative)
    result = records["result.json"]
    logical = result.get("logical_arm")
    require(logical in ("control", "mtp") and result.get("arm") == arm
            and result.get("status") == "HTTP_ARM_COMPLETE" and result.get("parent_error") is None,
            "owner arm incomplete or mismatched")
    reference = spec["http_serving"]["bindings"][logical]
    config_name = f"review/HTTP-BINDING-{logical}.json"
    require(reference.get("path") == config_name and reference.get("sha256") == entries.get(config_name),
            "owner configuration substitution")
    config = load(config_name)
    template = config["binding_template"]
    source_sha = result.get("source_manifest_sha256")
    require(isinstance(source_sha, str) and len(source_sha) == 64
            and template.get("source_manifest_sha256") == source_sha, "owner source identity mismatch")

    def same_run(value, expected_arm, source_required=True):
        require(value.get("session_sha256") == session and value.get("spec_sha256") == spec_sha
                and value.get("arm") == expected_arm, "cross-run or cross-arm evidence")
        if source_required or "source_manifest_sha256" in value:
            require(value.get("source_manifest_sha256") == source_sha, "cross-run source identity")

    same_run(result, arm)
    same_run(receipt["binding"], arm, source_required=False)
    http = controller["http_serving"][arm]
    same_run(http["binding"], logical)
    require(http.get("status") == "SERVING_SCREEN_COMPLETE" and http.get("wire_complete") is True,
            "native serving readout incomplete")
    gate = result["native_gate"]
    same_run(gate, logical)
    require(gate.get("post_warmup_native_ready") is True, "native readiness missing")
    # Templates legitimately have unfilled session/spec fields. Runtime records
    # above must contain the actual values; null runtime identities still fail.
    require(template.get("session_sha256") in (None, session)
            and template.get("spec_sha256") in (None, spec_sha), "foreign configuration template")
    for role, filename in (("asset-SERVING-STACK.json", "SERVING-STACK.json"),
                           ("asset-FINAL-SETTINGS.json", "FINAL-SETTINGS.json")):
        require(template["sources"][role].get("sha256") == entries.get("review/" + filename),
                "runtime/settings source substitution")
    stack = load("review/SERVING-STACK.json")["torch_candidate"]
    observed = {"torch_version": stack.get("version"), "hip_version": stack.get("hip_version"),
                "sdk_version": stack["sdk"].get("version"),
                "sdk_manifest_sha256": stack["sdk"]["manifest"].get("sha256")}
    for key, value in observed.items():
        require(value == profile[key], "unsupported/stale bound stack: " + key)
    workers = records["native-http-bundle.json"]["workers"]
    ranks = {w["rank"]: w for w in gate["workers"]}
    require(len(workers) == len(ranks) == len(gate["workers"]) == 2 and set(ranks) == {0, 1}
            and {w["rank"] for w in workers} == {0, 1}, "native worker coverage missing")
    for worker in workers:
        same_run(worker, logical)
        require(worker.get("sources") == template["sources"]
                and worker.get("arm_config") == config["worker_template"]["arm_config"], "native source/config mismatch")
        identity = ranks[worker["rank"]]
        require(all(worker.get(k) == identity.get(k) for k in ("boot_id", "pid", "start_ticks", "identity_sha256"))
                and worker.get("boot_id") == manifest["boot_id"], "native worker identity mismatch")
    summary_name = f"results/{arm}-http-client-tree/RUN-SUMMARY.json"
    summary_raw = member(summary_name)
    summary = decoded_json(summary_raw)
    settings = load("review/FINAL-SETTINGS.json")
    bundled = records["http-client-bundle.json"]["files"]["RUN-SUMMARY.json"]
    require(bundled.get("bytes") == len(summary_raw) and bundled.get("sha256") == entries[summary_name]
            and base64.b64decode(bundled["base64"], validate=True) == summary_raw
            and result.get("client_summary") == summary, "native summary/readout substitution")
    if http.get("rows") != [row["contract"] for row in summary["rows"]]:
        require("groups" not in summary and settings.get("performance_benchmark", {}).get("mode")
                == "m2047-fixed1024-c1-v1", "native summary/readout substitution")
        responses = []
        for row in summary["rows"]:
            relative = row["wire"]["raw_body_path"]
            name = f"results/{arm}-http-client-tree/" + relative
            raw = member(name)
            blob = records["http-client-bundle.json"]["files"][relative]
            require(blob.get("bytes") == len(raw) and blob.get("sha256") == entries[name]
                    and base64.b64decode(blob["base64"], validate=True) == raw,
                    "fixed C1 response/bundle substitution")
            responses.append(raw)
        fixed_c1_readout(summary["rows"], http.get("rows", []), responses)
    requests = settings["requests"]
    require(len(requests) == len(summary["rows"]), "request schedule length mismatch")
    for request, row in zip(requests, summary["rows"]):
        require(request.get("ordinal") == row["ordinal"] and request.get("case") == row["case"]
                and request.get("sha256") == row["body_sha256"] == row["contract"]["request_sha256"],
                "request schedule substitution")
    common = load("review/COMMON-STACK-RECEIPT.json")
    inventory = load("review/MODEL-INVENTORY.json")
    cli = template["canonical_server_cli"]
    require(cli.count("--revision") == 1, "explicit unique model revision required")
    revision = cli[cli.index("--revision") + 1]
    require(isinstance(revision, str) and len(revision) == 40
            and all(c in "0123456789abcdef" for c in revision)
            and manifest["model_root"].endswith("/" + revision), "model revision mismatch")
    require(spec["server_args"].get("revision") == revision
            and manifest.get("model_inventory_file") == "review/MODEL-INVENTORY.json", "owner model binding mismatch")
    model_files = {item["name"]: item["sha256"] for item in inventory}
    require(len(model_files) == len(inventory) and model_files.get("config.json")
            == template["resolved_contract"]["model_config_sha256"], "model configuration mismatch")
    require(common["target"]["sglang"] == profile["framework_version"]
            and template["resolved_contract"]["tp_size"] == 2, "framework/placement mismatch")
    campaign = controller["campaign"]
    post = campaign["independent_postcheck"]
    require(campaign.get("independent_release_verified") is True and campaign.get("cleanup_errors") == []
            and campaign.get("unresolved") is False and post.get("session_sha256") == session
            and post.get("boot_id") == manifest["boot_id"] and post.get("failures") == []
            and post.get("authenticated_processes_present") == [] and post["allocation"].get("gpu_requests") == 0
            and post["allocation"].get("scope") == "all-namespaces-active-workloads"
            and post["holders"].get("exit_code") == 1 and post["holders"].get("stdout") == ""
            and post["holders"].get("stderr") == ""
            and post["holders"].get("visibility") == "all-users-platform"
            and post["holders"].get("visibility_contract") == "native-host-root-fuser-prerequisites-checked"
            and post["root_fuser_prerequisites"].get("passed") is True, "independent release incomplete")
    projection = None
    if "groups" not in summary:
        intents = []
        for row in summary["rows"]:
            relative = f"requests/{row['ordinal']:02d}-{row['case']}.intent.json"
            name = f"results/{arm}-http-client-tree/" + relative
            raw = member(name)
            bundled = records["http-client-bundle.json"]["files"][relative]
            require(bundled.get("bytes") == len(raw) and bundled.get("sha256") == entries[name]
                    and base64.b64decode(bundled["base64"], validate=True) == raw,
                    "fixed C1 intent/bundle substitution")
            intents.append(decoded_json(raw))
        summary = fixed_c1_projection(summary, settings, intents)
        finished = max(row["wire"]["received_monotonic"] for row in summary["rows"])
        projection = {"profile": "fixed2047-input1024-output-C1", "native_summary_unchanged": True,
            "source_summary_sha256": entries[summary_name], "excluded_warmup_ordinal": 0,
            "clock_source": "sealed client intent and response records",
            "timing_scope": "one non-stream request-to-terminal wall; not isolated decode/TTFT/ITL"}
    else:
        finished = summary["c2_clock"].get("finished")
    released = post.get("observed_monotonic")
    require(positive(finished) and positive(released) and released >= finished, "release predates workload completion")
    require(post["target"].get("hostname") == "threadripper-ai" and post["target"].get("uid") == 1000
            and post["target"].get("boot_id") == manifest["boot_id"], "cleanup target identity mismatch")
    scopes = post["root_resource_release"]["scopes"]
    require(any(s.get("arm") == arm and s.get("absent") is True for s in scopes), "owner scope not released")
    final = [r["final"] for r in controller["resource_receipts"] if r["final"].get("arm") == arm]
    require(len(final) == 1 and final[0].get("benchmark_spec_sha256") == spec_sha
            and final[0].get("session_sha256") == session and final[0].get("complete") is True
            and final[0].get("child_exit") == 0 and final[0].get("child_reaped") is True
            and final[0].get("resource_boundaries_clear") is True, "owner child/resource release incomplete")
    binding = {"seal_sha256": seal_sha, "arm": arm, "logical_arm": logical,
               "session_sha256": session, "spec_sha256": spec_sha, "source_manifest_sha256": source_sha,
               "historical_boot_id": manifest["boot_id"], "model_revision": revision,
               "model_inventory_sha256": entries["review/MODEL-INVENTORY.json"],
               "stack_sha256": entries["review/SERVING-STACK.json"], "stack": observed,
               "parsed_configuration": template["resolved_contract"],
               "request_schedule_sha256": entries["review/FINAL-SETTINGS.json"],
               "verified_members": consumed,
               "base_image": {"manifest": common.get("image_manifest"), "config": common.get("image_config"),
                              "source_revision": common["target"].get("source_revision")},
               "expected_devices": spec["http_serving"].get("expected_devices"),
               "source_inventory": template["sources"], "model_identity_bound": True,
               "framework_source_identity_bound": True, "independent_release_bound": True,
               "native_quality": {k: http.get(k) for k in ("status", "wire_complete", "qualification", "code_correctness_graded")},
               "qualification": False, "automatic_optimization": False}
    if projection is not None:
        binding["measurement_projection"] = projection
    return summary, entries[summary_name], binding


QUALITY_CASES = frozenset({"quality-c1-a", "quality-c1-b", "quality-c2-a", "quality-c2-b",
                           "prefix-branch", "raw-tool-json", "sampled-running-totals"})


def quality_join(binding, summary, path, expected_sha):
    """Join an explicitly reviewed native CPU quality verdict, not execute it."""
    try:
        quality = checked_json(path, expected_sha, 2 * 1024 * 1024)
        parent = quality["parent_readout"]["parent_authentication"]
        require(parent["remote_seals"]["workload"]["manifest_sha256"] == binding["seal_sha256"]
                and parent["controller_result_sha256"] == binding["verified_members"]["results/CONTROLLER-RESULT.json"]
                and parent.get("actual_transport_and_release_verified") is True,
                "quality belongs to another owner run")
        result = quality["summary"]
        require(quality.get("status") == "QUALITY_PASS"
                and result.get("status") == "NATIVE_CPU_FINAL_QUALITY_AND_RELEASE_VALIDATED"
                and result.get("quality_pass") is True and result.get("independent_release_verified") is True
                and result.get("native_generation_performed") is False
                and quality.get("qualification") is False and quality.get("selected") is False,
                "native CPU quality result incomplete or failed")
        expected = {(r["ordinal"], r["case"]) for r in summary["rows"] if r["case"] in QUALITY_CASES}
        require(len(expected) == 7 and {case for _, case in expected} == QUALITY_CASES,
                "unsupported quality task coverage")
        grades = result["grades"]
        keys = [(g["arm"], g["ordinal"], g["case"]) for g in grades]
        require(len(keys) == len(set(keys)) == 14
                and set(keys) == {(arm, ordinal, case) for arm in ("control", "mtp") for ordinal, case in expected}
                and all(g.get("quality_passed") is True and g.get("status") == "QUALITY_PASS" for g in grades),
                "quality task coverage or grade failed")
        input_sha = quality.get("actual_input_sha256")
        require(isinstance(input_sha, str) and len(input_sha) == 64
                and all(c in "0123456789abcdef" for c in input_sha), "quality input identity missing")
        return {"result_sha256": expected_sha, "actual_input_sha256": input_sha,
                "status": "QUALITY_PASS", "task_checks_passed": True,
                "grades": [g for g in grades if g["arm"] == binding["logical_arm"]],
                "total_graded_answers": 14, "independent_cpu_release_verified": True,
                "sampling_distribution_verified": result.get("sampling_distribution_verified"),
                "actual_sampled_verification": result.get("actual_sampled_verification"),
                "all_timed_work_matched": quality["parent_readout"].get("all_timed_work_matched"),
                "final_review_deferred": quality.get("final_Astra_review_deferred"),
                "qualification": False}
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("invalid quality artifact structure") from error


def export_bound(profile, root, seal_sha, arm, output, upstream, quality_path=None, quality_sha=None):
    require(profile.get("schema") == 1 and profile.get("framework") == "sglang"
            and profile.get("standalone_launch") is False and profile.get("automatic_optimization") is False,
            "unsupported bridge configuration")
    require((quality_path is None) == (quality_sha is None), "quality file and reviewed SHA256 required together")
    summary, summary_sha, binding = owner_run(profile, root, seal_sha, arm)
    if quality_path is not None:
        binding["external_quality"] = quality_join(binding, summary, quality_path, quality_sha)
    return write_reports(profile, summary, summary_sha, output, upstream, binding=binding)
