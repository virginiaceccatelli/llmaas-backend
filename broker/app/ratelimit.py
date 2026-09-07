"""
Fixed-window request rate limiting in Redis: one counter per key per minute.

Deliberately the simplest thing that works. Its known weakness is burstiness at
the window boundary (up to 2x the limit across two adjacent windows).

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


async def check(key_id: str, limit_per_min: int) -> None:
    window = int(time.time()) // 60
    bucket = f"rl:{key_id}:{window}"

    rdb = cache.client()
    count = await rdb.incr(bucket)
    if count == 1:
        # Expire slightly after the window so the key always cleans itself up.
        await rdb.expire(bucket, 90)

    if count > limit_per_min:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit exceeded ({limit_per_min} requests/min)",
            headers={"Retry-After": str(60 - int(time.time()) % 60)},
        )
