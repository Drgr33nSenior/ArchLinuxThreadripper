"""Derive a repeated C1/C2 menu from the selected, authenticated request corpus.

Messages, native token IDs and answer oracles are reused. Only request identity,
cache intent and schedule metadata change. This module performs no tokenization,
framework import, network request or GPU operation.
"""

import base64
import copy
import json
import zlib

from hyperloom_exchange import canonical, digest
from hyperloom_sglang import require


SAMPLES = 3
CONTEXT = 131040
POOL = 262144
OUTPUT_CAP = 4096
WARM_PERCENT = 95
CASES = (
    ("long-c1-cold", (3,), "cold"),
    ("long-c1-warm", (3,), "warm"),
    ("long-c2-cold", (8, 9), "cold"),
    ("long-c2-warm", (8, 9), "warm"),
    # These selected service-configuration tasks require at least 512 final
    # answer tokens, so short prompts also exercise sustained generation.
    ("short-c1-cold", (12,), "cold"),
    ("short-c2-cold", (12, 13), "cold"),
)


def _unpack(raw, cap=16 << 20):
    inflater = zlib.decompressobj()
    result = inflater.decompress(raw, cap + 1)
    require(len(result) <= cap and inflater.eof and not inflater.unused_data
            and not inflater.unconsumed_tail, "bounded complete request compression required")
    return result


def derive(files):
    """Return the existing owner's menu interface plus a scoring case map."""
    settings = json.loads(files["FINAL-SETTINGS.json"])
    logical = {k: base64.b64decode(v, validate=True) for k, v in
               json.loads(_unpack(files["FINAL-REQUESTS.zlib"])).items()}
    original_assets = {k: base64.b64decode(v, validate=True) for k, v in
                       json.loads(_unpack(logical["FINAL-REQUESTS.zlib"])).items()}
    client = json.loads(_unpack(logical["CLIENT-BINDING.zlib"]))
    originals = client["rows"]
    require(len(settings["requests"]) == len(originals) == 16,
            "selected sixteen-request C4 donor required")
    for i, (plan, row) in enumerate(zip(settings["requests"], originals, strict=True)):
        require(plan["ordinal"] == row["ordinal"] == i
                and canonical(row["body"]) == original_assets[plan["path"]]
                and canonical(row["expected"]) == original_assets[plan["expected"]]
                and digest(original_assets[plan["path"]]) == plan["sha256"] == row["body_sha256"]
                and digest(original_assets[plan["expected"]]) == plan["expected_sha256"],
                "donor request/body/oracle identity mismatch")
    rows, bound, groups, assets, cache, cases = [], [], [], {}, {}, {}
    seeds = {}

    def group(case, indices, intent, repetition, warmup=False):
        gid = case if warmup else f"{case}-r{repetition}"
        kind = "warmup" if warmup else "timing"
        ordinals = []
        contract = []
        for position, old in enumerate(indices):
            source = originals[old]
            body, expected = copy.deepcopy(source["body"]), copy.deepcopy(source["expected"])
            require(body["max_completion_tokens"] == OUTPUT_CAP and body["reasoning_effort"] == "low"
                    and body["chat_template_kwargs"] == {"enable_thinking": True}
                    and body["ignore_eos"] is False and body["stream"] is False
                    and expected["task_grade"]["kind"] == "exact_json",
                    "selected thinking-enabled natural-stop task required")
            require(digest(canonical(expected["input_ids"])) == expected["input_ids_sha256"]
                    and digest(canonical(body["messages"])) == expected["messages_sha256"]
                    and len(expected["input_ids"]) == source["input_tokens"],
                    "native prompt token/message binding changed")
            ordinal = len(rows)
            name = gid + (("-" + chr(97 + position)) if len(indices) == 2 else "")
            # Each repetition has its own cache namespace. A warm row reuses
            # only its immediately preceding cold partner's namespace.
            family = case.replace("-warm", "-cold")
            salt = f"hyperloom-{family}-r{repetition}-{position}"
            seed_key = (family, repetition, position)
            if intent == "warm":
                require(seed_key in seeds, "warm request has no cold seed")
                seed, seed_ids = seeds[seed_key]
                require(seed_ids == expected["input_ids"], "warm prompt differs from its seed")
                maximum = source["input_tokens"] - 1
                policy = {"intent": "warm", "minimum_cached_tokens": (WARM_PERCENT * maximum + 99) // 100,
                          "maximum_cached_tokens": maximum}
            else:
                require(seed_key not in seeds, "cold request namespace reused")
                seed = None
                seeds[seed_key] = (ordinal, expected["input_ids"])
                policy = {"intent": "cold", "minimum_cached_tokens": 0, "maximum_cached_tokens": 0}
            body.update(rid=f"hyperloom-{ordinal:02d}-{name}", cache_salt=salt)
            expected.update(case=name, context_length=CONTEXT, completion_policy="natural-stop", cache_policy=policy)
            path = f"FINAL-REQUEST-{ordinal:02d}-{name}.json"
            exp_path = path.replace(".json", ".expected.json")
            raw, exp_raw = canonical(body), canonical(expected)
            require(len(raw) <= 1 << 20 and len(exp_raw) <= 1 << 20, "request member exceeds owner cap")
            plan = dict(settings["requests"][old], ordinal=ordinal, case=name, group=gid, category=kind,
                        context_envelope_tokens=CONTEXT, path=path, expected=exp_path,
                        sha256=digest(raw), bytes=len(raw), expected_sha256=digest(exp_raw))
            joined = dict(source, **{k: v for k, v in plan.items() if k != "expected"},
                          body=body, body_sha256=plan["sha256"], expected=expected)
            require(set(joined) == set(source), "owner row schema changed")
            rows.append(plan)
            bound.append(joined)
            assets[path], assets[exp_path] = raw, exp_raw
            cache[ordinal] = dict(body_sha256=plan["sha256"], cache_salt=salt, rid=body["rid"],
                                  intent=intent, seed=seed)
            ordinals.append(ordinal)
            # Stable across repetitions; request identities intentionally are not.
            contract.append(dict(messages_sha256=expected["messages_sha256"],
                                 input_ids_sha256=expected["input_ids_sha256"],
                                 input_tokens=source["input_tokens"], maximum_output_tokens=OUTPUT_CAP,
                                 task_grade=expected["task_grade"], cache_policy=policy,
                                 thinking=True, reasoning_effort="low"))
        task = originals[indices[0]]["task"]
        require(all(originals[i]["task"] == task for i in indices), "mixed task in concurrent group")
        groups.append(dict(id=gid, kind=kind, concurrency=len(indices), ordinals=ordinals, task=task))
        capacities = [rows[i]["input_tokens"] + OUTPUT_CAP + 16 for i in ordinals]
        require(max(capacities) <= CONTEXT and sum(capacities) <= POOL, "selected context/pool capacity exceeded")
        if not warmup:
            case_hash = digest(canonical(contract))
            entry = cases.setdefault(case, dict(concurrency=len(indices), contract_sha256=case_hash, samples=[]))
            require(entry["contract_sha256"] == case_hash, "case changed between repetitions")
            entry["samples"].append(dict(group=gid, ordinals=ordinals, repetition=repetition))

    group("warmup-c1", (0,), "cold", 0, True)
    group("warmup-c2", (1, 2), "cold", 0, True)
    for repetition in range(SAMPLES):
        for case, indices, intent in CASES:
            group(case, indices, intent, repetition)
    require(len(rows) == 30 and len(groups) == 20 and len(cases) == 6, "complete repeated menu required")
    protocol = dict(settings["paired_serving"], groups=groups,
                    order={arm: [g["id"] for g in groups] for arm in ("control", "mtp")},
                    requests_per_start=len(rows), measured_natural_requests_per_start=27,
                    natural_quality_requests_per_start=0, warmup_requests_per_start=3)
    settings = dict(settings, requests=rows, paired_serving=protocol, instrumentation="production")
    settings_raw = canonical(settings)
    client_raw = canonical(dict(client, rows=bound))
    inner_raw = canonical({k: base64.b64encode(v).decode() for k, v in assets.items()})
    require(len(settings_raw) < 256 << 10 and len(client_raw) <= 16 << 20 and len(inner_raw) <= 16 << 20,
            "expanded menu exceeds existing owner caps")
    logical["FINAL-REQUESTS.zlib"] = inner = zlib.compress(inner_raw, 9)
    logical["CLIENT-BINDING.zlib"] = client_z = zlib.compress(client_raw, 9)
    outer = canonical({k: base64.b64encode(v).decode() for k, v in logical.items()})
    packed = zlib.compress(outer, 9)
    require(len(inner) <= 2 << 20 and len(client_z) <= 2 << 20 and len(outer) <= 16 << 20,
            "packed menu exceeds existing owner caps")
    pins = dict(REQUEST_LOGICAL={k: dict(sha256=digest(v), bytes=len(v)) for k, v in logical.items()},
                REQUEST_STORED=dict(sha256=digest(packed), bytes=len(packed)),
                REQUEST_EXPANDED=dict(sha256=digest(outer), bytes=len(outer)),
                REQUEST_INHERITED=dict(sha256=digest(inner), bytes=len(inner)))
    return dict(settings_raw=settings_raw, packed=packed, pins=pins, client_z=client_z,
                client_raw=client_raw, groups=groups, protocol=protocol, rows=rows,
                bound=bound, cache=cache, cases=cases)


def derive_profile(files):
    """The same cold C2 pair, with three warmups; profiling is diagnostic only."""
    menu = derive(files)
    chosen = [0, 1, 2, 5, 6]
    mapping = {old: new for new, old in enumerate(chosen)}
    settings = json.loads(menu["settings_raw"])
    logical = {k: base64.b64decode(v, validate=True) for k, v in
               json.loads(_unpack(menu["packed"])).items()}
    assets = json.loads(_unpack(logical["FINAL-REQUESTS.zlib"]))
    rows, bound, cache, kept = [], [], {}, {}
    for old in chosen:
        new = mapping[old]
        plan, row = copy.deepcopy(menu["rows"][old]), copy.deepcopy(menu["bound"][old])
        old_path, old_expected = plan["path"], plan["expected"]
        plan["ordinal"] = row["ordinal"] = new
        plan["path"] = row["path"] = f"FINAL-REQUEST-{new:02d}-{plan['case']}.json"
        plan["expected"] = plan["path"].replace(".json", ".expected.json")
        rows.append(plan)
        bound.append(row)
        cache[new] = copy.deepcopy(menu["cache"][old])
        require(cache[new]["intent"] == "cold" and cache[new]["seed"] is None, "profile pair must be cold")
        kept[plan["path"]] = assets[old_path]
        kept[plan["expected"]] = assets[old_expected]
    groups = [copy.deepcopy(g) for g in menu["groups"] if set(g["ordinals"]) <= set(chosen)]
    for group in groups:
        group["ordinals"] = [mapping[o] for o in group["ordinals"]]
    require(len(groups) == 3 and len(rows) == 5, "five-request C2 diagnostic menu")
    protocol = dict(menu["protocol"], groups=groups,
                    order={a: [g["id"] for g in groups] for a in ("control", "mtp")},
                    requests_per_start=5, measured_natural_requests_per_start=2)
    settings.update(requests=rows, paired_serving=protocol)
    client = json.loads(menu["client_raw"])
    client["rows"] = bound
    client_raw = canonical(client)
    logical["CLIENT-BINDING.zlib"] = client_z = zlib.compress(client_raw, 9)
    logical["FINAL-REQUESTS.zlib"] = inner = zlib.compress(canonical(kept), 9)
    outer = canonical({k: base64.b64encode(v).decode() for k, v in logical.items()})
    packed = zlib.compress(outer, 9)
    pins = dict(REQUEST_LOGICAL={k: dict(sha256=digest(v), bytes=len(v)) for k, v in logical.items()},
                REQUEST_STORED=dict(sha256=digest(packed), bytes=len(packed)),
                REQUEST_EXPANDED=dict(sha256=digest(outer), bytes=len(outer)),
                REQUEST_INHERITED=dict(sha256=digest(inner), bytes=len(inner)))
    return dict(settings_raw=canonical(settings), packed=packed, pins=pins, client_z=client_z,
                client_raw=client_raw, groups=groups, protocol=protocol, rows=rows, bound=bound,
                cache=cache, cases={}, diagnostic_only=True)
