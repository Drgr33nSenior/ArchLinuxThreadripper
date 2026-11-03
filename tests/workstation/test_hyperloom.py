"""Offline identity, substitution and execution-boundary tests; no GPU/API use."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import hyperloom as integration


class HyperloomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def wheel(self, directory, version="1.1.5"):
        path = directory / "hyperloom.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hyperloom.dist-info/METADATA", f"Name: hyperloom-inference-optimizer\nVersion: {version}\n")
            archive.writestr("hyperloom/example.py", "original = True\n")
        return path

    def test_release_and_runtime_are_bound_without_serving_dependencies(self):
        contract = integration.read_contract(ROOT)
        pins = integration.requirements(ROOT / integration.DEPENDENCIES)
        self.assertEqual(len(pins), 47)
        self.assertEqual(pins["hyperloom-inference-optimizer"],
                         (contract["HYPERLOOM_VERSION"], contract["HYPERLOOM_WHEEL_SHA256"]))
        self.assertFalse({"torch", "triton", "sglang", "vllm", "ray"} & pins.keys())

    def test_wheel_hash_missing_extra_and_symlink_refuse(self):
        folder = self.work / "wheels"
        folder.mkdir()
        wheel = self.wheel(folder)
        pins = {"hyperloom-inference-optimizer": ("1.1.5", integration.digest(wheel))}
        self.assertEqual(integration.verify_wheels(folder, pins), wheel.stat().st_size)
        with wheel.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(ValueError):
            integration.verify_wheels(folder, pins)
        wheel.unlink()
        wheel = self.wheel(folder)
        (folder / "unexpected.txt").write_text("not an approved wheel")
        with self.assertRaises(ValueError):
            integration.verify_wheels(folder, pins)
        (folder / "unexpected.txt").unlink()
        wheel.unlink()
        with self.assertRaises(ValueError):
            integration.verify_wheels(folder, pins)
        wheel.symlink_to(self.work / "elsewhere")
        with self.assertRaises(ValueError):
            integration.verify_wheels(folder, pins)

    def test_source_substitution_is_rejected_before_offline_execution(self):
        folder = self.work / "wheels"
        folder.mkdir()
        wheel = self.wheel(folder)
        prefix = self.work / "installed"
        prefix.mkdir()
        (prefix / "release.whl").write_bytes(wheel.read_bytes())
        contract = {"HYPERLOOM_WHEEL_SHA256": integration.digest(wheel)}
        (prefix / "INSTALLATION.json").write_text(json.dumps({"status": "installed", "contract": contract}))
        python = prefix / "venv/bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text("fixture interpreter; must never execute\n")
        source = prefix / "venv/lib/python3.14/site-packages/hyperloom/example.py"
        source.parent.mkdir(parents=True)
        source.write_text("original = True\n")
        self.assertEqual(integration.installed_python(prefix, contract), python)
        source.write_text("original = False\n")
        with self.assertRaises(ValueError):
            integration.installed_python(prefix, contract)

    def test_private_environment_does_not_inherit_credentials_or_user_home(self):
        env = integration.private_environment(self.work)
        self.assertEqual(set(env), {"HOME", "PATH", "LANG", "PYTHONDONTWRITEBYTECODE",
                                    "PIP_DISABLE_PIP_VERSION_CHECK", "HYPERLOOM_REPORT_LLM_BACKEND"})
        self.assertEqual(env["HYPERLOOM_REPORT_LLM_BACKEND"], "none")
        self.assertEqual(Path(env["HOME"]).stat().st_mode & 0o777, 0o700)

    def test_offline_commands_cannot_connect_spawn_or_open_gpu(self):
        guard = integration.OFFLINE.split('if sys.argv[1] == "status":')[0]
        check = '''
import socket
blocked = 0
expected = OSError if sys.argv[1:2] == ["trace-analysis"] else RuntimeError
for operation in (lambda: socket.create_connection(("127.0.0.1",9)),
                  lambda: __import__("subprocess").run(["/usr/bin/true"]),
                  lambda: open("/dev/kfd")):
    try: operation()
    except expected: blocked += 1
assert blocked == 3
print("offline execution boundaries passed")
'''
        # Trace analysis refuses with PermissionError so upstream best-effort probes degrade.
        for argv in ([], ["trace-analysis"]):
            result = subprocess.run([sys.executable, "-I", "-B", "-c", guard + check, *argv],
                                    capture_output=True, text=True, timeout=15, check=True)
            self.assertIn("boundaries passed", result.stdout)

    def test_source_paths_reach_public_resolver_without_changing_installed_files(self):
        package = self.work / "packages"
        modules = {
            "TraceLens/__init__.py": "",
            "TraceLens/TraceUtils/__init__.py": "",
            "TraceLens/TraceUtils/kernel_source.py":
                'def resolve_kernel_source(name, *, search_paths=None): return search_paths\n',
            "hyperloom/__init__.py": "",
            "hyperloom/orchestrator/__init__.py": "",
            "hyperloom/orchestrator/trace_analysis/__init__.py": "",
            "hyperloom/orchestrator/trace_analysis/bypass_trace_analysis.py":
                'from TraceLens.TraceUtils.kernel_source import resolve_kernel_source\n'
                'import json\nprint(json.dumps(resolve_kernel_source("sentinel")))\n',
        }
        for name, content in modules.items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        paths = [str(self.work / "source with spaces"), str(self.work / "second")]
        code = "import sys; sys.path.insert(0, " + repr(str(package)) + ")\n" + integration.OFFLINE
        env = integration.private_environment(self.work)
        env["WORKSTATION_TRACELENS_SOURCE_PATHS"] = json.dumps(paths)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, "trace-analysis"],
                                env=env, capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(json.loads(result.stdout), paths)
        for name, content in modules.items():
            self.assertEqual((package / name).read_text(), content)

    def test_analysis_keeps_home_and_caches_in_new_output_and_refuses_missing_source(self):
        prefix, output = self.work / "installed", self.work / "analysis"
        trace = self.work / "trace.json"
        trace.write_text("{}")
        argv = ["hyperloom.py", "--root", str(ROOT), "trace-analysis", str(prefix), str(trace), str(output),
                "--source-path", str(self.work), "--source-path", str(self.work)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(integration, "installed_python", return_value=Path(sys.executable)), \
             mock.patch.object(integration, "tracelens_root", return_value=self.work), \
             mock.patch.object(integration.subprocess, "run") as run:
            self.assertEqual(integration.main(), 0)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["HOME"], str(output / "private-home"))
            self.assertEqual(json.loads(env["WORKSTATION_TRACELENS_SOURCE_PATHS"]), [str(self.work.resolve())])
            self.assertFalse(prefix.exists())
            argv[6] = str(self.work / "refused-output")
            argv[8] = str(self.work / "absent")
            run.reset_mock()
            self.assertEqual(integration.main(), 1)
            run.assert_not_called()
            self.assertFalse((self.work / "refused-output").exists())

    def test_tracelens_is_commit_pinned_and_excludes_unused_requirements(self):
        contract = integration.read_contract(ROOT)
        tracelens = integration.read_contract(ROOT, "TRACELENS")
        self.assertEqual(tracelens["TRACELENS_COMMIT"], "614c597bc0c66cabf6a15152037324acc7386090")
        self.assertEqual(tracelens["TRACELENS_ARCHIVE_URL"],
                         "https://codeload.github.com/AMD-AGI/TraceLens/tar.gz/" + tracelens["TRACELENS_COMMIT"])
        pins = integration.requirements(ROOT / integration.TRACELENS_DEPENDENCIES)
        self.assertEqual(pins["hyperloom-inference-optimizer"],
                         (contract["HYPERLOOM_VERSION"], contract["HYPERLOOM_WHEEL_SHA256"]))
        self.assertEqual(set(pins), {"hyperloom-inference-optimizer", "pandas", "numpy", "python-dateutil", "six",
                                     "tzdata", "tqdm", "openpyxl", "et-xmlfile", "itanium-demangler"})
        self.assertNotIn("[runtime]", (ROOT / integration.TRACELENS_DEPENDENCIES).read_text())
        self.assertFalse({"office365-rest-python-client", "msal", "matplotlib", "torch", "sglang"} & pins.keys())

    def tracelens_archive(self, commit, extra=None):
        source = self.work / "source"
        top = source / ("TraceLens-" + commit)
        (top / "TraceLens/TraceUtils").mkdir(parents=True)
        (top / "TraceLens/__init__.py").write_text("")
        (top / "TraceLens/TraceUtils/kernel_source.py").write_text("original = True\n")
        (top / "tests").mkdir()
        (top / "tests/fixture.json").write_text("{}")
        path = self.work / "tracelens.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            archive.add(top, arcname=top.name)
            if extra:
                archive.add(top / "tests/fixture.json", arcname=extra)
        return path

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "runtime requires CPython 3.14 tarfile filters")
    def test_tracelens_source_substitution_and_planted_modules_are_rejected(self):
        commit = "0" * 40
        archive = self.tracelens_archive(commit)
        contract = {"TRACELENS_COMMIT": commit, "TRACELENS_ARCHIVE_SHA256": integration.digest(archive)}
        prefix = self.work / "installed"
        (prefix / integration.SITE_PACKAGES).mkdir(parents=True)
        root = integration.unpack_tracelens(archive, prefix, contract)
        self.addCleanup(lambda: [os.chmod(d, 0o700) for d, _, _ in os.walk(root)])
        self.assertEqual(integration.tracelens_root(prefix, contract), root)
        self.assertEqual((prefix / integration.SITE_PACKAGES / integration.TRACELENS_PATH_FILE).read_text(), f"{root}\n")
        with self.assertRaises(PermissionError):
            (root / "TraceLens/__pycache__").mkdir()
        module = root / "TraceLens/TraceUtils/kernel_source.py"
        module.write_text("original = False\n")
        with self.assertRaises(ValueError):
            integration.tracelens_root(prefix, contract)
        module.write_text("original = True\n")
        os.chmod(root / "TraceLens", 0o700)
        (root / "TraceLens/planted.py").write_text("")
        with self.assertRaises(ValueError):
            integration.tracelens_root(prefix, contract)

    @unittest.skipUnless(hasattr(tarfile, "data_filter"), "runtime requires CPython 3.14 tarfile filters")
    def test_tracelens_archive_outside_commit_directory_is_rejected(self):
        commit = "1" * 40
        archive = self.tracelens_archive(commit, extra="elsewhere.json")
        contract = {"TRACELENS_COMMIT": commit, "TRACELENS_ARCHIVE_SHA256": integration.digest(archive)}
        prefix = self.work / "installed"
        (prefix / integration.SITE_PACKAGES).mkdir(parents=True)
        with self.assertRaises(ValueError):
            integration.unpack_tracelens(archive, prefix, contract)
        self.assertFalse((prefix / "TraceLens").exists())



if __name__ == "__main__":
    unittest.main()
