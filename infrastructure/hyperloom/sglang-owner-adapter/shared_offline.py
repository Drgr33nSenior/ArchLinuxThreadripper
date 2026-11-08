"""Post-release comparison layered on the existing complete owner replay.

The request carries the three retained seals and five authored package pins.
No partial run, unsealed extraction, or fixture is accepted by the entrypoint.
This consumes CPU only and confers no quality, adoption, or GPU authority.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

CONFIG = None


def need(value, reason):
    if not value:
        raise ValueError(reason)


def load(path, name, pin):
    raw = path.read_bytes()
    need(not path.is_symlink() and hashlib.sha256(raw).hexdigest() == pin, "offline helper pin")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compare(reader, entry, extraction):
    """Inputs here are the products of a successful full retained replay."""
    owner = entry.modules(); starts = []
    for arm in ("bf16", "kv4"):
        root = extraction / (arm + "-artifacts")
        result = reader.read(root / "result.json")
        receipt = result["shared_diagnostic"]
        identities = receipt["identities"]
        need(result["status"] == "HTTP_ARM_COMPLETE" and result["parent_error"] is None,
             "complete authenticated start")
        need(receipt["schema"] == 1 and receipt["trust_root"] == "parent-barrier-before-teardown"
             and receipt["diagnostic_success"] is False, "barrier trust root")
        raw = reader.held(extraction / (arm + "-http-client-tree") / "paired/RUN-SUMMARY.json",
                          receipt["summary_sha256"], 8 << 20)
        mounts = [None if i["start"] == 1 else dict(
            path=f"/mnt/shared-trace-raw-rank-{i['rank']}", fstype="tmpfs",
            capacity_bytes=32 << 20, verified=True) for i in identities]
        starts.append(owner.read_start(root / "shared-diagnostic", identities, entry.CONFIG["menu"],
            receipt["close_pins"], receipt["http_pin"], raw, receipt["summary_sha256"],
            raw_mounts=mounts, device_pins=receipt["device_pins"]))
    first, second = starts
    compared = owner.readout.compare_starts(first["records"], second["records"],
                                            first["http"], second["http"])
    return dict(schema=1, status="POST_RELEASE_DIAGNOSTIC_READOUT",
        cross_start=compared, rank_comparisons=second["rank_comparisons"],
        start_readout_sha256=[owner.core.sha(owner.core.canonical(s)) for s in starts],
        inventory=[dict(start=k + 1, ranks=[dict(rank=r["identity"]["rank"],
                       batches=len(r["batches"]), summary_sha256=r["summary_sha256"])
                       for r in s["records"]]) for k, s in enumerate(starts)],
        request_cost_model="COVERAGE_ONLY_NOT_INSTANTIATED",
        quality_grade=False, diagnostic_success=False, speedup_claim=False,
        execution_authorized=False, adoption_authorized=False)


def run(request_path, destination):
    need(CONFIG is not None, "materialized offline configuration required")
    quality = Path(CONFIG["quality_root"])
    reader = load(quality / "retained_replay.py", "shared_exact_retained_replay",
                  CONFIG["reader_sha256"])
    request = reader.read(request_path)
    need(set(request) == {"operation_root", "operation_seal_sha256", "workload_seal_sha256",
                         "protected_seal_sha256", "package_root", "expected_package_pins"},
         "exact post-release request")
    need(request["package_root"] == CONFIG["package_root"]
         and request["expected_package_pins"] == CONFIG["package_pins"], "frozen package identity")
    destination = Path(destination)
    need(destination.is_absolute() and destination.parent.resolve(strict=True) == destination.parent
         and not destination.exists(), "fresh canonical post-release destination")
    destination.mkdir(mode=0o700)
    extraction = destination / "replay"
    replay = reader.replay_retained(**request, destination=extraction,
                                    sealer_path=quality / "seal_evidence.py")
    need(replay["status"] == "RETAINED_TWO_ARM_OWNER_RESOURCE_RELEASE_HTTP_REPLAY_COMPLETE",
         "full owner source, wire and independent-release replay")
    review = Path(CONFIG["package_root"]) / "review"
    with reader.modules(review):
        entry = load(review / "shared_entry.py", "offline_bound_shared_entry", CONFIG["entry_sha256"])
        result = compare(reader, entry, extraction)
    result["owner_replay_sha256"] = reader.sha(reader.canonical(replay))
    result["owner_provenance"] = replay["provenance"]
    raw = reader.canonical(result)
    need(len(raw) <= 12 << 20, "bounded offline report")
    with (destination / "SHARED-READOUT.json").open("xb") as stream:
        stream.write(raw)
    return dict(status=result["status"], output_sha256=reader.sha(raw), bytes=len(raw),
                cross_start=result["cross_start"], execution_authorized=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    # This is a local CPU reader. No child, framework, network or device access.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 << 20, 128 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def guard(event, args):
        if event.startswith(("subprocess.", "socket.", "os.exec", "os.spawn")) or event in (
                "os.fork", "os.forkpty", "os.posix_spawn", "os.system"):
            raise RuntimeError("offline process/network refusal")
        if event == "import" and args[0].split(".")[0] in ("torch", "triton", "sglang"):
            raise RuntimeError("offline framework refusal")
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = args[0].decode() if isinstance(args[0], bytes) else args[0]
            if path == "/dev/kfd" or path.startswith("/dev/dri/"):
                raise RuntimeError("offline GPU refusal")
    sys.addaudithook(guard)
    print(json.dumps(run(args.request, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
