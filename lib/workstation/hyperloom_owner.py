"""Controller handoff to an existing reviewed, single-use native owner.

This module creates no grant and implements no GPU admission or process killer.
The supplied owner retains those duties and its independent release procedure.
"""

import importlib.util
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tarfile

from hyperloom_sglang import checked_bytes, checked_json, export_bound, require


CONTROLLER_FILES = frozenset({"run_native.py", "collect_results.py", "observe_closeout.py",
                             "observe_failed_closeout.py", "local_commands.py", "connection.py", "seal_evidence.py"})
STAGES = ("upload", "launch", "collect", "closeout")


def controller_runtime_ready():
    """The unchanged collector requires filtered tar extraction."""
    return callable(getattr(tarfile, "data_filter", None)) and "filter" in inspect.signature(tarfile.TarFile.extractall).parameters


def prepare(session, authorization_sha, selection, expected_boot):
    """Validate the existing owner packet without importing or executing it."""
    session = Path(session).absolute()
    require(session.is_dir() and not session.is_symlink() and session.resolve() == session,
            "canonical controller session required")
    require(not (session / "FINAL-ARTIFACTS.sha256").exists()
            and not any((session / name).exists() for name in
                        ("HYPERLOOM-STARTED.json", "upload-once.json", "native-launch.json", "collected")),
            "owner session sealed or already consumed; prepare a fresh owner packet")
    auth = checked_json(session / "RUN-AUTHORIZATION.json", authorization_sha)
    require(auth.get("native_root") == str(session) and auth.get("no_automatic_retry") is True
            and auth.get("owner_invocations_allowed") == 1 and auth.get("deployment_authorized") is False
            and auth.get("promotion_authorized") is False, "owner invocation/deployment boundaries")
    require(str(auth.get("status", "")).startswith("REGISTERED_"), "registered owner packet required")
    selection = Path(selection)
    require(selection.is_file() and not selection.is_symlink(), "current serving selection required")
    checked_bytes(selection, auth.get("selection_sha256"), 1024 * 1024)
    extension = auth.get("hyperloom_controller", {})
    pins = extension.get("files", {})
    require(set(pins) == CONTROLLER_FILES, "complete reviewed controller closure required")
    require({p.relative_to(session).as_posix() for p in session.rglob("*.py")} == CONTROLLER_FILES,
            "unexpected controller import source")
    for name, sha in pins.items():
        checked_bytes(session / name, sha, 1024 * 1024)
    binding_raw = checked_bytes(session / "LAUNCHER-BINDINGS.json", extension.get("binding_sha256"))
    binding = checked_json(session / "LAUNCHER-BINDINGS.json", extension.get("binding_sha256"))
    nonce = binding.get("nonce")
    require(isinstance(nonce, str) and len(nonce) == 16 and all(c in "0123456789abcdef" for c in nonce)
            and auth.get("nonce") == nonce and binding.get("boot_id") == expected_boot,
            "fresh owner nonce/boot required")
    require(binding.get("target_root") == "/home/developer/fp8-kv-benchmark." + nonce
            and binding.get("remote_upload_root") == "/home/developer/fp8-benchmark-upload." + nonce
            and binding.get("protected_root") == "/var/tmp/fp8-benchmark-source." + nonce,
            "owner target paths changed")
    source = Path(auth["source_root"])
    source_seal = auth["source_seal"]
    require(source.is_dir() and not source.is_symlink() and source_seal.get("root") == str(source),
            "reviewed owner source required")
    seal_raw = checked_bytes(source / "FINAL-ARTIFACTS.sha256", source_seal.get("manifest_sha256"), 2 * 1024 * 1024)
    settings_pins = [line.split("  ", 1)[0] for line in seal_raw.decode().splitlines()
                     if line.endswith("  review/FINAL-SETTINGS.json")]
    require(len(settings_pins) == 1, "one sealed external request schedule required")
    frozen = checked_json(source / "review/FINAL-SETTINGS.json", settings_pins[0], 32768)
    rows = frozen.get("requests")
    require(frozen.get("mode") == "native-http-fp8-sprint01-v1" and isinstance(rows, list)
            and len(rows) in (4, 30) and [row.get("ordinal") for row in rows] == list(range(len(rows))),
            "supported four/thirty-request owner schedule required")
    require(checked_bytes(source / "LAUNCHER-BINDINGS.json", extension.get("binding_sha256")) == binding_raw,
            "controller/author launcher binding differs")
    identities_raw = checked_bytes(session / "PACKAGE-IDENTITIES.json", extension.get("identities_sha256"))
    require(checked_bytes(source / "PACKAGE-IDENTITIES.json", extension.get("identities_sha256")) == identities_raw,
            "controller/author package identities differ")
    identities = checked_json(session / "PACKAGE-IDENTITIES.json", extension.get("identities_sha256"))
    require(all(identities.get(k) == binding.get(k) for k in
                ("nonce", "target_root", "manifest_sha256", "archive_sha256")),
            "collector package identities differ from launcher")
    require(binding.get("manifest_sha256") == auth.get("manifest_sha256")
            and binding.get("archive_sha256") == auth.get("archive_sha256"), "owner payload identity changed")
    owner_seconds, starts, requests = (auth.get(k) for k in ("owner_seconds", "model_starts_allowed", "generations"))
    # Both supported owners run two arms. Derive external work from the sealed
    # consumed schedule, rather than shrinking it by editing authorization.
    require(type(owner_seconds) is int and owner_seconds == 5400
            and type(starts) is int and starts == 2 and type(requests) is int and requests == 2 * len(rows),
            "owner protocol requires 5400 seconds, two starts and the sealed external request count")
    caps = extension.get("phase_seconds", {})
    require(set(caps) == set(STAGES) and all(type(v) is int and v > 0 for v in caps.values())
            and caps["upload"] <= 90 and caps["collect"] <= 180 and caps["closeout"] <= 120
            and owner_seconds + 100 <= caps["launch"] <= owner_seconds + 180,
            "controller caps must preserve owner termination/reap headroom")
    complete = sum(caps.values()) + 10 * len(STAGES)
    require(type(extension.get("campaign_seconds")) is int and complete <= extension["campaign_seconds"] <= 6100,
            "complete controller envelope not covered")
    return {"session": str(session), "authorization_sha256": authorization_sha, "boot_id": expected_boot,
            "selection": str(selection.absolute()), "selection_sha256": auth["selection_sha256"],
            "nonce": nonce, "target_root": binding["target_root"],
            "model_starts_allowed": starts, "external_requests_allowed": requests,
            "requests_per_start": len(rows), "request_schedule_sha256": settings_pins[0],
            "phase_seconds": caps, "maximum_controller_seconds": complete,
            "binding_sha256": extension["binding_sha256"], "identities_sha256": extension["identities_sha256"],
            "controller_files": pins, "qualification": False, "automatic_optimization": False}


def run(plan, output, profile, report_writer):
    """Delegate once; preserve the owner's receipts, cleanup and failure path."""
    require(os.geteuid() != 0, "controller must run as the ordinary owner user")
    session, output = Path(plan["session"]), Path(output)
    require(prepare(session, plan["authorization_sha256"], plan["selection"], plan["boot_id"]) == plan,
            "owner plan changed before execution")
    require(controller_runtime_ready(),
            "controller Python lacks the existing collector's tar data filter; use a declared compatible controller SDK")
    require(output.is_absolute() and not output.exists() and not output.is_symlink(), "fresh absolute report output required")
    # Reserve before loading/spawning; failure never permits an automatic retry.
    with (session / "HYPERLOOM-STARTED.json").open("x") as stream:
        json.dump(plan, stream, indent=2)
    output.mkdir(mode=0o700)
    state = {"status": "OWNER_STARTED", "plan": plan, "stages": [], "failures": [],
             "qualification": False, "automatic_optimization": False, "closeout_observed": False}
    try:
        supervisor_path = session / "local_commands.py"
        checked_bytes(supervisor_path, plan["controller_files"]["local_commands.py"], 1024 * 1024)
        spec = importlib.util.spec_from_file_location("_hyperloom_existing_owner_commands", supervisor_path)
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)

        def stage(name, filename, arguments=()):
            argv = [sys.executable, "-E", "-B", str(session / filename), *arguments]
            try:
                # Check the controller closure outside the measured model path.
                for member, sha in plan["controller_files"].items():
                    checked_bytes(session / member, sha, 1024 * 1024)
                checked_bytes(session / "RUN-AUTHORIZATION.json", plan["authorization_sha256"])
                checked_bytes(session / "LAUNCHER-BINDINGS.json", plan["binding_sha256"])
                checked_bytes(session / "PACKAGE-IDENTITIES.json", plan["identities_sha256"])
                supervisor.run("hyperloom-" + name, argv, plan["phase_seconds"][name], cwd=session)
                state["stages"].append({"stage": name, "status": "PASS"})
                return True
            except BaseException as error:
                state["stages"].append({"stage": name, "status": "FAILED", "error": type(error).__name__})
                state["failures"].append(name)
                return False

        if stage("upload", "run_native.py", ("upload",)):
            launched = stage("launch", "run_native.py", ("launch",))
            collected = stage("collect", "collect_results.py")
            closed = stage("closeout", "observe_closeout.py" if launched else "observe_failed_closeout.py")
            state["closeout_observed"] = closed
            if launched and collected and closed:
                owner = session / "collected/workload" / Path(plan["target_root"]).name
                seal_sha = hashlib.sha256((owner / "FINAL-ARTIFACTS.sha256").read_bytes()).hexdigest()
                for arm in ("bf16", "kv4"):
                    export_bound(profile, owner, seal_sha, arm, output / arm, report_writer)
                state.update(status="OWNER_COMPLETED_REPORTS_IMPORTED", owner_root=str(owner), owner_seal_sha256=seal_sha)
            else:
                state["status"] = "OWNER_FAILED_RETAINED"
        else:
            state["status"] = "UPLOAD_FAILED_NOT_LAUNCHED"
    except Exception as error:
        state.update(status="ADAPTER_FAILED_RETAINED", adapter_error=type(error).__name__)
    finally:
        (output / "OWNER-RESULT.json").write_text(json.dumps(state, indent=2) + "\n")
    return state
