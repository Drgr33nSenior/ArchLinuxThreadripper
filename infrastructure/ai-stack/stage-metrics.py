#!/usr/bin/env python3
"""Record AI-stack stage progress as node-exporter textfile metrics.

Keeps per-stage state in DIR/ai-stack-state.json and atomically rewrites
DIR/ai-stack.prom, which node-exporter's textfile collector reads. Labels are
fixed: the stage name only. Telemetry must never fail a build, so an unusable
directory prints a warning and exits 0.

Usage: stage-metrics.py DIR STAGE start
       stage-metrics.py DIR STAGE finish --status N [--ccache-stats FILE]
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

STAGES = ("rocm", "torch", "sglang", "image", "comfyui", "swarmui", "creative", "graphiti")
STATE = "ai-stack-state.json"
TEXTFILE = "ai-stack.prom"


def ccache_ratio(path):
    """Hit ratio from `ccache --print-stats` (tab-separated key/value lines)."""
    try:
        stats = dict(line.split("\t", 1) for line in Path(path).read_text().splitlines() if "\t" in line)
        hits = int(stats.get("direct_cache_hit", 0)) + int(stats.get("preprocessed_cache_hit", 0))
        total = hits + int(stats.get("cache_miss", 0))
    except (OSError, ValueError):
        return None
    return hits / total if total else None


def render(state, now):
    series = {
        "workstation_ai_stack_stage_running": lambda s: int(s.get("running", False)),
        "workstation_ai_stack_stage_started_timestamp_seconds": lambda s: s.get("started"),
        "workstation_ai_stack_stage_last_result": lambda s: s.get("result"),
        "workstation_ai_stack_stage_last_finish_timestamp_seconds": lambda s: s.get("finished"),
        "workstation_ai_stack_stage_last_duration_seconds": lambda s: s.get("duration"),
        "workstation_ai_stack_ccache_hit_ratio": lambda s: s.get("ccache_hit_ratio"),
    }
    body = []
    for name, value in series.items():
        for stage in STAGES:
            if stage in state and value(state[stage]) is not None:
                body.append(f'{name}{{stage="{stage}"}} {value(state[stage])}')
    body.append(f"workstation_ai_stack_metrics_timestamp_seconds {now:.3f}")
    return "\n".join(body) + "\n"


def write_atomic(directory, name, text):
    descriptor, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, directory / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("event", choices=("start", "finish"))
    parser.add_argument("--status", type=int)
    parser.add_argument("--ccache-stats", type=Path)
    args = parser.parse_args()
    if args.event == "finish" and args.status is None:
        parser.error("finish needs --status")
    directory = args.directory
    if not (directory.is_dir() and os.access(directory, os.W_OK)):
        print(f"WARNING: build metrics not recorded; {directory} is not a writable directory", file=sys.stderr)
        return 0
    now = time.time()
    try:
        state = json.loads((directory / STATE).read_text())
    except (OSError, ValueError):
        state = {}
    entry = state.setdefault(args.stage, {})
    if args.event == "start":
        entry.update(running=True, started=round(now, 3))
    else:
        entry.update(running=False, result=int(args.status == 0), finished=round(now, 3),
                     duration=round(now - entry.get("started", now), 3))
        ratio = ccache_ratio(args.ccache_stats) if args.ccache_stats else None
        if ratio is not None:
            entry["ccache_hit_ratio"] = round(ratio, 4)
    write_atomic(directory, STATE, json.dumps(state, indent=2) + "\n")
    write_atomic(directory, TEXTFILE, render(state, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
