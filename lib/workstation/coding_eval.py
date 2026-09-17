"""Evaluate bounded coding and tool-call responses without executing model text.

The installed Bridge worker deliberately accepts only named build recipes, so it
is not an execution authority for this corpus.  Code tasks retain their required
compile/unit-test contract but report unavailable until a separately reviewed
isolated evaluator supplies a sealed attestation.  This program never calls a
shell, opens the network, or reads credentials.
"""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path


MAX_FILE = 2 * 1024 * 1024
MAX_RESPONSE = 64 * 1024
ALLOWED_KINDS = frozenset(("structured", "tool", "negative", "code"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("evaluation input must be a regular bounded JSON file")
    data = path.read_bytes()
    try:
        return json.loads(data), digest(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("evaluation input is not valid JSON") from error


def corpus_path():
    return Path(__file__).with_name("coding_tasks.json")


def load_corpus(path=None):
    value, raw_hash = read_json(corpus_path() if path is None else path)
    if (value.get("schema") != 1 or value.get("kind") != "workstation-coding-evaluation-corpus"
            or not isinstance(value.get("version"), str) or not isinstance(value.get("tasks"), list)
            or not 8 <= len(value["tasks"]) <= 12):
        raise ValueError("unsupported coding evaluation corpus")
    ids = set()
    templates = {}
    for task in value["tasks"]:
        if (not isinstance(task, dict) or set(task) != {"id", "kind", "template", "expected"}
                or not isinstance(task["id"], str) or task["id"] in ids or task["kind"] not in ALLOWED_KINDS
                or not isinstance(task["template"], str) or not task["template"] or len(task["template"]) > 2048
                or not isinstance(task["expected"], dict)):
            raise ValueError("invalid coding evaluation task")
        ids.add(task["id"])
        templates[task["id"]] = digest(task["template"].encode())
    return value, raw_hash, templates


def number(value, name):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def generation_settings(value):
    """Keep every generation setting explicit for reproducible comparisons."""
    if not isinstance(value, dict) or set(value) != {"model", "temperature", "max_output_tokens", "seed"}:
        raise ValueError("generation settings must be complete")
    if not isinstance(value["model"], str) or not value["model"] or len(value["model"]) > 256:
        raise ValueError("invalid generation model")
    if not isinstance(value["temperature"], (int, float)) or isinstance(value["temperature"], bool) or not math.isfinite(value["temperature"]) or not 0 <= value["temperature"] <= 2:
        raise ValueError("invalid generation temperature")
    if (not isinstance(value["max_output_tokens"], int) or isinstance(value["max_output_tokens"], bool)
            or not 1 <= value["max_output_tokens"] <= 131072):
        raise ValueError("invalid generation output token bound")
    if (not isinstance(value["seed"], int) or isinstance(value["seed"], bool)
            or not 0 <= value["seed"] <= 2**63 - 1):
        raise ValueError("invalid generation seed")
    return value


def response_map(value, corpus_hash):
    if (value.get("schema") != 1 or value.get("kind") != "workstation-coding-evaluation-responses"
            or value.get("corpus_sha256") != corpus_hash or set(value) != {"schema", "kind", "corpus_sha256", "generation", "responses"}
            or not isinstance(value["generation"], dict) or not isinstance(value["responses"], list)):
        raise ValueError("responses do not bind the selected corpus")
    generation_settings(value["generation"])
    rows = {}
    for row in value["responses"]:
        if (not isinstance(row, dict) or set(row) - {"task_id", "attempts", "latency_ms", "input_tokens", "output_tokens", "response", "protocol"}
                or not isinstance(row.get("task_id"), str) or row["task_id"] in rows
                or not isinstance(row.get("attempts"), int) or not 1 <= row["attempts"] <= 3
                or not isinstance(row.get("response"), str) or len(row["response"].encode()) > MAX_RESPONSE):
            raise ValueError("invalid task response")
        if "protocol" in row and (not isinstance(row["protocol"], dict) or len(canonical(row["protocol"])) > MAX_RESPONSE):
            raise ValueError("protocol response exceeds its bound")
        for name in ("latency_ms", "input_tokens", "output_tokens"):
            number(row.get(name), name)
        rows[row["task_id"]] = row
    return rows


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON constant")


def parse_object(text):
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except json.JSONDecodeError:
        raise ValueError("response is not a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("response is not a JSON object")
    return value


def protocol_tool(value, expected):
    """Validate one complete tool call from real OpenAI-compatible wire shapes.

    This is an offline assertion, never permission to invoke the named tool.
    A disconnect, cancellation or token-limit finish is incomplete, even when
    the arguments parsed before the interruption. Call IDs may differ by run.
    """
    if (not isinstance(value, dict) or set(value) != {"non_streaming", "streaming"}
            or not isinstance(value["streaming"], str) or len(canonical(value)) > MAX_RESPONSE):
        raise ValueError("invalid bounded protocol pair")

    def choice(message):
        if not isinstance(message, dict) or "error" in message:
            raise ValueError("protocol error response")
        rows = message.get("choices")
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or type(rows[0].get("index")) is not int or rows[0]["index"] != 0):
            raise ValueError("require exactly one protocol choice")
        return rows[0]

    def checked_call(call):
        if (not isinstance(call, dict) or call.get("type") != "function"
                or not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 256
                or not isinstance(call.get("function"), dict)):
            raise ValueError("incomplete protocol tool identity")
        function = call["function"]
        if not isinstance(function.get("arguments"), str):
            raise ValueError("protocol arguments must be a complete JSON string")
        actual = {"tool": function.get("name"), "arguments": parse_object(function["arguments"])}
        if not json_equal(actual, expected):
            raise ValueError("protocol tool selection or arguments differ")

    whole = choice(value["non_streaming"])
    message = whole.get("message")
    if (whole.get("finish_reason") != "tool_calls" or not isinstance(message, dict)
            or message.get("content") not in (None, "") or not isinstance(message.get("tool_calls"), list)
            or len(message["tool_calls"]) != 1):
        raise ValueError("non-streaming tool call is incomplete or leaks content")
    checked_call(message["tool_calls"][0])
    body = value["streaming"].replace("\r\n", "\n")
    if not body.endswith("\n\n"):
        raise ValueError("incomplete SSE frame")
    call = {"function": {"name": "", "arguments": ""}}
    finished = done = False
    for frame in body.split("\n\n"):
        if not frame:
            continue
        lines = [line for line in frame.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        if done or any(not line.startswith("data:") for line in lines):
            raise ValueError("unexpected SSE frame")
        data = "\n".join(line[5:].lstrip(" ") for line in lines)
        if data == "[DONE]":
            if not finished:
                raise ValueError("tool call lacks a successful finish")
            done = True
            continue
        chunk = parse_object(data)
        if finished and chunk.get("choices") == [] and isinstance(chunk.get("usage"), dict) and "error" not in chunk:
            continue
        row = choice(chunk)
        delta = row.get("delta")
        if finished or not isinstance(delta, dict) or delta.get("content") not in (None, ""):
            raise ValueError("stream leaks content or continues after finish")
        calls = delta.get("tool_calls", [])
        if not isinstance(calls, list) or len(calls) > 1:
            raise ValueError("unexpected streamed tool count")
        for item in calls:
            if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != 0:
                raise ValueError("unexpected streamed tool index")
            for key in ("id", "type"):
                if key in item:
                    if key in call and call[key] != item[key]:
                        raise ValueError("streamed tool identity changed")
                    call[key] = item[key]
            function = item.get("function", {})
            if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                raise ValueError("invalid streamed function")
            for key, fragment in function.items():
                if not isinstance(fragment, str):
                    raise ValueError("invalid streamed fragment")
                call["function"][key] += fragment
        if row.get("finish_reason") is not None:
            if row["finish_reason"] != "tool_calls":
                raise ValueError("tool stream was truncated or cancelled")
            finished = True
    if not done:
        raise ValueError("tool stream disconnected before DONE")
    checked_call(call)


def json_equal(actual, expected):
    """Compare JSON values without Python's boolean-as-integer coercion."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(json_equal(actual[key], expected[key]) for key in expected)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(json_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def evaluate_task(task, response):
    """Return a redacted deterministic result.  Never retain response text."""
    result = {"task_id": task["id"], "kind": task["kind"], "attempts": response["attempts"],
              "latency_ms": response.get("latency_ms"), "input_tokens": response.get("input_tokens"),
              "output_tokens": response.get("output_tokens"),
              "response_sha256": digest(response["response"].encode())}
    if task["kind"] == "tool":
        result["protocol_status"] = "unknown-not-collected"
        if "protocol" in response:
            result["protocol_sha256"] = digest(canonical(response["protocol"]))
            result["protocol_status"] = "failed"
    try:
        if "protocol" in response and task["kind"] != "tool":
            raise ValueError("protocol pairs are supported only for tool tasks")
        actual = parse_object(response["response"])
        if task["kind"] == "structured":
            if not json_equal(actual, task["expected"]):
                raise ValueError("structured fields differ")
            result["status"] = "passed"
        elif task["kind"] == "tool":
            if set(actual) != {"tool", "arguments"} or not json_equal(actual, task["expected"]):
                raise ValueError("tool selection or arguments differ")
            if "protocol" in response:
                protocol_tool(response["protocol"], task["expected"])
                result["protocol_status"] = "passed"
            result["status"] = "passed"
        elif task["kind"] == "negative":
            if not json_equal(actual, task["expected"]):
                raise ValueError("negative case did not refuse exactly")
            result["status"] = "passed"
        else:
            if set(actual) != {"language", "source"} or actual["language"] != task["expected"]["language"]:
                raise ValueError("code language differs")
            source = actual["source"]
            if not isinstance(source, str) or len(source.encode()) > MAX_RESPONSE or task["expected"]["contains"] not in source:
                raise ValueError("required code shape is absent")
            # Parsing detects malformed Python, but is not compilation or a
            # unit test.  No source is run without a future sealed executor.
            ast.parse(source, mode="exec")
            result["status"] = "unavailable-no-reviewed-isolated-executor"
            result["required_verification"] = task["expected"]["verification"]
    except (ValueError, TypeError, SyntaxError) as error:
        result["status"] = "failed"
        result["failure"] = type(error).__name__
    return result


def evaluate(corpus_file, responses_file):
    corpus, corpus_hash, templates = load_corpus(corpus_file)
    responses, response_hash = read_json(responses_file)
    rows = response_map(responses, corpus_hash)
    results = []
    for task in corpus["tasks"]:
        row = rows.pop(task["id"], None)
        if row is None:
            results.append({"task_id": task["id"], "kind": task["kind"], "status": "unknown-missing-response"})
            if task["kind"] == "tool":
                results[-1]["protocol_status"] = "unknown-not-collected"
        else:
            results.append(evaluate_task(task, row))
    if rows:
        raise ValueError("responses include unknown task IDs")
    passed = sum(row["status"] == "passed" for row in results)
    failed = sum(row["status"] == "failed" for row in results)
    unavailable = sum(row["status"] == "unavailable-no-reviewed-isolated-executor" for row in results)
    unknown = sum(row["status"].startswith("unknown") for row in results)
    protocol = {status: sum(row.get("protocol_status") == status for row in results)
                for status in ("passed", "failed", "unknown-not-collected")}
    status = "passed" if failed == unavailable == unknown == 0 else "incomplete-unqualified"
    return {"schema": 1, "kind": "workstation-coding-evaluation", "status": status,
            "corpus_version": corpus["version"], "corpus_sha256": corpus_hash,
            "template_sha256": templates, "responses_sha256": response_hash,
            "generation": responses["generation"], "results": results, "successes": passed,
            "failures": failed, "unavailable": unavailable, "unknown": unknown, "protocol": protocol,
            "execution": "unavailable: Bridge worker only accepts named build recipes; response text was not executed",
            "limitations": ["Task text, model responses and tool payloads are not retained in this result.",
                            "Static parsing is not a compile or unit-test result.",
                            "This coding suite supplements, and does not replace, numerical quality checks."]}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("responses")
    parser.add_argument("output")
    parser.add_argument("--corpus", default=str(corpus_path()))
    args = parser.parse_args()
    write(args.output, evaluate(args.corpus, args.responses))


if __name__ == "__main__":
    main()
