"""Target-only numerical and RCCL checks. No downloads or system mutations."""

import argparse
import datetime
import json
import os
import tempfile
import time

import torch
import torch.distributed as dist
import torch.multiprocessing as mp


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
    parser.add_argument("--collective", action="store_true")
    args = parser.parse_args()
    if not torch.version.hip or not torch.cuda.is_available():
        raise RuntimeError("This interpreter is not a working ROCm PyTorch environment")
    if torch.cuda.device_count() != args.count:
        raise RuntimeError("PyTorch must expose every expected GPU")
    names = [torch.cuda.get_device_name(i) for i in range(args.count)]
    if any(args.model not in name for name in names):
        raise RuntimeError(f"Unexpected GPU model: {names}")
    report = {"torch": torch.__version__, "hip": torch.version.hip, "devices": names, "tests": []}
    if args.collective:
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
