"""
The data plane: OpenAI-compatible inference endpoints.

Request flow, end to end:
    client key -> authenticate -> rate limit -> resolve model to an upstream
               -> rewrite body (upstream model id + tenant cache salt)
               -> proxy to vLLM -> record usage

This is the piece Envoy AI Gateway eventually replaces. Keeping it small and
boring makes that swap cheap.
"""
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from .. import metering, ratelimit, registry, upstream
from ..config import Settings, get_settings
from ..security import require_key

router = APIRouter(prefix="/v1", tags=["inference"])


@router.get("/models")
async def list_models(key: asyncpg.Record = Depends(require_key)):
    """Advertise our public model names — never the upstream ids or URLs."""
    return {
        "object": "list",
        "data": [
            {"id": r.name, "object": "model", "owned_by": "llmaas"}
            for r in registry.registry().all()
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    key: asyncpg.Record = Depends(require_key),
    settings: Settings = Depends(get_settings),
):
    await ratelimit.check(str(key["id"]), settings.rate_limit_per_min)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be a JSON object")

    public_model = body.get("model")
    route = registry.registry().get(public_model) if public_model else None
    if route is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"unknown model {public_model!r}; see GET /v1/models",
        )

    user_id = key["user_id"]
    payload = upstream.build_payload(route, body, user_id)
    headers = upstream.build_headers(route)
    url = f"{route.base_url}/chat/completions"

    # --- streaming -------------------------------------------------------
    if body.get("stream"):
        usage_sink: dict = {}

        async def relay():
            try:
                async for chunk in upstream.stream_sse(url, payload, headers, usage_sink):
                    yield chunk
            finally:
                # Runs even if the client disconnects mid-stream, so partial
                # generations still get billed.
                await metering.record(key["id"], route.name, usage_sink)

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- non-streaming ---------------------------------------------------
    resp = await upstream.client().post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        # Surface the upstream's error but not its identity/URL.
        raise HTTPException(resp.status_code, f"upstream error: {resp.text[:500]}")

    data = resp.json()
    await metering.record(key["id"], route.name, data.get("usage"))

    # Present our public model name back to the caller, not the upstream id.
    data["model"] = route.name
    return data


@router.post("/completions")
async def completions(*_, **__):
    """Legacy text-completions endpoint.

    EXTEND: if customers need it, copy chat_completions and point at
    `{base_url}/completions`. Same for /v1/embeddings once you serve an
    embedding model. Both are just another route in the registry.
    """
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "not implemented")
