"""Project the existing owner's authenticated HTTP and native quality evidence.

The controller must first replay the sealed owner and validate the native CPU
grader with their original readers. This module joins those products; it never
treats an optimizer-written benchmark report as authenticated evidence.
"""

import re

import hyperloom_exchange as exchange
import hyperloom_latency as latency
from hyperloom_sglang import positive, require


ARMS = (("bf16", "control"), ("kv4", "mtp"))


def _ordered(rows, count, label):
    require(isinstance(rows, list) and len(rows) == count
            and all(isinstance(row, dict) and type(row.get("ordinal")) is int
                    and row["ordinal"] == index for index, row in enumerate(rows)),
            "complete ordered " + label + " required")
    return rows


def _quality(quality, decoded, count):
    require(isinstance(quality, dict)
            and quality.get("status") == "NATIVE_CPU_FINAL_QUALITY_AND_RELEASE_VALIDATED"
            and quality.get("independent_release_verified") is True
            and quality.get("quality_pass") is True and quality.get("long_answer_coverage_pass") is True
            and quality.get("decoded_rows") == quality.get("natural_rows") == 2 * count
            and quality.get("ungraded_cap_work_rows") == quality.get("state_only_cancellations") == 0
            and quality.get("qualification") is False and quality.get("selected") is False,
            "all natural answers and native CPU release must pass")
    require(isinstance(decoded, dict) and isinstance(decoded.get("answers"), list)
            and len(decoded["answers"]) == 2 * count and len(quality.get("grades", [])) == 2 * count,
            "complete native decode and grade rows required")
    answers, grades = {}, {}
    for offset, (physical, _) in enumerate(ARMS):
        answers[physical] = _ordered(decoded["answers"][offset * count:(offset + 1) * count], count, "decodes")
        grades[physical] = _ordered(quality["grades"][offset * count:(offset + 1) * count], count, "grades")
        for answer, grade in zip(answers[physical], grades[physical], strict=True):
            require(answer.get("arm") == grade.get("arm") == physical
                    and answer.get("case") == grade.get("case")
                    and answer.get("final_status") == "FINAL_SEGMENT_DECODED"
                    and answer.get("finish_reason") == "stop" and answer.get("wire_decode_matches") is True
                    and answer.get("error") is None and grade.get("status") == "QUALITY_PASS"
                    and grade.get("quality_passed") is True and grade.get("long_answer_coverage_passed") is True
                    and answer.get("final_answer_token_count") == grade.get("final_answer_token_count"),
                    "native answer/grade identity or quality mismatch")
    return answers


def _arm_rows(summary, answers, frozen_groups, count):
    require(isinstance(summary, dict) and summary.get("stopped_early") is False
            and summary.get("paired_error") is None
            and summary.get("executed_case_count") == summary.get("frozen_case_count") == count,
            "complete retained HTTP run required")
    rows = _ordered(summary.get("rows"), count, "HTTP rows")
    for row, answer in zip(rows, answers, strict=True):
        contract = row.get("contract", {})
        require(row.get("case") == contract.get("case") == answer["case"]
                and row.get("body_sha256") == contract.get("request_sha256") == answer["request_body_sha256"]
                and contract.get("raw_http_response_sha256") == answer["response_raw_sha256"]
                and contract.get("raw_response_sha256") == answer["response_canonical_sha256"]
                and contract.get("response_token_ids_sha256") == answer["output_ids_sha256"]
                and contract.get("status") == "NORMAL_STOP_COMPLETE"
                and contract.get("finish_reason") == "stop" and contract.get("metric_eligible") is True
                and contract.get("response_id_count") == contract.get("reported_completion_tokens") == answer["output_tokens"]
                and positive(contract.get("client_elapsed_seconds")),
                "HTTP/native answer binding or completion mismatch")
        segments = contract.get("thinking_segments", {})
        require(all(segments.get(k) == answer[k] for k in
                    ("reasoning_token_count", "final_answer_token_count", "closing_marker_index")),
                "wire/native output segmentation mismatch")
    actual = summary.get("groups")
    require(isinstance(actual, list) and len(actual) == len(frozen_groups), "complete HTTP group schedule required")
    groups, ordinals = {}, []
    for group, plan in zip(actual, frozen_groups, strict=True):
        require(isinstance(group, dict) and group.get("group_id") == plan["id"]
                and group["group_id"] not in groups and group.get("kind") == plan["kind"]
                and group.get("ordinals") == plan["ordinals"]
                and type(group.get("concurrency")) is int and group["concurrency"] == plan["concurrency"]
                and len(plan["ordinals"]) == plan["concurrency"]
                and group.get("excluded_warmup") is (plan["kind"] == "warmup")
                and group.get("measured") is (plan["kind"] == "timing"),
                "HTTP group differs from frozen simultaneous schedule")
        times = [rows[i]["contract"]["client_elapsed_seconds"] for i in plan["ordinals"]]
        total = sum(answers[i]["output_tokens"] for i in plan["ordinals"])
        require(group.get("per_request_latency_seconds") == times and group.get("completion_tokens") == total
                and positive(group.get("makespan_seconds"))
                and all(t <= group["makespan_seconds"] + 1e-6 for t in times),
                "HTTP group accounting mismatch")
        groups[plan["id"]] = group
        ordinals.extend(plan["ordinals"])
    require(ordinals == list(range(count)), "HTTP requests omitted or reused by schedule")
    return rows, groups


def serving_report(*, summaries, decoded, quality, quality_sha256, workload,
                   request, binding, owner_seal_sha256, observation_id):
    """Create both paired series from fully replayed, unprofiled serving evidence."""
    require(request["kind"] == "serving" and request["campaign_sha256"] == binding["campaign_sha256"]
            and request["selection_sha256"] == binding["selection_sha256"], "wrong serving campaign")
    require(isinstance(workload, dict) and workload.get("diagnostic_only") is False
            and workload.get("requests") == 30 and workload.get("samples_per_case") == 3
            and exchange.digest(exchange.canonical(workload)) == binding["workload_sha256"],
            "exact balanced serving workload required")
    require(set(summaries) == {p for p, _ in ARMS} and re.fullmatch(r"[0-9a-f]{16}", observation_id),
            "two physical arms and registered owner nonce required")
    for name, pin in (("quality", quality_sha256), ("owner", owner_seal_sha256)):
        exchange._identity(pin, name)
    answers = _quality(quality, decoded, workload["requests"])
    series, physical_throughput = {}, {}
    for physical, logical in ARMS:
        rows, groups = _arm_rows(summaries[physical], answers[physical], workload["groups"], workload["requests"])
        cases = []
        measured_ordinals, total_seconds, total_tokens = [], 0.0, 0
        for name, plan in sorted(workload["cases"].items()):
            samples = []
            for frozen in plan["samples"]:
                group = groups[frozen["group"]]
                require(group["kind"] == "timing" and group["ordinals"] == frozen["ordinals"]
                        and group["concurrency"] == plan["concurrency"], "case/group identity mismatch")
                contracts = [rows[i]["contract"] for i in frozen["ordinals"]]
                native = [answers[physical][i] for i in frozen["ordinals"]]
                samples.append(dict(id=observation_id + ":" + logical + ":" + frozen["group"],
                    group_id=frozen["group"], warmup=False, completed=plan["concurrency"],
                    request_e2e_ms=[1000 * c["client_elapsed_seconds"] for c in contracts],
                    group_wall_ms=1000 * group["makespan_seconds"],
                    prompt_tokens=[c["prompt_token_count"] for c in contracts],
                    completion_tokens=[a["output_tokens"] for a in native],
                    reasoning_tokens=[a["reasoning_token_count"] for a in native],
                    final_tokens=[a["final_answer_token_count"] for a in native],
                    response_sha256=[a["response_raw_sha256"] for a in native], task_quality_passed=True))
                measured_ordinals.extend(frozen["ordinals"])
                total_seconds += group["makespan_seconds"]
                total_tokens += group["completion_tokens"]
            cases.append(dict(id=name, concurrency=plan["concurrency"],
                              request_contract_sha256=plan["contract_sha256"], samples=samples))
        require(sorted(measured_ordinals) == list(range(3, 30)), "every non-warmup request must be scored once")
        value = dict(schema=1, kind="serving", profiled=False, complete=True, quality_passed=True,
            independent_release=True, observation_id=observation_id, arm=logical,
            **{k: binding[k] for k in latency.FIXED_IDENTITIES},
            candidate_sha256=(binding["selected_source_set_sha256"] if logical == "control"
                              else request["source_set_sha256"]),
            owner_seal_sha256=owner_seal_sha256, quality_sha256=quality_sha256, cases=cases)
        latency._series(value, binding["latency_policy"])
        series[logical] = value
        physical_throughput[logical] = dict(output_throughput=total_tokens / total_seconds,
            completed_requests=27, num_prompts=27, output_tokens=total_tokens,
            measured_group_seconds=total_seconds, scope="combined TP2 completed tokens per measured group wall second")
    return dict(success=True, framework="sglang", model=binding["benchmark"]["model"],
        benchmark_backend="workstation_owner", benchmark_valid=True, diagnostic_only=False,
        workload_identity=binding["workload_sha256"], workstation_latency=series["mtp"],
        workstation_latency_control=series["control"], throughput=physical_throughput["mtp"],
        control_throughput=physical_throughput["control"], latency={},
        quality_gate=dict(passed=True, native_quality_sha256=quality_sha256, decoded_rows=60),
        owner_binding=dict(owner_seal_sha256=owner_seal_sha256,
            request_sha256=exchange.digest(exchange.canonical(request)), source_set_sha256=request["source_set_sha256"]),
        paired_comparison=latency.compare(series["control"], series["mtp"], binding["latency_policy"]),
        adoption_authorized=False)


def profile_report(*, summaries, decoded, quality, quality_sha256, workload,
                   request, binding, owner_seal_sha256, observation_id, capture):
    """Join a replayed C2 diagnostic to its ten native answer grades.

    Capture validation is performed by the sealed packet's original validator.
    These instrumented HTTP durations never enter the serving comparator.
    """
    require(request["kind"] == "profile" and request["campaign_sha256"] == binding["campaign_sha256"]
            and request["selection_sha256"] == binding["selection_sha256"], "wrong profile campaign")
    require(isinstance(workload, dict) and workload.get("diagnostic_only") is True
            and workload.get("requests") == 5 and workload.get("samples_per_case") == 1
            and workload.get("cases") == {}, "exact C2 diagnostic workload required")
    require(set(summaries) == {p for p, _ in ARMS} and re.fullmatch(r"[0-9a-f]{16}", observation_id),
            "two physical profile arms and owner nonce required")
    for name, pin in (("quality", quality_sha256), ("owner", owner_seal_sha256)):
        exchange._identity(pin, name)
    answers = _quality(quality, decoded, 5)
    require([(g["kind"], g["ordinals"], g["concurrency"]) for g in workload["groups"]]
            == [("warmup", [0], 1), ("warmup", [1, 2], 2), ("timing", [3, 4], 2)],
            "profile warmups and simultaneous request pair required")
    for physical, _ in ARMS:
        _arm_rows(summaries[physical], answers[physical], workload["groups"], 5)
    require(isinstance(capture, dict) and len(capture.get("profiles", [])) == 7
            and [r["rank"] for r in capture.get("ranks", [])] == [0, 1], "complete two-rank C2 capture required")
    frozen = capture["request"]["requests"]
    require(len(frozen) == 2 and [r["body_sha256"] for r in frozen]
            == [a["request_body_sha256"] for a in answers["kv4"][3:]], "profile and graded HTTP pair differ")
    owner = dict(owner_seal_sha256=owner_seal_sha256,
                 request_sha256=exchange.digest(exchange.canonical(request)), source_set_sha256=request["source_set_sha256"])
    return dict(success=True, framework="sglang", model=binding["benchmark"]["model"],
        benchmark_backend="workstation_owner", benchmark_valid=True, diagnostic_only=True,
        workload_identity=exchange.digest(exchange.canonical(workload)), observation_id=observation_id,
        workstation_latency=None, workstation_latency_control=None, throughput={}, latency={},
        quality_gate=dict(passed=True, native_quality_sha256=quality_sha256, decoded_rows=10), owner_binding=owner,
        profile_coverage=dict(complete=True, owner_seal_sha256=owner_seal_sha256, concurrency=2,
                              windows=7, ranks=capture["ranks"], request=capture["request"]),
        profile_capture=dict(schema=1, status="complete", arm="mtp", **owner, profiles=capture["profiles"]),
        adoption_authorized=False)
