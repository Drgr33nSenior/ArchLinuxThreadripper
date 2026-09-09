"""Validate the pinned llama.cpp numerical-test CSV and perplexity output."""
import argparse
import csv
import json
import math
from pathlib import Path
import re


def ops(path, backend, run_record):
    # The pinned CSV printer omits `passed`. A clean executable exit AND
    # error-free supported rows are required; CSV alone cannot prove success.
    run = json.loads(Path(run_record).read_text())
    if (run.get("status") != "measured-not-qualified" or type(run.get("returncode")) is not int
            or run["returncode"] != 0 or run.get("error_category")):
        raise ValueError("numerical executable or its telemetry did not complete successfully")
    fields = ["backend_name", "op_name", "op_params", "test_mode", "supported", "error_message", "backend_reg_name"]
    with open(path, newline="") as source:
        reader = csv.DictReader(source, strict=True)
        if reader.fieldnames != fields:
            raise ValueError("numerical CSV does not match the pinned output contract")
        rows = list(reader)
    if any(set(r) != set(fields) or any(value is None for value in r.values()) for r in rows):
        raise ValueError("malformed numerical CSV row")
    if any(r["backend_name"] != backend or r["test_mode"] != "test" or r["supported"] not in ("0", "1") for r in rows):
        raise ValueError("unexpected backend, mode or support flag in numerical CSV")
    supported = [r for r in rows if r["supported"] == "1"]
    if not supported or any(r["error_message"].strip() for r in supported):
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
    parser.add_argument("--run-record")
    args = parser.parse_args()
    if args.mode == "ops" and (not args.backend or not args.run_record):
        parser.error("ops requires --backend and the executable's --run-record")
    print(json.dumps(ops(args.file, args.backend, args.run_record) if args.mode == "ops" else perplexity(args.file)))


if __name__ == "__main__":
    main()
