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
    def protocol(self):
        tool = {"id": "fixture-call", "type": "function", "function": {
            "name": "lookup", "arguments": '{"city":"Paris"}'}}
        whole = {"choices": [{"index": 0, "finish_reason": "tool_calls",
                               "message": {"role": "assistant", "content": None, "tool_calls": [tool]}}]}
        chunks = [{"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
            {"index": 0, "id": "stream-call", "type": "function", "function": {
                "name": "lookup", "arguments": '{"city":'}}]}}]},
                  {"choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                      {"index": 0, "function": {"arguments": '"Paris"}'}}]}}]},
                  {"choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]}]
        return {"non_streaming": whole, "streaming": "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n"}

    def test_real_protocol_shapes_match_without_executing_tools(self):
        expected = {"tool": "lookup", "arguments": {"city": "Paris"}}
        task = {"id": "fixture", "kind": "tool", "expected": expected}
        row = {"attempts": 1, "response": json.dumps(expected), "protocol": self.protocol()}
        result = coding_eval.evaluate_task(task, row)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["protocol_status"], "passed")
        self.assertNotIn("Paris", json.dumps(result))

    def test_sglang_nullable_stream_fragments_preserve_complete_identity(self):
        expected = {"tool": "lookup", "arguments": {"city": "Paris"}}
        protocol = self.protocol()
        frames = [json.loads(frame[6:]) for frame in protocol["streaming"].split("\n\n")
                  if frame and frame != "data: [DONE]"]
        # The pinned image serializes optional fields explicitly as null.
        frames.insert(0, {"choices": [{"index": 0, "finish_reason": None,
                                      "delta": {"role": "assistant", "content": "", "tool_calls": None}}]})
        item = frames[2]["choices"][0]["delta"]["tool_calls"][0]
        item.update(id=None, type="function")
        item["function"]["name"] = None
        for variant in ("valid", "changed-id", "missing-id", "missing-name", "missing-arguments", "bad-null-list"):
            modified = json.loads(json.dumps(frames))
            first = modified[1]["choices"][0]["delta"]["tool_calls"][0]
            later = modified[2]["choices"][0]["delta"]["tool_calls"][0]
            if variant == "changed-id":
                later["id"] = "different-call"
            elif variant == "missing-id":
                first["id"] = None
            elif variant == "missing-name":
                first["function"]["name"] = None
            elif variant == "missing-arguments":
                later["function"]["arguments"] = None
            elif variant == "bad-null-list":
                modified[0]["choices"][0]["delta"]["tool_calls"] = [None]
            protocol["streaming"] = "".join("data: " + json.dumps(frame) + "\n\n" for frame in modified) + "data: [DONE]\n\n"
            with self.subTest(variant=variant):
                if variant == "valid":
                    coding_eval.protocol_tool(protocol, expected)
                else:
                    with self.assertRaises(ValueError):
                        coding_eval.protocol_tool(protocol, expected)

    def test_truncated_disconnected_or_leaking_protocol_fails(self):
        expected = {"tool": "lookup", "arguments": {"city": "Paris"}}
        task = {"id": "fixture", "kind": "tool", "expected": expected}
        for variant in ("disconnect", "length", "partial-arguments", "markup", "after-done", "bad-index", "duplicate-json"):
            protocol = self.protocol()
            if variant == "disconnect":
                protocol["streaming"] = protocol["streaming"].replace("data: [DONE]\n\n", "")
            elif variant == "length":
                protocol["non_streaming"]["choices"][0]["finish_reason"] = "length"
            elif variant == "partial-arguments":
                protocol["non_streaming"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"city":"Par'
            elif variant == "markup":
                protocol["non_streaming"]["choices"][0]["message"]["content"] = '<tool_call><function'
            elif variant == "after-done":
                protocol["streaming"] += 'data: {}\n\n'
            elif variant == "bad-index":
                protocol["streaming"] = protocol["streaming"].replace('"index": 0', '"index": true')
            else:
                protocol["non_streaming"]["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"city":"Rome","city":"Paris"}'
            row = {"attempts": 1, "response": json.dumps(expected), "protocol": protocol}
            with self.subTest(variant=variant):
                self.assertEqual(coding_eval.evaluate_task(task, row)["status"], "failed")

    def test_json_assertions_distinguish_booleans_from_numbers_recursively(self):
        for kind in ("structured", "tool", "negative"):
            for expected, actual in ((1, True), (False, 0),
                                     ({"nested": [1, {"value": False}]}, {"nested": [True, {"value": 0}]})):
                wanted, got = {"value": expected}, {"value": actual}
                if kind == "tool":
                    wanted, got = ({"tool": "fixture", "arguments": value} for value in (wanted, got))
                task = {"id": "fixture", "kind": kind, "expected": wanted}
                response = {"attempts": 1, "response": json.dumps(got)}
                with self.subTest(kind=kind, expected=expected):
                    self.assertEqual(coding_eval.evaluate_task(task, response)["status"], "failed")
                    response["response"] = json.dumps(wanted)
                    self.assertEqual(coding_eval.evaluate_task(task, response)["status"], "passed")

    def test_failed_or_missing_tool_response_cannot_hide_protocol_coverage(self):
        corpus, digest, _ = coding_eval.load_corpus()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "responses.json"
            responses = self.responses(corpus, digest)
            tools = [row for row in responses["responses"] if row["task_id"].startswith("tool-")]
            tools[0].update(response="not JSON", protocol=self.protocol())
            responses["responses"].remove(tools[1])
            path.write_text(json.dumps(responses))
            result = coding_eval.evaluate(coding_eval.corpus_path(), path)
            self.assertEqual(result["protocol"], {"passed": 0, "failed": 1, "unknown-not-collected": 2})

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
