"""
All runtime configuration comes from environment variables (12-factor style),
so the same image runs in docker-compose today and in Kubernetes later.

See .env.example at the repo root for the full list.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -----------------------------------------------------
    database_url: str = "postgresql://llmaas:llmaas@postgres:5432/llmaas"
    redis_url: str = "redis://redis:6379/0"

    # Path to the model registry (see serving/models.*.yaml). This file is what
    # maps a public model name -> a concrete upstream (vLLM instance or the
    # Hugging Face router).
    models_file: str = "/srv/models.yaml"

    # --- API keys -----------------------------------------------------------
    # Prefix makes keys greppable in logs/secret scanners and easy to rotate.
    key_prefix: str = "wiit"

    # --- Rate limiting ------------------------------------------------------
    # Requests-per-minute per key. EXTEND: add a token-per-minute budget and
    # per-model limits; both belong in Envoy AI Gateway once you adopt it.
    rate_limit_per_min: int = 60

    # --- Auth (TEMPORARY) ---------------------------------------------------
    # Until real user auth exists, the key-management endpoints trust an
    # X-Dev-User header and fall back to this id. See docs/AUTH.md.
    # MUST be turned off before anything is exposed beyond the private network.
    dev_auth_enabled: bool = True
    dev_user_id: str = "demo-user"

    # --- Misc ---------------------------------------------------------------
    upstream_timeout_s: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
