"""Timestamp domains and causal per-round offset bounds for one Kineto/ROCm window.

Source basis (nine byte-matched Kineto blobs, commit 15f4784a): asynchronous
GPU records are stored in a monotonic-like clock and shifted once by
getTimeOffset() during processing; runtime-API records are approximate-clock
readings passed through get_time_converter(); torch RecordFunction events use
torch's own converter. The trace carries no calibration values. This module
keeps four domains separate and derives offset *intervals* only from semantic
happens-before relations inside each observed scheduler round:

  HOST_MONO -> CPU_OP   observer journal brackets around the round's OBS marker
  CPU_OP    -> RUNTIME  runtime record attributed by its unique "External id"
                        to exactly one enclosing CPU event on the same thread
  RUNTIME   -> GPU      a dispatch cannot begin before its launch call begins;
                        a synchronous copy call cannot return before its copy ends

off(X, Y) is the constant with y = x + off for one physical instant, assumed
constant inside one round only. A contradiction inside a round refuses the pair
(the causal premises failed). A one-sided or over-ceiling round is refused by
itself. Bound rounds must overlap, otherwise the offset moved between rounds by
more than the bound (clock step/drift) and the pair is refused. Conversions use
the interval of the named round; without a round they need every round bound
and use the union. Results are bounds conditional on HIP/Kineto semantics, not
profiler-supplied calibration values.
"""
from decimal import Decimal
import json

DOMAINS = ("HOST_MONO", "CPU_OP", "RUNTIME", "GPU")
CATEGORY_DOMAIN = {"cpu_op": "CPU_OP", "user_annotation": "CPU_OP",
                   "cuda_runtime": "RUNTIME", "kernel": "GPU",
                   "gpu_memcpy": "GPU", "gpu_memset": "GPU",
                   "gpu_user_annotation": "GPU"}
# Synchronous host calls whose own correlated dispatch must finish before return.
SYNC_COPY_APIS = frozenset({"hipMemcpyWithStream", "hipMemcpy"})
# R2: only device-to-host copies (hipMemcpyKind 2 in the trace args) are admitted as
# "copy ends before the call returns" lower bounds; other directions give no bound.
D2H_KIND = "2"
# R4: declared before any new capture; an assumption, not a calibration value. Any two
# domains may differ in rate by at most RATE_BOUND within one scheduler round, so each
# converted pair interval widens by ceil(RATE_BOUND * round marker span) on each side.
RATE_BOUND = Decimal("0.0001")
CLAIM_FACTOR = 2
LAUNCH_APIS = frozenset({"hipLaunchKernel", "hipModuleLaunchKernel", "hipExtModuleLaunchKernel",
                         "hipGraphLaunch", "hipMemcpyAsync", "hipMemsetAsync",
                         "hipMemcpyWithStream", "hipMemcpy"})
PAIRS = ("HOST_MONO->CPU_OP", "CPU_OP->RUNTIME", "RUNTIME->GPU")
# The trace writes the SDK version as a JSON number; it is parsed as an exact Decimal.
EXPECTED_METADATA = {"schemaVersion": 1, "displayTimeUnit": "ms",
                     "rocprofiler-sdk_version": Decimal("1.4"),
                     "hip_runtime_version": 71726416, "hip_driver_version": 71726416}
MAX_EVENTS = 250000


def need(ok, message):
    if not ok:
        raise ValueError("clock domains: " + message)


def load(raw):
    """Parse trace bytes with exact decimals and duplicate-key refusal."""
    need(type(raw) is bytes and len(raw) <= 32 << 20, "bounded trace bytes")
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    def constant(_):
        need(False, "non-finite JSON constant")
    return json.loads(raw, object_pairs_hook=pairs, parse_float=Decimal, parse_constant=constant)


def ns(value):
    """Trace microseconds -> exact integer nanoseconds; refuses other resolutions."""
    need(type(value) in (int, Decimal), "numeric trace time")
    scaled = Decimal(value) * 1000
    need(scaled.is_finite() and scaled == scaled.to_integral_value() and scaled >= 0,
         "trace time is not an exact non-negative nanosecond multiple of microseconds")
    return int(scaled)


def bind(trace, identity, expected=EXPECTED_METADATA):
    """Bind revision, unit, rank and process identity before any timing use."""
    need(type(trace) is dict and type(trace.get("traceEvents")) is list
         and 0 < len(trace["traceEvents"]) <= MAX_EVENTS, "bounded trace event list")
    for key, value in expected.items():
        need(key in trace and trace[key] == value and type(trace[key]) is type(value),
             "trace revision/unit metadata: " + key)
    base = trace.get("baseTimeNanoseconds")
    need(type(base) is int and base > 0, "integer trace base time")
    info = trace.get("distributedInfo", {})
    need(info.get("rank") == identity["rank"] and info.get("world_size") == 2, "trace rank identity")
    return {"base_ns": base, "rank": identity["rank"], "pid": identity["pid"],
            "device": identity["rank"], "metadata": {k: str(trace[k]) for k in expected}}


def _interval(event):
    begin = ns(event.get("ts"))
    duration = ns(event.get("dur"))
    return begin, begin + duration, duration


def _unique(rows, message):
    need(len(rows) == 1, message)
    return rows[0]


class Bounds:
    """Happens-before constraints for one ordered domain pair, grouped by round."""
    def __init__(self, pair):
        need(pair[0] in DOMAINS and pair[1] in DOMAINS and pair[0] != pair[1], "declared domain pair")
        self.pair = pair
        self.rows = []  # (round or None, lower or None, upper or None)

    def before(self, x, y, group):
        """x (source domain) happened no later than y (target domain)."""
        self.rows.append((group, None, y - x))

    def after(self, y, x, group):
        """y (target domain) happened no later than x (source domain)."""
        self.rows.append((group, y - x, None))

    def result(self, ceiling_ns):
        name = "->".join(self.pair)
        groups = sorted({g for g, _, _ in self.rows if g is not None})
        need(groups, "no in-round constraints for " + name)
        rounds = {}
        for g in groups:
            lows = [lo for k, lo, _ in self.rows if k == g and lo is not None]
            ups = [up for k, _, up in self.rows if k == g and up is not None]
            if not lows or not ups:
                rounds[g] = {"status": "REFUSED", "reason": "one-sided round constraints"}
                continue
            lower, upper = max(lows), min(ups)
            need(lower <= upper, "contradictory constraints for %s in round %r" % (name, g))
            row = {"lower_ns": lower, "upper_ns": upper, "width_ns": upper - lower,
                   "constraints": len(lows) + len(ups)}
            row["status"] = "BOUND" if upper - lower <= ceiling_ns else "REFUSED"
            if row["status"] == "REFUSED":
                row["reason"] = "round bound wider than frozen ceiling"
            rounds[g] = row
        bound = [r for r in rounds.values() if r["status"] == "BOUND"]
        need(bound, "no round within the frozen ceiling for " + name)
        for a in bound:
            for b in bound:
                need(a["lower_ns"] <= b["upper_ns"],
                     "offset moved between rounds beyond the bound (step/drift) for " + name)
        lower = min(r["lower_ns"] for r in bound)
        upper = max(r["upper_ns"] for r in bound)
        return {"pair": list(self.pair), "status": "BOUND", "rounds": {str(k): v for k, v in rounds.items()},
                "all_rounds_bound": len(bound) == len(rounds), "lower_ns": lower, "upper_ns": upper,
                "width_ns": upper - lower, "constraints": sum(1 for g, _, _ in self.rows if g is not None),
                "out_of_round_constraints": sum(1 for g, _, _ in self.rows if g is None)}


def markers(events, identity, rows):
    """Exactly one OBS marker per journal row, keyed by its sequence."""
    need(type(rows) is list and rows, "journal rows")
    out = []
    for row in rows:
        clock = row["host_ns"]
        need(type(clock) is list and len(clock) == 4 and all(type(v) is int and v >= 0 for v in clock)
             and clock == sorted(clock), "four monotone host clocks")
        name = "OBS:%s:%d" % (identity["nonce"], row["sequence"])
        marker = _unique([e for e in events if e.get("ph") == "X" and e.get("cat") == "user_annotation"
                          and e.get("name") == name], "one observer marker " + name)
        need(marker.get("pid") == identity["pid"], "marker worker identity")
        begin, end, _ = _interval(marker)
        out.append({"sequence": row["sequence"], "clock": clock, "span": (begin, end), "tid": marker.get("tid")})
    spans = sorted(m["span"] for m in out)
    need(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), "observer markers overlap")
    return out


def _round(marks, time_ns, margin_ns=0):
    """The sequence whose marker definitely contains a CPU-domain instant, else None."""
    hits = [m["sequence"] for m in marks if m["span"][0] + margin_ns <= time_ns <= m["span"][1] - margin_ns]
    return hits[0] if len(hits) == 1 else None


def unit_check(marks, ratio=(Decimal("0.5"), Decimal("1"))):
    """Trace microseconds vs host nanoseconds: each marker must fill most of its bracket."""
    for m in marks:
        bracket = m["clock"][1] - m["clock"][0]
        duration = m["span"][1] - m["span"][0]
        need(bracket > 0 and ratio[0] * bracket <= duration <= ratio[1] * bracket,
             "marker duration is incompatible with nanosecond host bracket (unit check)")
    return {"rows": len(marks), "ratio_bounds": [str(ratio[0]), str(ratio[1])]}


def host_cpu(events, identity, marks, ceiling_ns, start_returned_ns=None):
    """Observer brackets; the optional post-start host clock tightens round 1 only."""
    bounds = Bounds(("HOST_MONO", "CPU_OP"))
    if start_returned_ns is not None:
        first = marks[0]
        need(type(start_returned_ns) is int and first["clock"][0] <= start_returned_ns <= first["clock"][1],
             "profiler start clock inside round 1 bracket")
        bounds.before(start_returned_ns, first["span"][0], first["sequence"])
    for m in marks:
        bounds.before(m["clock"][0], m["span"][0], m["sequence"])
        bounds.after(m["span"][1], m["clock"][1], m["sequence"])
    return bounds.result(ceiling_ns)


def cpu_runtime(events, identity, marks, ceiling_ns):
    """CPU_OP->RUNTIME bound from exact External-id attribution on one thread."""
    owners = {}
    for e in events:
        if e.get("ph") == "X" and e.get("cat") in ("cpu_op", "user_annotation"):
            x = e.get("args", {}).get("External id")
            if x is not None:
                need(type(x) is int, "integer External id")
                owners.setdefault(x, []).append(e)
    bounds = Bounds(("CPU_OP", "RUNTIME"))
    for e in events:
        if e.get("ph") != "X" or e.get("cat") != "cuda_runtime":
            continue
        x = e.get("args", {}).get("External id")
        if x is None:
            continue
        owner = _unique(owners.get(x, []), "ambiguous or missing External-id owner %r" % (x,))
        need(owner.get("pid") == e.get("pid") == identity["pid"] and owner.get("tid") == e.get("tid"),
             "External-id owner thread identity")
        c0, c1, _ = _interval(owner)
        r0, r1, _ = _interval(e)
        group = _round(marks, c0)
        bounds.before(c0, r0, group)
        bounds.after(r1, c1, group)
    return bounds.result(ceiling_ns)


def runtime_gpu(events, identity, marks, cpu_runtime_row, ceiling_ns):
    """RUNTIME->GPU bound from unique launch correlations and synchronous copies."""
    runtime, dispatch = {}, {}
    for e in events:
        if e.get("ph") != "X":
            continue
        domain = CATEGORY_DOMAIN.get(e.get("cat"))
        if domain == "RUNTIME":
            need(e.get("pid") == identity["pid"], "runtime record process identity")
            c = e.get("args", {}).get("correlation")
            need(type(c) is int and c >= 0, "runtime correlation")
            runtime.setdefault(c, []).append(e)
        elif domain == "GPU" and e.get("cat") != "gpu_user_annotation":
            args = e.get("args", {})
            need(args.get("device") == identity["rank"] and e.get("pid") == identity["rank"],
                 "GPU record device identity")
            c = args.get("correlation")
            need(type(c) is int and c >= 0, "dispatch correlation")
            dispatch.setdefault(c, []).append(e)
    for c, rows in runtime.items():
        need(len(rows) == 1, "ambiguous runtime correlation %d" % c)
    # Group runtime instants by marker only when the CPU_OP<->RUNTIME bound makes it definite.
    margin = max(abs(cpu_runtime_row["lower_ns"]), abs(cpu_runtime_row["upper_ns"]))
    bounds = Bounds(("RUNTIME", "GPU")); orphans = 0; ignored_copies = 0
    for c, rows in dispatch.items():
        api = runtime.get(c)
        if api is None:
            orphans += 1
            continue
        api = api[0]
        need(api.get("name") in LAUNCH_APIS, "dispatch correlated to non-launch API " + str(api.get("name")))
        a0, a1, _ = _interval(api)
        spans = [_interval(e) for e in rows]
        need(all(d > 0 for _, _, d in spans), "positive dispatch duration")
        if api.get("name") != "hipGraphLaunch":
            need(len(rows) == 1, "ambiguous direct-launch dispatch set %d" % c)
        group = _round(marks, a0, margin) if api.get("tid") in {m["tid"] for m in marks} else None
        bounds.before(a0, min(s for s, _, _ in spans), group)
        if api.get("name") in SYNC_COPY_APIS:
            if api.get("args", {}).get("kind") != D2H_KIND:
                ignored_copies += 1
                continue
            bounds.after(max(t for _, t, _ in spans), a1, _round(marks, a1, margin) if group is not None else None)
    row = bounds.result(ceiling_ns)
    row.update(orphan_dispatch_correlations=orphans, runtime_records=len(runtime),
               non_d2h_sync_copies_without_lower_bound=ignored_copies,
               dispatch_correlations=len(dispatch))
    return row


def compose(*rows, sequence=None):
    """Sum adjacent pair intervals for one round (or the all-round union)."""
    need(rows, "pairs to compose")
    for left, right in zip(rows, rows[1:]):
        need(left["pair"][1] == right["pair"][0], "non-adjacent domain chain")
    parts = []
    for row in rows:
        need(row["status"] == "BOUND", "conversion through refused pair " + "->".join(row["pair"]))
        if sequence is None:
            need(row["all_rounds_bound"], "all-round conversion needs every round bound for "
                 + "->".join(row["pair"]))
            parts.append((row["lower_ns"], row["upper_ns"]))
        else:
            r = row["rounds"].get(str(sequence))
            need(r is not None and r["status"] == "BOUND",
                 "round %r not bound for %s" % (sequence, "->".join(row["pair"])))
            parts.append((r["lower_ns"], r["upper_ns"]))
    allowance = sum(row.get("allowance_ns", {}).get("all" if sequence is None else str(sequence), 0)
                    for row in rows)
    return {"pair": [rows[0]["pair"][0], rows[-1]["pair"][1]], "sequence": sequence,
            "lower_ns": sum(p[0] for p in parts) - allowance, "upper_ns": sum(p[1] for p in parts) + allowance,
            "width_ns": sum(p[1] - p[0] for p in parts) + 2 * allowance,
            "constraint_width_ns": sum(p[1] - p[0] for p in parts), "allowance_ns": allowance,
            "via": [r["pair"] for r in rows]}


def stream_overlaps(events, identity):
    """Same-stream GPU dispatch overlap is impossible; any overlap refuses timing use."""
    streams = {}
    for e in events:
        if e.get("ph") == "X" and e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset"):
            args = e.get("args", {})
            need(args.get("device") == identity["rank"], "dispatch device identity")
            begin, end, duration = _interval(e)
            need(duration > 0, "positive dispatch duration")
            streams.setdefault((args.get("device"), args.get("stream")), []).append((begin, end))
    for key, spans in streams.items():
        last = None
        for begin, end in sorted(spans):
            need(last is None or begin >= last, "impossible same-stream dispatch overlap on %r" % (key,))
            last = end if last is None else max(last, end)
    return {"streams": len(streams), "overlaps": 0}


def calibrate(trace, identity, rows, ceilings, start_returned_ns=None):
    """Hard identity/unit/overlap checks, then one explicit status per domain pair.

    A pair that is contradictory, has no bound round or moves between rounds is
    REFUSED with its reason; conversions through it refuse later.
    """
    need(set(ceilings) == set(PAIRS), "frozen ceilings")
    binding = bind(trace, identity)
    events = trace["traceEvents"]
    overlap = stream_overlaps(events, identity)
    marks = markers(events, identity, rows)
    units = unit_check(marks)
    if start_returned_ns is not None:  # producer receipt integrity is a hard check
        need(type(start_returned_ns) is int and marks[0]["clock"][0] <= start_returned_ns <= marks[0]["clock"][1],
             "profiler start clock inside round 1 bracket")
    pairs = {}
    for key in PAIRS:
        try:
            if key == "HOST_MONO->CPU_OP":
                pairs[key] = host_cpu(events, identity, marks, ceilings[key], start_returned_ns)
            elif key == "CPU_OP->RUNTIME":
                pairs[key] = cpu_runtime(events, identity, marks, ceilings[key])
            else:
                need(pairs["CPU_OP->RUNTIME"]["status"] == "BOUND", "runtime grouping needs CPU_OP->RUNTIME")
                pairs[key] = runtime_gpu(events, identity, marks, pairs["CPU_OP->RUNTIME"], ceilings[key])
        except ValueError as error:
            pairs[key] = {"pair": key.split("->"), "status": "REFUSED", "reason": str(error)}
    # R4: per-round drift allowance from the declared rate bound and the round marker span.
    allowance = {str(m["sequence"]): int((RATE_BOUND * (m["span"][1] - m["span"][0])).to_integral_value(
        rounding="ROUND_CEILING")) for m in marks}
    allowance["all"] = max(allowance.values())
    for key in PAIRS:
        if pairs[key]["status"] == "BOUND":
            pairs[key]["allowance_ns"] = allowance
    chains = []
    for m in marks:
        try:
            chains.append(compose(*(pairs[k] for k in PAIRS), sequence=m["sequence"]))
        except ValueError as error:
            chains.append({"sequence": m["sequence"], "status": "REFUSED", "reason": str(error)})
    complete = all("reason" not in c for c in chains)
    return {"status": "CAUSAL_OFFSET_BOUNDS" if complete else "PARTIAL_OFFSET_BOUNDS",
            "binding": binding, "stream_check": overlap, "unit_check": units,
            "pairs": pairs, "rounds": chains,
            "assumptions": ["HIP dispatch begins no earlier than its launch call begins",
                            "a synchronous copy call returns no earlier than its copy dispatch ends",
                            "a runtime record's External id names its enclosing CPU event",
                            "observer host_ns[0:2] brackets the OBS marker",
                            "offset constant inside one scheduler round up to the declared rate bound; rounds must agree",
                            "relative rate error between any two domains <= RATE_BOUND (1e-4) within one round; "
                            "converted intervals widen by ceil(RATE_BOUND * marker span) per pair and side",
                            "only device-to-host synchronous copies (args.kind == '2') bound RUNTIME->GPU from above",
                            "producer order: KinetoBackend.begin reads CLOCK_MONOTONIC after profile.start() returns "
                            "and before the round-1 OBS marker opens (start clock <= marker begin)",
                            "cross-domain claims need an effect >= CLAIM_FACTOR x the composed width incl. allowance"],
            "profiler_calibration_values_used": False, "cross_rank_alignment": False}


def to_domain(value_ns, source, target, calibration, sequence=None):
    """Map one point to an interval in another domain for one round (or all rounds)."""
    need(type(value_ns) is int, "integer nanoseconds")
    need(source in DOMAINS and target in DOMAINS, "declared domain")
    if source == target:
        return [value_ns, value_ns]
    order = list(DOMAINS)
    i, j = order.index(source), order.index(target)
    rows = [calibration["pairs"][a + "->" + b]
            for a, b in zip(order[min(i, j):max(i, j)], order[min(i, j) + 1:max(i, j) + 1])]
    chain = compose(*rows, sequence=sequence)
    if i < j:
        return [value_ns + chain["lower_ns"], value_ns + chain["upper_ns"]]
    return [value_ns - chain["upper_ns"], value_ns - chain["lower_ns"]]


def ordered(a_interval, b_interval):
    """Raw interval order only; never a reportable claim (use claim_gap)."""
    return a_interval[1] <= b_interval[0]


def claim_gap(effect_ns, chain):
    """R4 claim rule: a cross-domain ordering or gap is reportable only when its magnitude is
    at least CLAIM_FACTOR x the composed width (constraint width plus drift allowance)."""
    need(type(effect_ns) in (int, Decimal) and type(chain.get("width_ns")) in (int, Decimal) and "allowance_ns" in chain,
         "claim needs a composed chain with its allowance")
    resolved = abs(effect_ns) >= CLAIM_FACTOR * chain["width_ns"]
    return {"effect_ns": effect_ns, "width_ns": chain["width_ns"], "allowance_ns": chain["allowance_ns"],
            "factor": CLAIM_FACTOR, "status": "CLAIMABLE" if resolved else "BELOW_RESOLUTION"}


def window_calibration(trace, identity, rows, ceilings, export_row):
    """Matched readout entry: the round-1 post-start host clock is required and consumed."""
    need(type(export_row) is dict and type(export_row.get("profile_start_returned_ns")) is int,
         "matched export row must carry the post-start host clock")
    return calibrate(trace, identity, rows, ceilings, export_row["profile_start_returned_ns"])


def cross_rank(left, right, left_boot, right_boot, sequence):
    """GPU(rank0)->GPU(rank1) for one shared round via the host monotonic clock of one boot."""
    need(type(left_boot) is str and left_boot and left_boot == right_boot, "same host boot required")
    need(left["binding"]["rank"] == 0 and right["binding"]["rank"] == 1, "ordered ranks")
    a = compose(*(left["pairs"][k] for k in PAIRS), sequence=sequence)
    b = compose(*(right["pairs"][k] for k in PAIRS), sequence=sequence)
    return {"pair": ["GPU0", "GPU1"], "sequence": sequence, "lower_ns": b["lower_ns"] - a["upper_ns"],
            "upper_ns": b["upper_ns"] - a["lower_ns"], "width_ns": a["width_ns"] + b["width_ns"],
            "allowance_ns": a["allowance_ns"] + b["allowance_ns"], "via": "HOST_MONO"}
