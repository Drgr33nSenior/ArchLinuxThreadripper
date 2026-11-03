"""Finite component operation behind the trusted Hyperloom controller.

Native CPU preparation, proposal and execution are separate steps. GPU execution
requires the controller's exact campaign-derived authority and an independently
released global checkpoint. No action occurs on import.
"""

import base64
import difflib
import json
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owner_operation as op
import build_component as builder
import compile_component as compiler
import hyperloom_exchange as exchange
import hyperloom_owner_backend as backend
from hyperloom_sglang import checked_bytes, checked_json, decoded_json, require

CALLER_SECONDS = 2190
POST_SECONDS = 120
CALLER_DONOR_SHA = "d90ea1c4a9c55bfb9c8f100a04a0e36d9aac98766dc2d70d3053b4ab0d0d44e2"


def snapshot(root, bound):
    root = op._canonical_dir(root, "component snapshot")
    request_raw = exchange._read(root / "REQUEST.json")
    request = exchange._request(decoded_json(request_raw))
    require(exchange.canonical(request) == request_raw and request["kind"] == "component"
            and request["campaign_sha256"] == bound["campaign_sha256"]
            and request["selection_sha256"] == bound["selection_sha256"]
            and request["caller_seconds"] == bound["caller_seconds"]["component"] == CALLER_SECONDS
            and request["post_release_seconds"] == POST_SECONDS
            and request["expires_unix_ms"] <= bound["campaign_deadline_unix_ms"], "component snapshot campaign/limits")
    config_raw = exchange._read(root / "CONFIG.json")
    require(exchange.digest(config_raw) == request["config_sha256"]
            and backend.validate_config(decoded_json(config_raw), bound) == "component", "component configuration binding")
    claim = op._json(root / "CLAIM.json")
    require(claim["id"] == request["id"] and claim["request_sha256"] == exchange.digest(request_raw)
            and claim["controller_sha256"] == bound["controller_sha256"] and claim["gpu_admission"] is False,
            "component snapshot claim")
    rows = request["sources"]
    require(len(rows) == 1 and rows[0]["name"] == op.EVALUABLE
            and {p.name for p in (root / "sources").iterdir()} == {op.EVALUABLE}, "one declared attention candidate")
    candidate = root / "sources" / op.EVALUABLE
    require(len(checked_bytes(candidate, rows[0]["sha256"], exchange.MAX_SOURCE)) == rows[0]["bytes"], "candidate extent")
    return dict(request=request, request_sha256=exchange.digest(request_raw), source_set_sha256=request["source_set_sha256"],
                candidate_sha256=rows[0]["sha256"], candidate=str(candidate), snapshot_root=str(root),
                campaign_sha256=bound["campaign_sha256"], controller_sha256=bound["controller_sha256"])


def prepare(snapshot_root, bound, *, destination, owner_inputs, support_inputs=None):
    root = op._fresh(destination, "component preparation")
    binding = snapshot(snapshot_root, bound)
    request = binding["request"]
    end = min(request["created_unix_ms"] / 1000 + exchange.COMPONENT_ADMISSION_SECONDS,
              request["expires_unix_ms"] / 1000 - CALLER_SECONDS - POST_SECONDS,
              bound["campaign_deadline_unix_ms"] / 1000 - CALLER_SECONDS - POST_SECONDS)
    deadline = time.monotonic() + end - time.time()
    _, inputs = op._pinned(owner_inputs, "component preparation inputs")
    require(set(inputs) == {"compiler_inputs", "donor_root"}, "closed component preparation input template")
    _, compiler_inputs = op._pinned(inputs["compiler_inputs"], "compiler inputs")
    root.mkdir(mode=0o700)
    build = root / "build"
    build.mkdir(mode=0o700)
    runner = op.commands(compiler_inputs["commands"], build)

    def run(label, script, arguments, maximum=40):
        seconds = op._work_budget(deadline, maximum, label, reserve=10)
        receipt = runner.run(label, [op.PYTHON, "-I", "-B", "-S", str(HERE / script), *arguments], seconds,
                             cwd=build, output_limit=2 << 20)
        return receipt

    compiler_inputs = dict(compiler_inputs, candidate=binding["candidate"], candidate_sha256=binding["candidate_sha256"])
    compile_root = root / "compile"
    metadata = compiler.prepare(compiler_inputs, compile_root)
    require(op._work_budget(deadline, 255, "native component compiler", reserve=10) == 255,
            "whole native CPU compiler must fit preparation")
    run("native-compile", "compile_component.py", ["execute", "--root", str(compile_root), "--metadata-sha256",
        exchange.digest((compile_root / "INPUTS.json").read_bytes())], 255)
    report = op._json(compile_root / "COMPILE-RESULT.json")
    if not report["gpu_eligible"]:
        return dict(status="CPU_CANDIDATE_REJECTED", gpu_work=False, registration=False,
                    compiler_gate=report["compiler_gate"], compiler_result=op._pin(compile_root / "COMPILE-RESULT.json"),
                    reason="Frozen compiler resource/distinct-code gate refused this candidate")
    packet_inputs = dict(donor_root=inputs["donor_root"], compile_root=str(compile_root),
        compile_inputs_sha256=exchange.digest((compile_root / "INPUTS.json").read_bytes()),
        compile_result_sha256=exchange.digest((compile_root / "COMPILE-RESULT.json").read_bytes()),
        compile_receipt_sha256=exchange.digest((compile_root / "target-compile.json").read_bytes()),
        candidate=binding["candidate"], candidate_sha256=binding["candidate_sha256"])
    pin = op._write(build / "PACKET-INPUTS.json", packet_inputs)
    packet = root / "packet"
    run("prepare-component-source", "build_component.py", ["source", "--root", str(packet),
         "--inputs", pin["path"], "--inputs-sha256", pin["sha256"]])
    for action, maximum in (("native-source", 65), ("owner", 40), ("native-owner", 80), ("finalize", 40)):
        run(action, "build_component.py", [action, "--root", str(packet)], maximum)
    require(time.monotonic() < deadline, "component preparation deadline exceeded")
    material = op._json(packet / "COMPONENT-PACKET.json")
    require(material["candidate_sha256"] == binding["candidate_sha256"]
            and material["baseline_sha256"] == op.C4_SHA and material["protected_helpers_unchanged"] == 13,
            "prepared component identity")
    op._write(root / "SOURCE-BINDING.json", binding)
    sealer = op.sealer()
    seals = {name: sealer.seal(packet / name) for name in ("source", "native-component", "owner")}
    context = op._json(packet / "owner/driver/OPERATOR-CONTEXT.json")
    readiness = dict(schema=1, status="COORDINATOR_REAL_AB_COMPONENT_READY",
        context_sha256=material["context"]["sha256"], package_sha256=material["package"]["sha256"],
        review_deferred_by_owner=True, component_only=True, capture_provenance=builder.PROVENANCE,
        native_source_checks_passed=True, model_starts=0, seals=seals, schedule=context["schedule"],
        component=context["package"]["component"], gpu_execution=False, registration=False, qualification=False,
        selected=False, profiling=False)
    op._write(root / "READINESS.json", readiness)
    record = dict(schema=1, status="PREPARED_NOT_REGISTERED_OR_RUN", kind="component", root=str(root),
        source_binding=op._pin(root / "SOURCE-BINDING.json"), context=material["context"], package=material["package"],
        readiness=op._pin(root / "READINESS.json"), seals=seals,
        nonce=context["package"]["remote_root"].rsplit(".", 1)[1], candidate_sha256=binding["candidate_sha256"],
        limits=dict(owner_seconds=1500, caller_seconds=CALLER_SECONDS, worker_seconds=600, card_seconds=280),
        gpu_work=False, registration=False, adoption_authorized=False)
    record["root_seal"] = sealer.seal(root)
    record["record"] = op._write(root.with_name(root.name + ".component-preparation.json"), record)
    return record


def verified(preparation, bound):
    _, record = op._pinned(preparation, "component preparation")
    root = op._canonical_dir(record["root"], "component preparation")
    require(record["status"] == "PREPARED_NOT_REGISTERED_OR_RUN" and record["kind"] == "component", "component readiness")
    sealer = op.sealer()
    require(record["root_seal"]["root"] == str(root), "component root seal subject")
    sealer.verify(root, record["root_seal"]["manifest_sha256"])
    for name, seal in record["seals"].items():
        require(seal["root"] == str(root / "packet" / name), "nested component seal subject")
        sealer.verify(Path(seal["root"]), seal["manifest_sha256"])
    _, binding = op._pinned(record["source_binding"], "component source binding")
    require(snapshot(binding["snapshot_root"], bound) == binding, "component snapshot changed after preparation")
    _, context = op._pinned(record["context"], "component context")
    require(context["package_sha256"] == record["package"]["sha256"]
            and context["package"]["sources"][op.EVALUABLE] == binding["candidate_sha256"], "component package/source join")
    return dict(record=record, root=root, binding=binding, context=context)


def propose(preparation, bound, *, campaign_parent, checkpoint_root, mx_root):
    value = verified(preparation, bound)
    prep, root, context, binding = value["record"], value["root"], value["context"], value["binding"]
    checkpoint = op._checkpoint(checkpoint_root)
    index, owner0, caller0 = op._idle(checkpoint["state"])
    parent = op._canonical_dir(campaign_parent, "component campaign parent")
    native = op._canonical_dir(mx_root, "component run parent") / ("hyperloom-component-run-" + prep["nonce"])
    require(not native.exists(), "component run root already exists")
    campaign = parent / ("hyperloom-component-campaign-" + prep["nonce"])
    campaign.mkdir(mode=0o700)
    donor = op.R2 / "p1-gdn-layout-campaign-d18eb1b7274ad61b/execute_once.py"
    original = checked_bytes(donor, CALLER_DONOR_SHA).decode()
    lines = original.splitlines(keepends=True)

    def swap(prefix, text):
        matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
        require(len(matches) == 1, "component caller donor changed: " + prefix)
        lines[matches[0]] = text + "\n"

    swap('"""Inert one-use caller', '"""Inert one-use Hyperloom component caller; exact campaign authority required.')
    swap("expected_seals=", "expected_seals=" + repr(prep["seals"]))
    swap("sys.path.insert(0,", "sys.path.insert(0," + repr(str(root / "packet/owner/driver")) + ")")
    swap("g=json.loads(", "assert sys.argv[1:]==['--registration-sha256',hashlib.sha256(grant.read_bytes()).hexdigest()]\n" +
         "g=json.loads(grant.read_bytes());assert g.get('registered') is True and g.get('proposal_only') is not True,'registered owner grant required'")
    swap("result=operator_driver.execute_once(", "result=operator_driver.execute_once(" +
         ",".join(map(repr, (prep["context"]["path"], prep["context"]["sha256"], prep["readiness"]["path"], prep["readiness"]["sha256"]))) +
         ",str(grant),hashlib.sha256(grant.read_bytes()).hexdigest()," + repr(str(native)) + ")")
    caller = "".join(lines)
    compile(caller, "component-execute-once", "exec", dont_inherit=True)
    (campaign / "execute_once.py").write_text(caller)
    (campaign / "CALLER-DERIVATION.diff").write_text("".join(difflib.unified_diff(original.splitlines(True), lines, str(donor), "execute_once.py")))
    proposal = dict(schema=1, status="OWNER_AUTHORISED_REAL_AB_COMPONENT_EXTERNAL_ENVELOPE_AND_ORDINARY_ENTRY",
        proposal_only=True, registered=False, global_attempt_index=index, maximum_invocations=1, retry=False,
        model_starts=0, http_requests=0, component_only=True, review_deferred_by_owner=True, promotion_authorized=False,
        qualification=False, capture_provenance=builder.PROVENANCE, component=context["package"]["component"],
        context_sha256=prep["context"]["sha256"], readiness_sha256=prep["readiness"]["sha256"],
        package_sha256=prep["package"]["sha256"], owner_start_contract="PRIOR_TARGET_PROBE_ABSOLUTE_BOUND_BEFORE_ADMISSION",
        schedule=context["schedule"], owner_packet_seal=prep["seals"]["owner"], seals=prep["seals"],
        remote_root=context["package"]["remote_root"], protected_root=context["package"]["protected_root"],
        local_receipt_root=str(native), owner_seconds=1500, caller_seconds=CALLER_SECONDS, worker_seconds=600,
        card_seconds=280, prior_model_scope_owner_seconds_charged=owner0, prior_model_scope_caller_seconds_charged=caller0,
        request_sha256=binding["request_sha256"], source_set_sha256=binding["source_set_sha256"],
        candidate_sha256=binding["candidate_sha256"], adoption_authorized=False)
    prop_pin = op._dump(campaign / "REGISTRATION-PROPOSED.json", proposal)
    result = dict(schema=1, status="PROPOSED_NOT_REGISTERED_OR_RUN", kind="component", preparation=preparation,
        campaign_root=str(campaign), native_root=str(native), index=index, nonce=prep["nonce"], proposal=prop_pin,
        execute_once=op._pin(campaign / "execute_once.py"), cumulative=dict(owner_seconds=owner0, caller_seconds=caller0),
        checkpoint={k: checkpoint[k] for k in ("path", "sha256", "number")})
    result["record"] = op._write(campaign / "OWNER-PROPOSAL.json", result)
    return result


def _replay(campaign, runner, verified, native, deadline):
    context, root = verified["context"], verified["root"]
    driver = root / "packet/owner/driver"
    recipe = "import sys,types,base64,json,time,resource\nresource.setrlimit(resource.RLIMIT_CPU,(60,65))\nresource.setrlimit(resource.RLIMIT_FSIZE,(128<<20,128<<20))\n"
    recipe += "def guard(event,args):\n if event in ('subprocess.Popen','os.fork','os.posix_spawn','os.system') or event.startswith(('os.exec','socket.')):raise RuntimeError('component replay refuses external actions')\nsys.addaudithook(guard)\n"
    for name, file in (("exporter", "sealed_export.py"), ("reader", "local_readout.py")):
        raw = checked_bytes(driver / file, context["ordinary_operator_sources"][file])
        recipe += (name + "=types.ModuleType(" + repr(name) + ");" + name + ".__file__=" + repr(str(driver / file)) + "\n" +
                   "exec(compile(base64.b64decode(" + repr(base64.b64encode(raw)) + ")," + name + ".__file__,'exec')," + name + ".__dict__)\n")
    recipe += "value=reader.run(" + repr(context) + ",exporter," + repr(str(native / "bounded-retain.stdout")) + "," + repr(str(campaign / "component-replay")) + ",time.monotonic()+60)\nprint(json.dumps(value,sort_keys=True))\n"
    receipt = runner.run("component-replay", [op.PYTHON, "-I", "-B", "-S", "-"],
        op._work_budget(deadline, 65, "component retained replay", reserve=20), stdin=recipe.encode(), cwd=campaign, output_limit=4 << 20)
    raw = checked_bytes(campaign / "component-replay.stdout", receipt["stdout"]["sha256"], 4 << 20)
    # The retained analyzer prints progress before the explicit final object.
    value = decoded_json([line for line in raw.splitlines() if line.strip()][-1])
    require(value["exit_code"] == 0 and value["native_execution"] is value["selection"] is False,
            "original component reader refused the retained output")
    return value["summary"], receipt


def execute(proposal, authorization, bound, *, checkpoint_root, cancelled, commands_module=None, replay=None):
    _, prop = op._pinned(proposal, "component proposal")
    verified_value = verified(prop["preparation"], bound)
    binding = verified_value["binding"]
    request = binding["request"]
    campaign, native = Path(prop["campaign_root"]), Path(prop["native_root"])
    result = dict(schema=1, kind="component", id=request["id"], campaign_sha256=bound["campaign_sha256"],
        controller_sha256=bound["controller_sha256"], request_sha256=binding["request_sha256"],
        config_sha256=request["config_sha256"], source_set_sha256=binding["source_set_sha256"],
        status="REFUSED", reason=None, owner_started=False, independent_release=False, owner_seal_sha256=None,
        payload={}, park_campaign=False, automatic_retry=False, adoption_authorized=False, qualification=False,
        campaign_root=str(campaign), native_root=str(native), index=prop["index"], checkpoints={}, receipts={},
        release_disposition="NOT_STARTED", registered=False)
    registration, checkpoint = None, None
    try:
        _, proposed = op._pinned(prop["proposal"], "component registration proposal")
        checked_bytes(Path(prop["execute_once"]["path"]), prop["execute_once"]["sha256"])
        auth = op._authorization(authorization, prop, verified_value, bound)
        require(not native.exists() and all(not (campaign / n).exists() for n in
            ("REGISTRATION.json", "CALLER-INVOKED.json", "caller-once.json", "OWNER-EXECUTION.json")), "one fresh component invocation")
        checkpoint = op._checkpoint(checkpoint_root)
        index, owner0, caller0 = op._idle(checkpoint["state"])
        require(index == prop["index"] and owner0 == prop["cumulative"]["owner_seconds"]
                and caller0 == prop["cumulative"]["caller_seconds"], "checkpoint changed after component proposal")

        def admissible():
            if cancelled():
                raise op.Cancelled("component request cancelled before GPU owner start")
            require(time.time() + CALLER_SECONDS + POST_SECONDS <= min(request["expires_unix_ms"], bound["campaign_deadline_unix_ms"]) / 1000,
                    "whole component caller and retained replay must fit")

        admissible()
        grant = dict(proposed, registered=True, proposal_only=False, owner_answer=auth["record"]["owner_answer"],
                     approval_record=dict(path=auth["path"], sha256=auth["sha256"]), registered_utc=op._utc(time.time()))
        raw = (json.dumps(grant, sort_keys=True, indent=2) + "\n").encode()
        registration = dict(path=str(campaign / "REGISTRATION.json"), sha256=exchange.digest(raw))
        opened = op._checkpoint_record(checkpoint, time.time(), active_caller=str(campaign), active_owner="hyperloom-component",
            current_global_attempt_index=index, last_completed_global_attempt_index=index - 1, registration=registration,
            status="HYPERLOOM_COMPONENT_CALLER_STARTING", caller_deadline_utc=op._utc(time.time() + CALLER_SECONDS),
            hyperloom_request_sha256=binding["request_sha256"], hyperloom_campaign_sha256=bound["campaign_sha256"])
        result["checkpoints"]["registered"] = op._write_checkpoint(checkpoint_root, checkpoint["number"] + 1, opened)
        with (campaign / "REGISTRATION.json").open("xb") as stream:
            stream.write(raw)
        result.update(registered=True, registration=registration)
        admissible()
        runner = commands_module or op.commands(verified_value["root"] / "packet/owner/component/local_commands.py", campaign)
        result.update(owner_started=True, park_campaign=True, release_disposition="UNVERIFIED_PARK_CAMPAIGN")
        receipt = runner.run("caller-once", [op.PYTHON, "-I", "-B", "-S", str(campaign / "execute_once.py"),
            "--registration-sha256", registration["sha256"]], CALLER_SECONDS - 10, cwd=campaign, output_limit=16 << 20)
        result["receipts"]["caller"] = receipt
        deadline = time.monotonic() + min(POST_SECONDS,
            min(request["expires_unix_ms"], bound["campaign_deadline_unix_ms"]) / 1000 - time.time()) - 30
        operator = op._json(native / "OPERATOR-RESULT.json")
        require(operator["accepted"] is True and operator["owner_exit_code"] == 0 and not operator["command_failures"],
                "component caller did not retain a complete accepted operation")
        summary, replay_receipt = (replay or _replay)(campaign, runner, verified_value, native, deadline)
        result["receipts"]["replay"] = replay_receipt
        require(summary == op._json(native / "results/RESULT-SUMMARY.json") and summary["component_complete"] is True
                and summary["independent_release_verified"] is True, "component readout/release mismatch")
        seals = op._json(native / "results/RETAINED-SEALS.json")
        package = verified_value["context"]["package"]
        require(seals["workload"]["root"] == package["remote_root"] and seals["protected"]["root"] == package["protected_root"],
                "retained release belongs to another owner namespace")
        operation_seal = op.sealer().seal(native)
        result.update(independent_release=True, park_campaign=False, release_disposition="RELEASED",
                      owner_seal_sha256=operation_seal["manifest_sha256"], operation_seal=operation_seal)
        result["observed_release"] = dict(launch_elapsed_seconds=op._json(native / "physical-owner.json")["elapsed_seconds"])
        rows = summary["usefulness"]
        require([r["card"] for r in rows] == [0, 1], "both component cards in sequential order")
        correct = all(r["candidate_status"] == "CORRECT" for r in rows)
        useful = correct and all(r["timing"]["useful"] is True for r in rows)
        no_regression = correct and all(r["timing"]["zero_prefix_adoption_gate"] is True for r in rows)
        report = dict(schema=1, diagnostic_only=True, serving_tested=False, adoption_authorized=False,
            owner_binding=dict(owner_seal_sha256=operation_seal["manifest_sha256"], request_sha256=binding["request_sha256"],
                source_set_sha256=binding["source_set_sha256"], controller_sha256=bound["controller_sha256"]),
            correct_both_cards=correct, useful_both_cards=useful, zero_prefix_no_regression=no_regression,
            eligible_for_serving=correct and useful and no_regression, summary=summary)
        require(time.monotonic() < deadline, "component post-release budget exceeded")
        result.update(status="SUCCEEDED", payload={"component_report": report})
        backend.validate_component(result, request, bound)
    except op.Cancelled as error:
        result.update(status="CANCELLED", reason=op._error(error))
    except op.REFUSALS as error:
        result.update(status="FAILED" if result["registered"] else "REFUSED", reason=op._error(error),
                      park_campaign=result["registered"])
    finally:
        if result["checkpoints"].get("registered"):
            op._close(result, checkpoint_root, checkpoint, registration, time.time())
        if not (campaign / "OWNER-EXECUTION.json").exists():
            result["record"] = op._write(campaign / "OWNER-EXECUTION.json", result)
    return result
