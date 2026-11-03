"""Explicit, private GraphRAG pilot preparation and bounded local-model execution."""
import argparse
import datetime
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "infrastructure/graphrag"
WORKER = ROOT / "lib/workstation/graphrag_worker.py"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path, maximum=262144):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
            "expected a bounded regular JSON file")
    return json.loads(path.read_bytes())


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".pending")
    require(not path.is_symlink() and not temporary.exists() and not temporary.is_symlink(), "unsafe journal path")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def local_api(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port is not None
            and 1 <= parsed.port <= 65535 and parsed.path == "/v1"
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "use an owner-established 127.0.0.1 HTTP /v1 tunnel, with an explicit port")
    return value


def verify_tokenizers(directory):
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), "staged tokenizer directory missing")
    for asset in read_json(INPUTS / "tokenizers.json")["assets"]:
        filename = hashlib.sha1(asset["url"].encode()).hexdigest()
        path = directory / filename
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 4194304
                and digest(path.read_bytes()) == asset["sha256"], "tokenizer asset missing or changed")


def stage_tokenizers(output):
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "tokenizer output must be new")
    output.mkdir(parents=True, mode=0o700)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for asset in read_json(INPUTS / "tokenizers.json")["assets"]:
        with opener.open(asset["url"], timeout=60) as response:
            data = response.read(4194305)
        require(len(data) <= 4194304 and digest(data) == asset["sha256"], "tokenizer download differs")
        (output / hashlib.sha1(asset["url"].encode()).hexdigest()).write_bytes(data)
    verify_tokenizers(output)
    print("Staged and verified tokenizer assets:", output)


def prepare(args):
    profile = read_json(INPUTS / "profile.json")
    contract_path = Path(args.contract)
    contract = read_json(contract_path)
    require(digest(contract_path.read_bytes()) == args.contract_sha256, "candidate contract digest differs")
    model = contract.get("served_model", "")
    require(isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}", model)
            and ".." not in model, "invalid candidate model identity")
    require(type(contract.get("context_length")) is int and contract["context_length"] >= 131040,
            "this pilot requires the selected 128K candidate contract")
    require(contract.get("quantization") == "quark" and contract.get("kv_cache_dtype") == "bf16",
            "candidate precision contract differs")
    require(1 <= len(args.documents) <= profile["max_documents"] and len(set(args.documents)) == len(args.documents),
            "select distinct explicit document paths")
    questions = read_json(INPUTS / "questions.json")
    require(set(questions["corpus"]).issubset(args.documents), "include the three evaluation corpus documents")
    snapshots, total = [], 0
    for name in args.documents:
        require(re.fullmatch(r"docs/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.md", name) and ".." not in name,
                "pilot inputs must be explicit repository docs/*.md files")
        source = ROOT / name
        require(not source.is_symlink() and source.is_file()
                and source.resolve().is_relative_to((ROOT / "docs").resolve()), "document escapes repository docs")
        require(source.stat().st_size <= profile["max_corpus_bytes"], "document exceeds pilot byte bound")
        data = source.read_bytes()
        data.decode("utf-8")
        total += len(data)
        require(total <= profile["max_corpus_bytes"], "corpus exceeds the 64 KiB pilot bound")
        snapshots.append((name, data))
    output = Path(args.output)
    require(not output.exists() and not output.is_symlink(), "pilot output must be new")
    output.mkdir(parents=True, mode=0o700)
    output = output.resolve()
    for name in ("input", "output", "cache", "runs"):
        (output / name).mkdir(mode=0o700)
    sources = []
    for index, (name, data) in enumerate(snapshots):
        filename = f"input/{index:02d}-{Path(name).stem}-{digest(data)[:12]}.txt"
        (output / filename).write_bytes(data)
        sources.append({"source_path": name, "sha256": digest(data), "file": filename, "bytes": len(data)})
    write_json(output / "profile.json", profile)
    write_json(output / "candidate-contract.json", contract)
    write_json(output / "questions.json", questions)
    write_json(output / "tokenizers.json", read_json(INPUTS / "tokenizers.json"))
    (output / "requirements.lock").write_bytes((INPUTS / "requirements-python314.lock").read_bytes())
    files = {name: digest((output / name).read_bytes()) for name in
             ("profile.json", "candidate-contract.json", "questions.json", "tokenizers.json", "requirements.lock")}
    files.update({entry["file"]: entry["sha256"] for entry in sources})
    write_json(output / "pilot.json", {"schema": 1, "status": "prepared-not-indexed",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "method": "standard-global",
        "scope": "single-owner-explicit-documents", "bytes": total, "sources": sources, "files": files,
        "candidate_contract_sha256": args.contract_sha256, "worker_sha256": digest(WORKER.read_bytes()),
        "controller_sha256": digest(Path(__file__).read_bytes())})
    write_json(output / "state.json", {"schema": 1, "requests": 0, "request_bytes": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "usage_missing": 0, "uncertain": False,
        "inflight": False, "failed_requests": 0, "index_status": "not-run"})
    print("Prepared private GraphRAG pilot; no model request or ingestion performed:", output)


def verify(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "expected a private pilot directory")
    root = root.resolve()
    require(root.stat().st_uid == os.geteuid() and root.stat().st_mode & 0o077 == 0,
            "pilot directory must be caller-owned mode 0700")
    record = read_json(root / "pilot.json")
    require(record.get("schema") == 1 and record.get("method") == "standard-global", "invalid pilot manifest")
    required_files = {"profile.json", "candidate-contract.json", "questions.json", "tokenizers.json", "requirements.lock"}
    required_files.update(entry["file"] for entry in record["sources"])
    require(set(record["files"]) == required_files, "incomplete pilot manifest")
    for name, expected in record["files"].items():
        path = root / name
        require(not Path(name).is_absolute() and path.resolve().is_relative_to(root)
                and path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == expected,
                "pilot source/configuration changed")
    require(digest(WORKER.read_bytes()) == record["worker_sha256"], "worker changed; prepare a fresh pilot")
    require(digest(Path(__file__).read_bytes()) == record["controller_sha256"], "controller changed; prepare a fresh pilot")
    profile = read_json(root / "profile.json")
    require(profile == read_json(INPUTS / "profile.json"), "pilot profile differs from reviewed source")
    require(read_json(root / "tokenizers.json") == read_json(INPUTS / "tokenizers.json"),
            "pilot tokenizer identities differ from reviewed source")
    require((root / "requirements.lock").read_bytes() == (INPUTS / "requirements-python314.lock").read_bytes(),
            "pilot dependency lock differs from reviewed source")
    state = read_json(root / "state.json")
    for key in ("requests", "request_bytes", "prompt_tokens", "completion_tokens", "usage_missing", "failed_requests"):
        require(type(state.get(key)) is int and state[key] >= 0, "invalid usage journal")
    require(state.get("uncertain") is False and state.get("inflight") is False,
            "prior model work is uncertain; retain evidence and review before a new pilot")
    require(state.get("index_status") in {"not-run", "complete", "failed"}, "interrupted index; prepare a fresh pilot")
    require(state["requests"] <= profile["max_requests"]
            and state["request_bytes"] <= profile["max_total_request_bytes"], "pilot budget exceeded")
    return root, record, profile, state


class LocalModel:
    """One fixed loopback destination, durable call/byte limits, no redirects/retries."""
    def __init__(self, root, profile, state, model, api_base, key=None):
        self.root, self.profile, self.state, self.model = root, profile, state, model
        self.api_base, self.key = local_api(api_base), key
        self.guard = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def save(self):
        write_json(self.root / "state.json", self.state)

    def completion(self, payload):
        with self.guard:
            require(not self.state["uncertain"] and not self.state["inflight"], "model request already active or uncertain")
            require(isinstance(payload, dict) and payload.get("model") == self.model
                    and payload.get("stream", False) is False and payload.get("n", 1) == 1,
                    "unexpected model, streaming or multiple completions")
            require(not set(payload) - {"model", "messages", "max_tokens", "max_completion_tokens", "temperature",
                "top_p", "top_k", "presence_penalty", "response_format", "n", "stream", "chat_template_kwargs", "seed", "stop"},
                "unsupported completion field")
            require(isinstance(payload.get("messages"), list) and 1 <= len(payload["messages"]) <= 16,
                    "invalid message list")
            require(not ("max_tokens" in payload and "max_completion_tokens" in payload), "ambiguous output allowance")
            field = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
            cap = payload.get(field, self.profile["max_output_tokens"])
            require(type(cap) is int and 1 <= cap <= self.profile["max_output_tokens"], "completion allowance exceeded")
            payload[field] = cap
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            data = json.dumps(payload).encode()
            require(len(data) <= self.profile["max_request_bytes"], "request byte bound exceeded")
            require(self.state["requests"] < self.profile["max_requests"]
                    and self.state["request_bytes"] + len(data) <= self.profile["max_total_request_bytes"],
                    "pilot model budget exhausted")
            self.state["requests"] += 1
            self.state["request_bytes"] += len(data)
            self.state["inflight"] = True
            self.save()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.api_base + "/chat/completions", data=data, headers=headers)
        uncertain = False
        try:
            with self.opener.open(request, timeout=self.profile["request_timeout_seconds"]) as response:
                raw = response.read(self.profile["max_response_bytes"] + 1)
            require(len(raw) <= self.profile["max_response_bytes"], "response byte bound exceeded")
            result = json.loads(raw)
            require(result.get("model") == self.model and len(result.get("choices", [])) == 1,
                    "response model or completion count differs")
            require(result["choices"][0].get("finish_reason") == "stop", "incomplete or truncated model response")
            message = result["choices"][0].get("message", {})
            require(isinstance(message.get("content"), str) and message["content"].strip(), "empty model answer")
            usage = result.get("usage", {})
            with self.guard:
                if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                    self.state["prompt_tokens"] += usage["prompt_tokens"]
                    self.state["completion_tokens"] += usage["completion_tokens"]
                else:
                    self.state["usage_missing"] += 1
            return result
        except urllib.error.HTTPError as exc:
            uncertain = exc.code >= 500 or exc.code == 408
            exc.close()
            raise ValueError("local model rejected the request; inspect bounded operator diagnostics") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            uncertain = True
            raise ValueError("local model connection failed; upstream completion is uncertain") from None
        finally:
            with self.guard:
                self.state["inflight"] = False
                self.state["uncertain"] = self.state["uncertain"] or uncertain
                self.save()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_handler(model, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(403)
                return
            try:
                self.connection.settimeout(model.profile["request_timeout_seconds"])
                length = int(self.headers.get("Content-Length", "0"))
                require(0 < length <= model.profile["max_request_bytes"] and not self.headers.get("Transfer-Encoding"),
                        "invalid request framing")
                payload = json.loads(self.rfile.read(length))
                require(isinstance(payload, dict), "invalid completion object")
                stream = payload.get("stream", False)
                require(type(stream) is bool, "invalid streaming flag")
                if stream:
                    # GraphRAG's reducer consumes SSE. Complete the bounded upstream
                    # call first, then frame that one accepted answer for its client.
                    options = payload.pop("stream_options", None)
                    require(options in (None, {"include_usage": True}), "unsupported stream options")
                    payload["stream"] = False
                response = model.completion(payload)
                if stream:
                    common = {"id": response.get("id", "pilot-completion"), "created": response.get("created", 0),
                              "model": response["model"], "object": "chat.completion.chunk"}
                    chunks = [{**common, "choices": [{"index": 0, "finish_reason": None,
                        "delta": {"role": "assistant", "content": response["choices"][0]["message"]["content"]}}]},
                        {**common, "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
                         "usage": response.get("usage", {})}]
                    result = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                              + "data: [DONE]\n\n").encode()
                else:
                    result = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
            except (ValueError, KeyError, TypeError, OSError):
                # Never send upstream exceptions, credentials or payloads to logs.
                with model.guard:
                    model.state["failed_requests"] += 1
                    model.save()
                self.send_error(400, "Pilot request failed validation or local-model acceptance")
    return Handler


def artifact_size(root):
    total = 0
    for path in root.rglob("*"):
        require(not path.is_symlink(), "pilot artifacts contain a symlink")
        if path.is_file():
            total += path.stat().st_size
    return total


def run(args):
    local_api(args.api_base)
    root, record, profile, state = verify(args.pilot)
    lock_path = root / "pilot.lock"
    require(not lock_path.is_symlink(), "unsafe pilot lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        root, record, profile, state = verify(root)
        if args.action == "index":
            require(state["index_status"] == "not-run", "index already attempted; preserve it and prepare a new pilot")
        else:
            require(state["index_status"] == "complete", "complete the index before querying")
        questions = read_json(root / "questions.json")["questions"]
        if args.action == "query":
            require(args.question_id in {q["id"] for q in questions}, "unknown prepared question")
        # Resolving a venv's python symlink would select the base interpreter.
        runtime = Path(args.runtime_python).absolute()
        require(runtime.is_file() and os.access(runtime, os.X_OK), "supply the isolated GraphRAG interpreter")
        verify_tokenizers(args.tokenizer_cache)
        contract = read_json(root / "candidate-contract.json")
        model = LocalModel(root, profile, state, contract["served_model"], args.api_base,
                           os.environ.get("WORKSTATION_AGENT_API_KEY"))
        token = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(model, token))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        run_root = root / "runs" / secrets.token_hex(8)
        run_root.mkdir(mode=0o700)
        command = [str(runtime), "-B", str(WORKER), args.action, str(root), str(run_root)]
        if args.action == "query":
            command += [args.question_id, args.mode]
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT", "LANG", "LC_ALL"}}
        env.update({"HOME": str(run_root), "GRAPHRAG_PILOT_URL": f"http://127.0.0.1:{server.server_port}/v1",
                    "GRAPHRAG_PILOT_KEY": token, "TIKTOKEN_CACHE_DIR": str(Path(args.tokenizer_cache).resolve()),
                    "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
                    "NUMEXPR_NUM_THREADS": "2", "NUMBA_NUM_THREADS": "2", "DO_NOT_TRACK": "1",
                    "LITELLM_LOG": "ERROR", "PYTHONDONTWRITEBYTECODE": "1"})
        started = time.monotonic()
        before = state["requests"]
        failures_before = state["failed_requests"]
        status = "failed"
        process = None
        try:
            if args.action == "index":
                state["index_status"] = "running"
                model.save()
            process = subprocess.Popen(command, env=env, cwd=root, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            while process.poll() is None:
                if time.monotonic() - started > profile["run_timeout_seconds"] or artifact_size(root) > profile["max_artifact_bytes"]:
                    raise ValueError("pilot run exceeded its time or sampled artifact bound")
                time.sleep(0.2)
            require(process.returncode == 0, "GraphRAG worker failed; inspect the private worker status")
            result = read_json(run_root / "result.json", profile["max_response_bytes"])
            require(result.get("status") == "complete", "worker did not complete")
            require(state["failed_requests"] == failures_before and not state["uncertain"],
                    "model calls failed or remain uncertain; worker output is not accepted")
            require(artifact_size(root) <= profile["max_artifact_bytes"], "pilot artifact bound exceeded")
            status = "complete"
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            with model.guard:
                if state["inflight"]:
                    state["uncertain"] = True
                if args.action == "index":
                    state["index_status"] = "complete" if status == "complete" else "failed"
                model.save()
            server.shutdown()
            server.server_close()
            write_json(run_root / "receipt.json", {"schema": 1, "status": status, "action": args.action,
                "question_id": getattr(args, "question_id", None), "mode": getattr(args, "mode", None),
                "seconds": time.monotonic() - started, "requests": state["requests"] - before,
                "cumulative_requests": state["requests"], "uncertain": state["uncertain"],
                "candidate_pointer_sha256": contract["pointer_sha256"],
                "pilot_sha256": digest((root / "pilot.json").read_bytes())})
        print("Completed bounded pilot operation; review answer quality separately:", run_root)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("output")
    prepare_parser.add_argument("documents", nargs="+")
    prepare_parser.add_argument("--contract", required=True)
    prepare_parser.add_argument("--contract-sha256", required=True)
    for action in ("index", "query"):
        sub = commands.add_parser(action)
        sub.add_argument("pilot")
        sub.add_argument("--runtime-python", required=True)
        sub.add_argument("--api-base", required=True)
        sub.add_argument("--tokenizer-cache", required=True)
        if action == "query":
            sub.add_argument("question_id")
            sub.add_argument("--mode", choices=["global", "direct"], default="global")
    check = commands.add_parser("verify")
    check.add_argument("pilot")
    tokenizer_parser = commands.add_parser("stage-tokenizers")
    tokenizer_parser.add_argument("output")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args)
        elif args.action == "stage-tokenizers":
            stage_tokenizers(args.output)
        elif args.action == "verify":
            verify(args.pilot)
            print("Pilot snapshot/configuration verified; no model request performed.")
        else:
            run(args)
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("GraphRAG pilot refused or failed; retain its private status and verify inputs, budget and endpoint.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
