"""Authenticate real campaign qualification and prepare immutable reuse inputs."""

from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[2] / "lib/workstation")]
import owner_operation as op
import continuation
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
import hyperloom_latency as latency
from hyperloom_sglang import checked_json, require


def measurements(jobs, bound):
    """No historical run can substitute for one of this campaign's three jobs."""
    require(len(jobs) == 3, "exactly three real qualification jobs required")
    by_kind = {}
    for request, result in jobs:
        exchange._request(request)
        exchange.validate_result(result, request, controller_sha256=bound["controller_sha256"])
        kind = request["kind"]
        require(kind in {"component", "serving", "profile"} and kind not in by_kind,
                "one component, A/A and profile required")
        require(request["campaign_sha256"] == bound["campaign_sha256"]
                and request["selection_sha256"] == bound["selection_sha256"]
                and request["expires_unix_ms"] <= bound["campaign_deadline_unix_ms"]
                and result["status"] == "SUCCEEDED" and result["independent_release"] is True,
                "qualification did not complete and release in this campaign")
        if kind == "component":
            require(request["sources"][0]["sha256"] ==
                    "e945ad72296777556ed93859263442c077e0f1d1d7082268c1f669e157a324e9",
                    "qualification component differs from the reviewed oracle candidate")
            report = backend.validate_component(result, request, bound)
            require(report["correct_both_cards"] is True, "both real component oracles must pass")
        else:
            require(request["source_set_sha256"] == bound["selected_source_set_sha256"],
                    "qualification must run the unchanged selected C4 source")
            report = backend.validate_report(result, request, bound)
        by_kind[kind] = (request, result, report)
    serving = by_kind["serving"][2]
    comparison = latency.compare(serving["workstation_latency_control"], serving["workstation_latency"],
                                 bound["latency_policy"])
    require(all(reason == "balanced_gain_not_resolved" for reason in comparison["reasons"]),
            "A/A qualification has noisy, unequal-work or regressed cases")
    require(all(axis["stable"] and abs(axis["candidate_median_ms"] - axis["baseline_median_ms"]) <= axis["noise_band_ms"]
                for case in comparison["cases"].values() for axis in case["axes"].values()),
            "A/A arm difference exceeds the frozen noise band")
    return by_kind, comparison


def prepare(controller_root, bound, destination, *, native_root, runtime_observation,
            runtime_result, cpu_proof, evidence_bundle, continued=None):
    """Write a proof only after owner seals, release checkpoints and tests verify."""
    controller_root, destination, native_root = map(Path, (controller_root, destination, native_root))
    require(controller_root.resolve() == controller_root and destination.parent.resolve() == destination.parent
            and not destination.exists(), "fresh canonical qualification output required")
    require(native_root.is_absolute() and str(native_root).startswith("/home/developer/hyperloom-owner-campaign-"),
            "private native campaign root required")
    require(runtime_observation.get("gpu_devices") is False
            and runtime_observation.get("replies_read_only") is True
            and runtime_observation.get("inputs_read_only") is True
            and runtime_observation.get("deadline_unix_ms") == bound["campaign_deadline_unix_ms"],
            "real qualification runtime isolation was not observed")
    require(runtime_result.get("mode") == "qualification" and runtime_result.get("exit_code") == 0
            and runtime_result.get("failure") is None and runtime_result.get("child_reaped") is True
            and runtime_result.get("owned_group_absent") is True
            and runtime_result.get("deadline_unix_ms") == bound["campaign_deadline_unix_ms"],
            "qualification client did not finish naturally and release")
    tests = checked_json(Path(cpu_proof["path"]), cpu_proof["sha256"])
    require(tests.get("status") == "PASS" and tests.get("controller_sha256") == bound["controller_sha256"]
            and tests.get("failure_retention") is True and tests.get("actual_baseline_writeback") is True
            and tests.get("actual_profile_writeback") is True and tests.get("upstream_balanced_keep_paths") is True,
            "current CPU lifecycle and upstream decision proof required")
    bundle = checked_json(Path(evidence_bundle["path"]), evidence_bundle["sha256"], 2 << 20)
    require({'serving_profile', 'trace_census', 'research_reconciliation'} <=
            {row.get('id') for row in bundle.get('artifacts', [])},
            "retained C1 traces, trace census and reconciled research required")
    jobs = []
    sealed = {}
    roots=sorted((controller_root / "jobs").iterdir())
    carried=None
    if continued is not None:
        carried=continuation.validate(continued,bound,int(time.time()*1000))
        roots.append(Path(carried['component_root']))
    for root in roots:
        request = op._json(root / "snapshot/REQUEST.json")
        result = op._json(root / "OUTCOME.json", exchange.MAX_RESULT)
        published = op._json(root / "PUBLICATION.json")
        require(published["published_sha256"] == exchange.digest(exchange.canonical(result)),
                "qualification result was not published intact")
        closing = result["checkpoints"]["closed"]
        require(set(closing) == {'path','sha256','number'}, 'completed closing checkpoint required')
        closed = op._pinned({key:closing[key] for key in ('path','sha256')}, "closing checkpoint")[1]
        require(closed.get("independent_release_verified") is True
                and closed.get("last_completed_global_attempt_index") == result["index"]
                and not closed.get("active_owner") and not closed.get("active_caller"),
                "qualification closing checkpoint does not attest release")
        seal = op.sealer().verify(Path(result["native_root"]), result["owner_seal_sha256"])
        sealed[request["kind"]] = Path(seal["root"]) / "FINAL-ARTIFACTS.sha256"
        jobs.append((request, result))
    verified, comparison = measurements(jobs, bound)
    # The profile decoder validates all seven windows and both rank digests.
    from hyperloom_owner_profile import materialize_captures
    destination.mkdir(mode=0o700)
    trace_validation = destination / "profile-validation"
    trace_validation.mkdir(mode=0o700)
    materialize_captures(verified["profile"][2], verified["profile"][0], trace_validation)
    proof = dict(schema=1, kind="workstation-owner-native-qualification", status="PASS",
        **{k: bound[k] for k in ("campaign_sha256", "campaign_deadline_unix_ms", "selection_sha256",
                                "runtime_sha256", "workload_sha256", "controller_sha256")},
        checks={name: True for name in ("serving_aa", "c1_c2_profile", "component_oracle", "owner_release",
            "failure_retention", "response_mount_read_only", "agents_have_no_gpu_devices",
            "upstream_balanced_keep_paths", "campaign_deadline")},
        cpu_proof=dict(path=str(native_root/'inputs/cpu-proof.json'),sha256=cpu_proof['sha256']),
        retained_evidence=dict(path=str(native_root/'evidence/EVIDENCE-INDEX.json'),sha256=evidence_bundle['sha256']),
        profile_scope="Fresh C2 prefill and batch-2 decode, both ranks; retained C1 traces stay historical.",
        aa_comparison=comparison, measurements={}, owner_seals=[], adoption_authorized=False)
    if carried is not None:
        proof['continued_component']=dict(evidence=continued,index=carried['result']['index'],
            request_sha256=carried['result']['request_sha256'],new_measurement=False,
            scope='Frozen component oracle only; operation did not test a serving workload.')
    for kind, (request, result, report) in verified.items():
        local = destination / kind
        local.mkdir(mode=0o700)
        remote = native_root / "inputs/qualification" / kind
        raw = exchange._read(sealed[kind])
        exchange._publish(local / "FINAL-ARTIFACTS.sha256", raw)
        proof["owner_seals"].append(dict(path=str(remote / "FINAL-ARTIFACTS.sha256"), sha256=exchange.digest(raw)))
        if kind == "component":
            exchange._publish(local / "component_report.json", exchange.canonical(report))
            continue
        reference = dict(workspace=str(remote))
        for name, key, value in (("OWNER-JOB.json", "request_sha256", request),
                                 ("OWNER-RESULT.json", "result_sha256", result),
                                 ("benchmark_report.json", "report_sha256", report)):
            raw = exchange.canonical(value)
            exchange._publish(local / name, raw)
            reference[key] = exchange.digest(raw)
        proof["measurements"]["serving_aa" if kind == "serving" else "c1_c2_profile"] = reference
    op._write(destination / "QUALIFICATION.json", proof)
    return proof
