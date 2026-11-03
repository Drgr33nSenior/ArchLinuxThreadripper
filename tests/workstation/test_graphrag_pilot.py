"""Offline pilot tests: provenance, isolation, durable budgets and local HTTP routing."""
import argparse
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import graphrag_pilot as pilot
import graphrag_worker as worker


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append(payload)
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/must-not-follow")
            self.end_headers()
            return
        result = {"model": self.server.model, "choices": [{"finish_reason": self.server.finish,
                    "message": {"role": "assistant", "content": "Supported answer [docs/ASSISTANT.md]"}}],
                  "usage": {"prompt_tokens": 23, "completion_tokens": 7}}
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.calls, server.model, server.finish, server.redirect = [], "local-test", "stop", False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.documents = pilot.read_json(pilot.INPUTS / "questions.json")["corpus"]
        for name in self.documents:
            (self.repo / name).write_text("Reviewed source " + name)
        self.contract = self.root / "contract.json"
        pilot.write_json(self.contract, {"served_model": "local-test", "context_length": 131040,
            "quantization": "quark", "kv_cache_dtype": "bf16", "pointer_sha256": "a" * 64})
        self.args = argparse.Namespace(output=str(self.root / "pilot"), documents=self.documents,
            contract=str(self.contract), contract_sha256=pilot.digest(self.contract.read_bytes()))
        self.profile = pilot.read_json(pilot.INPUTS / "profile.json")

    def prepare(self):
        with patch.object(pilot, "ROOT", self.repo), contextlib.redirect_stdout(io.StringIO()):
            pilot.prepare(self.args)
        return Path(self.args.output)

    def model(self, root, api, **limits):
        profile = {**self.profile, **limits}
        state = copy.deepcopy(pilot.read_json(root / "state.json"))
        return pilot.LocalModel(root, profile, state, "local-test", api)

    def payload(self, **updates):
        return {"model": "local-test", "messages": [{"role": "user", "content": "Evidence?"}], **updates}

    def test_snapshot_hashes_permissions_and_tamper_detection(self):
        root = self.prepare()
        _, record, _, state = pilot.verify(root)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(state["requests"], 0)
        self.assertEqual(len(record["sources"]), 3)
        source = record["sources"][0]
        self.assertEqual((root / source["file"]).read_bytes(), (self.repo / source["source_path"]).read_bytes())
        (root / source["file"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            pilot.verify(root)

    def test_prepare_rejects_wrong_contract_escaping_duplicate_or_missing_corpus(self):
        for updates in ({"contract_sha256": "0" * 64}, {"documents": self.documents + ["../secret"]},
                        {"documents": self.documents + [self.documents[0]]}, {"documents": self.documents[:1]}):
            args = argparse.Namespace(**{**vars(self.args), **updates})
            with self.subTest(updates=updates), patch.object(pilot, "ROOT", self.repo), self.assertRaises(ValueError):
                pilot.prepare(args)
            self.assertFalse(Path(self.args.output).exists())

    def test_large_or_symlink_source_is_rejected_before_output(self):
        path = self.repo / self.documents[0]
        path.write_bytes(b"x" * (self.profile["max_corpus_bytes"] + 1))
        with patch.object(pilot, "ROOT", self.repo), self.assertRaises(ValueError):
            pilot.prepare(self.args)
        path.unlink()
        path.symlink_to(self.contract)
        with patch.object(pilot, "ROOT", self.repo), self.assertRaises(ValueError):
            pilot.prepare(self.args)
        self.assertFalse(Path(self.args.output).exists())

    def test_loopback_only_and_no_credential_urls(self):
        for value in ("https://api.openai.com/v1", "http://localhost:1000/v1", "http://127.0.0.2:1000/v1",
                      "http://127.0.0.1/v1", "http://key@127.0.0.1:1000/v1", "http://127.0.0.1:1000/v1?q=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pilot.local_api(value)

    def test_call_budget_is_durable_and_non_thinking_output_is_capped(self):
        root = self.prepare()
        with upstream() as (server, api):
            model = self.model(root, api, max_requests=1)
            model.completion(self.payload())
            with self.assertRaisesRegex(ValueError, "exhausted"):
                model.completion(self.payload())
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(server.calls[0]["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(server.calls[0]["max_tokens"], 2048)
        state = pilot.read_json(root / "state.json")
        self.assertEqual((state["requests"], state["prompt_tokens"], state["completion_tokens"]), (1, 23, 7))
        self.assertFalse(state["inflight"])

    def test_invalid_payloads_and_bytes_never_reach_upstream(self):
        root = self.prepare()
        with upstream() as (server, api):
            model = self.model(root, api)
            for kwargs in ({"model": "cloud-model"}, {"stream": True}, {"max_tokens": 2049},
                           {"max_tokens": 12, "max_completion_tokens": 12}, {"tools": []},
                           {"messages": [{"role": "user", "content": "x" * 131072}]}):
                with self.subTest(kwargs=list(kwargs)), self.assertRaises(ValueError):
                    model.completion(self.payload(**kwargs))
            self.assertEqual(server.calls, [])
            self.assertEqual(model.state["requests"], 0)

    def test_mismatched_or_truncated_answers_and_redirects_fail(self):
        root = self.prepare()
        with upstream() as (server, api):
            model = self.model(root, api)
            server.model = "different-model"
            with self.assertRaisesRegex(ValueError, "model"):
                model.completion(self.payload())
            server.model, server.finish = "local-test", "length"
            with self.assertRaisesRegex(ValueError, "truncated"):
                model.completion(self.payload())
            server.finish, server.redirect = "stop", True
            with self.assertRaisesRegex(ValueError, "rejected"):
                model.completion(self.payload())
            self.assertEqual(len(server.calls), 3)

    def test_connection_failure_and_stale_inflight_fail_closed(self):
        root = self.prepare()
        model = self.model(root, "http://127.0.0.1:1/v1", request_timeout_seconds=1)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            model.completion(self.payload())
        with self.assertRaisesRegex(ValueError, "uncertain"):
            pilot.verify(root)
        model.state.update(uncertain=False, inflight=True)
        model.save()
        with self.assertRaisesRegex(ValueError, "uncertain"):
            pilot.verify(root)

    def test_direct_baseline_uses_same_snapshot_through_budget_proxy(self):
        root = self.prepare()
        with upstream() as (server, api):
            model = self.model(root, api)
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), pilot.proxy_handler(model, "ephemeral-test-value"))
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"GRAPHRAG_PILOT_URL": f"http://127.0.0.1:{proxy.server_port}/v1",
                                           "GRAPHRAG_PILOT_KEY": "ephemeral-test-value"}):
                    result = worker.direct_query(root, {"served_model": "local-test"}, "Evidence?", self.profile)
                self.assertIn("Supported answer", result["answer"])
                for name in self.documents:
                    self.assertIn("SOURCE " + name, server.calls[0]["messages"][1]["content"])
                self.assertEqual(model.state["requests"], 1)
            finally:
                proxy.shutdown()
                proxy.server_close()
                thread.join()

    def test_tokenizer_integrity_refuses_empty_or_symlink_cache(self):
        cache = self.root / "tokens"
        cache.mkdir()
        with self.assertRaisesRegex(ValueError, "tokenizer"):
            pilot.verify_tokenizers(cache)
        link = self.root / "token-link"
        link.symlink_to(cache, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "tokenizer"):
            pilot.verify_tokenizers(link)

    def test_graph_reducer_sse_is_framed_after_one_complete_upstream_call(self):
        root = self.prepare()
        with upstream() as (server, api):
            model = self.model(root, api)
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), pilot.proxy_handler(model, "fixture-key"))
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                payload = self.payload(stream=True, stream_options={"include_usage": True})
                request = urllib.request.Request(f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
                    data=json.dumps(payload).encode(), headers={"Authorization": "Bearer fixture-key"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.headers["Content-Type"], "text/event-stream")
                    chunks = response.read().decode()
                self.assertIn('"finish_reason": "stop"', chunks)
                self.assertTrue(chunks.endswith("data: [DONE]\n\n"))
                self.assertEqual(len(server.calls), 1)
                self.assertIs(server.calls[0]["stream"], False)
                self.assertNotIn("stream_options", server.calls[0])
                self.assertEqual(model.state["requests"], 1)
                self.assertFalse(model.state["inflight"])
            finally:
                proxy.shutdown()
                proxy.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
