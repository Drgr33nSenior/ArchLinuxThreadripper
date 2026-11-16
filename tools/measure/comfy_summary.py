#!/usr/bin/env python3
"""Print comfy_mem.py's per-phase peaks (summary.json) on one line. Usage: comfy_summary.py SUMMARY_JSON"""
import json
import sys


def line(summary):
    return " | ".join(
        phase + " " + " ".join(f"{pod}: anon+shmem {v['anon'] + v['shmem']:.2f} ws {v['working_set']:.2f} "
                               f"cur {v['current']:.2f}" for pod, v in pods.items())
        for phase, pods in summary.items() if phase != "limit_gib")


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if len(argv) == 2 else 2
    with open(argv[1]) as handle:
        print(line(json.load(handle)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
