"""Read host metadata and reviewed serving provenance; never upgrade or launch."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import urllib.parse


def classify(servers):
    result = []
    for server in servers:
        url = urllib.parse.urlsplit(server)
        archive = url.hostname == "archive.archlinux.org" and re.match(r"/repos/[0-9]{4}/[0-9]{2}/[0-9]{2}/", url.path)
        result.append({"host": url.hostname, "scheme": url.scheme,
                       "classification": "dated-archive" if archive else "rolling-candidate-unverified",
                       "archive_date": url.path.split("/")[2:5] if archive else None})
    return result


SERVING_REPOSITORY = "https://github.com/Drgr33nSenior/Spry.ai-rdna4-serving"
SERVING_IMPORTS = {
    "mxfp4-128k-20261101": ({"MXFP4-CANDIDATE-PHASE1-20261031.json", "MXFP4-CANDIDATE-128K-20261031.json"}, 65536),
    "splitk-selection-20261107": ({"MXFP4-SPLITK-S4-SELECTION-20261107.json", "MXFP4-FINALIZATION-20261105.json"}, 262144),
}
CURRENT_IMPORT = "splitk-selection-20261107"
SELECTION_RECORD = "MXFP4-SPLITK-S4-SELECTION-20261107.json"


def _hex(value, size=64):
    return isinstance(value, str) and re.fullmatch(f"[a-f0-9]{{{size}}}", value) is not None


def _arm(row, logical="logical", physical="physical"):
    arm = {"logical": row.get(logical), "physical": row.get(physical), "start": row.get("start"),
           "operation": row.get("operation")}
    if (arm["logical"] not in {"mtp", "control"} or arm["physical"] not in {"bf16", "kv4"}
            or type(arm["start"]) is not int or type(arm["operation"]) is not int):
        raise ValueError("serving selection names an invalid arm")
    return arm


def _selection(chosen, final):
    """Allowlisted selection facts from the hash-pinned public selection record.

    The arm, package and rollback come from data, so a later owner selection is
    adopted only by importing its published record, never by editing code.
    """
    package, runtime, back = chosen["package"], chosen["selected_runtime"], chosen["rollback"]
    arm = _arm(package["selected_arm"])
    if (_arm(runtime, "logical_arm", "physical_arm") != arm or chosen.get("programme_parked") is not True
            or chosen.get("deployment") is not False or chosen.get("gpu_work") is not False
            or back.get("launch_authorized") is not False or final.get("parked") is not True
            or not all(_hex(value) for value in (chosen["current_private_pointer"]["sha256"],
                                                   chosen["previous_private_pointer"]["sha256"],
                                                   package["package_sha256"], package["manifest_sha256"]))
            or not isinstance(runtime.get("canonical_server_cli"), list)):
        raise ValueError("serving selection record does not describe one parked selected arm")
    rollback = _arm(back, "logical_arm", "physical_arm")
    if rollback == arm:
        raise ValueError("serving selection rollback is the selected arm")
    # The rollback must be the arm the finalization record selected before.
    if {k: rollback[k] for k in ("logical", "physical", "start", "operation")} != final["final_selection"]["selected_arm"]:
        raise ValueError("serving rollback differs from the finalization record's selection")
    return {"status": chosen["status"], "parked": True, "operation": arm["operation"],
            "selected_arm": {key: arm[key] for key in ("logical", "physical", "start")},
            "members": list(chosen["candidate_members"]),
            "pointer_sha256": chosen["current_private_pointer"]["sha256"],
            "previous_pointer_sha256": chosen["previous_private_pointer"]["sha256"],
            "superseded_pointer_sha256": [chosen["previous_private_pointer"]["sha256"],
                                          final["final_selection"]["previous_pointer"]["sha256"]],
            "rollback": {**rollback, "launch_authorized": False,
                         "http_binding_sha256": back["http_binding"]["sha256"], "kernel_sha256": back["kernel"]["sha256"],
                         "package_sha256": final["final_selection"]["package"]["package_sha256"],
                         "pointer_sha256": back["previous_candidate_pointer"]["sha256"]},
            "package": {key: package[key] for key in ("package_sha256", "archive_sha256", "manifest_sha256",
                                                      "source_manifest_sha256", "worker_source_manifest_sha256")},
            "canonical_server_cli": list(runtime["canonical_server_cli"]),
            "resolved_contract": runtime["resolved_contract"],
            "measured_request_profile": runtime.get("sampled_request_profile"),
            "frozen_verdict": chosen.get("frozen_classification", {}).get("verdict"),
            "tested_deviations": list(runtime["tested_deviations"]),
            "limits_and_deviations": list(chosen["limits_and_deviations"]),
            "installed": False, "deployment": False}


def serving_project(root):
    """Report pinned public evidence, without treating it as runtime admission."""
    source = Path(root) / "infrastructure/sglang"
    tracking = json.loads((source / "serving-project.json").read_text())
    imports = tracking.get("imports")
    if (tracking.get("schema") != 2 or tracking.get("repository") != SERVING_REPOSITORY
            or tracking.get("current_import") != CURRENT_IMPORT or not isinstance(imports, list)
            or [row.get("id") for row in imports if isinstance(row, dict)] != list(SERVING_IMPORTS)):
        raise ValueError("invalid serving project tracking record")
    records, summary = {}, []
    for row in imports:
        names, bound = SERVING_IMPORTS[row["id"]]
        if (not _hex(row.get("revision", ""), 40) or set(row.get("records", {})) != names
                or row.get("status") != ("current" if row["id"] == CURRENT_IMPORT else "historical")):
            raise ValueError("invalid serving project tracking record")
        for name, expected in row["records"].items():
            member = source / "serving-evidence" / name
            if member.is_symlink() or not member.is_file() or member.stat().st_size > bound:
                raise ValueError("bounded regular serving evidence required")
            with member.open("rb") as stream:
                raw = stream.read(bound + 1)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("serving project evidence identity changed")
            records[name] = json.loads(raw)
        summary.append({key: row[key] for key in ("id", "status", "revision", "checked_at", "records")})
    phase1 = records["MXFP4-CANDIDATE-PHASE1-20261031.json"]
    historical = records["MXFP4-CANDIDATE-128K-20261031.json"]
    c4 = historical["c4_serving_ab"]
    current = next(row for row in summary if row["id"] == CURRENT_IMPORT)
    return {"status": "reviewed-source-evidence-only", "schema": tracking["schema"],
            "repository": tracking["repository"], "revision": current["revision"],
            "checked_at": current["checked_at"], "release": tracking["release"],
            "distribution": tracking["distribution"], "imports": summary,
            "selection": _selection(records[SELECTION_RECORD], records["MXFP4-FINALIZATION-20261105.json"]),
            # Historical 1 November import: the earlier C4 candidate, kept for provenance.
            "historical_c4_candidate": {
                "revision": summary[0]["revision"], "profile": historical["profile"],
                "candidate": historical["candidate"]["description"],
                "adoption": c4["owner_adoption"]["status"],
                "historical_adoption_verdict": c4["sealed_adoption_result"]["verdict"],
                "stock_endurance_graded_answers": historical["w5_v2_endurance"]["graded_answers"],
                "c4_endurance": c4["c4_endurance"],
                "unresolved_gpu_fault": phase1["fault"]["error"]},
            "reported_deployed": historical["deployed"],
            "public_fp8_selection_changed": historical["public_fp8_selection_changed"],
            "deployment_qualified": False, "target_inspected": False,
            "remote_rechecked_by_inventory": False}


def main():
    repos = subprocess.check_output(["pacman-conf", "--repo-list"], text=True).splitlines()
    result = {}
    for repo in repos:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", repo):
            raise ValueError("unsupported repository name")
        servers = subprocess.check_output(["pacman-conf", "--repo", repo, "Server"], text=True).splitlines()
        result[repo] = classify(servers)
    packages = subprocess.check_output(["pacman", "-Q"], text=True).splitlines()
    print(json.dumps({"schema": 1, "host_packages": packages, "mirrors": result,
                      "container_runtime": "collect separately with rocm serving-evidence; do not mix library prefixes",
                      "remote_freshness_verified": False,
                      "serving_project": serving_project(Path(__file__).resolve().parents[2]),
                      "upgrade": "owner-controlled full pacman -Syu after reviewed mirror migration; retain stable/LTS recovery"}, indent=2))


if __name__ == "__main__":
    main()
