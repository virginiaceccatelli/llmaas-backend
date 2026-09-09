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
    payload = dict(body)

    # The customer uses our public name; the upstream uses its own model id.
    payload["model"] = route.upstream_model

    if route.kind == "vllm":
        # Tenant isolation of the prefix/KV cache (see security.tenant_cache_salt) - vLLM
        payload["cache_salt"] = tenant_cache_salt(user_id)

    if payload.get("stream"):
        opts = dict(payload.get("stream_options") or {})
        opts["include_usage"] = True
        payload["stream_options"] = opts

    # EXTEND: clamp `max_tokens` to a per-plan ceiling here, and strip any
    # parameters you do not want customers to set (e.g. `logprobs`, or
    # sampling params that let them fingerprint the backend).
    return payload


def build_headers(route: ModelRoute) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if route.api_key:
        headers["Authorization"] = f"Bearer {route.api_key}"
    return headers


def describe_error(status_code: int, body: str) -> str:
    stripped = body.lstrip()
    if stripped.startswith("<") or "<html" in stripped[:200].lower():
        if status_code in (401, 403):
            return (
                "upstream rejected our credentials — the broker's API key for "
                "this model is missing, expired, or not permitted"
            )
        return f"upstream returned an error page (HTTP {status_code})"

    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return f"upstream error (HTTP {status_code}): {body[:200]}"

    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return f"upstream error: {err['message'][:300]}"
    if isinstance(err, str):
        return f"upstream error: {err[:300]}"
    return f"upstream error (HTTP {status_code})"


def extract_usage_from_sse(chunk_text: str, sink: dict) -> None:
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
