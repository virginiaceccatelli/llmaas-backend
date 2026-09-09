"""
Fixed-window request rate limiting: one counter per key per minute.
Two backends:
  "redis"  - correct. Counters are shared, so the limit holds across every
             broker process. This is the only valid choice in production.
  "memory" - LOCAL DEV ONLY. Counters live in this process, so N workers means
             N x the limit, and a restart resets everything. It exists purely
             so you can run the broker on a laptop without a Redis server.

EXTEND, in rough order of value:
  1. Token-based limits (tokens/minute), not just requests/minute — that is
     what actually protects GPU capacity. Charge estimated prompt tokens up
     front, reconcile with real usage after the response.
  2. A sliding window or leaky bucket to remove the boundary burst.
  3. Move all of this into Envoy AI Gateway, which does token-based rate
     limiting natively and keeps it off the Python hot path.
"""
import time

from fastapi import HTTPException, status

from . import cache

# backend name -> counter fn; set once at startup by configure().
_backend = "redis"

# In-memory counters for the "memory" backend: {(key_id, window): count}.
# Old windows are dropped opportunistically so this cannot grow without bound.
_counters: dict[tuple[str, int], int] = {}


def configure(backend: str) -> None:
    global _backend
    if backend not in ("redis", "memory"):
        raise ValueError(f"RATE_LIMIT_BACKEND must be 'redis' or 'memory', got {backend!r}")
    _backend = backend


async def _incr_redis(bucket: str) -> int:
    rdb = cache.client()
    count = await rdb.incr(bucket)
    if count == 1:
        # Expire slightly after the window so the key always cleans itself up.
        await rdb.expire(bucket, 90)
    return count


def _incr_memory(key_id: str, window: int) -> int:
    for stale in [k for k in _counters if k[1] < window]:
        del _counters[stale]
    count = _counters.get((key_id, window), 0) + 1
    _counters[(key_id, window)] = count
    return count


async def check(key_id: str, limit_per_min: int) -> None:
    window = int(time.time()) // 60

    if _backend == "redis":
        count = await _incr_redis(f"rl:{key_id}:{window}")
    else:
        count = _incr_memory(key_id, window)

    if count > limit_per_min:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded ({limit_per_min} requests/min)",
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )
