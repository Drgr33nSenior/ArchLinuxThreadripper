#!/usr/bin/env python3
"""Sample the ComfyUI pods' cgroup memory every 0.5 s until OUT_DIR/STOP exists; print per-phase peaks.

Usage: comfy_mem.py OUT_DIR POD_UID...
Mark phases by appending "EPOCH NAME" lines to OUT_DIR/marks. Records memory.current, anon, file, shmem
and the working set (current - inactive_file), the figure the kubelet evicts on. Writes timeline.json and
summary.json to OUT_DIR. Read-only; run on the workstation (cgroup v2, kubepods.slice).
"""
import itertools
import json
import sys
import time
from pathlib import Path

GIB = 1 << 30


def pod_path(uid):
    return Path(f"/sys/fs/cgroup/kubepods.slice/kubepods-pod{uid.replace('-', '_')}.slice")


def sample(path):
    stat = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
    current = int((path / "memory.current").read_text())
    return {"current": current, "anon": int(stat["anon"]), "file": int(stat["file"]), "shmem": int(stat["shmem"]),
            "working_set": current - int(stat["inactive_file"])}


def parse_marks(text):
    marks = [line.split(maxsplit=1) for line in text.splitlines() if line.strip()]
    return [(float(t), name) for t, name in marks]


def summarise(rows, marks, limits):
    """Peak GiB per phase and pod. A phase runs from its mark to the next one (the last to the end)."""
    summary = {"limit_gib": {name: limit / GIB for name, limit in limits.items()}}
    if not rows:
        return summary
    bounds = list(marks) + [(rows[-1]["t"] + 1, "end")]
    pods = list(limits)
    for (start, name), (stop, _) in itertools.pairwise(bounds):
        window = [r for r in rows if start <= r["t"] < stop]
        if window:
            summary[name] = {pod: {k: round(max(r[pod][k] for r in window) / GIB, 2) for k in window[0][pod]}
                             for pod in pods}
    return summary


def main(argv):
    if len(argv) < 3 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if len(argv) > 1 and argv[1] in ("-h", "--help") else 2
    out = Path(argv[1])
    pods = {uid[:8]: pod_path(uid) for uid in argv[2:]}
    rows = []
    while not (out / "STOP").exists():
        rows.append({"t": time.time(), **{name: sample(path) for name, path in pods.items()}})
        time.sleep(0.5)
    limits = {name: int((path / "memory.max").read_text()) for name, path in pods.items()}
    summary = summarise(rows, parse_marks((out / "marks").read_text()), limits)
    (out / "timeline.json").write_text(json.dumps(rows))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
