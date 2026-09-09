"""Real file/shared-library identity tests, with non-Arch host probes mocked."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("llama_runtime", ROOT / "lib/workstation/llama_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def test_dependency_output(self):
        with patch.object(runtime, "command", return_value="linux-vdso.so.1 (0x123)\n libggml.so => /build/libggml.so (0x123)\n /lib64/ld-linux.so.2 (0x123)\n"):
            self.assertEqual(runtime.dependencies(Path("unused")), [Path("/build/libggml.so"), Path("/lib64/ld-linux.so.2")])
        for bad in ("", "libggml.so => not found\n", "not a dynamic executable\n"):
            with patch.object(runtime, "command", return_value=bad), self.assertRaises(ValueError):
                runtime.dependencies(Path("unused"))

    def test_loader_overrides(self):
        for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "VK_DRIVER_FILES", "GGML_BACKEND_PATH", "LLAMA_ARG_SPLIT_MODE"):
            with patch.dict(os.environ, {key: ""}, clear=True), self.assertRaises(ValueError):
                runtime.loader_environment()

    @unittest.skipUnless(shutil.which("cc"), "C compiler unavailable: real shared-library replacement NOT RUN")
    def test_changed_library_unchanged_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bins = root / "build/bin"
            bins.mkdir(parents=True)
            library = bins / "libfixture.so.1"
            link = bins / "libfixture.so"
            link.symlink_to(library.name)
            source = root / "value.c"
            main = root / "main.c"
            main.write_text('#include <stdio.h>\nint value(void); int main(void) { printf("%d\\n", value()); }\n')
            source.write_text("int value(void) { return 1; }\n")
            flags = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
            subprocess.run(["cc", *flags, str(source), "-o", str(library)], check=True)
            executable = bins / "llama-bench"
            subprocess.run(["cc", str(main), str(library), "-Wl,-rpath," + str(bins), "-o", str(executable)], check=True)
            for name in runtime.EXECUTABLES:
                if name != executable.name:
                    shutil.copy2(executable, bins / name)
            before_hash = runtime.digest(executable)
            self.assertEqual(subprocess.check_output([str(executable)]), b"1\n")
            # These tests exercise real hashing/resolution, but do not pretend
            # that Mac otool is Linux ldd or that pacman/amdgpu is installed.
            clean_env = {"TMPDIR": str(root), "PATH": os.environ.get("PATH", os.defpath)}
            with patch.object(runtime, "dependencies", return_value=[]), patch.object(runtime, "command", return_value="fixture host"), patch.object(runtime, "icd_paths", return_value=[]), patch.dict(os.environ, clean_env, clear=True):
                before = runtime.collect(root)
                manifest = root / "runtime.json"
                manifest.write_text(json.dumps(before))
                runtime.verify(manifest, runtime.collect(root))
                source.write_text("int value(void) { return 2; }\n")
                subprocess.run(["/usr/bin/cc", *flags, str(source), "-o", str(library)], check=True)
                self.assertEqual(subprocess.check_output([str(executable)]), b"2\n")
                self.assertEqual(runtime.digest(executable), before_hash)
                with self.assertRaises(ValueError):
                    runtime.verify(manifest, runtime.collect(root))
                # A retargeted link is a change even when bytes are identical.
                manifest.write_text(json.dumps(runtime.collect(root)))
                other = bins / "libfixture.so.2"
                shutil.copy2(library, other)
                link.unlink()
                link.symlink_to(other.name)
                with self.assertRaises(ValueError):
                    runtime.verify(manifest, runtime.collect(root))


if __name__ == "__main__":
    unittest.main()
