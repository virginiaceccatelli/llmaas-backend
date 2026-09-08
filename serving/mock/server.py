"""
Offline stand-in for vLLM / the Hugging Face router.

Speaks enough of the OpenAI API for the broker to be exercised end-to-end with
no GPU, no network and no credits. Use it when your HF free tier is exhausted
or you are working offline.

    uvicorn server:app --app-dir serving/mock --port 8000

Then point the broker at it:  MODELS_FILE=./serving/models.mock.yaml

It is NOT part of any deployment — the gateway VM talks to real vLLM.
"""
import asyncio
import json
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="mock-upstream")

MODELS = ["mock-qwen-instruct", "mock-qwen-coder"]


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = MODELS[0]
    messages: list[Message] = []
    stream: bool = False
    max_tokens: int | None = None
    stream_options: dict | None = None
    # vLLM extension the broker injects for tenant cache isolation. Accepting
    # it here proves the broker is actually sending it.
    cache_salt: str | None = None


def _reply(req: ChatRequest) -> str:
    last = req.messages[-1].content if req.messages else ""
    if "pong" in last.lower():
        return "pong"
    return f"[{req.model}] mock reply to: {last[:80]}"


# Records the last request body so tests can assert what the broker actually
# sent (e.g. that cache_salt arrives, and differs per tenant). Dev tool only.
_last_request: dict = {}
_seq = 0


@app.get("/debug/last")
async def debug_last():
    return _last_request


@app.get("/v1/models")
async def list_models():
    return {"object": "list",
            "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    global _seq
    _seq += 1
    _last_request.clear()
    _last_request.update(req.model_dump())
    _last_request["seq"] = _seq
    reply = _reply(req)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    usage = {
        "prompt_tokens": sum(len(m.content.split()) for m in req.messages),
        "completion_tokens": len(reply.split()),
    }
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    if req.stream:
        include_usage = bool((req.stream_options or {}).get("include_usage"))

        async def sse():
            for word in reply.split():
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": created,
                         "model": req.model,
                         "choices": [{"index": 0, "delta": {"content": word + " "},
                                      "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.02)     # simulate token-by-token latency
            yield ("data: " + json.dumps(
                {"id": cid, "object": "chat.completion.chunk", "created": created,
                 "model": req.model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n")
            if include_usage:
                # Final usage frame — this is what the broker's metering reads.
                yield ("data: " + json.dumps(
                    {"id": cid, "object": "chat.completion.chunk", "created": created,
                     "model": req.model, "choices": [], "usage": usage}) + "\n\n")
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return {"id": cid, "object": "chat.completion", "created": created, "model": req.model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": usage}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
