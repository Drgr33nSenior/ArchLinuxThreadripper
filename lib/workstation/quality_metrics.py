"""Validate the pinned llama.cpp numerical-test CSV and perplexity output."""
import argparse
import csv
import json
import math
from pathlib import Path
import re


def ops(path, backend):
    with open(path) as source:
        rows = list(csv.DictReader(source))
    rows = [r for r in rows if r.get("backend_name") == backend and r.get("test_mode") == "test"]
    supported = [r for r in rows if r.get("supported") in ("1", "true")]
    if not supported or any(r.get("passed") not in ("1", "true") for r in supported):
        raise ValueError("no numerical tests ran or a supported test failed")
    for operation in ("MUL_MAT", "RMS_NORM", "SOFT_MAX"):
        if not any(r.get("op_name") == operation for r in supported):
            raise ValueError("a required numerical operation was not tested")
    return {"supported_passes": len(supported), "unsupported": len(rows) - len(supported)}


def perplexity(path):
    matches = re.findall(r"Final estimate: PPL = ([0-9.eE+-]+) \+/- ([0-9.eE+-]+)", Path(path).read_text())
    if len(matches) != 1:
        raise ValueError("missing or ambiguous final perplexity result")
    value, error = map(float, matches[0])
    if not math.isfinite(value) or not math.isfinite(error) or value <= 0 or error < 0:
        raise ValueError("invalid perplexity estimate")
    return {"perplexity": value, "uncertainty": error,
            "acceptance": "compare same corpus/model against HIP and CPU reference; not an automatic quality pass"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ops", "perplexity"))
    parser.add_argument("file")
    parser.add_argument("--backend")
    args = parser.parse_args()
    print(json.dumps(ops(args.file, args.backend) if args.mode == "ops" else perplexity(args.file)))


if __name__ == "__main__":
    main()
