"""
All runtime configuration comes from environment variables (12-factor style),
so the same image runs in docker-compose today and in Kubernetes later.

See .env.example at the repo root for the full list.
"""
import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = ".env"


def load_env_file(path: str = ENV_FILE) -> list[str]:
    """Copy .env into os.environ, without overriding real environment vars.

    pydantic-settings reads .env for *its own* fields, but leaves os.environ
    untouched. The model registry expands ${VAR} from os.environ, so without
    this the broker starts with an empty upstream API key when run outside
    Docker and every call fails with an opaque HTML error page from upstream.

    Real environment variables win, so Docker/Kubernetes keep control.
    Returns the names that were loaded, for logging.
    """
    f = Path(path)
    if not f.exists():
        return []
    loaded = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    database_url: str = "postgresql://llmaas:llmaas@postgres:5432/llmaas"
    redis_url: str = "redis://redis:6379/0"
    models_file: str = "/srv/models.yaml"
    key_prefix: str = "wiit"
    # Requests-per-minute per key. EXTEND: add a token-per-minute budget and
    rate_limit_per_min: int = 60
    # "redis" (correct) or "memory" (LOCAL DEV ONLY — counters are per-process,
    # so with more than one worker the real limit is N x this value).
    rate_limit_backend: str = "redis"
    # --- Control-plane auth: who may manage keys? See docs/AUTH.md -----------
    # "dev"   - LOCAL ONLY. Trusts an X-Dev-User header. No proof of identity.
    # "hs256" - Verify a JWT signed by the frontend BFF with a shared secret.
    # "oidc"  - Verify a JWT from an identity provider (Authentik/Keycloak/...)
    #           against its published JWKS. Recommended for production.
    auth_mode: str = "dev"
    dev_user_id: str = "demo-user"

    # auth_mode="hs256"
    auth_jwt_secret: str = ""
    # auth_mode="oidc"
    auth_oidc_jwks_url: str = ""
    # Both JWT modes. Empty disables that specific check — set them in prod:
    # without an audience check, a token minted for another service is accepted.
    auth_jwt_issuer: str = ""
    auth_jwt_audience: str = ""

    upstream_timeout_s: float = 120.0

    # How often the background sweep revokes keys past their expires_at.
    # Expiry is enforced on every request regardless; this only keeps the
    # table tidy. See broker/app/reaper.py.
    key_reap_interval_s: float = 300.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
