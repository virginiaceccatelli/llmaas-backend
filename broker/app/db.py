# Postgres connection holds API keys and usage records
import asyncpg

_pool: asyncpg.Pool | None = None


async def connect(database_url: str) -> None:
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


# EXTEND: schema changes are applied by db/init.sql on first container start
# only. Once you have data you care about, switch to a migration tool
# (Alembic, or plain numbered .sql files run by a job) instead of editing
# init.sql in place.
