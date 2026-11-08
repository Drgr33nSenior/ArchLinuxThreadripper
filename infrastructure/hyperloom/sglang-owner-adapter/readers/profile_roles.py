"""Matched profiling roles, consumed op227 route map and per-role member witnesses.

The roles are the exact operation227 arms: control (physical kv4, logical
"control") and candidate (physical bf16, logical "mtp"). Effective members are
derived from compiler names and caps, not from the inherited four-member label
list in BUNDLE-STARTUP. Cap32, Down-M16, BW-03 and any fifth member refuse.

Trace visibility differs by member. F1 has a candidate-only dispatch symbol.
Prefetch (attention stage1) and gate/up wave0 reuse the control symbols, so the
trace cannot distinguish them; their identity rests on startup compiler records
and capture-time counters, joined to the profiled worker before HTTP.
"""
from collections import Counter

# R7: one authority for the matched identity and its frozen start order.
import shared_diagnostic as core

ROLES = {
    "control": {"arm": "control", "physical": "kv4", "members": [],
                "compilers": ["attention-stage1", "attention-stage2-cap16",
                              "gate-up-m16-wave1", "gate-up-m8-wave1"]},
    "candidate": {"arm": "mtp", "physical": "bf16",
                  "members": ["gatednorm_quant", "gate_up_wave", "verify_prefetch"],
                  "compilers": ["attention-stage1", "attention-stage2-cap16", "f1-t16", "f1-t8",
                                "gate-up-m16-wave0", "gate-up-m8-wave0"]},
}
EXCLUDED = ("verify_cap32", "down_m16", "bw03", "cap32")
GDN_LAYERS = 48
TARGET_LAYERS = 64
F1_SYMBOL = "_gated_norm_mxfp4_kernel.kd"
QUANT_SYMBOL = "_dynamic_mxfp4_quant_kernel.kd"
SHARED_SYMBOLS = {"verify_prefetch": "_fwd_grouped_kernel_stage1.kd",
                  "gate_up_wave": "_gemm_afp4wfp4_kernel.kd"}
IDENTITY_FIELDS = {"schema", "role", "start", "rank", "pid", "nonce", "session_sha256",
                   "source_manifest_sha256", "worker_identity_sha256", "members",
                   "power_cap_watts", "profiled", "control"}


def need(ok, message):
    if not ok:
        raise ValueError("profile roles: " + message)


def hex64(value):
    need(type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value), "sha256")
    return value


def identity_check(identity, order=None):
    """Delegate to the candidate shared_diagnostic.identity_check (role, members, start order)."""
    need(order is None or order == ["candidate", "control"], "frozen two-role order")
    core.identity_check(identity)
    need(type(identity) is dict and set(identity) == IDENTITY_FIELDS, "identity fields")
    need(type(identity["schema"]) is int and identity["schema"] == 2, "matched identity schema")
    need(identity["role"] in ROLES and identity["members"] == ROLES[identity["role"]]["members"],
         "role/member set")
    need(identity["control"] is (identity["role"] == "control"), "control flag follows role")
    need(identity["profiled"] is True and identity["power_cap_watts"] == 235, "profiled at configured cap")
    need(type(identity["rank"]) is int and identity["rank"] in (0, 1)
         and type(identity["pid"]) is int and identity["pid"] > 0, "rank/pid")
    for key in ("session_sha256", "source_manifest_sha256", "worker_identity_sha256"):
        hex64(identity[key])
    need(type(identity["nonce"]) is str and len(identity["nonce"]) == 16
         and all(c in "0123456789abcdef" for c in identity["nonce"]), "nonce")
    return identity


def route_map(observations, compiler_pins, runtime_identity):
    """Bind both roles on both ranks from authenticated op227 worker observations.

    observations: {role: {"startup": [rank0, rank1], "enabled": [rank0, rank1]}}.
    """
    need(set(observations) == set(ROLES), "both roles observed")
    need(set(compiler_pins) == {"control", "mtp"}, "compiler pins per logical arm")
    arms = runtime_identity["serving_arms"]
    cli = {role: arms[ROLES[role]["arm"]]["canonical_server_cli"] for role in ROLES}
    diff = [i for i, (a, b) in enumerate(zip(cli["control"], cli["candidate"], strict=True)) if a != b]
    need(len(diff) == 1 and cli["control"][diff[0] - 1] == "--port", "server CLIs differ only in port")
    contract = arms["control"]["resolved_contract"]
    need(contract == arms["mtp"]["resolved_contract"], "identical resolved runtime contract")
    need(contract["cuda_graph_bs_decode"] == [1, 2] and contract["tp_size"] == 2
         and contract["cuda_graph_backend_decode"] == "full"
         and contract["cuda_graph_backend_prefill"] == "disabled", "TP2 full decode graphs at bs 1/2")
    rows = []
    for role, spec in ROLES.items():
        pins = compiler_pins[spec["arm"]]
        for rank in (0, 1):
            startup = observations[role]["startup"][rank]
            enabled = observations[role]["enabled"][rank]
            need(startup["identity"]["arm"] == spec["arm"] and startup["identity"]["rank"] == rank
                 and enabled["arm"] == spec["arm"] and enabled["rank"] == rank, "worker role/rank identity")
            need(startup["identity"]["boot_id"] == enabled["boot_id"]
                 and startup["identity"]["pid"] == enabled["pid"], "startup/route worker join")
            names = sorted(startup["selected_compilers"])
            need(not any("cap32" in n for n in names), "cap32 compiler present")
            need(names == spec["compilers"], "%s rank %d compiler names" % (role, rank))
            for name in names:
                need(startup["selected_compilers"][name] == pins[name], "compiler record differs from pin: " + name)
            need(enabled["verification_caps"] == [16] and enabled["native_decode_caps"] == [16],
                 "private verification and ordinary decode cap16")
            prefetch = enabled["prefetch_capture_calls"]
            need(prefetch == ({"16": 16, "8": 16} if role == "candidate" else {"16": 0, "8": 0}),
                 "prefetch capture calls follow role")
            # f1_capture counts *eligible* captured T8/T16 geometries in both arms;
            # the control then returns the original path. Route identity is the
            # selected f1-t8/t16 compiler records plus the trace F1 symbol.
            need(startup["f1_fallback8"] is False and len(startup["f1_capture"]) == GDN_LAYERS and
                 all(v == {"16": 1, "8": 1} for v in startup["f1_capture"].values()),
                 "F1 eligibility seen on all GDN layers at T8/T16 in both arms")
            need(len(startup["gemm_capture"]) == TARGET_LAYERS and
                 all(v["8"] > 0 and v["16"] > 0 for v in startup["gemm_capture"].values()),
                 "gate/up captured on all layers at M8/M16")
            rows.append({"role": role, "rank": rank, "arm": spec["arm"], "physical": spec["physical"],
                         "members": spec["members"], "compilers": names,
                         "compiler_hsaco_sha256": {n: pins[n]["parts"]["hsaco"]["sha256"] for n in names},
                         "verification_caps": [16], "native_decode_caps": [16],
                         "prefetch_capture_calls": prefetch, "worker_pid": enabled["pid"],
                         "boot_id": enabled["boot_id"]})
    differs = {}
    for name in ("attention-stage1", "attention-stage2-cap16"):
        differs[name] = compiler_pins["control"][name]["parts"]["hsaco"]["sha256"] != \
            compiler_pins["mtp"][name]["parts"]["hsaco"]["sha256"]
    need(differs["attention-stage1"], "prefetch stage1 object must differ from control")
    return {"status": "OP227_ROUTES_BOUND", "rows": rows, "graph_bs": [1, 2],
            "server_cli_difference": "port only", "attention_object_differs": differs,
            "excluded": list(EXCLUDED),
            "trace_symbol_visibility": {"gatednorm_quant": F1_SYMBOL,
                                        "verify_prefetch": "shared symbol " + SHARED_SYMBOLS["verify_prefetch"],
                                        "gate_up_wave": "shared symbol " + SHARED_SYMBOLS["gate_up_wave"]},
            "inherited_member_label_ignored": True}


def startup_witness(identity, startup, enabled, route):
    """Join a profiled worker's own pre-HTTP startup records to the frozen route row."""
    rows = [r for r in route["rows"] if r["role"] == identity["role"] and r["rank"] == identity["rank"]]
    need(len(rows) == 1, "one frozen route row")
    row = rows[0]
    need(startup["identity"]["pid"] == identity["pid"] == enabled["pid"], "profiled worker identity")
    need(startup.get("before_external_generations") is True and enabled.get("before_external_generations") is True,
         "witness recorded before HTTP generations")
    names = sorted(startup["selected_compilers"])
    need(names == row["compilers"], "profiled compiler names equal op227 route")
    for name in names:
        need(startup["selected_compilers"][name]["parts"]["hsaco"]["sha256"] == row["compiler_hsaco_sha256"][name],
             "profiled compiler object equals op227 route: " + name)
    need(enabled["verification_caps"] == row["verification_caps"]
         and enabled["native_decode_caps"] == row["native_decode_caps"]
         and enabled["prefetch_capture_calls"] == row["prefetch_capture_calls"], "profiled caps/prefetch capture")
    return {"role": identity["role"], "rank": identity["rank"], "status": "STARTUP_ROUTE_EQUALS_OP227",
            "trace_distinguishable": {"gatednorm_quant": True, "verify_prefetch": False, "gate_up_wave": False}}


def trace_witness(role, replays):
    """Per-replay member symbol checks; F1 exact, shared symbols counted only."""
    need(role in ROLES and replays, "role replays")
    out = []
    for replay in replays:
        symbols = Counter(replay["symbols"])
        f1 = symbols.get(F1_SYMBOL, 0)
        if replay["graph_role"] == "VERIFY" and role == "candidate":
            need(f1 == GDN_LAYERS, "candidate VERIFY replay must contain one F1 dispatch per GDN layer")
        else:
            need(f1 == 0, "F1 dispatch outside candidate VERIFY")
        out.append({"sequence": replay["sequence"], "graph_role": replay["graph_role"], "bs": replay["bs"],
                    "f1": f1, "quant": symbols.get(QUANT_SYMBOL, 0),
                    "stage1": symbols.get(SHARED_SYMBOLS["verify_prefetch"], 0),
                    "mxfp4_gemm": symbols.get(SHARED_SYMBOLS["gate_up_wave"], 0)})
    return out


def matched_delta(control_rows, candidate_rows):
    """Source-derived structural expectation between matched VERIFY replays of one rank/bs."""
    pairs = {}
    for side, rows in (("control", control_rows), ("candidate", candidate_rows)):
        for r in rows:
            if r["graph_role"] == "VERIFY":
                pairs.setdefault(side, []).append(r)
    need(set(pairs) == {"control", "candidate"}, "matched VERIFY replays")
    for key in ("quant", "stage1", "mxfp4_gemm"):
        need(len({r[key] for r in pairs["control"]}) == 1 and len({r[key] for r in pairs["candidate"]}) == 1,
             "replay-stable member counts: " + key)
    c, k = pairs["control"][0], pairs["candidate"][0]
    need(c["bs"] == k["bs"], "same batch size")
    need(c["quant"] - k["quant"] == GDN_LAYERS, "F1 removes one standalone quantizer per GDN layer")
    need(c["stage1"] == k["stage1"] and c["mxfp4_gemm"] == k["mxfp4_gemm"],
         "prefetch and wave0 keep dispatch counts (identity bound at startup)")
    return {"bs": c["bs"], "quant_delta": GDN_LAYERS, "stage1": c["stage1"], "mxfp4_gemm": c["mxfp4_gemm"],
            "status": "STRUCTURAL_MEMBER_DELTA_CONSISTENT"}
