"""CPU bookkeeping for a bounded trace of one frozen concurrent request pair.

The pinned scheduler provides Python prefix_lens and extend_lens before
run_batch mutates its speculative state. No device tensor is inspected here.
"""

import hashlib
import json
import math
from array import array


def c2_need(value, message):
    if not value:
        raise ValueError("C2 profile: " + message)


def c2_initialize(capture):
    capture.report["request_progress"] = {}
    capture.report["batch_coverage"] = {"prefill": [], "decode": []}
    capture.c2_selected_batch = None
    capture.c2_requests_seen = set()
    capture.c2_prefill_marks = set()


def c2_select(capture, batch, request):
    rows = request["requests"]
    c2_need(isinstance(rows, list) and len(rows) == 2, "two frozen requests required")
    frozen = {r["rid"]: r for r in rows}
    c2_need(len(frozen) == 2, "distinct request identities required")
    actual = [getattr(r, "rid", None) for r in batch.reqs]
    if not set(actual).intersection(frozen):
        return None
    c2_need(0 < len(actual) <= 2 and len(set(actual)) == len(actual) and set(actual) <= set(frozen),
            "selected batch contains an unexpected or duplicate request")
    for req in batch.reqs:
        expected = frozen[req.rid]
        c2_need(req.cache_salt == expected["cache_salt"], "request cache namespace changed")
        if req.rid not in capture.c2_requests_seen:
            ids = req.origin_input_ids
            c2_need(isinstance(ids, (list, array)) and len(ids) == expected["input_tokens"]
                    and all(type(n) is int and n >= 0 for n in ids), "native request token extent")
            raw = (json.dumps(list(ids), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
            c2_need(hashlib.sha256(raw).hexdigest() == expected["input_ids_sha256"], "native request token identity")
            capture.c2_requests_seen.add(req.rid)
            capture.report["request_progress"][req.rid] = 0
    capture.seen_request = True
    is_extend, is_decode = batch.forward_mode.is_extend(), batch.forward_mode.is_decode()
    c2_need(is_extend or is_decode, "unsupported scheduler forward mode")
    witness = dict(rids=list(actual), batch_size=len(actual),
                   forward_mode=str(getattr(batch.forward_mode, "name", batch.forward_mode)))
    previous = capture.report["prefill_tokens"]
    added = 0
    if is_extend:
        starts, lengths = batch.prefix_lens, batch.extend_lens
        c2_need(isinstance(starts, list) and isinstance(lengths, list)
                and len(starts) == len(lengths) == len(actual)
                and all(type(n) is int and n >= 0 for n in starts)
                and all(type(n) is int and n > 0 for n in lengths), "CPU scheduler extent lists required")
        c2_need(type(batch.extend_num_tokens) is int and sum(lengths) == batch.extend_num_tokens
                and batch.extend_num_tokens <= 2050, "chunk plus at most two mixed decode tokens")
        witness.update(prefix_lens=list(starts), extend_lens=list(lengths))
        for rid, start, length in zip(actual, starts, lengths, strict=True):
            goal = frozen[rid]["input_tokens"]
            progress = capture.report["request_progress"][rid]
            if start < goal:
                c2_need(start == progress and start + length <= goal,
                        "cold prompt interval skipped, reused or exceeded")
                capture.report["request_progress"][rid] = start + length
                added += length
            else:
                c2_need(progress == goal, "decode tail appeared before complete prefill")
    else:
        c2_need(all(capture.report["request_progress"][rid] == frozen[rid]["input_tokens"] for rid in actual),
                "decode appeared before complete cold prefill")
    total = sum(row["input_tokens"] for row in rows)
    if added:
        index = capture.report["prefill_batches"]
        ceiling = sum(math.ceil(row["input_tokens"] / 2048) for row in rows)
        c2_need(index < ceiling and previous + added <= total, "prefill coverage bound")
        capture.report["prefill_batches"] += 1
        capture.report["prefill_tokens"] += added
        after = previous + added
        # Labels identify coverage milestones, not guessed batch numbers. Each
        # trace retains the actual scheduler batch index and membership.
        mark = 0 if previous == 0 else 2 if after == total else 1 if previous < (total + 1) // 2 <= after else None
        if mark is None or mark in capture.c2_prefill_marks:
            return None
        capture.c2_prefill_marks.add(mark)
        phase, label = "prefill", mark
        witness.update(scheduler_batch_index=index, prompt_tokens_before=previous, prompt_tokens_after=after)
    else:
        c2_need(capture.report["prefill_tokens"] <= total, "invalid prefill total")
        # A C2 request pair does not imply a two-request decode batch. Capture
        # only actual two-request decode work, and report missing coverage.
        if set(actual) != set(frozen) or capture.report["prefill_tokens"] != total:
            return None
        index = capture.report["decode_batches"]
        c2_need(index < 4096, "natural-stop decode bound")
        capture.report["decode_batches"] += 1
        if index >= 4:
            return None
        phase, label = "decode", index
        witness["scheduler_batch_index"] = index
    capture.c2_selected_batch = witness
    capture.report["batch_coverage"][phase].append(dict(label=label, **witness))
    return (phase, label) if not capture.disabled else None


def c2_complete(row, request):
    """Verify prompt coverage and the actual membership of retained captures."""
    frozen = {r["rid"]: r for r in request["requests"]}
    total = sum(r["input_tokens"] for r in frozen.values())
    ceiling = sum(math.ceil(r["input_tokens"] / 2048) for r in frozen.values())
    c2_need(isinstance(row.get("request_progress"), dict)
            and set(row["request_progress"]) <= set(frozen), "unknown progress identity")
    for rid, count in row["request_progress"].items():
        c2_need(type(count) is int and 0 <= count <= frozen[rid]["input_tokens"], "invalid request progress")
    coverage = row.get("batch_coverage")
    c2_need(isinstance(coverage, dict) and set(coverage) == {"prefill", "decode"}, "batch coverage schema")
    captures = {(cap["phase"], cap["index"]): cap for cap in row["captures"]}
    for phase, witnesses in coverage.items():
        c2_need(isinstance(witnesses, list) and len(witnesses) <= (3 if phase == "prefill" else 4),
                "batch witness count")
        labels = set()
        for witness in witnesses:
            label = witness["label"]
            c2_need(type(label) is int and label not in labels
                    and label in (range(3) if phase == "prefill" else range(4)), "batch witness label")
            labels.add(label)
            ids = witness["rids"]
            c2_need(isinstance(ids, list) and len(set(ids)) == len(ids)
                    and witness["batch_size"] == len(ids) and set(ids) <= set(frozen)
                    and len(ids) in (1, 2), "batch witness identity")
            if phase == "decode":
                c2_need(set(ids) == set(frozen), "C2 decode witness contains only one request")
            else:
                before, after = witness["prompt_tokens_before"], witness["prompt_tokens_after"]
                c2_need(type(before) is int and type(after) is int and 0 <= before < after <= total,
                        "prefill milestone extent")
                c2_need((label == 0 and before == 0) or (label == 1 and before < (total + 1) // 2 <= after)
                        or (label == 2 and after == total), "prefill milestone label")
            cap = captures.get((phase, label))
            if cap is not None:
                c2_need(cap.get("batch") == {k: v for k, v in witness.items() if k != "label"},
                        "trace and batch witness differ")
    return (row["request_progress"] == {rid: r["input_tokens"] for rid, r in frozen.items()}
            and 0 < row["prefill_batches"] <= ceiling and row["prefill_tokens"] == total
            and 4 <= row["decode_batches"] <= 4096
            and {w["label"] for w in coverage["prefill"]} == {0, 1, 2}
            and {w["label"] for w in coverage["decode"]} == {0, 1, 2, 3})
