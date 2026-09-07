"""
Talking to the GPU tier.

One shared httpx client for the whole process (connection pooling matters a lot
when every request is a long-lived streamed completion), plus the logic that
rewrites a customer request into an upstream request.
"""
import json
from typing import Any, AsyncIterator

import httpx

from .registry import ModelRoute
from .security import tenant_cache_salt

_client: httpx.AsyncClient | None = None


async def connect(timeout_s: float) -> None:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("upstream client not initialised")
    return _client


def build_payload(route: ModelRoute, body: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Rewrite the customer's request body into what the upstream expects."""
    payload = dict(body)

    # The customer uses our public name; the upstream uses its own model id.
    payload["model"] = route.upstream_model

    if route.kind == "vllm":
        # Tenant isolation of the prefix/KV cache (see security.tenant_cache_salt).
        # This is a vLLM extension field, so only send it to vLLM upstreams.
        payload["cache_salt"] = tenant_cache_salt(user_id)

    if payload.get("stream"):
        # Ask the upstream to append a final chunk containing token usage,
        # otherwise streamed requests would be unbillable.
        opts = dict(payload.get("stream_options") or {})
        opts["include_usage"] = True
        payload["stream_options"] = opts

    # EXTEND: clamp `max_tokens` to a per-plan ceiling here, and strip any
    # parameters you do not want customers to set (e.g. `logprobs`, or
    # sampling params that let them fingerprint the backend).
    return payload


def build_headers(route: ModelRoute) -> dict[str, str]:
    """The customer's key NEVER goes upstream. We present our own credential."""
    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"
    return headers


def extract_usage_from_sse(chunk_text: str, sink: dict) -> None:
    """Scan streamed SSE lines for the final `usage` object and stash it.

    OpenAI-compatible servers send usage in the last data frame when
    stream_options.include_usage is set. We parse it out purely so metering
    works; the bytes themselves are passed through to the client untouched.
    """
    for line in chunk_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue  # partial frame; the caller re-buffers
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            sink.update(obj["usage"])


async def stream_sse(
    url: str, payload: dict, headers: dict, usage_sink: dict
) -> AsyncIterator[bytes]:
    """Proxy an SSE stream through to the client, sniffing usage on the way."""
    buffer = ""
    async with client().stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code >= 400:
            body = await resp.aread()
            yield b"data: " + json.dumps(
                {"error": {"message": body.decode("utf-8", "replace"),
                           "code": resp.status_code}}
            ).encode() + b"\n\n"
            return
        async for chunk in resp.aiter_bytes():
            yield chunk  # pass through first: never delay the customer
            # SSE frames can split across chunks, so keep the tail around.
            buffer += chunk.decode("utf-8", "replace")
            if "\n" in buffer:
                complete, _, buffer = buffer.rpartition("\n")
                extract_usage_from_sse(complete, usage_sink)
