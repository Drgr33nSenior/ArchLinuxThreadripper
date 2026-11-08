"""Within-job instrumentation overhead from observer journal host clocks.

For each captured decode window the two profiled rounds are compared with the
nearest unprofiled decode rounds of the same request set (same case, repetition,
members and graph route) in the same worker. Round 1 includes profiler start;
round 2 is steady-state tracing. Stop/export runs after round 2's last host
clock, so it is measured as the following inter-round gap. These are host-wall
ratios inside one job; they bound perturbation of profiled rounds and never
replace the unprofiled operation226/227 comparison.
"""
import statistics

MIN_REFERENCES = 4
MAX_REFERENCES = 8


def need(ok, message):
    if not ok:
        raise ValueError("overhead: " + message)


def _wall(row, a, b):
    clock = row["host_ns"]
    need(type(clock) is list and len(clock) == 4 and clock == sorted(clock), "four monotone host clocks")
    return clock[b] - clock[a]


def window(batches, selected):
    """batches: journal batch rows of one profiled worker; selected: capture window row."""
    need(selected["status"] == "ROUNDS_OBSERVED" and selected["window"].endswith("-decode"), "observed decode window")
    by_seq = {r["sequence"]: r for r in batches}
    seqs = selected["sequences"]
    need(len(seqs) == 2 and seqs[1] == seqs[0] + 1, "two adjacent profiled rounds")
    first = by_seq[seqs[0]]
    key = (first["case"], first["repetition"], tuple(sorted(first["members"])))
    def same(r):
        return (r["phase"] == "decode" and r["can_run_cuda_graph"] is True and
                (r["case"], r["repetition"], tuple(sorted(r["members"]))) == key)
    need(all(same(by_seq[s]) for s in seqs), "profiled rounds share one request set")
    ordered = [r for r in batches if same(r) and r["sequence"] not in seqs]
    ordered.sort(key=lambda r: (abs(r["sequence"] - seqs[0]), r["sequence"]))
    refs = ordered[:MAX_REFERENCES]
    need(len(refs) >= MIN_REFERENCES, "too few unprofiled reference rounds")
    ref_run = statistics.median(_wall(r, 0, 1) for r in refs)
    ref_round = statistics.median(_wall(r, 0, 3) for r in refs)
    later = sorted((r for r in batches if same(r) and r["sequence"] > seqs[1]), key=lambda r: r["sequence"])
    gaps = [b["host_ns"][0] - a["host_ns"][3] for a, b in zip(later, later[1:]) if b["sequence"] == a["sequence"] + 1]
    export_gap = later[0]["host_ns"][0] - by_seq[seqs[1]]["host_ns"][3] if later else None
    out = {"window": selected["window"], "references": len(refs),
           "reference_sequences": sorted(r["sequence"] for r in refs),
           "round1_run_ratio": _wall(by_seq[seqs[0]], 0, 1) / ref_run,
           "round2_run_ratio": _wall(by_seq[seqs[1]], 0, 1) / ref_run,
           "round1_round_ratio": _wall(by_seq[seqs[0]], 0, 3) / ref_round,
           "round2_round_ratio": _wall(by_seq[seqs[1]], 0, 3) / ref_round,
           "reference_round_ms": ref_round / 1e6,
           "post_window_gap_ms": None if export_gap is None else export_gap / 1e6,
           "reference_gap_ms": statistics.median(gaps) / 1e6 if gaps else None,
           "scope": "host wall inside one job; not GPU time, not a serving comparator"}
    return out


def matched(control, candidate, tolerance):
    """Per-window overhead symmetry between roles; asymmetric windows are flagged, not dropped."""
    need(set(control) == set(candidate), "same windows in both roles")
    rows = {}
    for w in sorted(control):
        delta = abs(control[w]["round2_round_ratio"] - candidate[w]["round2_round_ratio"])
        rows[w] = {"control": control[w]["round2_round_ratio"], "candidate": candidate[w]["round2_round_ratio"],
                   "absolute_difference": delta,
                   "status": "OVERHEAD_SYMMETRIC" if delta <= tolerance else "OVERHEAD_ASYMMETRIC"}
    return rows
