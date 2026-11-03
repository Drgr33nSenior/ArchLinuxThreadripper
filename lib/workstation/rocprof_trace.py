"""Convert one rocprofv3 JSON process to an offline Kineto-shaped kernel trace.

This adapter preserves HIP launch correlations, but cannot recover Torch
operators, stacks, tensor shapes or allocations. It never executes a workload.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

MAX_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 100_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name, minimum=0):
    require(type(value) is int and minimum <= value < 2**64, "invalid " + name)
    return value


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


def dimensions(value, name):
    require(isinstance(value, dict), "missing " + name)
    return [integer(value.get(axis), name, 1) for axis in ("x", "y", "z")]


def indexed(rows, key, name):
    require(isinstance(rows, list), "missing " + name)
    result = {}
    for row in rows:
        require(isinstance(row, dict), "invalid " + name)
        identity = integer(key(row), name)
        require(identity not in result, "duplicate " + name)
        result[identity] = row
    return result


def convert(raw):
    """Return JSON data; reject partial, ambiguous or empty kernel captures."""
    require(len(raw) <= MAX_BYTES, "rocprof input exceeds 16 MiB")
    document = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    require(isinstance(document, dict), "rocprof document must be an object")
    processes = document.get("rocprofiler-sdk-tool")
    require(isinstance(processes, list) and len(processes) == 1, "exactly one traced process required")
    process = processes[0]
    require(isinstance(process, dict), "invalid process")
    metadata = process.get("metadata", {})
    pid = integer(metadata.get("pid"), "process id", 1)
    origin = integer(metadata.get("init_time"), "initial timestamp")
    finish = integer(metadata.get("fini_time"), "final timestamp", origin + 1)
    agents = indexed(process.get("agents"), lambda a: a.get("id", {}).get("handle"), "agent id")
    symbols = indexed(process.get("kernel_symbols"), lambda s: s.get("kernel_id"), "kernel id")
    buffers = process.get("buffer_records")
    require(isinstance(buffers, dict) and all(isinstance(v, list) for v in buffers.values()), "invalid record lists")
    kernels, hip = buffers.get("kernel_dispatch", []), buffers.get("hip_api", [])
    require(kernels and len(kernels) + len(hip) <= MAX_EVENTS, "empty or excessive event count")
    operations = process.get("strings", {}).get("buffer_records", [])
    events, dispatches, devices = [], set(), {}

    def timing(row):
        start = integer(row.get("start_timestamp"), "start timestamp", origin)
        end = integer(row.get("end_timestamp"), "end timestamp", start)
        require(end <= finish, "event extends beyond profiler finalization")
        # Subtract the common integer clock origin before converting to microseconds.
        return (start - origin) / 1000, (end - start) / 1000

    for row in kernels:
        info = row["dispatch_info"]
        dispatch = integer(info.get("dispatch_id"), "dispatch id")
        require(dispatch not in dispatches, "duplicate dispatch id")
        dispatches.add(dispatch)
        agent = agents[integer(info["agent_id"]["handle"], "dispatch agent")]
        require(agent.get("type") == 2, "kernel dispatched to a non-GPU agent")
        device = integer(agent.get("gpu_index"), "GPU index")
        require(device not in devices or devices[device] == agent, "ambiguous GPU index")
        devices[device] = agent
        symbol = symbols[integer(info.get("kernel_id"), "dispatch kernel")]
        name = symbol.get("demangled_kernel_name") or symbol.get("formatted_kernel_name") or symbol.get("kernel_name")
        require(isinstance(name, str) and 0 < len(name) <= 65536, "invalid kernel name")
        ts, duration = timing(row)
        require(duration > 0, "kernel duration must be positive")
        block = dimensions(info.get("workgroup_size"), "workgroup dimensions")
        workitems = dimensions(info.get("grid_size"), "grid dimensions")
        queue = integer(info["queue_id"]["handle"], "queue id")
        stream = integer(row.get("stream_id", {"handle": queue})["handle"], "stream id")
        correlation = integer(row["correlation_id"]["internal"], "kernel correlation")
        events.append({"ph": "X", "cat": "kernel", "name": name, "pid": -device - 1,
                       "tid": queue, "ts": ts, "dur": duration,
                       "args": {"device": device, "stream": stream, "correlation": correlation,
                                "grid": [(g + b - 1) // b for g, b in zip(workitems, block)],
                                "block": block, "rocprof_grid_workitems": workitems,
                                "rocprof_queue": queue, "rocprof_dispatch_id": dispatch,
                                "rocprof_kernel_id": info["kernel_id"], "rocprof_agent_id": agent["id"]["handle"]}})
    for row in hip:
        kind = integer(row.get("kind"), "HIP kind")
        operation = integer(row.get("operation"), "HIP operation")
        require(kind < len(operations) and operations[kind].get("kind") in {"HIP_RUNTIME_API", "HIP_RUNTIME_API_EXT"},
                "unknown HIP API dictionary")
        names = operations[kind].get("operations", [])
        require(operation < len(names) and isinstance(names[operation], str), "unknown HIP operation")
        ts, duration = timing(row)
        events.append({"ph": "X", "cat": "cuda_runtime", "name": names[operation], "pid": pid,
                       "tid": integer(row.get("thread_id"), "HIP thread id"), "ts": ts, "dur": duration,
                       "args": {"correlation": integer(row["correlation_id"]["internal"], "HIP correlation"),
                                "backend": "HIP"}})
    properties = []
    for device, agent in sorted(devices.items()):
        location = integer(agent.get("location_id"), "PCI location")
        domain = integer(agent.get("domain"), "PCI domain")
        properties.append({"id": device, "name": agent.get("product_name", agent.get("name", "AMD GPU")),
                           "arch": agent.get("name"), "rocprof_agent_id": agent["id"]["handle"],
                           "pciBusID": f"{domain:04x}:{location >> 8:02x}:{(location >> 3) & 31:02x}.{location & 7}"})
        events.append({"ph": "M", "name": "process_name", "pid": -device - 1, "tid": 0,
                       "args": {"name": f"GPU {device}"}})
    return {"schemaVersion": 1, "displayTimeUnit": "us", "deviceProperties": properties,
            "traceEvents": events, "rocprof_conversion": {
                "schema": 1, "source_sha256": hashlib.sha256(raw).hexdigest(), "source_bytes": len(raw),
                "origin_ns": origin, "process_id": pid, "kernel_events": len(kernels), "hip_api_events": len(hip),
                "kernel_duration_ns": sum(r["end_timestamp"] - r["start_timestamp"] for r in kernels),
                "omitted_record_counts": {k: len(v) for k, v in sorted(buffers.items())
                                           if k not in {"kernel_dispatch", "hip_api"}},
                "operator_attribution": False, "runtime_category": "cuda_runtime denotes HIP compatibility only",
                "losses": ["Torch operators", "CPU stacks", "tensor shapes", "allocations", "non-kernel non-HIP records"]}}


def convert_file(source, destination):
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_BYTES, "bounded regular input required")
        raw = stream.read(MAX_BYTES + 1)
    result = convert(raw)
    encoded = (json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode()
    require(len(encoded) <= MAX_BYTES, "converted trace exceeds 16 MiB")
    with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(encoded)
    return result["rocprof_conversion"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(convert_file(args.source, args.destination), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError) as error:
        print("rocprof trace conversion refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
