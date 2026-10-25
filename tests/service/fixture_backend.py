"""Deterministic OpenAI fixture; no inference or tool execution."""
import asyncio
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
state = {"active_streams": 0, "unavailable_calls": 0}


@app.get("/fixture/status")
async def status():
    return state


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "Qwen3.8-27B-FP8", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    data = await request.json()
    if data["messages"][0].get("content") == "unavailable-fixture":
        state["unavailable_calls"] += 1
        return JSONResponse({"error": {"message": "fixture unavailable", "type": "service_unavailable"}}, status_code=503)
    if data["messages"][0].get("content") == "slow-fixture":
        await asyncio.sleep(4)
    message = {"role": "assistant", "content": "fixture"}
    reason = "stop"
    if data.get("tools"):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-fixture", "type": "function",
                    "function": {"name": "fixture_tool", "arguments": '{"value":7}'}}]}
        reason = "tool_calls"
    result = {"id": "chatcmpl-fixture", "object": "chat.completion", "created": 1,
              "model": data["model"], "choices": [{"index": 0, "message": message, "finish_reason": reason}],
              "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
    if data.get("stream"):
        async def chunks():
            state["active_streams"] += 1
            try:
                slow = data["messages"][0].get("content") == "disconnect-fixture"
                for text in (["fixture"] * 100 if slow else ("fix", "ture")):
                    chunk = {**result, "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                    yield "data: " + json.dumps(chunk) + "\n\n"
                    await asyncio.sleep(0.2 if slow else 0.02)
                yield 'data: [DONE]\n\n'
            finally:
                state["active_streams"] -= 1
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return result
