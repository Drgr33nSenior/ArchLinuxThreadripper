#!/usr/bin/env python3
"""Apply the AI-stack image vulnerability policy to a Trivy JSON report.

Every finding is recorded. The push fails only for CRITICAL findings that have
a fixed version available, unless the allowlist covers them. An allowlist
entry names a vulnerability (and optionally a package), says why it is
accepted, who reviewed it and when it expires; expired entries stop counting.
Prints a JSON summary and exits 1 when the policy blocks.

Usage: scan-policy.py TRIVY_JSON ALLOWLIST_JSON [TODAY]
"""
import collections
import datetime
import json
import sys


def load_allowlist(path, today):
    entries = json.load(open(path))
    if not isinstance(entries, list):
        raise SystemExit("STOP: allowlist must be a JSON list")
    active = []
    for e in entries:
        missing = {"id", "reason", "reviewed", "expires"} - set(e)
        if missing:
            raise SystemExit(f"STOP: allowlist entry {e.get('id')} lacks {sorted(missing)}")
        if datetime.date.fromisoformat(e["expires"]) >= today:
            active.append(e)
    return active


def allowed(finding, allowlist):
    return any(e["id"] == finding["id"] and e.get("package") in (None, finding["package"]) for e in allowlist)


def main():
    report = json.load(open(sys.argv[1]))
    today = datetime.date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else datetime.date.today()
    allowlist = load_allowlist(sys.argv[2], today)
    counts = collections.Counter()
    blocking, accepted = [], []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            severity = v.get("Severity", "UNKNOWN")
            counts[severity] += 1
            if severity != "CRITICAL" or not v.get("FixedVersion"):
                continue
            finding = {"id": v["VulnerabilityID"], "package": v.get("PkgName"),
                       "installed": v.get("InstalledVersion"), "fixed": v["FixedVersion"],
                       "target": result.get("Target")}
            (accepted if allowed(finding, allowlist) else blocking).append(finding)
    summary = {"schema": 1, "counts": dict(sorted(counts.items())), "blocking": blocking,
               "accepted": accepted, "passed": not blocking,
               "rule": "block CRITICAL with a fixed version unless allowlisted and unexpired"}
    print(json.dumps(summary, indent=2))
    return 0 if not blocking else 1


if __name__ == "__main__":
    sys.exit(main())
