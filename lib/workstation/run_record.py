"""Run records (interface run-record-v1): identity, failures, acceptance and resource joins.

A run executes one declared plan (tasks, arm and latency targets) and records one
attempt per try at a task. Attempts carry task/run/attempt/arm identities, a
causal parent (the attempt a retry follows) and the digest of the plan they
started under. Request-trace events, resource observations and independent task
acceptance join on those IDs. Acceptance is the task acceptance checker's result
record, read as the checker wrote it and validated by the checker's own rule
(task_acceptance.py, docs/TASK-ACCEPTANCE.md).

Records hold identifiers, counts, timings, digests and watts only. Every object
has a closed key set and every identifier a bounded format and length. That
keeps prompts, answers, paths and URLs out by shape. It cannot recognise a
secret that fits the identifier format: never derive an identifier from secret
or personal data. Identifiers belong in records, traces and joins, never in
Prometheus labels.

Tool outcomes are recorded, but never decide task acceptance. Sampled PSU DC
power is whole-system output; GPU board samples are a component of that load.
Neither is task energy, which stays null until an independent per-task meter
exists. No third-party modules.
"""
import argparse
import bisect
import collections
import datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

import telemetry_power


SCHEMA = 1
INTERFACE = "run-record-v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
# Fixed operation and code names written by the services, never caller text.
TOKEN = re.compile(r"[a-z0-9][a-z0-9_:.-]{0,63}\Z")
PCI_ADDRESS = re.compile(r"[a-f0-9]{4}:[a-f0-9]{2}:[a-f0-9]{2}\.[0-7]\Z")
MAX_NS = 2**63
MAX_COUNT = 10**12
MAX_RECORDS = 10000  # Declared tasks, attempts and acceptance results in one report.
MAX_SAMPLES = 100000  # PSU samples, GPU samples and request-trace events in one report.
MAX_LINE = 4 * 1024 * 1024
MAX_CONTEXT = 1024

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
LATENCY_TARGETS = ("task_wall_seconds",)
IDENTITY_KEYS = ("task_id", "task_version", "run_id", "attempt_id", "arm_id", "parent_id", "cause")
DEPLOYMENT_KEYS = ("serving_contract", "model", "tokenizer_sha256", "template_sha256")
# One request header (and the same value in one environment variable) carries an
# attempt's identity and plan digest to the gateway and service-tools. The
# services parse it with apps/base/shared-api/run_trace.py; the tests keep the
# two parsers identical.
CONTEXT_HEADER = "X-Workstation-Run-Context"
CONTEXT_ENV = "WORKSTATION_RUN_CONTEXT"
CONTEXT_FIELDS = (("task", "task_id"), ("task_version", "task_version"), ("run", "run_id"),
                  ("attempt", "attempt_id"), ("arm", "arm_id"), ("parent", "parent_id"), ("cause", "cause"))
TRACE_SERVICES = ("gateway", "service-tools")
TRACE_RESULTS = ("ok", "tool_error", "refused", "limited", "backend_error", "cancelled", "error")
GPU_SENSORS = ("power1_average", "power1_input")
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


def _digest(value, name, nullable=True):
    if value is None and nullable:
        return
    _require(isinstance(value, str) and SHA256.match(value) is not None,
             f"{name} must be a sha256 hex digest" + (" or null" if nullable else ""))


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
    _require(isinstance(tasks, list) and 0 < len(tasks) <= MAX_RECORDS, "plan declares no tasks or too many")
    for task in tasks:
        _keys(task, ("task_id", "task_version"), name="task")
        _id(task["task_id"], "task_id")
        _id(task["task_version"], "task_version")
    _require(len({task["task_id"] for task in tasks}) == len(tasks), "duplicate declared task")
    return value


def plan_digest(plan):
    """SHA-256 of the canonical plan: every target, task, the arm and the declaration time."""
    return hashlib.sha256(canonical(validate_plan(plan))).hexdigest()


def bind(plan, identity, now_unix_ns=None):
    """Digest of the plan an attempt starts under, for the attempt's records and requests.

    Call it when the attempt starts. report() later refuses any attempt or
    request-trace event whose digest differs from the plan it is given, so a
    target, task or arm edited after an attempt started is rejected.
    """
    digest = plan_digest(plan)
    validate_identity(identity)
    _require(identity["run_id"] == plan["run_id"], "identity belongs to another run")
    _require(identity["arm_id"] == plan["arm_id"], "identity arm differs from the declared arm")
    declared = {task["task_id"]: task["task_version"] for task in plan["tasks"]}
    _require(declared.get(identity["task_id"]) == identity["task_version"], "identity names an undeclared task version")
    _require(now_unix_ns is None or now_unix_ns >= plan["declared_unix_ns"],
             "latency targets must be declared before the first attempt starts")
    return digest


def context_value(identity, plan_sha256):
    """The CONTEXT_HEADER / CONTEXT_ENV value for one attempt: identity and plan digest."""
    validate_identity(identity)
    _digest(plan_sha256, "plan_sha256", nullable=False)
    fields = [f"{name}={'-' if identity[key] is None else identity[key]}" for name, key in CONTEXT_FIELDS]
    return ";".join(["v1", *fields, "plan=" + plan_sha256])


def parse_context(value):
    """(identity, plan_sha256) from a run context value; the inverse of context_value()."""
    _require(isinstance(value, str) and len(value) <= MAX_CONTEXT, "run context must be a bounded string")
    parts = value.split(";")
    _require(len(parts) == len(CONTEXT_FIELDS) + 2 and parts[0] == "v1", "unsupported run context")
    identity = {}
    for part, (name, key) in zip(parts[1:-1], CONTEXT_FIELDS):
        label, _, item = part.partition("=")
        _require(label == name, "run context fields are missing or out of order")
        identity[key] = item
    if identity["parent_id"] == "-":
        identity["parent_id"] = None
    label, _, digest = parts[-1].partition("=")
    _require(label == "plan", "run context has no plan digest")
    _digest(digest, "run context plan", nullable=False)
    return validate_identity(identity), digest


def validate_attempt(value):
    _keys(value, ("schema", "kind", "interface", "identity", "plan_sha256", "deployment", "started_unix_ns",
                  "ended_unix_ns", "outcome", "failure", "tool_calls", "tokens"), name="attempt")
    _header(value, "attempt")
    validate_identity(value["identity"])
    _digest(value["plan_sha256"], "plan_sha256", nullable=False)
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


def validate_gpu_sample(value):
    _keys(value, ("schema", "kind", "interface", "device", "sensor", "unix_ns", "watts"), name="gpu sample")
    _header(value, "gpu-power-sample")
    _id(value["device"], "device")
    _require(value["sensor"] in GPU_SENSORS, "GPU sensor must be power1_average or power1_input")
    _ns(value["unix_ns"], "unix_ns")
    _require(type(value["watts"]) in (int, float) and math.isfinite(value["watts"])
             and 0 <= value["watts"] <= telemetry_power.MAX_POWER_WATTS, "invalid GPU watts")
    return value


def validate_trace(value):
    """One request-trace event written by the gateway or service-tools (run_trace.py)."""
    _keys(value, ("schema", "kind", "interface", "service", "operation", "identity", "plan_sha256",
                  "started_unix_ns", "ended_unix_ns", "result", "status", "code"), name="request trace")
    _header(value, "request-trace")
    _require(value["service"] in TRACE_SERVICES, "request trace names an unknown service")
    _require(isinstance(value["operation"], str) and TOKEN.match(value["operation"]) is not None,
             "request trace operation must be a fixed name")
    validate_identity(value["identity"])
    _digest(value["plan_sha256"], "plan_sha256", nullable=False)
    _ns(value["started_unix_ns"], "started_unix_ns")
    _ns(value["ended_unix_ns"], "ended_unix_ns")
    _require(value["ended_unix_ns"] >= value["started_unix_ns"], "request ends before it starts")
    _require(value["result"] in TRACE_RESULTS, "unknown request trace result")
    _require(value["status"] is None or (type(value["status"]) is int and 100 <= value["status"] <= 599),
             "request trace status must be an HTTP status or null")
    _require(value["code"] is None or (isinstance(value["code"], str) and TOKEN.match(value["code"]) is not None),
             "request trace code must be a fixed name or null")
    return value


def attempt_from_receipt(receipt, deployment=None, tokens=None, tool_calls=None):
    """Attempt record from a measurement.py run.json written with a plan and an identity.

    A deployment value that was not captured stays null; it is never guessed.
    """
    _require(isinstance(receipt, dict) and receipt.get("schema") == 1, "unsupported measurement receipt")
    started = receipt.get("started_unix_ns")
    elapsed = receipt.get("elapsed_seconds")
    _ns(started, "receipt started_unix_ns")
    _require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0, "invalid receipt elapsed")
    succeeded = receipt.get("status") == "measured-not-qualified" and not receipt.get("error_category")
    category = None if succeeded else MEASUREMENT_FAILURES.get(receipt.get("error_category"), "unknown")
    return validate_attempt({
        "schema": SCHEMA, "kind": "attempt", "interface": INTERFACE, "identity": receipt.get("identity"),
        "plan_sha256": receipt.get("plan_sha256"),
        "deployment": dict.fromkeys(DEPLOYMENT_KEYS) if deployment is None else deployment,
        "started_unix_ns": started, "ended_unix_ns": started + round(elapsed * 1e9),
        "outcome": "completed" if succeeded else "failed",
        "failure": None if succeeded else {"category": category,
                                           "source": "measurement:" + str(receipt.get("error_category") or "status")},
        "tool_calls": tool_calls, "tokens": tokens})


def acceptance_from_result(result):
    """What the join needs from one task acceptance result record.

    The checker's own validate_result() decides whether the record is well
    formed and whether its verdict follows from the recorded components, so
    there is one acceptance contract and a self-reported verdict is refused.
    Every joined fact comes from the record: a caller cannot supply or override
    a task, an attempt or a verdict. A result that names no run and attempt
    cannot be joined. A result whose acceptance source was not verified records
    no task; its verdict is "unavailable" and it joins on run and attempt alone.
    """
    # Imported only here: task_acceptance imports assistant_acceptance, which imports this module.
    import task_acceptance
    result = task_acceptance.validate_result(result)
    subject, protection = result["subject"], result["components"]["protection"]
    _require(subject["run_id"] is not None and subject["attempt_id"] is not None,
             "task acceptance result names no run and attempt; run the checker with --run-id and --attempt-id")
    verified = protection["status"] == task_acceptance.PASSED
    if verified:
        _id(result["spec_version"], "task acceptance spec_version")
    _digest(result["spec_sha256"], "task acceptance spec_sha256", nullable=not verified)
    _digest(result["checker_sha256"], "task acceptance checker_sha256", nullable=False)
    created = result["created_utc"]
    _require(isinstance(created, str) and len(created) <= 64, "task acceptance result has no creation time")
    moment = datetime.datetime.fromisoformat(created)
    _require(moment.tzinfo is not None, "task acceptance creation time has no time zone")
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return {"run_id": subject["run_id"], "attempt_id": subject["attempt_id"],
            "task_id": result["task_id"] if verified else None,
            "task_version": result["spec_version"] if verified else None,
            "verdict": result["verdict"], "checker_sha256": result["checker_sha256"],
            "spec_sha256": result["spec_sha256"] if verified else None,
            "evaluated_unix_ns": (moment - epoch) // datetime.timedelta(microseconds=1) * 1000}


def gpu_samples_from_snapshot(row):
    """gpu-power-sample records from one measurement.snapshot() row (a benchmark's telemetry.jsonl).

    One sample per amdgpu display device that reported board power: the averaged
    sensor when the driver has one, else the instantaneous one. A suspended or
    unreadable card yields no sample, never zero watts.
    """
    samples = []
    if not isinstance(row, dict) or type(row.get("unix_ns")) is not int or not 0 < row["unix_ns"] < MAX_NS:
        return samples
    hwmon, gpus = row.get("hwmon"), row.get("gpus_by_bdf")
    if not isinstance(hwmon, dict) or not isinstance(gpus, dict):
        return samples
    for path, entry in sorted(hwmon.items()):
        device = PurePosixPath(path).parent.parent.name
        if (not isinstance(entry, dict) or entry.get("name") != "amdgpu" or device not in gpus
                or PCI_ADDRESS.match(device) is None or not isinstance(entry.get("values"), dict)):
            continue
        for sensor in GPU_SENSORS:
            raw = entry["values"].get(sensor)
            if (isinstance(raw, str) and re.fullmatch(r"[0-9]{1,12}", raw)
                    and int(raw) / 1000000 <= telemetry_power.MAX_POWER_WATTS):
                samples.append({"schema": SCHEMA, "kind": "gpu-power-sample", "interface": INTERFACE, "device": device,
                                "sensor": sensor, "unix_ns": row["unix_ns"], "watts": int(raw) / 1000000})
                break
    return samples


def psu_timeline(samples):
    """Classify recorded PSU samples once into consecutive wall-time segments.

    ``samples`` are telemetry_power.collect()-shaped results in recorded order.
    Each consecutive pair goes through telemetry_power.advance(); only an
    "available" interval is covered. The timeline advances with the highest wall
    time seen. A sample at or before that time (the wall clock stepped back)
    adds no segment, and the stretch up to the next later sample is a
    clock_discontinuity gap, so replayed wall time is never counted twice.
    """
    state, previous, pending = None, None, None
    mark = first = None
    stepped, steps_back = False, 0
    segments = []  # (start_unix_ns, end_unix_ns, gap reason or None, watts at start, watts at end)
    for sample in samples:
        _require(isinstance(sample, dict), "PSU sample must be an object")
        state, reason = telemetry_power.advance(state, sample)
        if state["anchor"] is None:
            pending = "sample_unavailable"
            continue
        current = state["anchor"]
        if mark is None:
            first = mark = current["unix_ns"]
        elif current["unix_ns"] <= mark:
            stepped, steps_back = True, steps_back + 1
        else:
            if stepped:
                gap = "clock_discontinuity"
            elif reason == "available":
                gap = None
            else:
                gap = pending if reason == "new_interval" and pending else reason
            segments.append((mark, current["unix_ns"], gap, previous["watts"] if gap is None else None,
                             current["watts"] if gap is None else None))
            mark, stepped = current["unix_ns"], False
        previous, pending = current, None
    return {"segments": segments, "ends": [segment[1] for segment in segments], "first": first, "last": mark,
            "trailing": pending, "steps_back": steps_back}


def psu_window(timeline, start_ns, end_ns):
    """Sampled PSU DC output energy over one window of a psu_timeline(), covered segments only.

    A covered segment is integrated with the trapezoid rule and linearly
    interpolated where it straddles a window edge. Every uncovered stretch is
    reported as a gap with its reason; adjacent gaps with one reason are merged.
    """
    covered_joules = 0.0
    covered_ns = resets = 0
    gaps = []

    def gap(start, end, reason):
        if gaps and gaps[-1]["reason"] == reason and gaps[-1]["end_unix_ns"] == start:
            gaps[-1]["end_unix_ns"] = end
        else:
            gaps.append({"start_unix_ns": start, "end_unix_ns": end, "reason": reason})

    window = end_ns - start_ns
    first, last, segments = timeline["first"], timeline["last"], timeline["segments"]
    if first is None or first >= end_ns or last <= start_ns:
        if window:
            gap(start_ns, end_ns, "no_samples")
    else:
        if first > start_ns:
            gap(start_ns, first, "no_samples")
        index = bisect.bisect_right(timeline["ends"], start_ns)
        while index < len(segments) and segments[index][0] < end_ns:
            segment_start, segment_end, reason, watts_start, watts_end = segments[index]
            a, b = max(start_ns, segment_start), min(end_ns, segment_end)
            if reason is None:
                slope = (watts_end - watts_start) / (segment_end - segment_start)
                watts_a = watts_start + slope * (a - segment_start)
                watts_b = watts_start + slope * (b - segment_start)
                covered_joules += (watts_a + watts_b) * (b - a) / 2e9
                covered_ns += b - a
            else:
                gap(a, b, reason)
                resets += reason in ("boot_changed", "device_changed")
            index += 1
        if last < end_ns:
            gap(last, end_ns, timeline["trailing"] or "no_samples")
    return {"attribution": "system-psu-dc-output", "covered_seconds": covered_ns / 1e9,
            "window_seconds": window / 1e9, "coverage": covered_ns / window if window else None,
            "covered_joules": covered_joules, "gaps": gaps, "resets": resets,
            "assumptions": ["Corsair hwmon DC output, not AC wall energy",
                            "trapezoidal integral of covered intervals only; gaps contribute nothing",
                            "linear interpolation inside a covered interval at the window edges",
                            "whole-system load, including concurrent and idle work"]}


def gpu_index(samples):
    """Validated GPU samples ordered by time, for gpu_window()."""
    rows = sorted((sample["unix_ns"], sample["device"], sample["sensor"], sample["watts"])
                  for sample in map(validate_gpu_sample, samples))
    return [row[0] for row in rows], rows


def gpu_window(index, start_ns, end_ns):
    """Instantaneous GPU board power samples in a window of a gpu_index(); not integrated as energy."""
    times, rows = index
    devices = {}
    for _, device, sensor, watts in rows[bisect.bisect_left(times, start_ns):bisect.bisect_right(times, end_ns)]:
        entry = devices.setdefault(device, {"watts": [], "sensors": set()})
        entry["watts"].append(watts)
        entry["sensors"].add(sensor)
    return {"attribution": "gpu-board-component", "devices": {
        device: {"samples": len(entry["watts"]), "mean_watts": sum(entry["watts"]) / len(entry["watts"]),
                 "max_watts": max(entry["watts"]), "sensors": sorted(entry["sensors"])}
        for device, entry in sorted(devices.items())},
        "assumptions": ["sampled board power; part of the PSU DC load, never added to it",
                        "sample counts are not coverage of short spikes"]}


def overlaps(attempts):
    """For each attempt_id, how many other attempts ran during some part of its interval."""
    starts = sorted(attempt["started_unix_ns"] for attempt in attempts)
    ends = sorted(attempt["ended_unix_ns"] for attempt in attempts)
    instants = collections.Counter(attempt["started_unix_ns"] for attempt in attempts
                                   if attempt["started_unix_ns"] == attempt["ended_unix_ns"])
    counts = {}
    for attempt in attempts:
        start, end = attempt["started_unix_ns"], attempt["ended_unix_ns"]
        # Others that started before this one ended, less those that had already ended.
        count = bisect.bisect_left(starts, end) - bisect.bisect_right(ends, start)
        counts[attempt["identity"]["attempt_id"]] = count + (instants[start] if start == end else -1)
    return counts


def _chains(plan, attempts, digest):
    by_id = {}
    for attempt in attempts:
        identity = attempt["identity"]
        _require(identity["attempt_id"] not in by_id, "duplicate attempt_id")
        _require(identity["run_id"] == plan["run_id"], "attempt belongs to another run")
        _require(identity["arm_id"] == plan["arm_id"], "attempt arm differs from the declared arm")
        _require(attempt["plan_sha256"] == digest,
                 "attempt was recorded under a different plan; a plan cannot change after an attempt starts")
        by_id[identity["attempt_id"]] = attempt
    declared = {task["task_id"]: task["task_version"] for task in plan["tasks"]}
    chains = {task_id: [] for task_id in declared}
    roots, children = {}, {}
    for attempt_id, attempt in by_id.items():
        identity = attempt["identity"]
        _require(declared.get(identity["task_id"]) == identity["task_version"], "attempt for an undeclared task version")
        _require(attempt["started_unix_ns"] >= plan["declared_unix_ns"],
                 "latency targets must be declared before the first attempt starts")
        parent = identity["parent_id"]
        if parent is None:
            _require(identity["task_id"] not in roots, "a task has more than one initial attempt in a run")
            roots[identity["task_id"]] = attempt_id
        else:
            _require(parent in by_id, "retry names an unknown parent attempt")
            parent_record = by_id[parent]
            _require(parent_record["identity"]["task_id"] == identity["task_id"], "retry parent is another task")
            _require(parent_record["ended_unix_ns"] <= attempt["started_unix_ns"], "retry starts before its parent ends")
            _require(parent not in children, "an attempt has more than one retry")
            children[parent] = attempt_id
    for task_id in declared:
        node = roots.get(task_id)
        while node is not None:
            chains[task_id].append(by_id[node])
            node = children.get(node)
    _require(sum(map(len, chains.values())) == len(by_id), "attempt chain is disconnected or cyclic")
    return chains


def _traces(plan, attempts, traces, digest):
    """Request-trace events grouped by recorded attempt, and the count that match none."""
    by_attempt = {attempt["identity"]["attempt_id"]: attempt for attempt in attempts}
    grouped, unmatched = {}, 0
    for trace in traces:
        validate_trace(trace)
        identity = trace["identity"]
        _require(identity["run_id"] == plan["run_id"], "request trace belongs to another run")
        _require(trace["plan_sha256"] == digest,
                 "request trace was recorded under a different plan; a plan cannot change after an attempt starts")
        attempt = by_attempt.get(identity["attempt_id"])
        if attempt is None:
            unmatched += 1
            continue
        _require(identity == attempt["identity"], "request trace identity differs from its attempt")
        grouped.setdefault(identity["attempt_id"], []).append(trace)
    return grouped, unmatched


def _trace_summary(events, attempt):
    services = {}
    for event in events:
        row = services.setdefault(event["service"], {"requests": 0, "by_result": {}, "by_operation": {}})
        row["requests"] += 1
        for key, value in (("by_result", event["result"]), ("by_operation", event["operation"])):
            row[key][value] = row[key].get(value, 0) + 1
    return {"requests": len(events), "services": services,
            "outside_attempt_window": sum(1 for event in events
                                          if event["started_unix_ns"] < attempt["started_unix_ns"]
                                          or event["ended_unix_ns"] > attempt["ended_unix_ns"])}


def report(plan, attempts, acceptances, psu_samples=(), gpu_samples=(), traces=()):
    """Join independent acceptance, request traces and resources to every attempt in one run.

    ``acceptances`` are task acceptance result records as the checker wrote them.
    """
    import task_acceptance  # Imported only here, as in acceptance_from_result().
    digest = plan_digest(plan)
    _require(len(attempts) <= MAX_RECORDS and len(acceptances) <= MAX_RECORDS, "too many records")
    _require(max(len(psu_samples), len(gpu_samples), len(traces)) <= MAX_SAMPLES, "too many samples or trace events")
    for attempt in attempts:
        validate_attempt(attempt)
    chains = _chains(plan, attempts, digest)
    verdicts = {}
    by_attempt = {a["identity"]["attempt_id"]: a for a in attempts}
    for acceptance in map(acceptance_from_result, acceptances):
        attempt = by_attempt.get(acceptance["attempt_id"])
        _require(attempt is not None and acceptance["run_id"] == plan["run_id"]
                 and acceptance["task_id"] in (None, attempt["identity"]["task_id"])
                 and acceptance["task_version"] in (None, attempt["identity"]["task_version"]),
                 "acceptance does not match a recorded attempt")
        _require(acceptance["attempt_id"] not in verdicts, "more than one acceptance result for an attempt")
        _require(acceptance["evaluated_unix_ns"] >= attempt["ended_unix_ns"], "acceptance predates the attempt's end")
        verdicts[acceptance["attempt_id"]] = acceptance
    traced, unmatched_traces = _traces(plan, attempts, traces, digest)
    timeline = psu_timeline(psu_samples)
    gpu = gpu_index(gpu_samples)
    overlapping = overlaps(attempts)
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
            events = traced.get(identity["attempt_id"])
            rows.append({"attempt_id": identity["attempt_id"], "parent_id": identity["parent_id"],
                         "cause": identity["cause"], "outcome": attempt["outcome"], "failure_category": category,
                         "acceptance": verdict["verdict"] if verdict else "missing",
                         "acceptance_checker_sha256": verdict["checker_sha256"] if verdict else None,
                         "acceptance_spec_sha256": verdict["spec_sha256"] if verdict else None,
                         "wall_seconds": wall, "tokens": attempt["tokens"], "tool_calls": attempt["tool_calls"],
                         "request_trace": _trace_summary(events, attempt) if events else None,
                         "overlapping_attempts": overlapping[identity["attempt_id"]],
                         "psu_dc": psu_window(timeline, attempt["started_unix_ns"], attempt["ended_unix_ns"]),
                         "gpu_component": gpu_window(gpu, attempt["started_unix_ns"], attempt["ended_unix_ns"]),
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
        run_psu = psu_window(timeline, start, end)
        run_gpu = gpu_window(gpu, start, end)
    else:
        run_psu = run_gpu = None
    deployments = sorted({canonical(a["deployment"]) for a in attempts})
    accepted_tasks = [t for t in tasks if t["status"] == "accepted"]
    return {
        "schema": SCHEMA, "kind": "run-record-report", "interface": INTERFACE,
        "acceptance_result": {"kind": task_acceptance.RESULT_KIND, "schema": task_acceptance.SCHEMA},
        "run_id": plan["run_id"], "arm_id": plan["arm_id"], "plan_sha256": digest,
        "plan_binding": "every attempt and request-trace event carries this digest from its start",
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
                     "attempts_with_request_trace": len(traced),
                     "request_trace_events": len(traces), "unmatched_request_trace_events": unmatched_traces,
                     "attempts": len(attempts), "psu_dc_run_coverage": run_psu["coverage"] if run_psu else None,
                     "psu_samples": len(psu_samples), "psu_clock_steps_back": timeline["steps_back"],
                     "gpu_component_samples": len(gpu_samples)},
        "energy": {"psu_dc_run_window": run_psu, "gpu_component_run_window": run_gpu,
                   "task_attributable": {"joules": None, "reason": TASK_ENERGY_REASON}},
        "tasks": tasks}


def _lines(path, limit, name):
    """Non-empty lines of a file, at most ``limit`` of them and MAX_LINE bytes each."""
    count = 0
    with open(path, "rb") as source:
        while True:
            line = source.readline(MAX_LINE + 1)
            if not line:
                return
            _require(len(line) <= MAX_LINE, f"{name} line is too long")
            if line.strip():
                count += 1
                _require(count <= limit, f"too many {name} records")
                yield line


def _jsonl(path, limit=MAX_RECORDS, name="input"):
    return [] if path is None else [json.loads(line) for line in _lines(path, limit, name)]


def _write_lines(path, rows):
    """Write JSON lines to a new file; never replace an existing one."""
    count = 0
    with open(path, "x") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            count += 1
    return count


def resources_from_telemetry(telemetry, psu, gpu):
    """Write PSU and GPU run-record samples from a benchmark's telemetry.jsonl; returns the counts."""
    rows = psu_available = 0
    gpu_rows = []

    def psu_samples():
        nonlocal rows, psu_available
        for line in _lines(telemetry, MAX_SAMPLES, "telemetry"):
            row = json.loads(line)
            rows += 1
            sample = telemetry_power.sample_from_snapshot(row)
            psu_available += sample["watts"] is not None
            gpu_rows.extend(gpu_samples_from_snapshot(row))
            _require(len(gpu_rows) <= MAX_SAMPLES, "too many GPU samples")
            yield sample

    _write_lines(psu, psu_samples())
    return {"telemetry_rows": rows, "psu_samples": rows, "psu_samples_available": psu_available,
            "gpu_samples": _write_lines(gpu, gpu_rows)}


def traces_from_logs(logs, run_id, output):
    """Write one run's request-trace events from gateway and service-tools log files.

    Other lines are counted, never copied: a log can hold other runs' events and
    unrelated process output.
    """
    _id(run_id, "run_id")
    counts = {"kept": 0, "other_runs": 0, "ignored_lines": 0}

    def events():
        for log in logs:
            for line in _lines(log, 10 * MAX_SAMPLES, "log"):
                try:
                    event = validate_trace(json.loads(line))
                except (ValueError, RecursionError):
                    counts["ignored_lines"] += 1
                    continue
                if event["identity"]["run_id"] != run_id:
                    counts["other_runs"] += 1
                    continue
                counts["kept"] += 1
                _require(counts["kept"] <= MAX_SAMPLES, "too many request-trace events")
                yield event

    _write_lines(output, events())
    return counts


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    action = actions.add_parser("report", help="join acceptance, traces and resources to one run's attempts")
    action.add_argument("--plan", required=True)
    action.add_argument("--attempts", required=True, help="attempt records, JSON lines")
    action.add_argument("--acceptance", required=True,
                        help="task acceptance result records as the checker wrote them, JSON lines")
    action.add_argument("--psu", help="PSU samples in recorded order, JSON lines")
    action.add_argument("--gpu", help="gpu-power-sample records, JSON lines")
    action.add_argument("--traces", help="request-trace events, JSON lines")
    action = actions.add_parser("context", help="bind an attempt to its plan; print the run context to send")
    action.add_argument("--plan", required=True)
    action.add_argument("--identity", required=True)
    action = actions.add_parser("attempt", help="attempt record from a measurement run directory")
    action.add_argument("run_directory")
    action.add_argument("--serving-contract")
    action.add_argument("--model")
    action.add_argument("--tokenizer-sha256")
    action.add_argument("--template-sha256")
    action.add_argument("--tokens", nargs=2, type=int, metavar=("PROMPT", "COMPLETION"))
    action.add_argument("--tool-calls", nargs=2, type=int, metavar=("SUCCEEDED", "FAILED"))
    action = actions.add_parser("resources", help="PSU and GPU samples from a measurement telemetry.jsonl")
    action.add_argument("telemetry")
    action.add_argument("--psu", required=True, help="new PSU samples file")
    action.add_argument("--gpu", required=True, help="new GPU samples file")
    action = actions.add_parser("traces", help="one run's request-trace events from service log files")
    action.add_argument("--run-id", required=True)
    action.add_argument("--output", required=True, help="new request-trace file")
    action.add_argument("log", nargs="+")
    action = actions.add_parser("acceptance", help="validate checker result files; print them as JSON lines for report")
    action.add_argument("result", nargs="+")
    args = parser.parse_args()
    try:
        if args.action == "report":
            result = report(json.loads(Path(args.plan).read_text()), _jsonl(args.attempts, name="attempt"),
                            _jsonl(args.acceptance, name="acceptance"), _jsonl(args.psu, MAX_SAMPLES, "PSU"),
                            _jsonl(args.gpu, MAX_SAMPLES, "GPU"), _jsonl(args.traces, MAX_SAMPLES, "request-trace"))
        elif args.action == "context":
            identity = json.loads(Path(args.identity).read_text())
            digest = bind(json.loads(Path(args.plan).read_text()), identity)
            result = {"header": CONTEXT_HEADER, "environment": CONTEXT_ENV, "plan_sha256": digest,
                      "value": context_value(identity, digest)}
        elif args.action == "attempt":
            receipt = json.loads((Path(args.run_directory) / "run.json").read_text())
            result = attempt_from_receipt(
                receipt, {"serving_contract": args.serving_contract, "model": args.model,
                          "tokenizer_sha256": args.tokenizer_sha256, "template_sha256": args.template_sha256},
                dict(zip(("prompt", "completion"), args.tokens)) if args.tokens else None,
                dict(zip(("succeeded", "failed"), args.tool_calls)) if args.tool_calls else None)
        elif args.action == "resources":
            result = resources_from_telemetry(args.telemetry, args.psu, args.gpu)
        elif args.action == "traces":
            result = traces_from_logs(args.log, args.run_id, args.output)
        else:
            _require(len(args.result) <= MAX_RECORDS, "too many acceptance results")
            import task_acceptance  # Imported only here, as in acceptance_from_result().
            results = [task_acceptance.load_result(path) for path in args.result]
            for result in results:
                acceptance_from_result(result)
            lines = [json.dumps(result, sort_keys=True, allow_nan=False) for result in results]
    except (OSError, ValueError) as error:
        parser.exit(1, f"run record rejected: {error}\n")
    if args.action == "acceptance":
        print("\n".join(lines))  # The checker's records, unchanged, one per line.
    elif args.action == "attempt":
        print(json.dumps(result, sort_keys=True, allow_nan=False))  # One JSON line, ready to append.
    else:
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
