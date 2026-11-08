"""Closed capability manifest for the matched profile; unavailable means refused.

Every entry names the retained evidence that observed it. Nothing here was
executed on the workstation. A question may only be answered by a capability
whose status is OBSERVED (or STATIC within its stated scope); requests for
UNAVAILABLE/UNQUALIFIED/BLOCKED capabilities refuse instead of degrading.
"""

OBSERVED, STATIC, UNQUALIFIED, UNAVAILABLE, BLOCKED = (
    "OBSERVED", "STATIC_ONLY", "UNQUALIFIED", "UNAVAILABLE", "BLOCKED_ON_SOURCE")

MANIFEST = {
    "kineto.cpu_ops_and_user_annotations": dict(
        status=OBSERVED, scope="torch RecordFunction events in the CPU_OP domain",
        evidence="op217 retained traces (cat cpu_op/user_annotation, External id)"),
    "kineto.runtime_api_records": dict(
        status=OBSERVED, scope="ROCm runtime calls in the RUNTIME domain with unique correlation ids",
        evidence="op217 retained traces (cat cuda_runtime)"),
    "kineto.dispatch_records": dict(
        status=OBSERVED, scope="kernel/copy dispatch begin/end, device, stream, hsa_queue, correlation; "
                               "grid/block only for direct launches, absent for graph-launched dispatches",
        evidence="op217 retained traces (cat kernel)"),
    "kineto.copy_bytes_and_derived_bandwidth_arg": dict(
        status=UNQUALIFIED, scope="direct copies carry bytes and a Kineto-derived 'memory bandwidth (GB/s)' "
                                  "argument computed from bytes/duration; not a physical traffic counter",
        evidence="op217 retained traces"),
    "kineto.nccl_collective_metadata": dict(
        status=OBSERVED, scope="direct collective dispatches with collective name and sizes; duration includes waiting",
        evidence="op217 retained traces"),
    "kineto.gpu_user_annotation": dict(
        status=OBSERVED, scope="Kineto-synthesized GPU-domain spans from external correlation; GPU domain only",
        evidence="op217 retained traces"),
    "clock.causal_offset_bounds": dict(
        status=OBSERVED, scope="per-window intervals HOST_MONO->CPU_OP->RUNTIME->GPU from happens-before relations",
        evidence="clock_domains.py over 12 op217 decode windows"),
    "clock.profiler_calibration_values": dict(
        status=UNAVAILABLE, scope="getTimeOffset()/converter values are not exported by the selected Kineto",
        evidence="nine byte-matched Kineto sources; CLOCK-SOURCE-AUDIT 4f28ade5"),
    "graph.replay_dispatch_membership": dict(
        status=OBSERVED, scope="dispatches correlated to one hipGraphLaunch", evidence="op217 retained traces"),
    "graph.capture_node_inventory": dict(
        status=BLOCKED, scope="authenticated node list of each captured graph",
        evidence="installed torch/HIP graph-introspection API not retained locally; native source check prepared"),
    "graph.label_producer": dict(
        status=BLOCKED, scope="source of step[DRAFT|VERIFY bs=N] labels",
        evidence="absent from the 34 retained selected installed sources; release reference hash mismatched"),
    "rocprofv3.kernel_trace": dict(
        status=OBSERVED, scope="SDK 1.4.2 kernel trace route in a smoke component only",
        evidence="operation175 smoke (failed allocator guard; trace flushed)"),
    "rocprofv3.hardware_counters": dict(
        status=UNAVAILABLE, scope="PMC/SQ/TCC/TCP counters on gfx1201; no --list-avail output retained, "
                                  "the adapter supports no hardware counters",
        evidence="no retained counter inventory"),
    "isa.static_resource_metadata": dict(
        status=STATIC, scope="per-object VGPR/SGPR/LDS/waves_per_eu/num_warps and amdgcn instruction text; "
                             "not dynamic counts, stalls, occupancy or a speed bound",
        evidence="BUNDLE-COMPILER-PINS amdgcn/metadata hashes"),
    "isa.dynamic_instruction_counts": dict(status=UNAVAILABLE, scope="needs hardware counters", evidence="none"),
    "occupancy.dynamic": dict(status=UNAVAILABLE, scope="needs hardware counters", evidence="none"),
    "stalls.dependency_or_memory": dict(status=UNAVAILABLE, scope="needs hardware counters", evidence="none"),
    "cache.hit_rates": dict(status=UNAVAILABLE, scope="needs TCC/TCP counters", evidence="none"),
    "memory.physical_bandwidth": dict(
        status=UNAVAILABLE, scope="physical GB/s needs DRAM/UMC traffic counters",
        evidence="AMD-SMI MEM% maps to average_umc_activity (activity %), sysfs mem_busy_percent is activity"),
    "sysfs.activity_clock_power_temperature": dict(
        status=OBSERVED, scope="10 s caller-side point samples; EBUSY retained; contradictory power reads retained",
        evidence="op227 SAMPLES.jsonl (149 rows)"),
    "sysfs.gpu_metrics_decode": dict(
        status=BLOCKED, scope="decode requires the installed kernel's gpu_metrics struct for header 78000103",
        evidence="raw 120-byte records only; kernel 7.3.0-rc6-3-git source not retained"),
    "energy_throttle_cap_residency": dict(
        status=UNAVAILABLE, scope="10 s point samples cannot establish energy, throttling or cap residency",
        evidence="op227 resource review F4"),
}

QUESTIONS = {
    "kernel_time": ["kineto.dispatch_records", "clock.causal_offset_bounds"],
    "operand_preparation_cost": ["kineto.dispatch_records", "isa.static_resource_metadata"],
    "memory_traffic": ["memory.physical_bandwidth"],
    "memory_activity": ["sysfs.activity_clock_power_temperature"],
    "cache_behaviour": ["cache.hit_rates"],
    "register_pressure_static": ["isa.static_resource_metadata"],
    "occupancy_dynamic": ["occupancy.dynamic"],
    "dependency_stalls": ["stalls.dependency_or_memory"],
    "host_launch_gaps": ["kineto.runtime_api_records", "kineto.dispatch_records", "clock.causal_offset_bounds"],
    "graph_coverage": ["graph.replay_dispatch_membership", "graph.capture_node_inventory"],
    "graph_role_labels": ["graph.label_producer"],
    "tp_wait": ["kineto.nccl_collective_metadata", "kineto.dispatch_records", "clock.causal_offset_bounds"],
    "copies": ["kineto.dispatch_records", "kineto.runtime_api_records"],
    "power_clocks_temperature": ["sysfs.activity_clock_power_temperature"],
    "gpu_metrics_fields": ["sysfs.gpu_metrics_decode"],
    "energy_or_throttle": ["energy_throttle_cap_residency"],
}
ANSWERABLE = (OBSERVED, STATIC)


def need(ok, message):
    if not ok:
        raise ValueError("capability: " + message)


def require(question):
    """Return the capabilities for a question, or refuse naming the missing one."""
    need(question in QUESTIONS, "undeclared question " + repr(question))
    rows = []
    for name in QUESTIONS[question]:
        entry = MANIFEST[name]
        need(entry["status"] in ANSWERABLE, "%s needs %s (%s)" % (question, name, entry["status"]))
        rows.append(dict(entry, name=name))
    return rows


def disposition():
    out = {}
    for question in sorted(QUESTIONS):
        try:
            rows = require(question)
            out[question] = {"answerable": True, "scope": [r["status"] + ": " + r["scope"] for r in rows]}
        except ValueError as error:
            out[question] = {"answerable": False, "refusal": str(error)}
    return out
