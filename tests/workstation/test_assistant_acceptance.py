"""Assistant API profile, isolated executor and acceptance runner tests.

The fixture server is a deterministic stand-in that tests the runner only.
Nothing here is model, gateway, browser or native-runtime evidence.
"""
import http.server
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "lib/workstation")]
import assistant_acceptance
import assistant_api
import coding_eval


def sse(frames):
    return "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"


class ProfileTests(unittest.TestCase):
    def test_segmentation_inline_separated_and_unfinished(self):
        self.assertEqual(("think", "17", "inline"), assistant_api.segment("think\n</think>\n\n17", None, True))
        self.assertEqual(("think", "17", "separated"), assistant_api.segment("17", "think", True))
        self.assertEqual("unfinished-thinking", assistant_api.segment("still thinking", None, True)[2])
        self.assertEqual(("", "17", "no-thinking"), assistant_api.segment("17", None, False))
        with self.assertRaisesRegex(ValueError, "leaked"):
            assistant_api.segment("x</think>17", "think", True)

    def test_finish_reasons_detect_truncation_and_unfinished_output(self):
        def message(content, finish, reasoning=None):
            return {"choices": [{"index": 0, "finish_reason": finish,
                                 "message": {"content": content, "reasoning_content": reasoning}}]}
        self.assertEqual("final", assistant_api.response(message("17", "stop", "r"), True)["outcome"])
        self.assertEqual("truncated", assistant_api.response(message("partial", "length", "r"), True)["outcome"])
        self.assertEqual("unfinished-thinking", assistant_api.response(message("thinking only", "stop"), True)["outcome"])
        self.assertEqual("truncated", assistant_api.response(message("", "stop", "r"), True)["outcome"])
        with self.assertRaises(ValueError):
            assistant_api.response({"error": {"code": "x"}}, True)

    def test_stream_assembles_fragmented_tool_arguments_and_ids(self):
        frames = [{"choices": [{"index": 0, "delta": {"reasoning_content": "search"}}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                "function": {"name": "search_web", "arguments": '{"query":"a'}}]}}]},
                  {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": None, "arguments": 'b","count":2}'}}]}}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                  {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}]
        result = assistant_api.stream(sse(frames), True)
        self.assertEqual("tool_calls", result["outcome"])
        self.assertEqual([{"id": "c1", "name": "search_web", "arguments": {"query": "ab", "count": 2}}], result["tool_calls"])
        self.assertEqual(9, result["prompt_tokens"])
        cut = sse(frames).replace("data: [DONE]\n\n", "")
        self.assertEqual("cancelled", assistant_api.stream(cut, True)["outcome"])
        changed = json.loads(json.dumps(frames))
        changed[2]["choices"][0]["delta"]["tool_calls"][0]["id"] = "c2"
        with self.assertRaisesRegex(ValueError, "identity changed"):
            assistant_api.stream(sse(changed), True)

    def test_followup_requires_one_result_per_call_id(self):
        assistant = {"tool_calls": [{"id": "c1", "name": "f", "arguments": {"a": 1}}]}
        messages = assistant_api.followup_messages([{"role": "user", "content": "q"}], assistant, {"c1": {"r": 2}})
        self.assertEqual({"role": "tool", "tool_call_id": "c1", "content": '{"r":2}'}, messages[-1])
        self.assertEqual('{"a":1}', messages[1]["tool_calls"][0]["function"]["arguments"])
        for results in ({}, {"c2": {}}, {"c1": {}, "c3": {}}):
            with self.assertRaises(ValueError):
                assistant_api.followup_messages([], assistant, results)


class ExecutorTests(unittest.TestCase):
    def test_sandbox_argv_is_deny_by_default(self):
        argv = coding_eval.sandbox_argv(["/usr/bin/python3", "check.py"], 20)
        for required in ("--unshare-all", "--die-with-parent", "--clearenv", "--new-session"):
            self.assertIn(required, argv)
        self.assertEqual(["/usr/bin/timeout", "--signal=TERM", "--kill-after=2", "20", "/usr/bin/bwrap"], argv[:5])
        self.assertNotIn("--share-net", argv)
        self.assertNotIn(os.path.expanduser("~"), " ".join(argv))
        for command in (["python3"], [], ["/usr/bin/python3"] * 0):
            with self.assertRaises(ValueError):
                coding_eval.sandbox_argv(command, 20)
        with self.assertRaises(ValueError):
            coding_eval.sandbox_argv(["/usr/bin/true"], 301)

    def test_model_code_never_runs_outside_the_sandbox(self):
        calls = []
        with patch("coding_eval.sandbox_available", return_value="no sandbox"), \
                patch("coding_eval.subprocess.run", side_effect=lambda *a, **k: calls.append(a)):
            result = coding_eval.execute_python("def f():\n    return 1\n", ["f() == 1"])
        self.assertEqual("unavailable-no-isolated-executor", result["status"])
        self.assertEqual([], calls)
        seen = []

        class Done:
            returncode, stdout, stderr = 0, b"ok", b""
        with patch("coding_eval.sandbox_available", return_value=None), \
                patch("coding_eval.subprocess.run", side_effect=lambda argv, **k: seen.append(argv) or Done()):
            result = coding_eval.execute_python("def f():\n    return 1\n", ["f() == 1"])
        self.assertEqual("tests-passed", result["status"])
        self.assertEqual("/usr/bin/bwrap", seen[0][4])
        self.assertIn("--unshare-all", seen[0])

    def test_responses_cannot_replace_tests(self):
        fixture = ROOT / "infrastructure/sglang/assistant-tasks/python-bugfix/fixture"
        with self.assertRaisesRegex(ValueError, "editable files"):
            coding_eval.run_task(fixture, {"test_windows.py": "pass\n"}, ["/usr/bin/true"], allowed=("windows.py",))

    @unittest.skipUnless(platform.system() == "Linux" and coding_eval.sandbox_available() is None,
                         "network-free bubblewrap sandbox unavailable on this host")
    def test_sandbox_runs_reference_and_refuses_network(self):
        passed = coding_eval.execute_python("def f():\n    return 1\n", ["f() == 1"])
        self.assertEqual("tests-passed", passed["status"])
        network = coding_eval.execute_python(
            "import socket\ndef f():\n    socket.create_connection(('1.1.1.1', 53), 2)\n    return 1\n", ["f() == 1"])
        self.assertEqual("tests-failed", network["status"])

    def test_python_fixture_fails_and_reference_passes_with_real_tests(self):
        # Authored reference only; model output never uses sandbox=False.
        task = ROOT / "infrastructure/sglang/assistant-tasks/python-bugfix"
        command = [sys.executable, "-E", "-s", "-B", "-m", "unittest", "-q", "test_windows"]
        broken = coding_eval.run_task(task / "fixture", {}, command, sandbox=False)
        fixed = coding_eval.run_task(task / "fixture", {"windows.py": (task / "reference/windows.py").read_text()},
                                     command, allowed=("windows.py",), sandbox=False)
        self.assertEqual(("tests-failed", "tests-passed"), (broken["status"], fixed["status"]))
        self.assertFalse(fixed["isolated"])


class FixtureServer(http.server.BaseHTTPRequestHandler):
    """Deterministic stand-in for an OpenAI-compatible endpoint."""
    answers = {}
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FixtureServer.seen.append(body)
        text = body["messages"][0]["content"]
        if body.get("tools") and body["messages"][-1]["role"] == "user" and len(body["messages"]) == 1:
            payload = sse([{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                           "function": {"name": "search_web", "arguments": '{"query":"SGLang prefix caching documentation",'}}]}}]},
                           {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"count":2}'}}]}}]},
                           {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]).encode()
            kind = "text/event-stream"
        else:
            content = next((value for key, value in FixtureServer.answers.items() if key in text), "UNKNOWN")
            if body["messages"][-1]["role"] in ("tool", "user") and len(body["messages"]) > 1:
                content = "The capacity is 17 entries, per https://example.com/cache"
            payload = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "content": content, "reasoning_content": "brief" if body.get("chat_template_kwargs", {}).get("enable_thinking") else None}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.menu = assistant_acceptance.load_menu()
        FixtureServer.seen = []
        FixtureServer.answers = {"Return exactly one JSON object": '{"status":"ready","count":3}',
                                 "newest client version": "3.3",
                                 "kubectl get pod": json.dumps({"root_cause": "OOMKilled", "container_limit": "512Mi",
                                                                "trigger_env": "CACHE_SHARDS", "evidence": ["E2", "E4"]})}
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureServer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_menu_keeps_existing_cases_and_labels_profiles(self):
        cases = {case["id"]: case for case in self.menu["cases"]}
        self.assertEqual("prepared-not-run", self.menu["status"])
        self.assertEqual(["baseline"], cases["coding-regression"]["profiles"])
        self.assertEqual(["feature-nonthinking"], cases["instruction-format"]["profiles"])
        self.assertEqual({"enable_thinking": True}, self.menu["profiles"]["baseline"]["request"]["chat_template_kwargs"])
        self.assertTrue(cases["long-context-retrieval"]["experiment"])
        held = [case for case in self.menu["cases"] if case.get("held_out")]
        self.assertEqual({"coding", "config", "troubleshooting", "grounded"}, {case["category"] for case in held})
        for case in held:
            if case["grading"]["kind"] == "repository-tests":
                self.assertTrue(case["grading"]["command"][0].startswith("/usr/bin/"))
                for name in case["editable"]:
                    self.assertTrue((ROOT / "infrastructure/sglang/assistant-tasks" / case["task"] / "reference" / name).is_file())

    def test_fixture_boundary_round_trip_grades_and_counts_tasks_separately(self):
        cases = ["instruction-format", "search-tool", "tool-result-followup", "project-evidence",
                 "version-selection", "k8s-troubleshoot", "long-context-retrieval", "python-bugfix", "rag-and-web-version"]
        records = assistant_acceptance.run(self.menu, "fixture", self.base, None, "fixture-model", 1024, 10,
                                           repetitions=2, cases=cases)
        result = assistant_acceptance.report(self.menu, records, "fixture", {})
        grades = {}
        for row in records:
            grades.setdefault(row["case"], set()).add(row["grade"])
        self.assertEqual({"passed"}, grades["instruction-format"])
        self.assertEqual({"passed"}, grades["search-tool"])
        self.assertEqual({"passed"}, grades["tool-result-followup"])
        self.assertEqual({"passed"}, grades["project-evidence"])
        self.assertEqual({"passed"}, grades["k8s-troubleshoot"])
        self.assertEqual({"needs-review"}, grades["rag-and-web-version"])
        self.assertEqual({"NOT RUN"}, grades["long-context-retrieval"])
        # The fixture answer returns no editable file, so the coding task fails;
        # an executor that cannot run would be NOT RUN, never passed.
        self.assertEqual({"failed"}, grades["python-bugfix"])
        self.assertEqual(9, result["unique_tasks"])
        self.assertGreater(result["attempts"], result["unique_tasks"])
        self.assertEqual(1.0, result["metrics"]["tool_roundtrip_success"])
        self.assertEqual(1.0, result["metrics"]["abstention_correct"])
        follow = [body for body in FixtureServer.seen if any(m["role"] == "tool" for m in body["messages"])]
        self.assertTrue(follow)
        self.assertEqual("call-1", follow[0]["messages"][2]["tool_call_id"])
        self.assertEqual("low", follow[0]["reasoning_effort"])

    def test_unreachable_endpoint_is_not_run_not_pass(self):
        records = assistant_acceptance.run(self.menu, "gateway", "http://127.0.0.1:9", None, "m", 1024, 2,
                                           repetitions=1, cases=["instruction-format", "version-selection"])
        result = assistant_acceptance.report(self.menu, records, "gateway", {})
        self.assertEqual("NOT RUN", result["status"])
        self.assertTrue(all(row["grade"] == "NOT RUN" for row in records))
        self.assertIsNone(result["metrics"]["task_pass_rate"])

    def test_repository_answer_is_graded_only_by_tests(self):
        case = next(c for c in self.menu["cases"] if c["id"] == "python-bugfix")
        reference = (ROOT / "infrastructure/sglang/assistant-tasks/python-bugfix/reference/windows.py").read_text()
        with patch("coding_eval.sandbox_available", return_value="no sandbox"):
            passed, detail = assistant_acceptance.answer(case, {"final": f"```windows.py\n{reference}```"})
        self.assertIsNone(passed)  # executor unavailable: NOT RUN, not a pass
        self.assertEqual("unavailable-no-isolated-executor", detail["execution"]["status"])
        passed, _ = assistant_acceptance.answer(case, {"final": "```test_windows.py\npass\n```"})
        self.assertFalse(passed)

    @unittest.skipUnless(os.environ.get("CANDIDATE_TOKENIZER_DIR"), "set CANDIDATE_TOKENIZER_DIR to the pinned tokenizer")
    def test_budget_uses_actual_tokenizer_and_template(self):
        plans = assistant_acceptance.requests(self.menu, "m", 4096, repetitions=1)
        result = assistant_acceptance.budget(plans, os.environ["CANDIDATE_TOKENIZER_DIR"], 131040, 4096, 16)
        self.assertEqual("measured", result["status"])
        self.assertTrue(all(row["fits"] for row in result["rows"]))


if __name__ == "__main__":
    unittest.main()
