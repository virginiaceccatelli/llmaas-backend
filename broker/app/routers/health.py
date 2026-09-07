"""Liveness and readiness. Used by docker-compose healthchecks and, later,
by Kubernetes probes and the OpenStack load balancer."""
from fastapi import APIRouter

from .. import cache, db

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health():
    """Liveness: is the process up? Must stay dependency-free and fast."""
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    """Readiness: can we actually serve? Checks our two hard dependencies."""
    checks = {}
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {exc}"
    try:
        await cache.client().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"

    ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if ok else "degraded", "checks": checks}

# EXTEND: add /metrics (prometheus-fastapi-instrumentator) so you can graph
# request rate, latency and tokens/sec per model.
