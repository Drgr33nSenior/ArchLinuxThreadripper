"""Graph replay membership and authenticated capture-time inventory join (v2).

Replays are joined by runtime correlation, never by GPU timestamp order. A graph launch
is assigned to a role label only when its calibrated RUNTIME interval lies inside the
label in the CPU_OP domain. Replay consistency is necessary but not sufficient.
Completion is claimed only after the capture-time inventory written by graph_inventory
is bound by value (R3) and matches every labelled replay exactly:

* inventory bytes hash to the pin in the shared transport index;
* producer source, ABI manifest, profiling-twin Torch version and graphs.py pins equal
  the packet's expected values, and the trace header carries HIP 71726416;
* the inventory's rank, pid, nonce, start and role equal the worker identity;
* exactly one attributed, inventoried capture exists for the replay's role and bs,
  and the producer counted exactly the labelled replays of that capture index inside
  this window, with no replay of any other or unknown graph;
* within an observed round no hipGraphLaunch occurs on another thread of the worker.

Node-type to dispatch rules are explicit and prospective; any other node type refuses.
A wrong rule can only refuse (exact multiset equality), never accept a different graph.
"""
from collections import Counter
import hashlib
import json
import re

import clock_domains as cd

LABEL = re.compile(r"step\[(DRAFT|VERIFY) bs=([12])\]")
# Prospective node-type -> dispatch-symbol rules; anything else refuses.
NODE_RULES = {"kernel": "symbol", "memcpy": "__amd_rocclr_copyBuffer.kd",
              "memset": "__amd_rocclr_fillBufferAligned.kd", "empty": None,
              "event_record": None, "event_wait": None, "child_graph": None}
EXPECTED_PRODUCER = {"torch_version": "2.14.0+git2e66fe81.rocm10.2.0",
                     "torch_graphs_sha256": "2ce9b12b17d39bba15e4f36dac0a6f2153f8ed40dd6464d0e188053732b7dbdf"}
HIP_VERSION = "71726416"
INVENTORY_LIMIT = 192 << 10


def need(ok, message):
    if not ok:
        raise ValueError("graph coverage: " + message)


def signature(names):
    return hashlib.sha256(json.dumps(names, separators=(",", ":")).encode()).hexdigest()


def _contained(outer, api, calibration, sequence):
    """(definitely inside, definitely disjoint) for a RUNTIME record against a CPU_OP span."""
    l0, l1, _ = cd._interval(outer)
    a0, a1, _ = cd._interval(api)
    begin = cd.to_domain(a0, "RUNTIME", "CPU_OP", calibration, sequence)
    end = cd.to_domain(a1, "RUNTIME", "CPU_OP", calibration, sequence)
    inside = l0 <= begin[0] and end[1] <= l1
    disjoint = end[1] < l0 or begin[0] > l1
    return inside, disjoint


def window_graphs(trace, identity, calibration, rows, bs):
    """Join two DRAFT/VERIFY graph replays per observed round; refuse any ambiguity."""
    need(calibration["pairs"]["CPU_OP->RUNTIME"]["status"] == "BOUND"
         and calibration["pairs"]["RUNTIME->GPU"]["status"] == "BOUND", "label/launch/dispatch pairs bound")
    need(bs in (1, 2) and type(rows) is list and rows, "window geometry")
    events = trace["traceEvents"]
    complete = [e for e in events if e.get("ph") == "X"]
    labels = [e for e in complete if e.get("cat") == "user_annotation"
              and str(e.get("name", "")).startswith("step[")]
    for e in labels:
        match = LABEL.fullmatch(e["name"])
        need(match is not None and int(match[2]) == bs, "exact role label and batch size")
        need(e.get("pid") == identity["pid"], "label worker identity")
    apis = [e for e in complete if e.get("cat") == "cuda_runtime" and e.get("name") == "hipGraphLaunch"]
    dispatch = {}
    for e in complete:
        if e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset"):
            c = e.get("args", {}).get("correlation")
            need(type(c) is int, "dispatch correlation")
            dispatch.setdefault(c, []).append(e)
    owned_labels, owned_apis, replays = set(), set(), []
    for row in rows:
        name = "OBS:%s:%d" % (identity["nonce"], row["sequence"])
        marker = cd._unique([e for e in complete if e.get("cat") == "user_annotation" and e.get("name") == name],
                            "one marker " + name)
        m0, m1, _ = cd._interval(marker)
        in_marker = []
        for api in apis:
            if api.get("pid") != identity["pid"]:
                continue
            within, beyond = _contained(marker, api, calibration, row["sequence"])
            need(within or beyond, "graph launch straddles an observer marker within the offset bound")
            if api.get("tid") != marker.get("tid"):
                # R3: another worker thread may not launch a graph inside an observed round.
                need(beyond, "foreign-thread hipGraphLaunch inside an observed round")
                continue
            if within:
                in_marker.append(api)
        need(len(in_marker) == 2, "exactly two graph launches inside each observed round")
        inside = [e for e in labels if m0 <= cd._interval(e)[0] and cd._interval(e)[1] <= m1]
        need(sorted(LABEL.fullmatch(e["name"])[1] for e in inside) == ["DRAFT", "VERIFY"],
             "one DRAFT and one VERIFY label per observed round")
        inside.sort(key=lambda e: cd._interval(e)[0])
        need(LABEL.fullmatch(inside[0]["name"])[1] == "DRAFT"
             and cd._interval(inside[0])[1] <= cd._interval(inside[1])[0], "draft label precedes verify label")
        for label in inside:
            need(label.get("tid") == marker.get("tid"), "label thread identity")
            owned_labels.add(id(label))
            hits = []
            for api in in_marker:
                within, beyond = _contained(label, api, calibration, row["sequence"])
                need(within or beyond, "graph launch straddles a label within the offset bound")
                if within:
                    hits.append(api)
            api = cd._unique(hits, "one graph launch per role label")
            need(id(api) not in owned_apis, "graph launch owned twice")
            owned_apis.add(id(api))
            c = api["args"]["correlation"]
            rows_ = sorted(dispatch.get(c, []), key=lambda e: (cd._interval(e)[0], e["name"]))
            need(rows_, "labelled graph launch has no dispatch records")
            names = [e["name"] for e in rows_]
            spans = [cd._interval(e) for e in rows_]
            replays.append({"sequence": row["sequence"], "graph_role": LABEL.fullmatch(label["name"])[1],
                            "bs": bs, "correlation": c, "dispatches": len(names),
                            "signature": signature(names), "symbols": dict(Counter(names)),
                            "_names": names, "_gpu_span_ns": [min(s for s, _, _ in spans),
                                                              max(t for _, t, _ in spans)],
                            "_api_begin_ns": cd._interval(api)[0]})
    need(len(owned_apis) == 2 * len(rows), "every in-round graph launch owned by one role label")
    by_role = {}
    for replay in replays:
        by_role.setdefault(replay["graph_role"], []).append(replay)
    for role, items in by_role.items():
        need(len({r["signature"] for r in items}) == 1,
             "replays of one %s graph differ in ordered dispatch membership" % role)
    return {"status": "REPLAY_CONSISTENT_INVENTORY_UNBOUND", "rank": identity["rank"], "bs": bs,
            "replays": replays, "inventory_bound": False, "completion_established": False}


def read_inventory(raw, transport_pin, identity, expected, calibration):
    """R3: bind the inventory document by value before any node is used."""
    need(type(raw) is bytes and 0 < len(raw) <= INVENTORY_LIMIT, "bounded inventory bytes")
    need(type(transport_pin) is dict and transport_pin.get("sha256") == hashlib.sha256(raw).hexdigest()
         and transport_pin.get("bytes") == len(raw), "inventory bytes equal the transport index pin")
    doc = json.loads(raw)
    need(type(doc) is dict and doc.get("schema") == 1 and doc.get("status") == "COMPLETE"
         and doc.get("completion_claimed") is False and doc.get("host_metadata_only") is True
         and doc.get("gpu_tensor_reads") == 0 and doc.get("keep_graph_changed") is False, "complete inventory")
    for key in ("rank", "pid", "nonce", "start", "role"):
        need(doc.get(key) == identity[key], "inventory worker identity: " + key)
    producer = doc.get("producer")
    need(type(producer) is dict and producer.get("source_sha256") == expected["producer_sha256"]
         and producer.get("abi_sha256") == expected["abi_sha256"], "inventory producer and ABI pins")
    for key, value in EXPECTED_PRODUCER.items():
        need(producer.get(key) == value, "profiling twin pin: " + key)
    meta = calibration["binding"]["metadata"]
    need(meta.get("hip_runtime_version") == HIP_VERSION and meta.get("hip_driver_version") == HIP_VERSION,
         "trace HIP runtime/driver identity")
    need(not doc.get("foreign_replays") and doc.get("replay_thread_count") in (0, 1) and not doc.get("errors"),
         "no foreign, multi-thread or failed producer events")
    return doc


def _capture(doc, graph_role, bs):
    found = [c for c in doc["captures"] if c.get("attributed") is True
             and c.get("graph_role") == graph_role and c.get("bs") == bs]
    need(len(found) == 1, "exactly one attributed capture for %s bs=%d" % (graph_role, bs))
    capture = found[0]
    need(capture.get("status") == "INVENTORIED" and type(capture.get("capture_index")) is int
         and capture.get("keep_graph") is False, "inventoried capture without template retention")
    return capture


def expected_dispatch(capture):
    """Explicit node rules -> expected dispatch-name multiset; other node types refuse."""
    symbols = capture.get("symbols"); nodes = capture.get("nodes")
    need(type(symbols) is list and type(nodes) is list and 0 < len(nodes) <= 65536, "bounded node list")
    need(capture.get("node_count") == len(nodes), "node count")
    expected = Counter()
    for node in nodes:
        need(type(node) is list and len(node) == 2 and node[0] in NODE_RULES,
             "graph node type without a qualified dispatch rule")
        rule = NODE_RULES[node[0]]
        if rule == "symbol":
            need(type(node[1]) is int and 0 <= node[1] < len(symbols), "kernel node symbol index")
            symbol = symbols[node[1]]
            need(type(symbol) is str and 0 < len(symbol) <= 8192, "kernel node symbol")
            expected[symbol] += 1
        else:
            need(node[1] is None, "non-kernel node carries no symbol")
            if rule is not None:
                expected[rule] += 1
    return expected


def bind_inventory(window, window_name, identity, raw, transport_pin, expected, calibration):
    """Exact inventory equality and replay identity per labelled replay; only then GPU spans."""
    need(window["status"] == "REPLAY_CONSISTENT_INVENTORY_UNBOUND", "consistent replay set")
    doc = read_inventory(raw, transport_pin, identity, expected, calibration)
    counted = doc.get("replays", {}).get(window_name)
    need(type(counted) is dict, "producer replay record for this window")
    seen = Counter(); out = []
    for replay in window["replays"]:
        capture = _capture(doc, replay["graph_role"], window["bs"])
        seen[str(capture["capture_index"])] += 1
        want = expected_dispatch(capture)
        observed = Counter(replay["_names"])
        missing = want - observed
        extra = observed - want
        need(not missing, "graph nodes without dispatch records: %d" % sum(missing.values()))
        need(not extra, "dispatch records outside the inventory: %d" % sum(extra.values()))
        launch = cd.to_domain(replay["_api_begin_ns"], "RUNTIME", "GPU", calibration, replay["sequence"])
        begin, end = replay["_gpu_span_ns"]
        out.append({"sequence": replay["sequence"], "graph_role": replay["graph_role"], "bs": window["bs"],
                    "capture_index": capture["capture_index"], "nodes_sha256": capture.get("nodes_sha256"),
                    "gpu_span_ns": [begin, end], "gpu_duration_ns": end - begin,
                    "launch_to_first_dispatch_ns": [begin - launch[1], begin - launch[0]]})
    need(dict(seen) == {k: v for k, v in counted.items()},
         "producer-counted replays equal the labelled replays of the inventoried graphs")
    return {"status": "INVENTORY_COMPLETE_REPLAYS", "rank": identity["rank"], "bs": window["bs"],
            "window": window_name, "replays": out, "inventory_bound": True, "completion_established": True,
            "node_rules": "prospective explicit rules; exact multiset equality",
            "inventory_sha256": transport_pin["sha256"]}


def public(window):
    """Retain counts and signatures only; full symbol lists stay in the raw trace."""
    return dict(window, replays=[{k: v for k, v in r.items() if not k.startswith("_") and k != "symbols"}
                                 for r in window["replays"]])
