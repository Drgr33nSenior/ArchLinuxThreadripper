"""Explicit inert owner fixture; no live reader bypass or device imports."""
from pathlib import Path
import time
import stage_probe
import support
import test_support

def run(root):
    artifacts=support.Artifacts(Path(root),time.monotonic()+30)
    value=test_support.payload(artifacts,stage_probe.source_identity())
    artifacts.write(support.REPORT,support.canonical(value))
    return value,None
