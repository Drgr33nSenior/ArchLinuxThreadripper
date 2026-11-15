"""Run records (interface run-record-v1): identity, failures, acceptance and resource joins.

A run executes one declared plan (tasks, arm and latency targets) and records one
attempt per try at a task. Attempts carry task/run/attempt/arm identities and a
causal parent (the attempt a retry follows). Independent task acceptance
(interface task-acceptance-v1) and resource observations join on those IDs.

Records hold identifiers, counts, timings, digests and watts only: no prompts,
answers, paths, URLs or credentials. Every object has a closed key set and every
identifier a bounded format, so free text cannot enter a record. Identifiers
belong in records, traces and joins, never in Prometheus labels.

Tool outcomes are recorded, but never decide task acceptance. Sampled PSU DC
power is whole-system output; GPU board samples are a component of that load.
Neither is task energy, which stays null until an independent per-task meter
exists. No third-party modules.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

import telemetry_power


SCHEMA = 1
INTERFACE = "run-record-v1"
ACCEPTANCE_INTERFACE = "task-acceptance-v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
MAX_NS = 2**63
MAX_COUNT = 10**12
MAX_RECORDS = 10000

# Operational failure taxonomy. "unknown" is explicit: an unexplained failure
# is never folded into another category or dropped.
FAILURE_CATEGORIES = ("retrieval", "reasoning-answer", "edit-schema", "timeout-cancellation",
                      "environment", "verification", "unknown")
# measurement.py run.json error_category -> failure category. A nonzero
# workload exit alone does not say why the workload failed.
MEASUREMENT_FAILURES = {"workload-exit": "unknown", "timeout": "timeout-cancellation",
                        "interrupted": "timeout-cancellation", "telemetry": "environment",
                        "cleanup": "environment", "workload": "unknown"}
OUTCOMES = ("completed", "failed")
CAUSES = ("initial", "retry")
VERDICTS = ("accepted", "rejected", "unavailable")
LATENCY_TARGETS = ("task_wall_seconds",)
IDENTITY_KEYS = ("task_id", "task_version", "run_id", "attempt_id", "arm_id", "parent_id", "cause")
DEPLOYMENT_KEYS = ("serving_contract", "model", "tokenizer_sha256", "template_sha256")
TASK_ENERGY_REASON = ("no independent per-task meter: PSU DC samples are whole-system output and GPU board "
                      "samples are a component of it; neither is attributable to one task")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _keys(value, required, optional=(), name="record"):
    _require(isinstance(value, dict), f"{name} must be an object")
    keys = set(value)
    _require(set(required) <= keys <= set(required) | set(optional), f"{name} has unexpected or missing keys")


def _id(value, name, nullable=False):
    if value is None and nullable:
        return
    _require(isinstance(value, str) and ID.match(value) is not None, f"{name} must be a bounded identifier")


def _digest(value, name):
    _require(value is None or (isinstance(value, str) and SHA256.match(value) is not None),
             f"{name} must be a sha256 hex digest or null")


def _ns(value, name):
    _require(type(value) is int and 0 < value < MAX_NS, f"{name} must be positive integer nanoseconds")


def _count(value, name):
    _require(type(value) is int and 0 <= value <= MAX_COUNT, f"{name} must be a bounded count")


def _number(value, name, maximum=1e12):
    _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum,
             f"{name} must be a positive finite number")


def _header(value, kind, interface=INTERFACE):
    _require(value.get("schema") == SCHEMA and value.get("kind") == kind and value.get("interface") == interface,
             f"unsupported {kind} schema, kind or interface")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_identity(value):
    _keys(value, IDENTITY_KEYS, name="identity")
    for key in IDENTITY_KEYS[:5]:
        _id(value[key], key)
    _id(value["parent_id"], "parent_id", nullable=True)
    _require(value["cause"] in CAUSES, "cause must be initial or retry")
    _require((value["cause"] == "initial") == (value["parent_id"] is None),
             "an initial attempt has no parent; a retry names its parent attempt")
    _require(value["parent_id"] != value["attempt_id"], "an attempt cannot be its own parent")
    return value


def validate_plan(value):
    """The run plan, declared before the first attempt starts."""
    _keys(value, ("schema", "kind", "interface", "run_id", "arm_id", "declared_unix_ns", "latency_targets", "tasks"),
          name="plan")
    _header(value, "run-plan")
    _id(value["run_id"], "run_id")
    _id(value["arm_id"], "arm_id")
    _ns(value["declared_unix_ns"], "declared_unix_ns")
    targets = value["latency_targets"]
    _keys(targets, LATENCY_TARGETS, name="latency_targets")
    for key in LATENCY_TARGETS:
        _number(targets[key], key, 86400 * 7)
    tasks = value["tasks"]
    _require(isinstance(tasks, list) and 0 < len(tasks) <= MAX_RECORDS, "plan declares no tasks")
    for task in tasks:
        _keys(task, ("task_id", "task_version"), name="task")
        _id(task["task_id"], "task_id")
        _id(task["task_version"], "task_version")
    _require(len({task["task_id"] for task in tasks}) == len(tasks), "duplicate declared task")
    return value


def validate_attempt(value):
    _keys(value, ("schema", "kind", "interface", "identity", "deployment", "started_unix_ns", "ended_unix_ns",
                  "outcome", "failure", "tool_calls", "tokens"), name="attempt")
    _header(value, "attempt")
    validate_identity(value["identity"])
    deployment = value["deployment"]
    _keys(deployment, DEPLOYMENT_KEYS, name="deployment")
    _id(deployment["serving_contract"], "serving_contract", nullable=True)
    _id(deployment["model"], "model", nullable=True)
    _digest(deployment["tokenizer_sha256"], "tokenizer_sha256")
    _digest(deployment["template_sha256"], "template_sha256")
    _ns(value["started_unix_ns"], "started_unix_ns")
    _ns(value["ended_unix_ns"], "ended_unix_ns")
    _require(value["ended_unix_ns"] >= value["started_unix_ns"], "attempt ends before it starts")
    _require(value["outcome"] in OUTCOMES, "outcome must be completed or failed")
    failure = value["failure"]
    if value["outcome"] == "failed":
        _keys(failure, ("category", "source"), name="failure")
        _require(failure["category"] in FAILURE_CATEGORIES, "unknown failure category; use 'unknown'")
        _id(failure["source"], "failure source")
    else:
        _require(failure is None, "a completed attempt has no failure")
    if value["tool_calls"] is not None:
        _keys(value["tool_calls"], ("succeeded", "failed"), name="tool_calls")
        for key in ("succeeded", "failed"):
            _count(value["tool_calls"][key], "tool_calls." + key)
    if value["tokens"] is not None:
        _keys(value["tokens"], ("prompt", "completion"), name="tokens")
        for key in ("prompt", "completion"):
            _count(value["tokens"][key], "tokens." + key)
    return value


def validate_acceptance(value):
    """Consumed interface task-acceptance-v1: one independent verdict per attempt."""
    _keys(value, ("schema", "kind", "interface", "task_id", "task_version", "run_id", "attempt_id", "verdict",
                  "checker", "evaluated_unix_ns"), name="acceptance")
    _header(value, "task-acceptance", ACCEPTANCE_INTERFACE)
    for key in ("task_id", "task_version", "run_id", "attempt_id"):
        _id(value[key], key)
    _require(value["verdict"] in VERDICTS, "verdict must be accepted, rejected or unavailable")
    _keys(value["checker"], ("id", "sha256"), name="checker")
    _id(value["checker"]["id"], "checker id")
    _require(value["checker"]["sha256"] is not None, "checker digest is required")
    _digest(value["checker"]["sha256"], "checker sha256")
    _ns(value["evaluated_unix_ns"], "evaluated_unix_ns")
    return value


def validate_gpu_sample(value):
    _keys(value, ("schema", "kind", "interface", "device", "unix_ns", "watts"), name="gpu sample")
    _header(value, "gpu-power-sample")
    _id(value["device"], "device")
    _ns(value["unix_ns"], "unix_ns")
    _require(type(value["watts"]) in (int, float) and math.isfinite(value["watts"])
             and 0 <= value["watts"] <= telemetry_power.MAX_POWER_WATTS, "invalid GPU watts")
    return value


def attempt_from_receipt(receipt, deployment, tokens=None, tool_calls=None):
    """Attempt record from a measurement.py run.json that carries an identity."""
    _require(isinstance(receipt, dict) and receipt.get("schema") == 1, "unsupported measurement receipt")
    started = receipt.get("started_unix_ns")
    elapsed = receipt.get("elapsed_seconds")
    _ns(started, "receipt started_unix_ns")
    _require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "invalid receipt elapsed")
    succeeded = receipt.get("status") == "measured-not-qualified" and not receipt.get("error_category")
    category = None if succeeded else MEASUREMENT_FAILURES.get(receipt.get("error_category"), "unknown")
    return validate_attempt({
        "schema": SCHEMA, "kind": "attempt", "interface": INTERFACE, "identity": receipt.get("identity"),
        "deployment": deployment, "started_unix_ns": started, "ended_unix_ns": started + round(elapsed * 1e9),
        "outcome": "completed" if succeeded else "failed",
        "failure": None if succeeded else {"category": category,
                                           "source": "measurement:" + str(receipt.get("error_category") or "status")},
        "tool_calls": tool_calls, "tokens": tokens})


def _overlap(start, end, a, b):
    return max(start, a), min(end, b)


def psu_window(samples, start_ns, end_ns):
    """Sampled PSU DC output energy over one window, counting covered intervals only.

    ``samples`` are telemetry_power.collect() results in recorded order. Each
    consecutive pair goes through telemetry_power.advance(); only an "available"
    interval counts, linearly interpolated where it straddles a window edge.
    Every uncovered stretch is reported as a gap with its reason.
    """
    state, previous, pending = None, None, None
    covered_joules = covered_ns = 0.0
    gaps, resets = [], 0
    first = last = None
    for sample in samples:
        _require(isinstance(sample, dict), "PSU sample must be an object")
        state, reason = telemetry_power.advance(state, sample)
        if state["anchor"] is None:
            pending = "sample_unavailable"
            continue
        current = state["anchor"]
        if previous is not None:
            _require(current["unix_ns"] > previous["unix_ns"],
                     "PSU sample wall clock is not increasing; a wall-time join would be unreliable")
            a, b = _overlap(start_ns, end_ns, previous["unix_ns"], current["unix_ns"])
            if b > a:
                if reason == "available":
                    span = current["unix_ns"] - previous["unix_ns"]
                    slope = (current["watts"] - previous["watts"]) / span
                    watts_a = previous["watts"] + slope * (a - previous["unix_ns"])
                    watts_b = previous["watts"] + slope * (b - previous["unix_ns"])
                    covered_joules += (watts_a + watts_b) * (b - a) / 2e9
                    covered_ns += b - a
                else:
                    gap = pending if reason == "new_interval" and pending else reason
                    gaps.append({"start_unix_ns": a, "end_unix_ns": b, "reason": gap})
                    resets += reason in ("boot_changed", "device_changed")
        first = current["unix_ns"] if first is None else first
        last = current["unix_ns"]
        previous, pending = current, None
    window = end_ns - start_ns
    if first is None or first >= end_ns or last <= start_ns:
        gaps = [{"start_unix_ns": start_ns, "end_unix_ns": end_ns, "reason": "no_samples"}] if window else []
    else:
        if first > start_ns:
            gaps.insert(0, {"start_unix_ns": start_ns, "end_unix_ns": first, "reason": "no_samples"})
        if last < end_ns:
            gaps.append({"start_unix_ns": last, "end_unix_ns": end_ns, "reason": pending or "no_samples"})
    return {"attribution": "system-psu-dc-output", "covered_seconds": covered_ns / 1e9,
            "window_seconds": window / 1e9, "coverage": covered_ns / window if window else None,
            "covered_joules": covered_joules, "gaps": gaps, "resets": resets,
            "assumptions": ["Corsair hwmon DC output, not AC wall energy",
                            "trapezoidal integral of covered intervals only; gaps contribute nothing",
                            "linear interpolation inside a covered interval at the window edges",
                            "whole-system load, including concurrent and idle work"]}


def gpu_window(samples, start_ns, end_ns):
    """Instantaneous GPU board power samples in a window; not integrated as energy."""
    devices = {}
    for sample in samples:
        if start_ns <= sample["unix_ns"] <= end_ns:
            devices.setdefault(sample["device"], []).append(sample["watts"])
    return {"attribution": "gpu-board-component", "devices": {
        device: {"samples": len(values), "mean_watts": sum(values) / len(values), "max_watts": max(values)}
        for device, values in sorted(devices.items())},
        "assumptions": ["sampled board power; part of the PSU DC load, never added to it",
                        "sample counts are not coverage of short spikes"]}


def _chains(plan, attempts):
    by_id = {}
    for attempt in attempts:
        identity = attempt["identity"]
        _require(identity["attempt_id"] not in by_id, "duplicate attempt_id")
        _require(identity["run_id"] == plan["run_id"], "attempt belongs to another run")
        _require(identity["arm_id"] == plan["arm_id"], "attempt arm differs from the declared arm")
        by_id[identity["attempt_id"]] = attempt
    declared = {task["task_id"]: task["task_version"] for task in plan["tasks"]}
    chains = {task_id: [] for task_id in declared}
    children = {}
    for attempt_id, attempt in by_id.items():
        identity = attempt["identity"]
        _require(declared.get(identity["task_id"]) == identity["task_version"], "attempt for an undeclared task version")
        _require(attempt["started_unix_ns"] >= plan["declared_unix_ns"],
                 "latency targets must be declared before the first attempt starts")
        parent = identity["parent_id"]
        if parent is not None:
            _require(parent in by_id, "retry names an unknown parent attempt")
            parent_record = by_id[parent]
            _require(parent_record["identity"]["task_id"] == identity["task_id"], "retry parent is another task")
            _require(parent_record["ended_unix_ns"] <= attempt["started_unix_ns"], "retry starts before its parent ends")
            _require(parent not in children, "an attempt has more than one retry")
            children[parent] = attempt_id
    for task_id in declared:
        roots = [a for a in by_id.values() if a["identity"]["task_id"] == task_id and a["identity"]["parent_id"] is None]
        _require(len(roots) <= 1, "a task has more than one initial attempt in a run")
        node = roots[0]["identity"]["attempt_id"] if roots else None
        while node is not None:
            chains[task_id].append(by_id[node])
            node = children.get(node)
    _require(sum(map(len, chains.values())) == len(by_id), "attempt chain is disconnected or cyclic")
    return chains


def report(plan, attempts, acceptances, psu_samples=(), gpu_samples=()):
    """Join independent acceptance and resources to every attempt in one run."""
    validate_plan(plan)
    _require(len(attempts) <= MAX_RECORDS and len(acceptances) <= MAX_RECORDS, "too many records")
    for attempt in attempts:
        validate_attempt(attempt)
    gpu_samples = [validate_gpu_sample(sample) for sample in gpu_samples]
    chains = _chains(plan, attempts)
    verdicts = {}
    by_attempt = {a["identity"]["attempt_id"]: a for a in attempts}
    for acceptance in acceptances:
        validate_acceptance(acceptance)
        attempt = by_attempt.get(acceptance["attempt_id"])
        _require(attempt is not None and acceptance["run_id"] == plan["run_id"]
                 and acceptance["task_id"] == attempt["identity"]["task_id"]
                 and acceptance["task_version"] == attempt["identity"]["task_version"],
                 "acceptance does not match a recorded attempt")
        _require(acceptance["attempt_id"] not in verdicts, "more than one acceptance verdict for an attempt")
        _require(acceptance["evaluated_unix_ns"] >= attempt["ended_unix_ns"], "acceptance predates the attempt's end")
        verdicts[acceptance["attempt_id"]] = acceptance
    target = plan["latency_targets"]["task_wall_seconds"]
    failures = dict.fromkeys(FAILURE_CATEGORIES, 0)
    totals = {"attempts": 0, "failed_attempts": 0, "retries": 0, "attempt_wall_seconds": 0.0,
              "tokens": {"prompt": 0, "completion": 0, "attempts_with_tokens": 0},
              "tool_calls": {"succeeded": 0, "failed": 0, "attempts_with_tool_calls": 0,
                             "note": "tool outcomes only; never task acceptance"}}
    tasks = []
    for declared in plan["tasks"]:
        chain = chains[declared["task_id"]]
        rows, accepted = [], None
        for attempt in chain:
            identity = attempt["identity"]
            verdict = verdicts.get(identity["attempt_id"])
            wall = (attempt["ended_unix_ns"] - attempt["started_unix_ns"]) / 1e9
            category = attempt["failure"]["category"] if attempt["failure"] else None
            totals["attempts"] += 1
            totals["attempt_wall_seconds"] += wall
            totals["retries"] += identity["cause"] == "retry"
            if category:
                totals["failed_attempts"] += 1
                failures[category] += 1
            for key, counts in (("tokens", attempt["tokens"]), ("tool_calls", attempt["tool_calls"])):
                if counts is not None:
                    totals[key]["attempts_with_" + key] += 1
                    for field, value in counts.items():
                        totals[key][field] += value
            if verdict and verdict["verdict"] == "accepted" and accepted is None:
                accepted = attempt
            overlapping = sum(1 for other in attempts if other is not attempt
                              and other["started_unix_ns"] < attempt["ended_unix_ns"]
                              and other["ended_unix_ns"] > attempt["started_unix_ns"])
            rows.append({"attempt_id": identity["attempt_id"], "parent_id": identity["parent_id"],
                         "cause": identity["cause"], "outcome": attempt["outcome"], "failure_category": category,
                         "acceptance": verdict["verdict"] if verdict else "missing",
                         "checker": verdict["checker"] if verdict else None,
                         "wall_seconds": wall, "tokens": attempt["tokens"], "tool_calls": attempt["tool_calls"],
                         "overlapping_attempts": overlapping,
                         "psu_dc": psu_window(psu_samples, attempt["started_unix_ns"], attempt["ended_unix_ns"]),
                         "gpu_component": gpu_window(gpu_samples, attempt["started_unix_ns"], attempt["ended_unix_ns"]),
                         "task_energy_joules": None})
        if not chain:
            status = "not-attempted"
        elif accepted is not None:
            status = "accepted"
        else:
            final = rows[-1]["acceptance"]
            status = "rejected" if final == "rejected" else "unverified"
        task_wall = ((accepted["ended_unix_ns"] - chain[0]["started_unix_ns"]) / 1e9) if accepted else None
        tasks.append({"task_id": declared["task_id"], "task_version": declared["task_version"], "status": status,
                      "attempts": len(chain), "failed_attempts": sum(1 for r in rows if r["failure_category"]),
                      "accepted_attempt_id": accepted["identity"]["attempt_id"] if accepted else None,
                      "task_wall_seconds": task_wall,
                      "meets_latency_target": None if task_wall is None else task_wall <= target,
                      "attempt_records": rows})
    if attempts:
        start = min(a["started_unix_ns"] for a in attempts)
        end = max(a["ended_unix_ns"] for a in attempts)
        run_psu = psu_window(psu_samples, start, end)
        run_gpu = gpu_window(gpu_samples, start, end)
    else:
        run_psu = run_gpu = None
    deployments = sorted({canonical(a["deployment"]) for a in attempts})
    accepted_tasks = [t for t in tasks if t["status"] == "accepted"]
    return {
        "schema": SCHEMA, "kind": "run-record-report", "interface": INTERFACE,
        "acceptance_interface": ACCEPTANCE_INTERFACE,
        "run_id": plan["run_id"], "arm_id": plan["arm_id"],
        "plan_sha256": hashlib.sha256(canonical(plan)).hexdigest(),
        "latency_targets": plan["latency_targets"], "latency_targets_declared_unix_ns": plan["declared_unix_ns"],
        "deployments": [json.loads(d) for d in deployments], "deployment_changed": len(deployments) > 1,
        "summary": {"tasks_declared": len(tasks), "tasks_accepted": len(accepted_tasks),
                    "tasks_accepted_within_target": sum(1 for t in accepted_tasks if t["meets_latency_target"]),
                    "tasks_rejected": sum(1 for t in tasks if t["status"] == "rejected"),
                    "tasks_unverified": sum(1 for t in tasks if t["status"] == "unverified"),
                    "tasks_not_attempted": sum(1 for t in tasks if t["status"] == "not-attempted"),
                    **totals, "failures_by_category": failures},
        "coverage": {"attempts_with_acceptance": sum(1 for a in attempts
                                                     if a["identity"]["attempt_id"] in verdicts),
                     "attempts_with_tokens": totals["tokens"]["attempts_with_tokens"],
                     "attempts": len(attempts), "psu_dc_run_coverage": run_psu["coverage"] if run_psu else None,
                     "gpu_component_samples": len(gpu_samples)},
        "energy": {"psu_dc_run_window": run_psu, "gpu_component_run_window": run_gpu,
                   "task_attributable": {"joules": None, "reason": TASK_ENERGY_REASON}},
        "tasks": tasks}


def _jsonl(path):
    if path is None:
        return []
    rows = []
    with open(path) as source:
        for line in source:
            if line.strip():
                rows.append(json.loads(line))
                _require(len(rows) <= MAX_RECORDS, "too many records")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("report",))
    parser.add_argument("--plan", required=True)
    parser.add_argument("--attempts", required=True, help="attempt records, JSON lines")
    parser.add_argument("--acceptance", required=True, help="task-acceptance-v1 verdicts, JSON lines")
    parser.add_argument("--psu", help="telemetry_power.collect() samples, JSON lines in recorded order")
    parser.add_argument("--gpu", help="gpu-power-sample records, JSON lines")
    args = parser.parse_args()
    try:
        result = report(json.loads(Path(args.plan).read_text()), _jsonl(args.attempts), _jsonl(args.acceptance),
                        _jsonl(args.psu), _jsonl(args.gpu))
    except (OSError, ValueError) as error:
        parser.exit(1, f"run record rejected: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
