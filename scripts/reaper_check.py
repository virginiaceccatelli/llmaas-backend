"""Prove the expired-key reaper does the right thing, including the part that
would quietly destroy billing data if it were written the obvious way.

    python scripts/reaper_check.py

`usage.key_id` is `ON DELETE CASCADE`, so a reaper that DELETEs expired keys
takes their usage rows — the billing history — with them. This asserts it
revokes instead, and that the usage row is still there afterwards.

Needs a running Postgres (scripts\\local_postgres.ps1 start) and the 001
migration applied. Touches only its own `reaper-test` user, and cleans up.
"""
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "broker"))
sys.path.insert(0, str(ROOT / "scripts"))

from _env import settings as env_settings  # noqa: E402
from app import db, reaper, security  # noqa: E402

USER = "reaper-test"
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool) -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


async def main() -> int:
    env = env_settings()
    dsn = env.get("DATABASE_URL") or os.environ.get(
        "DATABASE_URL", "postgresql://llmaas:llmaas@127.0.0.1:5432/llmaas"
    )
    await db.connect(dsn)
    print(f"\nExpired-key reaper, against {dsn.rsplit('@', 1)[-1]}\n")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        async with db.pool().acquire() as c:
            await c.execute(
                "INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", USER)
            await c.execute("DELETE FROM api_keys WHERE user_id = $1", USER)
            expired_id = await c.fetchval(
                """INSERT INTO api_keys (user_id, label, key_prefix, key_hash, expires_at)
                   VALUES ($1,'expired','wiit_exp',$2,$3) RETURNING id""",
                USER, security.hash_key("expired-key"), now - dt.timedelta(seconds=5))
            await c.execute(
                """INSERT INTO api_keys (user_id, label, key_prefix, key_hash, expires_at)
                   VALUES ($1,'live','wiit_live',$2,$3)""",
                USER, security.hash_key("live-key"), now + dt.timedelta(hours=1))
            await c.execute(
                """INSERT INTO api_keys (user_id, label, key_prefix, key_hash)
                   VALUES ($1,'forever','wiit_fvr',$2)""",
                USER, security.hash_key("forever-key"))
            # A usage row on the expired key. This is what must survive.
            await c.execute(
                """INSERT INTO usage (key_id, model, prompt_tokens, completion_tokens)
                   VALUES ($1,'qwen-instruct',10,20)""", expired_id)
            before = await c.fetchval(
                "SELECT COUNT(*) FROM usage WHERE key_id = $1", expired_id)

        swept = await reaper.reap_once()
        print(f"  reap_once() swept {swept}\n")

        async with db.pool().acquire() as c:
            rows = await c.fetch(
                "SELECT label, revoked FROM api_keys WHERE user_id = $1", USER)
            state = {r["label"]: r["revoked"] for r in rows}
            after = await c.fetchval(
                "SELECT COUNT(*) FROM usage WHERE key_id = $1", expired_id)
            kept = await c.fetchval(
                "SELECT COUNT(*) FROM api_keys WHERE user_id = $1", USER)

        check("expired key is revoked", state.get("expired") is True)
        check("unexpired key is untouched", state.get("live") is False)
        check("key with no expiry is untouched", state.get("forever") is False)
        check("sweep count is right", swept == 1)
        check("usage row SURVIVED (revoke, not delete)", before == 1 and after == 1)
        check("key row kept for the audit trail", kept == 3)
        check("a second sweep is a no-op", await reaper.reap_once() == 0)
    finally:
        async with db.pool().acquire() as c:
            await c.execute("DELETE FROM users WHERE id = $1", USER)
        await asyncio.wait_for(db.disconnect(), timeout=10)

    failed = [n for ok, n in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed\n")
    for n in failed:
        print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
