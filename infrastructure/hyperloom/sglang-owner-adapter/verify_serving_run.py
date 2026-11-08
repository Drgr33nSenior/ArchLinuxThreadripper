"""One approved verification-prefetch serving A/B through the existing owner seam.

The reviewed packet is rebuilt byte-for-byte at admission so its exchange
request is fresh. Its nonce and all five package pins are frozen. This module
never registers a grant directly, invokes the caller itself, or starts Hyperloom.
"""

import argparse
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import controller
import owner_operation as op
import verify_serving as verify

exchange, backend, latency = op.exchange, op.backend, op.latency
host, require = controller.host, op.require
SECONDS = 7660
KIND = "one-verify-prefetch-serving-ab"
FOUR_KIND = 'one-four-member-serving-qualification-ab'


def validate(plan, plan_ref, approval):
    """No writes, network or owner admission before explicit exact approval."""
    kind=plan.get('kind')
    require(plan.get("schema") == 1 and kind in (KIND,FOUR_KIND) and plan.get("seconds") == SECONDS
            and plan.get("automatic_retry") is False and plan.get("adoption_authorized") is False
            and plan.get("optimizer_start_authorized") is False, "one finite verification serving A/B plan required")
    op.checked_bytes(HERE / "verify_serving_run.py", plan["registrar_sha256"], 1 << 20)
    require(controller.source_closure(plan["controller_sources"]) == plan["binding"]["controller_sha256"],
            "owner seam source closure changed")
    require(approval.get("schema") == 1 and approval.get("kind") == kind
            and approval.get("plan_sha256") == plan_ref["sha256"]
            and approval.get("runtime_sha256") == plan["binding"]["runtime_sha256"]
            and approval.get("owner_inputs_sha256") == plan["owner_inputs"]["sha256"]
            and approval.get("packet_pins") == plan["expected_package_pins"]
            and approval.get("seconds") == SECONDS and approval.get("one_invocation") is True
            and approval.get("automatic_retry") is False and approval.get("adoption_authorized") is False
            and approval.get("optimizer_start_authorized") is False
            and isinstance(approval.get("owner"), str) and approval["owner"].strip()
            and isinstance(approval.get("owner_answer"), str) and approval["owner_answer"].strip(),
            "fresh exact owner approval of this runtime, inputs and packet required")
    decision = approval["decision"]
    require(set(decision) == {"path", "sha256"} and decision["path"] == plan["decision_path"],
            "approval names another owner decision")
    op.checked_bytes(Path(decision["path"]), decision["sha256"], 1 << 20)
    inputs = controller.read(plan["owner_inputs"])
    family='four_member' if kind==FOUR_KIND else verify.FAMILY
    require(inputs.get("frozen_packet") == dict(nonce=plan["nonce"], pins=plan["expected_package_pins"])
            and inputs.get("candidate_sha256") == verify.CANDIDATE_SHA
            and inputs.get("source_family") == family
            and plan["binding"].get("serving_source_family") == family
            and plan["binding"].get("verify_serving_sha256") == op._pin(HERE / "verify_serving.py")["sha256"],
            "reviewed frozen verification packet inputs differ")
    op._runtime_configuration(plan["binding"], inputs)
    candidate = op.checked_bytes(Path(inputs["candidate"]), verify.CANDIDATE_SHA, 4 << 20)
    baseline = op.checked_bytes(Path(plan["baseline_source"]["path"]), verify.BASELINE_SHA, 4 << 20)
    require(plan["baseline_source"]["sha256"] == verify.BASELINE_SHA, "selected native decode pin differs")
    for raw, pin, expected in ((candidate, verify.CANDIDATE_SHA, plan["candidate_source_set_sha256"]),
                              (baseline, verify.BASELINE_SHA, plan["binding"]["selected_source_set_sha256"])):
        require(exchange.digest(exchange.canonical([dict(name=verify.FILE, bytes=len(raw), sha256=pin)])) == expected,
                "baseline or candidate source-set identity differs")
    cap=235000000 if kind==FOUR_KIND else 300000000
    require(plan["binding"].get("observed_power_cap_microwatts") == cap,
            "both starts must retain their exact source-bound observed power caps")
    if kind==FOUR_KIND:
        # The primary source-set eligibility key remains the prefetch file.
        # Every other member is bound by the full frozen package and this exact
        # fixed-contract reference; no optimizer may supply or change those files.
        require(set(plan['controller_sources'])==controller.BUNDLE_SOURCES,'complete fixed-bundle source closure')
        op._serving_surface(plan['binding'])
        outer=plan['outer_packet']
        op.sealer().verify(Path(outer['root']),outer['manifest_sha256'])
        review=plan['independent_review']
        require(set(review)=={'path','sha256'}, 'one pinned independent review')
        op.checked_bytes(Path(review['path']),review['sha256'],1<<20)
        require(Path(plan['reference_preparation']['root']).is_relative_to(Path(outer['root'])),
                'reference preparation must belong to the bound outer packet')
        _,fixed=op._pinned(plan['binding']['four_member_contract'],'fixed four-member contract')
        require(inputs['four_member']['compiler_pins']['sha256']==fixed['compiler_pins_sha256'],
                'four-member compiler identity differs')
    require(op._pin(op.R2 / "CANDIDATE-POINTER.json")["sha256"] == plan["binding"]["selection_sha256"],
            "selected pointer changed")
    reference = plan["reference_preparation"]
    op.sealer().verify(Path(reference["root"]), reference["manifest_sha256"])
    plan_pins = op._json(Path(reference["root"]) / "launcher/CALLER-PLAN.json")["expected_package_pins"]
    require(plan_pins == plan["expected_package_pins"], "reference packet differs")
    controller.read(plan["support_inputs"])
    return inputs


def authorize(proposal, request, bound, approval_ref, approval, destination):
    """Derive the usual exact-operation record from this one-operation approval."""
    value = dict(schema=1, kind="serving", campaign=proposal["campaign_root"],
        proposal_sha256=proposal["proposal"]["sha256"], execute_once_sha256=proposal["execute_once"]["sha256"],
        nonce=proposal["nonce"], global_gpu_attempt_index=proposal["index"],
        request_sha256=exchange.digest(exchange.canonical(request)), source_set_sha256=request["source_set_sha256"],
        candidate_sha256=verify.CANDIDATE_SHA, campaign_sha256=bound["campaign_sha256"], controller_sha256=bound["controller_sha256"],
        owner=approval["owner"], owner_answer=approval["owner_answer"], approval=dict(approval_ref),
        no_retry=True, one_invocation=True, deployment=False, promotion=False, publication=False,
        adoption_authorized=False, qualification=False, utc=op._utc(time.time()))
    return controller.write(destination, value)


def ab_report(outcome, request, bound):
    report = backend.validate_report(outcome, request, bound)
    return latency.compare(report["workstation_latency_control"], report["workstation_latency"], bound["latency_policy"])


def run(plan_ref, approval_ref, *, operation=op):
    plan, approval = controller.read(plan_ref), controller.read(approval_ref)
    validate(plan, plan_ref, approval)
    root = op._fresh(plan["execution_root"], "single-use verification serving A/B retention")
    # Do not start this operation's clock while another owner is active.
    operation._idle(operation._checkpoint(plan["checkpoint_root"])["state"])
    started = int(time.time() * 1000)
    bound = dict(plan["binding"], campaign_deadline_unix_ms=started + SECONDS * 1000)
    operation._bound(bound)
    root.mkdir(mode=0o700)
    result = dict(schema=1, status="FAILED_PARKED", kind=plan['kind'], plan=dict(plan_ref), approval=dict(approval_ref),
        started_unix_ms=started, deadline_unix_ms=bound["campaign_deadline_unix_ms"],
        automatic_retry=False, adoption_authorized=False, optimizer_started=False,
        hyperloom_campaign_qualified=False, original_campaign_extended=False, gpu_owner_started=False)
    try:
        controller.write(root / "ONE-SHOT-STARTED.json", result)
        for name in ("requests", "replies", "campaigns"):
            (root / name).mkdir(mode=0o700)
        require(bound["requests_root"] == str(root / "requests") and bound["replies_root"] == str(root / "replies"),
                "private local exchange mapping differs")
        controller.write(root / "BINDING.json", bound)
        _, request = exchange.submit(root / "requests", campaign_sha256=bound["campaign_sha256"],
            selection_sha256=bound["selection_sha256"], kind="serving", config=plan["benchmark_config"],
            sources=bound["sources"], caller_seconds=op.CALLER_SECONDS,
            campaign_deadline_unix_ms=bound["campaign_deadline_unix_ms"], now=started / 1000,
            expected_source_set=plan["candidate_source_set_sha256"])
        taken = host.dispatch("take", bound, job_id=request["id"])
        snapshot = root / "snapshot"
        host.retain_snapshot(taken, snapshot, bound)
        preparation = operation.prepare(snapshot, bound, destination=root / "preparation",
            owner_inputs=plan["owner_inputs"], support_inputs=plan["support_inputs"])
        controller.write(root / "PREPARATION-RESULT.json", preparation)
        require(preparation["status"] == "PREPARED_NOT_REGISTERED_OR_RUN", "fresh preparation refused: " + str(preparation.get("reason")))
        require(preparation["nonce"] == plan["nonce"]
                and {k: preparation["package"][k] for k in op.PIN_FIELDS} == plan["expected_package_pins"]
                and preparation["caller"]["sha256"] == plan["expected_caller_sha256"]
                and preparation["caller_plan"]["sha256"] == plan["expected_caller_plan_sha256"],
                "fresh preparation does not reproduce the approved packet and caller")
        proposal = operation.propose(preparation["record"], bound, campaign_parent=root / "campaigns",
            checkpoint_root=plan["checkpoint_root"], mx_root=op.MX)
        controller.write(root / "PROPOSAL-RESULT.json", proposal)
        require(proposal["status"] == "PROPOSED_NOT_REGISTERED_OR_RUN", "fresh proposal refused: " + str(proposal.get("reason")))
        authority = authorize(proposal, request, bound, approval_ref, approval, root / "OPERATION-AUTHORIZATION.json")
        result.update(gpu_owner_started=None, execution_entered=True)
        outcome = operation.execute(proposal["record"], authority, bound, checkpoint_root=plan["checkpoint_root"],
                                    cancelled=lambda: (root / "CANCEL").exists())
        result.update(gpu_owner_started=outcome["owner_started"], outcome=controller.write(root / "OUTCOME.json", outcome))
        require(outcome["status"] == "SUCCEEDED" and outcome["independent_release"] is True
                and outcome["grading_performed"] is True, "owner operation did not complete quality and release")
        result["comparison"] = ab_report(outcome, request, bound)
        require(int(time.time() * 1000) <= bound["campaign_deadline_unix_ms"], "standalone envelope expired")
        result["status"] = ('FOUR_MEMBER_SERVING_MEASURED_PARKED' if plan['kind']==FOUR_KIND
                            else "VERIFY_SERVING_MEASURED_PARKED")
    except Exception as error:
        result["reason"] = op._error(error)
    finally:
        result["finished_unix_ms"] = int(time.time() * 1000)
        controller.write(root / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "plan-sha256", "approval", "approval-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = run(dict(path=args.plan, sha256=args.plan_sha256), dict(path=args.approval, sha256=args.approval_sha256))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("VERIFY_SERVING_MEASURED_PARKED",'FOUR_MEMBER_SERVING_MEASURED_PARKED') else 1


if __name__ == "__main__":
    raise SystemExit(main())
