"""Source identity and real Hyperloom specialist-input contracts; no GPU/API."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import hyperloom_evidence as evidence


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input"
        self.source.mkdir()
        self.selected = self.source / "selection.json"
        self.selected.write_text('{"candidate":"current-C4"}\n')
        self.trace = self.source / "trace.json"
        self.trace.write_text('{"traceEvents":[{"name":"real-retained-fixture","ph":"X","dur":17}]}\n')
        self.review = self.source / "review.md"
        self.review.write_text("Fixture: no improvement was measured.\n")
        seal = "".join(sha(path.read_bytes()) + "  " + path.name + "\n"
                       for path in (self.selected, self.trace, self.review))
        (self.source / "FINAL-ARTIFACTS.sha256").write_text(seal)
        seal_ref = {"root": str(self.source), "sha256": sha(seal.encode())}
        self.spec = {
            "schema": 1, "objective": "Balanced C1/C2 latency with no regressions",
            "campaign_seconds": 14400, "constraints": ["Fixed selected precision and context", "No adoption"],
            "selected": "selection",
            "artifacts": [
                {"id": key, "kind": kind, "path": str(path), "sha256": sha(path.read_bytes()),
                 "bytes": path.stat().st_size, "description": description, "sealed_by": seal_ref}
                for key, kind, path, description in (
                    ("selection", "selection", self.selected, "Selected baseline"),
                    ("trace", "trace", self.trace, "Historical fixture; never new timing"),
                    ("review", "review", self.review, "Negative result"))
            ],
            "hypotheses": [{"id": "index32", "mechanism": "Less address live state",
                           "falsifier": "No reduction in exposed waits", "risk": "Overflow",
                           "evidence": ["trace", "review"]}],
            "history": [{"experiment": "fixture", "outcome": "resource gate refused",
                         "limits": "correctness and timing did not run", "evidence": "review"}],
        }
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "bundle"

    def write_spec(self):
        self.manifest.write_text(json.dumps(self.spec))
        return sha(self.manifest.read_bytes())

    def make_bundle(self):
        return evidence.bundle(self.manifest, self.write_spec(), self.output)

    def test_portable_bundle_retains_exact_trace_and_real_specialist_flags(self):
        result = self.make_bundle()
        index = json.loads((self.output / "EVIDENCE-INDEX.json").read_text())
        trace = next(row for row in index["artifacts"] if row["id"] == "trace")
        raw = (self.output / trace["path"]).read_bytes()
        self.assertEqual(gzip.decompress(raw), self.trace.read_bytes())
        self.assertEqual(trace["sha256"], sha(raw))
        self.assertEqual(trace["original_sha256"], sha(self.trace.read_bytes()))
        self.assertFalse(index["new_gpu_measurements"])
        self.assertFalse(index["adoption"])
        self.assertEqual(index["history"][0]["limits"], "correctness and timing did not run")
        args = json.loads((self.output / "HYPERLOOM-ARGUMENTS.json").read_text())
        self.assertEqual(args[::2], ["--custom-specialist-description", "--custom-specialist-prompt-file"])
        prompt = Path(args[-1]).read_text()
        self.assertIn(str(self.output / "EVIDENCE-INDEX.json"), prompt)
        self.assertIn("Historical traces and trials are priors", prompt)
        self.assertLessEqual(len(prompt.encode()), 16384)
        self.assertEqual(result["gpu_execution"], "NOT RUN")
        for line in (self.output / "BUNDLE.sha256").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(sha((self.output / name).read_bytes()), digest)

    def test_source_change_refuses_before_publishing_bundle(self):
        pin = self.write_spec()
        self.trace.write_text("changed trace")
        with self.assertRaises(ValueError):
            evidence.bundle(self.manifest, pin, self.output)
        self.assertFalse(self.output.exists())

    def test_repinning_member_without_seal_membership_refuses(self):
        self.trace.write_text("substituted")
        item = self.spec["artifacts"][1]
        item.update(bytes=self.trace.stat().st_size, sha256=sha(self.trace.read_bytes()))
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.assertFalse(self.output.exists())

    def test_manifest_substitution_refuses(self):
        pin = self.write_spec()
        self.manifest.write_text("{}")
        with self.assertRaises(ValueError):
            evidence.bundle(self.manifest, pin, self.output)

    def test_linked_parent_and_final_member_refuse(self):
        linked = self.root / "linked"
        linked.symlink_to(self.source, target_is_directory=True)
        self.spec["artifacts"][1]["path"] = str(linked / "trace.json")
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.assertFalse(self.output.exists())
        link_file = self.source / "alias.json"
        link_file.symlink_to(self.trace)
        self.spec["artifacts"][1]["path"] = str(link_file)
        with self.assertRaises(ValueError):
            self.make_bundle()

    def test_duplicate_id_and_unknown_reference_refuse(self):
        self.spec["artifacts"][1]["id"] = "selection"
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.spec["artifacts"][1]["id"] = "trace"
        self.spec["hypotheses"][0]["evidence"] = ["missing"]
        with self.assertRaises(ValueError):
            self.make_bundle()

    def test_bad_budget_missing_path_and_boolean_schema_refuse(self):
        for seconds in (0, -1, 14401, True, "14400"):
            self.spec["campaign_seconds"] = seconds
            with self.assertRaises(ValueError):
                self.make_bundle()
        self.spec["campaign_seconds"] = 14400
        self.spec["schema"] = True
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.spec["schema"] = 1
        del self.spec["artifacts"][0]["path"]
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.assertFalse(self.output.exists())

    def test_oversized_specialist_prompt_refuses_before_output(self):
        self.spec["constraints"] = ["x" * 990] * 25
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            self.make_bundle()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        marker = self.output / "user-data"
        marker.write_text("preserve")
        with self.assertRaises(ValueError):
            self.make_bundle()
        self.assertEqual(marker.read_text(), "preserve")

    def test_runtime_root_changes_only_generated_references(self):
        pin = self.write_spec()
        result = evidence.bundle(self.manifest, pin, self.output, runtime_root="/home/developer/hl-evidence")
        self.assertEqual(result["hyperloom_arguments"][-1], "/home/developer/hl-evidence/SPECIALIST-PROMPT.md")
        self.assertIn("/home/developer/hl-evidence/EVIDENCE-INDEX.json",
                      (self.output / "SPECIALIST-PROMPT.md").read_text())
        self.assertEqual(self.trace.read_text(), '{"traceEvents":[{"name":"real-retained-fixture","ph":"X","dur":17}]}\n')

    def test_actual_cli_prepares_bundle_without_an_installed_runtime(self):
        pin = self.write_spec()
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "lib/workstation/hyperloom.py"), "--root", str(ROOT),
             "sglang-evidence", str(self.manifest), str(self.output), "--manifest-sha256", pin],
            check=True, capture_output=True, text=True, timeout=20)
        self.assertEqual(json.loads(completed.stdout)["status"], "HISTORICAL_EVIDENCE_PREPARED")
        self.assertTrue((self.output / "SPECIALIST-PROMPT.md").is_file())


if __name__ == "__main__":
    unittest.main()
