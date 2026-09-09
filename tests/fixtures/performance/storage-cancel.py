"""Harmless storage-run integration fixture: no fio, real disks or large files."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

root = Path(sys.argv[2])
if sys.argv[1] == "descendant":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "descendant-ready").write_text(str(os.getpid()))
    time.sleep(60)
elif sys.argv[1] == "workload":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, __file__, "descendant", str(root)])
    deadline = time.monotonic() + 10
    while not (root / "descendant-ready").exists():
        if time.monotonic() > deadline:
            child.kill()
            child.wait()
            raise SystemExit(2)
        time.sleep(.02)
    (root / "workload-ready").write_text(json.dumps([os.getpid(), child.pid]))
    child.wait()
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib/workstation"))
    import storage_benchmark

    def metadata(args, **_):
        if args[0] == "findmnt":
            return json.dumps({"filesystems": [{"fstype": "xfs", "source": "/dev/fixture"}]})
        if args[0] == "lsblk":
            return json.dumps({"blockdevices": [{"type": "crypt", "children": [{"type": "raid0"}]}]})
        if args == ["fio", "--version"]:
            return "fixture-no-fio"
        raise AssertionError("unexpected metadata command")

    command = [sys.executable, __file__, "workload", str(root)]
    if sys.argv[1] == "exit7":
        command = [sys.executable, "-c", "raise SystemExit(7)"]
    with patch.object(storage_benchmark, "validate_directory", return_value=(root, 16)), \
            patch.object(storage_benchmark.subprocess, "check_output", side_effect=metadata), \
            patch.object(storage_benchmark.os, "posix_fallocate", create=True, side_effect=lambda fd, _offset, size: os.ftruncate(fd, size)), \
            patch.object(storage_benchmark, "geometry", return_value={}), \
            patch.object(storage_benchmark, "snapshot", return_value={}), \
            patch.object(storage_benchmark, "fio_args", return_value=command):
        storage_benchmark.run(root, root / "result", 1)
