"""
Usage accounting: one row per request, aggregated later for billing.

Writes are best-effort and must never fail the user's request — a dropped
usage row costs money, a 500 on a successful completion costs a customer.
"""
import logging
import uuid

from . import db

log = logging.getLogger(__name__)


async def record(key_id: uuid.UUID, model: str, usage: dict | None) -> None:
    usage = usage or {}
    try:
        async with db.pool().acquire() as conn:
            await conn.execute(
                """INSERT INTO usage (key_id, model, prompt_tokens, completion_tokens)
                   VALUES ($1, $2, $3, $4)""",
                key_id,
                model,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
    except Exception:  # noqa: BLE001 - never break the request path over metering
        log.exception("failed to record usage for key %s", key_id)


# EXTEND for billing:
#   - add a `price_per_1k_prompt` / `price_per_1k_completion` column per model
#     and materialise a cost column, or compute it in the billing job;
#   - add a nightly rollup table (usage_daily) so the /usage query stays fast;
#   - emit the same events to a message queue if you want real-time quotas.
