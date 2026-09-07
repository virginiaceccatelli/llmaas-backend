"""
LLMaaS backend-for-frontend (PoC).
  - Generate API keys  -> return plaintext ONCE, store only a SHA-256 hash.
  - Authenticate incoming chat requests by hashing the presented key.
  - Enforce a simple per-minute rate limit in Redis.
  - Proxy chat traffic to the (mock) vLLM upstream.
  - Log usage to Postgres for future billing.

Later, Envoy AI Gateway takes over auth + routing + rate limiting, and this
service shrinks to key management + session/login. For now it does everything
so you have the full flow working end-to-end.
"""
import os
import hashlib
import secrets
import contextlib

import asyncpg
import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import StreamingResponse

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://mock-vllm:8000")
KEY_PREFIX = os.environ.get("KEY_PREFIX", "wiit")
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))

app = FastAPI(title="llmaas-backend")

db_pool: asyncpg.Pool | None = None
rdb: redis.Redis | None = None


@app.on_event("startup")
async def startup():
    global db_pool, rdb
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    rdb = redis.from_url(REDIS_URL, decode_responses=True)


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()
    if rdb:
        await rdb.aclose()


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------
def _hash(raw_key: str) -> str:
    # Keys are high-entropy random tokens, so a plain SHA-256 is fine here
    # (no need for bcrypt/argon2, which are for low-entropy human passwords).
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _new_key() -> tuple[str, str]:
    """Returns (full_plaintext_key, display_prefix)."""
    token = secrets.token_urlsafe(32)
    full = f"{KEY_PREFIX}_{token}"
    display_prefix = full[: len(KEY_PREFIX) + 9]  # e.g. wiit_ab12cd3
    return full, display_prefix


# ---------------------------------------------------------------------------
# Key management endpoints (these become the frontend's account page)
# ---------------------------------------------------------------------------
@app.post("/keys")
async def create_key(user_id: str = "demo-user"):
    full, display_prefix = _new_key()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO api_keys (user_id, key_prefix, key_hash)
               VALUES ($1, $2, $3) RETURNING id, created_at""",
            user_id, display_prefix, _hash(full),
        )
    # The plaintext key is returned ONLY here and never stored.
    return {
        "id": str(row["id"]),
        "api_key": full,
        "prefix": display_prefix,
        "created_at": row["created_at"].isoformat(),
        "warning": "Store this key now. It will not be shown again.",
    }


@app.get("/keys")
async def list_keys(user_id: str = "demo-user"):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, key_prefix, created_at, revoked
               FROM api_keys WHERE user_id = $1 ORDER BY created_at DESC""",
            user_id,
        )
    return [
        {
            "id": str(r["id"]),
            "prefix": r["key_prefix"],
            "created_at": r["created_at"].isoformat(),
            "revoked": r["revoked"],
        }
        for r in rows
    ]


@app.delete("/keys/{key_id}")
async def revoke_key(key_id: str):
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE api_keys SET revoked = true WHERE id = $1", key_id
        )
    if result.endswith("0"):
        raise HTTPException(404, "key not found")
    return {"status": "revoked", "id": key_id}


# ---------------------------------------------------------------------------
# Auth + rate limit
# ---------------------------------------------------------------------------
async def _authenticate(authorization: str | None) -> asyncpg.Record:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    raw_key = authorization.removeprefix("Bearer ").strip()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, user_id, revoked FROM api_keys WHERE key_hash = $1""",
            _hash(raw_key),
        )
    if row is None or row["revoked"]:
        raise HTTPException(401, "invalid or revoked key")
    return row


async def _check_rate_limit(key_id: str):
    # Fixed-window counter: one bucket per key per minute.
    import time
    window = int(time.time()) // 60
    bucket = f"rl:{key_id}:{window}"
    count = await rdb.incr(bucket)
    if count == 1:
        await rdb.expire(bucket, 60)
    if count > RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "rate limit exceeded")


async def _log_usage(key_id: str, model: str, usage: dict):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO usage (key_id, model, prompt_tokens, completion_tokens)
               VALUES ($1, $2, $3, $4)""",
            key_id, model,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )


# ---------------------------------------------------------------------------
# Chat proxy  (client -> here -> upstream vLLM)
# ---------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(default=None)):
    key = await _authenticate(authorization)
    await _check_rate_limit(str(key["id"]))
    body = await request.json()
    stream = bool(body.get("stream", False))
    model = body.get("model", "mock-llama-8b")

    if stream:
        # Pass the streamed SSE straight through to the client.
        async def proxy_stream():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{UPSTREAM_URL}/v1/chat/completions", json=body
                ) as upstream:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
            # Note: token usage isn't in the stream by default; for the PoC we
            # skip precise stream accounting. Non-stream path logs real usage.
        return StreamingResponse(proxy_stream(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{UPSTREAM_URL}/v1/chat/completions", json=body
        )
    data = resp.json()
    await _log_usage(str(key["id"]), model, data.get("usage", {}))
    return data


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    await _authenticate(authorization)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{UPSTREAM_URL}/v1/models")
    return resp.json()


@app.get("/usage")
async def usage(user_id: str = "demo-user"):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.model,
                      SUM(u.prompt_tokens)     AS prompt_tokens,
                      SUM(u.completion_tokens) AS completion_tokens,
                      COUNT(*)                 AS requests
               FROM usage u
               JOIN api_keys k ON k.id = u.key_id
               WHERE k.user_id = $1
               GROUP BY u.model""",
            user_id,
        )
    return [dict(r) for r in rows]


@app.get("/health")
async def health():
    return {"status": "ok"}