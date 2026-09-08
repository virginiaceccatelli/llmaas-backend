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

from . import cache, db, ratelimit, registry, security, upstream
from . import config
from .config import get_settings
from .routers import chat, health, keys, usage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("broker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Must run before the registry is loaded: it expands ${VAR} from os.environ.
    loaded = config.load_env_file()
    if loaded:
        log.info("loaded %d settings from .env: %s", len(loaded), ", ".join(loaded))

    settings = get_settings()

    # Config problems must stop the process here, not surface as 500s later.
    security.validate_auth_config(settings)
    registry.load(settings.models_file)
    await db.connect(settings.database_url)
    ratelimit.configure(settings.rate_limit_backend)
    if settings.rate_limit_backend == "redis":
        await cache.connect(settings.redis_url)
    else:
        log.warning(
            "RATE_LIMIT_BACKEND=memory: rate limits are per-process and reset "
            "on restart. Local development only — never run this way in prod."
        )
    await upstream.connect(settings.upstream_timeout_s)

    log.info(
        "broker ready — auth_mode=%s, models: %s",
        settings.auth_mode,
        ", ".join(r.name for r in registry.registry().all()),
    )

    yield

    await upstream.disconnect()
    await cache.disconnect()   # no-op if it was never connected
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
