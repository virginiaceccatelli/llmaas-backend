"""
Mock vLLM server: mimics the OpenAI-compatible API that real vLLM exposes.
Swap this out for a real vLLM instance later by changing one URL in the backend.

Implements:
  GET  /v1/models
  POST /v1/chat/completions   (streaming + non-streaming)
"""
import json
import time
import uuid
import asyncio

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="mock-vllm")

# Pretend these models are loaded, each on its own "GPU" in the real setup.
MODELS = ["mock-llama-8b", "mock-mistral-7b"]


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "mock-llama-8b"
    messages: list[Message]
    stream: bool = False
    max_tokens: int | None = None


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "mock"} for m in MODELS
        ],
    }


def _fake_reply(req: ChatRequest) -> str:
    last = req.messages[-1].content if req.messages else ""
    return f"[{req.model}] You said: {last!r}. This is a canned mock response."


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    reply = _fake_reply(req)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Rough token counts so the usage-logging path has real numbers to store.
    prompt_tokens = sum(len(m.content.split()) for m in req.messages)
    completion_tokens = len(reply.split())

    if req.stream:
        async def event_stream():
            for word in reply.split():
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {"index": 0, "delta": {"content": word + " "},
                         "finish_reason": None}
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.03)  # simulate token-by-token latency
            done = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}