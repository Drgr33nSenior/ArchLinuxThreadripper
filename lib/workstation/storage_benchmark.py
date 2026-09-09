"""Bounded encrypted-filesystem measurement, never raw-device writes.

Only this command's new scratch file is writable. It is retained for explicit
owner cleanup. No cache drop, dm reload, filesystem change or TRIM is performed.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

from measurement import run_command, snapshot


def validate_directory(directory, size_gib):
    directory = Path(directory)
    if not directory.is_absolute() or directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("select an absolute canonical scratch directory, not a symlink")
    metadata = directory.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise ValueError("scratch directory must be owned by the caller and not writable by other users")
    if not 1 <= size_gib <= 16:
        raise ValueError("scratch size must be 1..16 GiB")
    size = size_gib * 1024 ** 3
    if size > shutil.disk_usage(directory).free // 4:
        raise ValueError("scratch size exceeds 25 percent of available filesystem space")
    return directory, size


def geometry():
    block = {}
    for device in sorted(Path("/sys/block").glob("*")):
        values = {}
        for name in ("queue/scheduler", "queue/read_ahead_kb", "queue/logical_block_size",
                     "queue/physical_block_size", "queue/minimum_io_size", "queue/optimal_io_size",
                     "alignment_offset", "md/level", "md/chunk_size", "md/raid_disks", "dm/name", "dm/uuid"):
            try:
                values[name] = (device / name).read_text().strip()
            except OSError:
                values[name] = None
        block[device.name] = values
    return block


def fio_args(file, size, mode, seconds=30):
    common = ["fio", "--name=workstation-scratch", f"--filename={file}", f"--size={size}",
              "--allow_file_create=0", "--output-format=json", "--group_reporting=1",
              "--eta=never", "--ioengine=libaio", "--thread=1", "--randrepeat=1", "--percentile_list=50:95:99:99.9"]
    if mode == "prepare":
        return common + ["--rw=write", "--bs=1m", "--direct=1", "--iodepth=4", "--end_fsync=1"]
    if mode == "qd1":
        return common + ["--readonly", "--rw=randread", "--bs=4k", "--direct=1", "--iodepth=1",
                         "--time_based=1", f"--runtime={seconds}", "--ramp_time=5"]
    if mode == "parallel":
        return common + ["--readonly", "--rw=read", "--bs=1m", "--direct=1", "--iodepth=16",
                         "--numjobs=2", "--time_based=1", f"--runtime={seconds}", "--ramp_time=5"]
    if mode in ("advisory-evicted-load", "warm-load"):
        return common + ["--readonly", "--rw=read", "--bs=1m", "--direct=0", "--iodepth=1",
                         "--ioengine=sync", "--invalidate=" + ("1" if mode == "advisory-evicted-load" else "0")]
    raise ValueError("unsupported scratch workload")


def validate_result(result):
    jobs = result.get("jobs")
    if not jobs or any(j.get("error") != 0 for j in jobs):
        raise ValueError("fio reported failures or no workload")
    if any(j.get("read", {}).get("io_bytes", 0) + j.get("write", {}).get("io_bytes", 0) <= 0 for j in jobs):
        raise ValueError("fio performed no measurable I/O")


def run(directory, output, size_gib):
    directory, size = validate_directory(directory, size_gib)
    output = Path(output)
    output.mkdir(mode=0o700)
    mount = json.loads(subprocess.check_output(["findmnt", "-J", "-T", str(directory), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"], text=True))
    if len(mount["filesystems"]) != 1 or mount["filesystems"][0]["fstype"] != "xfs":
        raise ValueError("select the installed XFS filesystem; no alternate layout will be created")
    source = mount["filesystems"][0]["source"]
    if not source.startswith("/dev/"):
        raise ValueError("cannot resolve the encrypted block stack")
    topology = json.loads(subprocess.check_output(["lsblk", "-s", "-J", "-b", "-o", "NAME,TYPE,FSTYPE,SIZE", source], text=True))
    def types(nodes):
        return [item for node in nodes for item in [node["type"], *types(node.get("children", []))]]
    stack = types(topology["blockdevices"])
    if "crypt" not in stack or "raid0" not in stack:
        raise ValueError("scratch path is not on the existing dm-crypt/RAID0 stack")
    file = directory / "workstation-fio.bin"
    fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.posix_fallocate(fd, 0, size)
    finally:
        os.close(fd)
    report = {"schema": 1, "status": "incomplete", "scratch_file": str(file), "scratch_bytes": size,
              "mount": mount, "topology": topology, "geometry": geometry(), "fio": subprocess.check_output(["fio", "--version"], text=True).strip(),
              "scope": "scratch-file filesystem I/O, not model deserialization/JIT or Argon2id unlock",
              "cache_state": "direct I/O cases bypass page cache; buffered eviction is advisory, not proven cold storage",
              "cleanup": "retained scratch file; remove this exact named file manually after reviewing results", "runs": []}
    try:
        for repetition in range(3):
            modes = ["qd1", "parallel", "advisory-evicted-load", "warm-load"]
            if repetition == 0:
                modes.insert(0, "prepare")
            for mode in modes:
                child = output / f"{repetition}-{mode}"
                args = fio_args(file, size, mode)
                if run_command(args, child, 300) != 0:
                    raise ValueError("fio failed; retained private command evidence")
                value = json.loads((child / "stdout.txt").read_text())
                validate_result(value)
                report["runs"].append({"repetition": repetition, "mode": mode, "arguments": args, "fio": value})
        report["status"] = "measured-not-qualified"
    finally:
        report["after"] = snapshot()
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scratch_directory")
    parser.add_argument("output")
    parser.add_argument("size_gib", type=int)
    args = parser.parse_args()
    run(args.scratch_directory, args.output, args.size_gib)


if __name__ == "__main__":
    main()
