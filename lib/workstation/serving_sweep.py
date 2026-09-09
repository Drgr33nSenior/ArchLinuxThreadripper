"""Generate independent, offline JSON patches; never apply to a cluster."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path


def plan(deployment):
    containers = deployment["spec"]["template"]["spec"]["containers"]
    indexes = [i for i, c in enumerate(containers) if c["name"] == "sglang"]
    if len(indexes) != 1:
        raise ValueError("expected one existing SGLang container")
    index = indexes[0]
    container = containers[index]
    root = f"/spec/template/spec/containers/{index}"
    resources = container["resources"]
    if resources["requests"] != resources["limits"]:
        raise ValueError("retain Guaranteed CPU/memory/GPU envelopes")
    # Each case starts from the supplied baseline, NOT the preceding case.
    cases = {}
    for key, values in (("MAX_RUNNING_REQUESTS", (1, 2, 4)), ("MEM_FRACTION_STATIC", ("0.70", "0.75", "0.80")),
                        ("HSA_OVERRIDE_CPU_AFFINITY_DEBUG", (0, 1))):
        for value in values:
            env = [e for e in container.get("env", []) if e["name"] != key]
            env.append({"name": key, "value": str(value)})
            cases[f"{key.lower()}-{value}"] = [{"op": "add", "path": root + "/env", "value": env}]
    for prefill in (512, 1024, 2048, 4096):
        args = list(container["args"])
        if "--chunked-prefill-size" in args:
            args[args.index("--chunked-prefill-size") + 1] = str(prefill)
        else:
            args += ["--chunked-prefill-size", str(prefill)]
        cases[f"prefill-{prefill}"] = [{"op": "replace", "path": root + "/args", "value": args}]
    for cpus in (8, 12, 20):
        value = copy.deepcopy(resources)
        value["requests"]["cpu"] = value["limits"]["cpu"] = str(cpus)
        cases[f"cpu-{cpus}"] = [{"op": "replace", "path": root + "/resources", "value": value}]
    return cases


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment")
    parser.add_argument("output")
    args = parser.parse_args()
    raw = Path(args.deployment).read_bytes()
    cases = plan(json.loads(raw))
    output = Path(args.output)
    output.mkdir(mode=0o700)
    for name, patch in cases.items():
        (output / f"{name}.json").write_text(json.dumps(patch, indent=2) + "\n")
    (output / "plan.json").write_text(json.dumps({"schema": 1, "status": "plan-only",
        "baseline_sha256": hashlib.sha256(raw).hexdigest(), "cases": list(cases),
        "constraints": ["Apply each patch independently to the same reviewed baseline, not cumulatively.",
                        "Do not raise pod memory; /dev/shm counts inside its existing limit.",
                        "Revalidate whole-core SMT divisibility, allocatable node memory and other pod requests.",
                        "2 requests and .80 memory are baselines, not measured optima.",
                        "Collect allowlisted serving-evidence launch arguments; candidate flags must exist in the image. Do not dump /server_info (it can include credentials).",
                        "Do not use 9B versus 27B as a GPU-scaling comparison."]}, indent=2) + "\n")


if __name__ == "__main__":
    main()
