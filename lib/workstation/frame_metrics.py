"""Check decoded FFmpeg framemd5 evidence from a changing frame-counter test.

This tests distinct decoded content, not compositor configuration alone. It does
not prove capture/encoding attribution; retain the matching session/client logs.
"""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import re

from measurement import sha256


def analyze(path, requested):
    if not 30 <= requested <= 240:
        raise ValueError("requested refresh must be 30..240 Hz")
    timebase = None
    frames = []
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"#tb 0: ([0-9]+/[1-9][0-9]*)", line)
        if match:
            timebase = Fraction(match[1])
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 6 or fields[0] != "0" or not re.fullmatch(r"[a-f0-9]{32}", fields[5]):
            raise ValueError("require a single video-stream framemd5 file")
        frames.append((int(fields[2]), fields[5]))
    if not timebase or len(frames) < 2 or any(b[0] <= a[0] for a, b in zip(frames, frames[1:])):
        raise ValueError("missing timebase, frames or monotonic presentation timestamps")
    duration = float((frames[-1][0] - frames[0][0]) * timebase)
    if duration < 5:
        raise ValueError("capture at least five seconds of the changing frame-counter test")
    distinct = len({frame[1] for frame in frames})
    rate = (distinct - 1) / duration
    return {"schema": 1, "requested_hz": requested, "distinct_frames": distinct,
            "decoded_frames": len(frames), "duration_seconds": duration,
            "distinct_frames_per_second": rate, "framemd5_sha256": sha256(path),
            "status": "frame-cadence-passed-not-hardware-qualified" if rate >= requested * .95 else "failed",
            "scope": "changing frame-counter capture only; ordinary static game scenes are not a valid input"}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("framemd5")
    parser.add_argument("requested_hz", type=int)
    parser.add_argument("output")
    args = parser.parse_args()
    result = analyze(args.framemd5, args.requested_hz)
    with open(args.output, "x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    raise SystemExit(1 if result["status"] == "failed" else 0)


if __name__ == "__main__":
    main()
