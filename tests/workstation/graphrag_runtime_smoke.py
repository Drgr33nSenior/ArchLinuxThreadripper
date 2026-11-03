"""Opt-in real GraphRAG workflow smoke with deterministic loopback model responses.

This checks package/configuration/protocol compatibility, never model quality.
Run using the isolated pilot interpreter and an already staged tokenizer cache.
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib/workstation"))
import graphrag_pilot as pilot


class FixtureModel(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = "\n".join(message.get("content", "") for message in request["messages"])
        if '"points"' in text:
            phase = "map"
            content = json.dumps({"points": [{"description": "Local Qwen uses Exa and private RAG [Data: Reports (0)]", "score": 80}]})
        elif '"rating_explanation"' in text:
            phase = "report"
            content = json.dumps({"title": "Local assistant", "summary": "Qwen uses Exa and private RAG.",
                "rating": 1.0, "rating_explanation": "Fixture relationship.",
                "findings": [{"summary": "Search and retrieval", "explanation": "Qwen uses Exa and RAG [Data: Entities (0,1,2)]"}]})
        elif "entity_types" in text.lower() or "entity types" in text.lower():
            phase = "extract"
            content = '("entity"<|>"QWEN"<|>"MODEL"<|>"Local assistant.")##' \
                '("entity"<|>"EXA"<|>"SERVICE"<|>"Public web search.")##' \
                '("entity"<|>"RAG"<|>"COMPONENT"<|>"Private document retrieval.")##' \
                '("relationship"<|>"QWEN"<|>"EXA"<|>"Uses public search."<|>9)##' \
                '("relationship"<|>"QWEN"<|>"RAG"<|>"Uses private retrieval."<|>9)##' \
                '("relationship"<|>"EXA"<|>"RAG"<|>"Separate evidence paths."<|>5)<|COMPLETE|>'
        else:
            phase = "answer"
            content = "Local Qwen uses Exa search and private RAG [Data: Reports (0)]."
        response = {"id": "fixture-completion", "object": "chat.completion", "created": 1,
            "model": request["model"], "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}}
        self.server.calls += 1
        self.server.phases.append(phase)
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="new private output directory")
    parser.add_argument("--tokenizer-cache", required=True)
    args = parser.parse_args()
    root = Path(args.output).absolute()
    root.mkdir(mode=0o700)
    repo = root / "fixture-repo"
    (repo / "docs").mkdir(parents=True)
    documents = pilot.read_json(pilot.INPUTS / "questions.json")["corpus"]
    for name in documents:
        (repo / name).write_text("Qwen is the local model. It uses Exa for public web search. "
                               "RAG supplies private document evidence to Qwen. Exa and RAG are separate services.")
    contract = root / "fixture-contract.json"
    pilot.write_json(contract, {"served_model": "fixture-local-model", "context_length": 131040,
        "quantization": "quark", "kv_cache_dtype": "bf16", "pointer_sha256": "0" * 64})
    preparation = argparse.Namespace(output=str(root / "pilot"), documents=documents,
        contract=str(contract), contract_sha256=pilot.digest(contract.read_bytes()))
    with patch.object(pilot, "ROOT", repo), contextlib.redirect_stdout(io.StringIO()):
        pilot.prepare(preparation)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModel)
    server.calls = 0
    server.phases = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    run = argparse.Namespace(pilot=preparation.output, runtime_python=sys.executable,
        api_base=f"http://127.0.0.1:{server.server_port}/v1", tokenizer_cache=args.tokenizer_cache, action="index")
    try:
        pilot.run(run)
        for mode in ("global", "direct"):
            run.action, run.question_id, run.mode = "query", "search-and-knowledge", mode
            pilot.run(run)
        answers = [pilot.read_json(path) for path in (Path(preparation.output) / "runs").glob("*/result.json")]
        for mode in ("global", "direct"):
            pilot.require(any(answer.get("mode") == mode and answer.get("answer") ==
                "Local Qwen uses Exa search and private RAG [Data: Reports (0)]." for answer in answers),
                "fixture answer path did not complete")
        require_calls = server.calls >= 5 and {"extract", "report", "map", "answer"}.issubset(server.phases)
        pilot.require(require_calls, "GraphRAG did not call the fixture model")
        pilot.write_json(root / "smoke.json", {"schema": 1, "status": "passed", "python": sys.version,
            "model_requests": server.calls, "phases": server.phases,
            "scope": "real-GraphRAG-index-global-direct-with-fixture-model",
            "model_quality_verified": False})
        print("GraphRAG runtime smoke passed; fixture responses do not establish model quality.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
