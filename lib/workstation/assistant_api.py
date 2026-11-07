"""Assistant API response profile ``assistant-api-v1``.

This profile describes what browser and coding clients need from a chat
completion: reasoning kept apart from the final answer, complete structured
tool calls, and a finish reason that proves the output was not cut off. It is
separate from the frozen serving benchmark contract, which expects inline
thinking in the full assistant text and refuses ``tool_calls`` and
``reasoning_content``. Neither profile replaces the other.

The functions only inspect bytes already received. They never send requests,
run tools or execute model text.
"""
import json

from coding_eval import MAX_RESPONSE, canonical, digest, parse_object


PROFILE = "assistant-api-v1"
THINK_END = "</think>"
# Classified outcomes. Only "final" and "tool_calls" are usable answers.
OUTCOMES = ("final", "tool_calls", "truncated", "unfinished-thinking", "cancelled", "error")


def _choice(message):
    if not isinstance(message, dict) or "error" in message:
        raise ValueError("error response")
    rows = message.get("choices")
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or type(rows[0].get("index", 0)) is not int or rows[0].get("index", 0) != 0):
        raise ValueError("require exactly one choice")
    return rows[0]


def _string(value, name):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value


def segment(content, reasoning, thinking):
    """Return (reasoning, final, status) for one assistant message.

    With a reasoning parser the runtime returns ``reasoning_content`` and a
    clean ``content``. Without one, the pinned template has already opened the
    thinking block, so ``content`` carries reasoning, a bare closing tag and
    the answer. A thinking-enabled response without a closing tag never
    reached its final answer.
    """
    content, reasoning = _string(content, "content"), _string(reasoning, "reasoning_content")
    if reasoning:
        if THINK_END in content:
            raise ValueError("reasoning leaked into the final answer")
        return reasoning, content, "separated"
    if THINK_END in content:
        head, _, tail = content.rpartition(THINK_END)
        return head.replace("<think>", "", 1).strip(), tail.strip(), "inline"
    if thinking:
        return content, "", "unfinished-thinking"
    return "", content, "no-thinking"


def _call(call):
    if (not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
            or not 1 <= len(call["id"]) <= 256 or not isinstance(call.get("function"), dict)
            or not isinstance(call["function"].get("name"), str) or not call["function"]["name"]
            or not isinstance(call["function"].get("arguments"), str)):
        raise ValueError("incomplete tool call identity")
    return {"id": call["id"], "name": call["function"]["name"],
            "arguments": parse_object(call["function"]["arguments"])}


def _classify(finish, final, status, calls):
    if finish == "length":
        return "truncated"
    if finish == "tool_calls":
        # A complete structured call ends the thinking block (the qwen3
        # detector treats <tool_call> as the end of reasoning).
        if not calls:
            raise ValueError("tool_calls finish without a call")
        return "tool_calls"
    if status == "unfinished-thinking":
        return "unfinished-thinking"
    if calls:
        raise ValueError("tool call without a tool_calls finish")
    if finish == "stop" and final.strip():
        return "final"
    if finish == "stop":
        return "truncated"  # natural stop with an empty final answer is not usable
    raise ValueError("unsupported finish reason")


def response(value, thinking):
    """Validate one non-streaming chat completion under assistant-api-v1."""
    row = _choice(value)
    message = row.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing message")
    calls = [_call(call) for call in (message.get("tool_calls") or [])]
    reasoning, final, status = segment(message.get("content"), message.get("reasoning_content"), thinking)
    if calls and final.strip():
        raise ValueError("tool call response also carries final text")
    outcome = _classify(row.get("finish_reason"), final, status, calls)
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    return {"profile": PROFILE, "outcome": outcome, "finish_reason": row.get("finish_reason"),
            "segmentation": status, "final": final, "reasoning_sha256": digest(reasoning.encode()),
            "reasoning_chars": len(reasoning), "tool_calls": calls,
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def stream(body, thinking):
    """Assemble and validate an SSE stream, including fragmented tool arguments.

    A stream without ``[DONE]`` or a finish reason is a disconnect, so it is
    classified as cancelled rather than parsed as a partial answer.
    """
    if not isinstance(body, str) or len(body.encode()) > 4 * MAX_RESPONSE:
        raise ValueError("invalid bounded stream")
    body = body.replace("\r\n", "\n")
    content, reasoning, finish, done, usage = [], [], None, False, {}
    calls = {}
    for frame in body.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        if done or any(not line.startswith("data:") for line in lines):
            raise ValueError("unexpected SSE frame")
        data = "\n".join(line[5:].lstrip(" ") for line in lines)
        if data == "[DONE]":
            done = True
            continue
        chunk = parse_object(data)
        if chunk.get("choices") == [] and isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
            continue
        row = _choice(chunk)
        if finish is not None:
            raise ValueError("stream continued after finish")
        delta = row.get("delta") or {}
        if not isinstance(delta, dict):
            raise ValueError("invalid delta")
        content.append(_string(delta.get("content"), "content"))
        reasoning.append(_string(delta.get("reasoning_content"), "reasoning_content"))
        for item in delta.get("tool_calls") or []:
            if not isinstance(item, dict) or type(item.get("index")) is not int or not 0 <= item["index"] < 8:
                raise ValueError("invalid streamed tool index")
            call = calls.setdefault(item["index"], {"id": None, "type": None, "function": {"name": "", "arguments": ""}})
            for key in ("id", "type"):
                if item.get(key) is not None:
                    if call[key] not in (None, item[key]):
                        raise ValueError("streamed tool identity changed")
                    call[key] = item[key]
            function = item.get("function") or {}
            if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
                raise ValueError("invalid streamed function")
            for key, fragment in function.items():
                if fragment is not None:
                    call["function"][key] += _string(fragment, key)
        if row.get("finish_reason") is not None:
            finish = row["finish_reason"]
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
    if not done or finish is None:
        return {"profile": PROFILE, "outcome": "cancelled", "finish_reason": finish, "final": "",
                "tool_calls": [], "segmentation": "incomplete-stream"}
    message = {"content": "".join(content), "reasoning_content": "".join(reasoning),
               "tool_calls": [calls[index] for index in sorted(calls)]}
    return response({"choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}, thinking)


def followup_messages(history, assistant, results):
    """Build the tool-result follow-up exactly as an OpenAI-compatible client does.

    Each result must answer a call ID from the assistant turn, once. Prior
    reasoning is not replayed: the pinned Open WebUI drops it and the pinned
    template renders history without it.
    """
    ids = [call["id"] for call in assistant["tool_calls"]]
    if sorted(ids) != sorted(results) or len(set(ids)) != len(ids):
        raise ValueError("tool results must answer each call ID exactly once")
    turn = {"role": "assistant", "content": "", "tool_calls": [
        {"id": call["id"], "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], separators=(",", ":"))}}
        for call in assistant["tool_calls"]]}
    return list(history) + [turn] + [{"role": "tool", "tool_call_id": call_id,
                                      "content": json.dumps(results[call_id], separators=(",", ":"))} for call_id in ids]


def fingerprint(value):
    return digest(canonical(value))
