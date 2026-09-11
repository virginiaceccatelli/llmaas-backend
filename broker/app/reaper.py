"""
Background sweep for expired API keys.

Why this exists: the frontend BFF mints one key per browser session and
revokes it at sign-out. If the BFF restarts, crashes or is redeployed while
sessions are live, that revocation never happens — and before `expires_at`
those keys stayed valid forever, accumulating in `api_keys` with nothing
tracking them. An unbounded set of live credentials nobody knows about is the
kind of thing you find out about from someone else.

It REVOKES rather than DELETES, deliberately. `usage.key_id` is
`ON DELETE CASCADE`, so deleting an expired key would take its usage rows —
and therefore its billing history — with it. Revoked rows are small, and they
keep the audit trail intact.

Expiry is enforced independently in security.require_key on every request, so
a key is dead the instant it expires whether or not this has swept. This is
hygiene, not the control.
"""
import asyncio
import logging

from . import db

log = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def reap_once() -> int:
    """Revoke every key past its expiry. Returns how many were swept."""
    async with db.pool().acquire() as conn:
        result = await conn.execute(
            """UPDATE api_keys
                  SET revoked = true
                WHERE expires_at IS NOT NULL
                  AND expires_at <= now()
                  AND NOT revoked"""
        )
    # asyncpg returns e.g. "UPDATE 3"
    try:
        return int(result.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


async def _loop(interval_s: float) -> None:
    while True:
        try:
            swept = await reap_once()
            if swept:
                log.info("reaped %d expired API key(s)", swept)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed sweep must not kill the loop
            log.exception("key reaper sweep failed; will retry")
        await asyncio.sleep(interval_s)


def start(interval_s: float = 300.0) -> None:
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_loop(interval_s))
    log.info("key reaper started (every %.0fs)", interval_s)


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
