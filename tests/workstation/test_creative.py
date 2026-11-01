"""Offline creative staging and launch contract; no native GPU claims."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("creative_prepare", ROOT / "infrastructure/creative/prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class CreativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.swarm = self.root / "swarm"
        self.comfy = self.root / "comfy"
        self.nuget = self.root / "nuget"
        self.wheels = self.root / "wheels"
        for path in (self.swarm, self.comfy, self.nuget, self.wheels):
            path.mkdir()
        (self.wheels / "runtime-debs").mkdir()
        (self.wheels / "runtime-debs/fixture.deb").write_bytes(b"staging-only fixture")
        (self.wheels / "runtime-debs/SHA256SUMS").write_text("staging-only fixture\n")
        self.backend = self.swarm / "src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs"
        self.backend.parent.mkdir(parents=True)
        self.backend.write_text('Process p = DoPythonCall($"-s -m pip {call}");')
        self.deps = self.swarm / "src/SwarmUI.deps.props"
        self.deps.write_text('<PackageReference Include="SixLabors.ImageSharp" Version="3.1.12" />')
        (self.nuget / "packages.lock.json").write_text("{}")
        self.requirements = self.root / "requirements.lock"
        self.requirements.write_text("fixture==1.0 --hash=sha256:" + "0" * 64 + "\n")
        self.destination = self.root / "context"

    def stage(self):
        lock = json.loads((ROOT / "infrastructure/creative/runtime.json").read_text())
        with patch.object(prepare.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, lock[name]["commit"] + "\n") for name in ("swarm", "comfy")
        ]), patch.object(prepare.subprocess, "check_output", return_value=b""):
            prepare.prepare(self.swarm, self.comfy, self.nuget, self.wheels, self.requirements, self.destination)

    def test_staging_keeps_source_and_applies_bounded_patches(self):
        before = self.backend.read_bytes(), self.deps.read_bytes()
        self.stage()
        backend = self.destination / "SwarmUI/src/BuiltinExtensions/ComfyUIBackend/ComfyUISelfStartBackend.cs"
        self.assertIn("runtime pip is prohibited", backend.read_text())
        self.assertEqual(before[1], (self.destination / "SwarmUI/src/SwarmUI.deps.props").read_bytes())
        self.assertEqual(before, (self.backend.read_bytes(), self.deps.read_bytes()))
        manifest = json.loads((self.destination / "staged-sha256.json").read_text())
        self.assertIn("verify_runtime.py", manifest)
        with self.assertRaises(FileExistsError):
            self.stage()

    def test_changed_runtime_install_anchor_is_rejected(self):
        self.backend.write_text("unexpected upstream implementation")
        with self.assertRaisesRegex(ValueError, "offline-installer guard anchor"):
            self.stage()

    def test_missing_native_closure_is_rejected_before_staging(self):
        (self.wheels / "runtime-debs/SHA256SUMS").unlink()
        with self.assertRaisesRegex(ValueError, "Ubuntu runtime-debs closure"):
            self.stage()
        self.assertFalse(self.destination.exists())

    def test_restarting_with_same_data_uses_fresh_private_runtime(self):
        swarm = self.root / "SwarmUI"
        binary = swarm / "src/bin/live_release/SwarmUI"
        binary.parent.mkdir(parents=True)
        receipt = self.root / "receipts"
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$XDG_RUNTIME_DIR" >> "$CREATIVE_TEST_RECEIPT"\n')
        binary.chmod(0o700)
        launch = self.root / "launch.sh"
        # Only relocate fixed application/output paths; execute real mktemp twice.
        launch.write_text((ROOT / "infrastructure/creative/launch.sh").read_text()
                          .replace("/SwarmUI/", str(swarm) + "/")
                          .replace("cd /SwarmUI\n", f"cd {swarm}\n")
                          .replace("/tmp/comfy-temp", str(self.root / "tmp")))
        for _ in range(2):
            subprocess.run(["bash", str(launch)], check=True,
                           env={**os.environ, "CREATIVE_TEST_RECEIPT": str(receipt)})
        directories = [Path(name) for name in receipt.read_text().splitlines()]
        self.assertEqual(2, len(set(directories)))
        for directory in directories:
            self.addCleanup(directory.rmdir)
            self.assertEqual(os.getuid(), directory.stat().st_uid)
            self.assertEqual(0o700, directory.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
