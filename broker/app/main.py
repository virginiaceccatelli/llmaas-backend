"""
LLMaaS broker — the application that runs on the Gateway/Broker VM.

Responsibilities today:
  - issue and revoke API keys (hash-at-rest, shown once)
  - authenticate inference requests by key
  - rate limit per key
  - route each public model name to the right vLLM instance
  - record token usage for billing

Responsibilities later: Envoy AI Gateway takes over auth + routing + rate
limiting on the data plane, and this service shrinks to the control plane
(key management, usage, billing). The split is already reflected in the
router layout: routers/chat.py is the part that goes away.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import cache, db, registry, upstream
from .config import get_settings
from .routers import chat, health, keys, usage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("broker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    registry.load(settings.models_file)
    await db.connect(settings.database_url)
    await cache.connect(settings.redis_url)
    await upstream.connect(settings.upstream_timeout_s)

    log.info(
        "broker ready — models: %s",
        ", ".join(r.name for r in registry.registry().all()),
    )
    if settings.dev_auth_enabled:
        log.warning(
            "DEV AUTH IS ENABLED: /keys and /usage are unauthenticated. "
            "Set DEV_AUTH_ENABLED=false and implement require_user() before "
            "exposing this service. See docs/AUTH.md."
        )

    yield

    await upstream.disconnect()
    await cache.disconnect()
    await db.disconnect()


app = FastAPI(
    title="LLMaaS Broker",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(keys.router)
app.include_router(usage.router)
app.include_router(chat.router)

# NOTE: no CORS middleware on purpose. This service must never be reachable
# from a browser — the frontend VM's FastAPI BFF is its only client, over the
# private network. If you find yourself needing CORS here, something is
# exposed that should not be.
#
# EXTEND: structured request logging with a request id, and OpenTelemetry
# tracing so you can follow one request across frontend -> broker -> vLLM.
