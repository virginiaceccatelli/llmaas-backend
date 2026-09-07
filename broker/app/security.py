"""
Two separate notions of identity live here — do not confuse them:

  1. API-key auth  (`require_key`)  - machine callers hitting /v1/*.
                                      This is production-shaped already.
  2. User auth     (`require_user`) - a human managing their keys via /keys.
                                      This is a DEV STUB. See docs/AUTH.md.
"""
import hashlib
import hmac
import secrets

import asyncpg
from fastapi import Depends, Header, HTTPException, status

from . import db
from .config import Settings, get_settings


# ---------------------------------------------------------------------------
# API key generation / hashing
# ---------------------------------------------------------------------------
def hash_key(raw_key: str) -> str:
    """API keys are 256 bits of CSPRNG output, so a plain SHA-256 is correct.

    bcrypt/argon2 exist to slow down guessing of *low-entropy* human passwords;
    they buy nothing here and would add ~100ms to every single request.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


def new_key(prefix: str) -> tuple[str, str]:
    """Returns (full_plaintext_key, display_prefix)."""
    full = f"{prefix}_{secrets.token_urlsafe(32)}"
    display_prefix = full[: len(prefix) + 9]  # e.g. "wiit_Ab12cd3"
    return full, display_prefix


def tenant_cache_salt(user_id: str) -> str:
    """Per-tenant salt for vLLM's prefix/KV cache.

    Without this, two tenants sending the same prompt prefix share cache
    entries, and the resulting timing difference leaks whether another tenant
    has sent a given prefix. Salting scopes the cache per tenant.
    """
    return hashlib.sha256(f"tenant:{user_id}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 1. API-key authentication  (for /v1/* — the data plane)
# ---------------------------------------------------------------------------
async def require_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> asyncpg.Record:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw_key = authorization[7:].strip()

    # Lookup is by hash, so a database dump never yields usable keys.
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, user_id, revoked, key_hash
                 FROM api_keys
                WHERE key_hash = $1""",
            hash_key(raw_key),
        )
    if row is None or row["revoked"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked key")

    # Belt-and-braces constant-time confirm (the index lookup above already
    # decided the match; this just keeps the comparison itself timing-safe).
    if not hmac.compare_digest(row["key_hash"], hash_key(raw_key)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid key")

    # EXTEND: record last_used_at here (batched / async, not on the hot path)
    # so users can spot keys that are no longer in use.
    return row


# ---------------------------------------------------------------------------
# 2. User authentication  (for /keys, /usage — the control plane)
# ---------------------------------------------------------------------------
async def require_user(
    x_dev_user: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """>>> THIS IS THE MAIN PLACE YOU MUST ADD REAL AUTHENTICATION. <<<

    Right now it trusts an `X-Dev-User` header, i.e. anyone who can reach the
    broker can mint API keys for any user. That is only acceptable because the
    broker is on a private network and the frontend VM is the only caller.

    Replace the body with ONE of:
      (a) Validate a session cookie / JWT issued by the frontend BFF, or
      (b) Validate an OIDC access token from Authentik / Auth.js / Keycloak
          (verify signature against the IdP's JWKS, check `aud`, `exp`, `iss`,
          then return the `sub` claim as the user id).

    Whichever you choose, this function must return a user id that the caller
    has actually proven they own. Everything downstream trusts it.
    See docs/AUTH.md.
    """
    if not settings.dev_auth_enabled:
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "dev auth is disabled and no real authentication is configured; "
            "implement require_user() in broker/app/security.py",
        )
    return x_dev_user or settings.dev_user_id
