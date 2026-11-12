#!/usr/bin/env python3
"""Prepare a service render for `workstationctl mode` to apply inside a session switch.

A render leaves every Deployment at zero replicas. Applied as it is, it would stop Open
WebUI, the gateway and the tools service. This writes the render as one JSON List with:

- every session-managed GPU Deployment at zero (the session starts them in order);
- every other Deployment at its live replica count (a Deployment the cluster doesn't have
  yet stays at the render's zero, and is reported).

Usage: service_mode.py prepare <workloads.yaml> <live-deployments.json> <new-apply.json> --managed a,b,c
Prints a JSON summary. Reads and writes local files only; never contacts the cluster.
"""
import argparse
import json
import os
import sys

import yaml


def prepare(workloads, live, output, managed):
    with open(workloads, encoding="utf-8") as handle:
        items = [doc for doc in yaml.safe_load_all(handle) if doc]
    with open(live, encoding="utf-8") as handle:
        replicas = {d["metadata"]["name"]: d["spec"].get("replicas", 0)
                    for d in json.load(handle).get("items", [])}
    names = [d["metadata"]["name"] for d in items if d.get("kind") == "Deployment"]
    if len(names) != len(set(names)):
        raise ValueError("the render holds a Deployment twice")
    summary = {"deployments": {}, "not_live": [], "managed": sorted(managed & set(names))}
    for doc in items:
        if doc.get("kind") != "Deployment":
            continue
        name = doc["metadata"]["name"]
        if name in managed:
            doc["spec"]["replicas"] = 0
        elif name in replicas:
            doc["spec"]["replicas"] = replicas[name]
        else:
            summary["not_live"].append(name)
        summary["deployments"][name] = doc["spec"]["replicas"]
    if os.path.exists(output):
        raise ValueError(f"{output} already exists")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"apiVersion": "v1", "kind": "List", "items": items}, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("prepare")
    run.add_argument("workloads")
    run.add_argument("live")
    run.add_argument("output")
    run.add_argument("--managed", required=True, help="comma-separated session-managed Deployment names")
    args = parser.parse_args(argv)
    managed = {name for name in args.managed.split(",") if name}
    if not managed:
        parser.error("--managed must name at least one Deployment")
    try:
        summary = prepare(args.workloads, args.live, args.output, managed)
    except (OSError, ValueError, KeyError, yaml.YAMLError) as error:
        parser.exit(1, f"service_mode: {error}\n")
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
