"""Target-only numerical and RCCL checks. No downloads or system mutations."""

import argparse
import datetime
import json
import math
import os
import statistics
import tempfile
import time

import torch
import torch.distributed as dist
import torch.multiprocessing as mp


def matmul_benchmark(count):
    """Fixed synthetic baseline; transfers and reference checks are not timed."""
    records = []
    torch.manual_seed(42)
    for index in range(count):
        torch.cuda.set_device(index)
        for dimension in (1024, 2048):
            a, b = torch.randn(dimension, dimension), torch.randn(dimension, dimension)
            for dtype, tolerance in ((torch.float32, 1e-3), (torch.float16, 0.03), (torch.bfloat16, 0.3)):
                if dtype == torch.bfloat16 and not torch.cuda.is_bf16_supported(including_emulation=False):
                    raise RuntimeError("Benchmark requires native BF16 support")
                left, right = a.to(dtype), b.to(dtype)
                reference = left.float() @ right.float()
                left, right = left.to(f"cuda:{index}"), right.to(f"cuda:{index}")
                # Reuse the output allocation. No download or cache tuning.
                actual = torch.empty_like(left)
                elapsed = []
                for repeat in range(12):
                    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                    start.record()
                    torch.mm(left, right, out=actual)
                    end.record()
                    end.synchronize()
                    seconds = start.elapsed_time(end) / 1000
                    if not math.isfinite(seconds) or seconds <= 0:
                        raise RuntimeError("Invalid GPU event duration")
                    # Validate every warmup and measured result outside the event interval.
                    torch.testing.assert_close(actual.float().cpu(), reference, rtol=tolerance, atol=tolerance)
                    if repeat >= 2:
                        elapsed.append(seconds)
                records.append({"device": index, "dtype": str(dtype), "dimension": dimension,
                                "seconds": elapsed, "median_seconds": statistics.median(elapsed),
                                "warmups": 2, "correctness": "passed"})
    return records


def collective(rank, count, rendezvous):
    torch.cuda.set_device(rank)
    dist.init_process_group(
        "nccl", init_method=f"file://{rendezvous}", rank=rank,
        world_size=count, timeout=datetime.timedelta(seconds=90),
    )
    try:
        records = []
        for elements in (1, 256, 4096, 262144, 4194304, 16777216):
            value = torch.empty(elements, device=f"cuda:{rank}")
            elapsed = []
            for repeat in range(12):
                value.fill_(rank + 1.0)
                torch.cuda.synchronize()
                dist.barrier()
                start = time.perf_counter()
                dist.all_reduce(value)
                torch.cuda.synchronize()
                seconds = time.perf_counter() - start
                torch.testing.assert_close(value.cpu(), torch.full((elements,), count * (count + 1) / 2))
                if repeat >= 2:
                    elapsed.append(seconds)
            records.append({"bytes": elements * 4, "seconds": elapsed,
                            "algorithm_bytes_per_second": [elements * 4 / t for t in elapsed],
                            "correctness": "passed", "warmups": 2})
        with open(f"{rendezvous}.rank{rank}.json", "w") as output:
            json.dump({"rank": rank, "samples": records}, output)
    finally:
        dist.destroy_process_group()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--model", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collective", action="store_true")
    mode.add_argument("--matmul-benchmark", action="store_true")
    args = parser.parse_args()
    if not torch.version.hip or not torch.cuda.is_available():
        raise RuntimeError("This interpreter is not a working ROCm PyTorch environment")
    if torch.cuda.device_count() != args.count:
        raise RuntimeError("PyTorch must expose every expected GPU")
    names = [torch.cuda.get_device_name(i) for i in range(args.count)]
    if any(args.model not in name for name in names):
        raise RuntimeError(f"Unexpected GPU model: {names}")
    report = {"torch": torch.__version__, "hip": torch.version.hip, "devices": names, "tests": []}
    if args.matmul_benchmark:
        report["matmul"] = matmul_benchmark(args.count)
        report["scope"] = "synthetic per-GPU matmul baseline; not inference, peer-transfer or performance qualification"
        report["timing"] = "GPU events; excludes transfers, allocation and reference checks"
        report["seed"] = 42
    elif args.collective:
        if not dist.is_nccl_available():
            raise RuntimeError("The ROCm build lacks its RCCL (NCCL API) backend")
        with tempfile.TemporaryDirectory(prefix="workstation-rccl-") as directory:
            mp.spawn(collective, args=(args.count, os.path.join(directory, "rendezvous")),
                     nprocs=args.count, join=True)
            report["collectives"] = []
            for rank in range(args.count):
                with open(os.path.join(directory, f"rendezvous.rank{rank}.json")) as source:
                    report["collectives"].append(json.load(source))
        report["transport"] = "NOT QUALIFIED: inspect synchronized NCCL_DEBUG=INFO INIT,GRAPH,P2P,SHM,NET evidence"
        report["tests"].append({"name": "RCCL-all-reduce", "status": "passed"})
    else:
        torch.manual_seed(42)
        # Reference uses the same quantized inputs, avoiding a false failure
        # caused solely by FP16/BF16 input rounding.
        a, b = torch.randn(128, 128), torch.randn(128, 128)
        for index in range(args.count):
            torch.cuda.set_device(index)
            for dtype, tolerance in ((torch.float32, 1e-3), (torch.float16, 0.03), (torch.bfloat16, 0.3)):
                if dtype == torch.bfloat16 and not torch.cuda.is_bf16_supported(including_emulation=False):
                    report["tests"].append({"device": index, "dtype": str(dtype), "status": "unsupported"})
                    continue
                left, right = a.to(dtype), b.to(dtype)
                reference = left.float() @ right.float()
                actual = (left.to(f"cuda:{index}") @ right.to(f"cuda:{index}")).float().cpu()
                torch.cuda.synchronize()
                torch.testing.assert_close(actual, reference, rtol=tolerance, atol=tolerance)
                report["tests"].append({"device": index, "dtype": str(dtype), "status": "passed"})
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
