# Redis connection. Used for fast rate-limit counters only.

import redis.asyncio as redis

_client: redis.Redis | None = None


async def connect(redis_url: str) -> None:
    global _client
    _client = redis.from_url(redis_url, decode_responses=True)
    await _client.ping()


async def disconnect() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def client() -> redis.Redis:
    if _client is None:
        raise RuntimeError("redis client not initialised")
    return _client
