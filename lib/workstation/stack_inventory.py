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


def serving_project(root):
    """Report pinned public evidence, without treating it as runtime admission."""
    source = Path(root) / "infrastructure/sglang"
    tracking = json.loads((source / "serving-project.json").read_text())
    names = {"MXFP4-CANDIDATE-PHASE1-20261031.json", "MXFP4-CANDIDATE-128K-20261031.json"}
    if (tracking.get("schema") != 1
            or tracking.get("repository") != "https://github.com/Drgr33nSenior/Spry.ai-rdna4-serving"
            or not re.fullmatch(r"[a-f0-9]{40}", tracking.get("revision", ""))
            or set(tracking.get("records", {})) != names):
        raise ValueError("invalid serving project tracking record")
    records = {}
    for name, expected in tracking["records"].items():
        member = source / "serving-evidence" / name
        if member.is_symlink() or not member.is_file() or member.stat().st_size > 65536:
            raise ValueError("bounded regular serving evidence required")
        with member.open("rb") as stream:
            raw = stream.read(65537)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("serving project evidence identity changed")
        records[name] = json.loads(raw)
    phase1 = records["MXFP4-CANDIDATE-PHASE1-20261031.json"]
    current = records["MXFP4-CANDIDATE-128K-20261031.json"]
    c4 = current["c4_serving_ab"]
    return {**tracking, "status": "reviewed-source-evidence-only",
            "profile": current["profile"], "candidate": current["candidate"]["description"],
            "adoption": c4["owner_adoption"]["status"],
            "historical_adoption_verdict": c4["sealed_adoption_result"]["verdict"],
            "stock_endurance_graded_answers": current["w5_v2_endurance"]["graded_answers"],
            "c4_endurance": c4["c4_endurance"],
            "unresolved_gpu_fault": phase1["fault"]["error"],
            "reported_deployed": current["deployed"],
            "public_fp8_selection_changed": current["public_fp8_selection_changed"],
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
