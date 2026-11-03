"""Balanced C1/C2 latency comparison for the workstation Hyperloom adapter.

This module scores authenticated measurements; it does not authenticate or run
an owner, change the selected stack, or turn a component timing into serving
evidence. Throughput stays a separate physical metric.
"""

import math
import re
import statistics

from hyperloom_sglang import positive, require


OBJECTIVE = "balanced_c1_c2_e2e_latency"
FIXED_IDENTITIES = ("selection_sha256", "runtime_sha256", "workload_sha256")


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _policy(policy):
    require(isinstance(policy, dict) and set(policy) == {
        "minimum_samples", "noise_floor_pct", "maximum_spread_pct", "minimum_gain_pct",
        "maximum_output_token_delta_pct"},
        "complete explicit latency policy required")
    require(type(policy["minimum_samples"]) is int and 3 <= policy["minimum_samples"] <= 32,
            "latency policy needs at least three independent samples")
    for name in ("noise_floor_pct", "maximum_spread_pct", "minimum_gain_pct", "maximum_output_token_delta_pct"):
        require(type(policy[name]) in (int, float) and math.isfinite(policy[name])
                and 0 <= policy[name] <= 10, "invalid latency policy: " + name)
    require(0 < policy["maximum_spread_pct"] and policy["minimum_gain_pct"] > 0
            and policy["noise_floor_pct"] <= policy["maximum_spread_pct"],
            "invalid noise/gain bounds")


def _series(record, policy):
    require(isinstance(record, dict) and type(record.get("schema")) is int
            and record["schema"] == 1, "latency measurement schema")
    require(record.get("kind") == "serving" and record.get("profiled") is False
            and record.get("complete") is True and record.get("quality_passed") is True
            and record.get("independent_release") is True,
            "complete unprofiled serving, quality and release evidence required")
    for name in (*FIXED_IDENTITIES, "candidate_sha256", "owner_seal_sha256", "quality_sha256"):
        require(_digest(record.get(name)), "latency evidence identity: " + name)
    nonce, arm = record.get("observation_id"), record.get("arm")
    require(isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{16}", nonce)
            and arm in ("control", "mtp"), "owner observation namespace required")
    cases = record.get("cases")
    require(isinstance(cases, list) and 2 <= len(cases) <= 64, "bounded C1/C2 cases required")
    result, sample_ids = {}, set()
    for case in cases:
        require(isinstance(case, dict), "latency case must be an object")
        name, concurrency = case.get("id"), case.get("concurrency")
        require(isinstance(name, str) and 0 < len(name) <= 128 and name not in result,
                "unique latency case ID required")
        require(type(concurrency) is int and concurrency in (1, 2), "only C1/C2 are scored")
        require(_digest(case.get("request_contract_sha256")), "case request contract required")
        samples = case.get("samples")
        require(isinstance(samples, list) and policy["minimum_samples"] <= len(samples) <= 32,
                "insufficient or excessive independent latency samples")
        values, walls, completions, prompts = [], [], [[] for _ in range(concurrency)], None
        for sample in samples:
            require(isinstance(sample, dict), "latency sample must be an object")
            sample_id = sample.get("id")
            group = sample.get("group_id")
            require(isinstance(sample_id, str) and 0 < len(sample_id) <= 128
                    and isinstance(group, str) and 0 < len(group) <= 80
                    and sample_id == nonce + ":" + arm + ":" + group
                    and sample_id not in sample_ids, "latency sample reused or namespace changed")
            sample_ids.add(sample_id)
            # Each C2 sample is one simultaneous pair. A pair must not be
            # flattened into two pseudo-independent C1 observations.
            times = sample.get("request_e2e_ms")
            wall = sample.get("group_wall_ms")
            require(isinstance(times, list) and len(times) == concurrency
                    and all(positive(v) for v in times) and positive(wall)
                    and all(v <= wall + 1e-3 for v in times), "invalid request/group latency")
            require(sample.get("warmup") is False and sample.get("completed") == concurrency
                    and type(sample.get("completed")) is int,
                    "warmup or incomplete group cannot be scored")
            for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "final_tokens"):
                counts = sample.get(field)
                require(isinstance(counts, list) and len(counts) == concurrency
                        and all(type(n) is int and 0 <= n <= 1 << 20 for n in counts),
                        "complete native token counts required: " + field)
            require(all(n > 0 for n in sample["prompt_tokens"] + sample["completion_tokens"] + sample["final_tokens"])
                    and all(r + f <= n for r, f, n in zip(sample["reasoning_tokens"], sample["final_tokens"],
                                                        sample["completion_tokens"], strict=True)),
                    "invalid native output segment counts")
            require(isinstance(sample.get("response_sha256"), list)
                    and len(sample["response_sha256"]) == concurrency
                    and all(_digest(n) for n in sample["response_sha256"])
                    and sample.get("task_quality_passed") is True, "sample quality/response binding required")
            prompts = sample["prompt_tokens"] if prompts is None else prompts
            require(prompts == sample["prompt_tokens"], "case prompt token counts changed")
            for counts, value in zip(completions, sample["completion_tokens"], strict=True):
                counts.append(value)
            values.append(statistics.mean(times))
            walls.append(wall)
        result[name] = {"concurrency": concurrency,
                        "request_contract_sha256": case["request_contract_sha256"],
                        "samples": len(values), "e2e_ms": values, "group_wall_ms": walls,
                        "prompt_tokens": prompts, "completion_tokens": completions}
    require({c["concurrency"] for c in result.values()} == {1, 2}, "both C1 and C2 required")
    return result, sample_ids


def _axis(before, after, policy):
    old, new = statistics.median(before), statistics.median(after)
    spread_before, spread_after = max(before) - min(before), max(after) - min(after)
    # A noisy candidate cannot enlarge its own acceptance band without limit.
    # Both relative ranges must pass the predeclared measurement-quality cap.
    stable = max(spread_before / old, spread_after / new) * 100 <= policy["maximum_spread_pct"]
    band_ms = max(old * policy["noise_floor_pct"] / 100, spread_before, spread_after)
    return {"baseline_median_ms": old, "candidate_median_ms": new,
            "baseline_range_ms": spread_before, "candidate_range_ms": spread_after,
            "noise_band_ms": band_ms, "stable": stable,
            "regression": new - old > band_ms,
            "resolved_improvement": old - new > band_ms,
            "speedup_ratio": old / new}


def compare(baseline, candidate, policy):
    """Return a dimensionless latency score plus separate per-case vetoes.

    Caller authenticates the enclosing owner/quality records before building
    these series. Invalid inputs raise ValueError. Incomplete, noisy or regressed
    results cannot KEEP. A KEEP is an experimental finding, never deployment.
    """
    _policy(policy)
    before, before_ids = _series(baseline, policy)
    after, after_ids = _series(candidate, policy)
    require(not before_ids.intersection(after_ids), "baseline/candidate reuse the same observations")
    require(all(baseline[k] == candidate[k] for k in FIXED_IDENTITIES),
            "selected stack or workload changed")
    require(set(before) == set(after), "matched latency case coverage required")
    cases, reasons = {}, []
    ratios = {1: [], 2: []}
    for name, old in before.items():
        new = after[name]
        require((old["concurrency"], old["request_contract_sha256"])
                == (new["concurrency"], new["request_contract_sha256"]),
                "case concurrency or request contract changed")
        require(old["samples"] == new["samples"], "matched sample counts required")
        require(old["prompt_tokens"] == new["prompt_tokens"], "matched prompt token counts required")
        axes = {axis: _axis(old[axis], new[axis], policy) for axis in ("e2e_ms", "group_wall_ms")}
        output_delta = [100 * abs(statistics.median(a) - statistics.median(b)) / statistics.median(a)
                        for a, b in zip(old["completion_tokens"], new["completion_tokens"], strict=True)]
        if any(delta > policy["maximum_output_token_delta_pct"] for delta in output_delta):
            reasons.append(name + ":unequal_generated_work")
        for axis, row in axes.items():
            if not row["stable"]:
                reasons.append(name + ":" + axis + ":noisy")
            elif row["regression"]:
                reasons.append(name + ":" + axis + ":regression")
        cases[name] = {"concurrency": old["concurrency"], "axes": axes,
                       "baseline_samples": old["samples"], "candidate_samples": new["samples"],
                       "output_token_delta_pct": output_delta}
        ratios[old["concurrency"]].append(axes["e2e_ms"]["speedup_ratio"])
    # Equal weights for C1 and C2, and equal case weights within each. More
    # C1 requests or a longer scenario must not silently dominate the goal.
    per_concurrency = {str(c): math.exp(statistics.mean(math.log(v) for v in values))
                       for c, values in ratios.items()}
    score = math.sqrt(per_concurrency["1"] * per_concurrency["2"])
    gain_pct = (score - 1) * 100
    resolved = any(row["axes"]["e2e_ms"]["resolved_improvement"] for row in cases.values())
    if gain_pct < policy["minimum_gain_pct"] or not resolved:
        reasons.append("balanced_gain_not_resolved")
    inconclusive = any(reason.endswith((":noisy", ":unequal_generated_work")) for reason in reasons)
    return {"schema": 1, "objective": OBJECTIVE, "score_unit": "dimensionless_latency_ratio",
            "baseline_score": 1.0, "candidate_score": score, "gain_pct": gain_pct,
            "per_concurrency_ratio": per_concurrency, "cases": cases, "policy": dict(policy),
            "verdict": "INCONCLUSIVE" if inconclusive else "REVERT" if reasons else "KEEP",
            "reasons": reasons, "adoption_authorized": False,
            "baseline_owner_seal_sha256": baseline["owner_seal_sha256"],
            "candidate_owner_seal_sha256": candidate["owner_seal_sha256"]}


def paired_comparison(reference, candidate, reference_control, candidate_control, policy):
    """Veto paired regressions and environmental drift before cross-job scoring."""
    for measurement, control in ((reference, reference_control), (candidate, candidate_control)):
        require(isinstance(measurement, dict) and isinstance(control, dict)
                and measurement.get("arm") == "mtp" and control.get("arm") == "control"
                and all(measurement.get(k) == control.get(k) for k in
                        (*FIXED_IDENTITIES, "observation_id", "owner_seal_sha256", "quality_sha256")),
                "same-owner paired selected control required")
    require(reference_control.get("candidate_sha256") == candidate_control.get("candidate_sha256"),
            "selected control source changed")
    # Validate both original pairs even if only the newer one is under review.
    compare(reference_control, reference, policy)
    paired = compare(candidate_control, candidate, policy)
    environment = compare(reference_control, candidate_control, policy)
    result = compare(reference, candidate, policy)
    environment_reasons = []
    for name, case in environment["cases"].items():
        for axis, row in case["axes"].items():
            if not row["stable"] or abs(row["candidate_median_ms"] - row["baseline_median_ms"]) > row["noise_band_ms"]:
                environment_reasons.append(name + ":" + axis + ":environment_drift")
    environment_reasons += [r for r in environment["reasons"] if r.endswith(":unequal_generated_work")]
    paired_reasons = [r for r in paired["reasons"] if r != "balanced_gain_not_resolved"]
    result["paired_control_comparison"] = paired
    result["environment_comparison"] = environment
    if environment_reasons:
        result.update(verdict="INCONCLUSIVE", reasons=environment_reasons)
    elif paired_reasons:
        result["reasons"] = sorted(set(result["reasons"] + ["paired:" + r for r in paired_reasons]))
        result["verdict"] = "INCONCLUSIVE" if paired["verdict"] == "INCONCLUSIVE" else "REVERT"
    return result
