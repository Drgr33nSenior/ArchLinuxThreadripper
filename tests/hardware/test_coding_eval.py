"""Deterministic coding/tool-evaluation harness tests; no model or code execution."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lib/workstation"))
import coding_eval


class CodingEvalTests(unittest.TestCase):
    def responses(self, corpus, digest):
        rows = []
        for task in corpus["tasks"]:
            expected = task["expected"]
            if task["kind"] == "code":
                response = {"language": "python", "source": "def add(a, b):\n return a + b\n" if task["id"] == "python-syntax-fix" else "def is_even(value):\n return value % 2 == 0\n"}
            else:
                response = expected
            rows.append({"task_id": task["id"], "attempts": 1, "latency_ms": None,
                         "input_tokens": 10, "output_tokens": 5, "response": json.dumps(response)})
        return {"schema": 1, "kind": "workstation-coding-evaluation-responses", "corpus_sha256": digest,
                "generation": {"model": "fixture", "temperature": 0, "max_output_tokens": 64, "seed": 1}, "responses": rows}

    def test_versioned_corpus_redacts_responses_and_does_not_execute_code(self):
        corpus, digest, _ = coding_eval.load_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            responses = self.responses(corpus, digest)
            path.write_text(json.dumps(responses))
            result = coding_eval.evaluate(coding_eval.corpus_path(), path)
            self.assertEqual(len(result["results"]), 10)
            self.assertEqual(result["successes"], 8)
            self.assertEqual(result["unavailable"], 2)
            self.assertEqual(result["status"], "incomplete-unqualified")
            self.assertNotIn("def add", json.dumps(result))
            self.assertIn("not executed", result["execution"])

    def test_rejects_wrong_tool_unknown_task_and_invalid_code(self):
        corpus, digest, _ = coding_eval.load_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            responses = self.responses(corpus, digest)
            responses["responses"][2]["response"] = json.dumps({"tool": "shell", "arguments": {}})
            responses["responses"][6]["response"] = json.dumps({"language": "python", "source": "def"})
            path.write_text(json.dumps(responses))
            result = coding_eval.evaluate(coding_eval.corpus_path(), path)
            self.assertEqual(result["failures"], 2)
            responses["responses"].append({"task_id": "unknown", "attempts": 1, "response": "{}"})
            path.write_text(json.dumps(responses))
            with self.assertRaises(ValueError):
                coding_eval.evaluate(coding_eval.corpus_path(), path)

    def test_rejects_incomplete_or_unbounded_generation_provenance(self):
        corpus, digest, _ = coding_eval.load_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            responses = self.responses(corpus, digest)
            responses["generation"].pop("seed")
            path.write_text(json.dumps(responses))
            with self.assertRaises(ValueError):
                coding_eval.evaluate(coding_eval.corpus_path(), path)
            responses = self.responses(corpus, digest)
            responses["generation"]["max_output_tokens"] = 131073
            path.write_text(json.dumps(responses))
            with self.assertRaises(ValueError):
                coding_eval.evaluate(coding_eval.corpus_path(), path)


if __name__ == "__main__":
    unittest.main()
