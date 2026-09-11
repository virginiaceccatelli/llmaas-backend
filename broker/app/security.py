"""
Two separate notions of identity live here — do not confuse them:

  1. API-key auth  (`require_key`)  - machine callers hitting /v1/*
  2. User auth     (`require_user`) - a human managing their keys via /keys (DEV ONLY)
"""
import asyncio
import hashlib
import hmac
import logging
import secrets
from functools import lru_cache

import asyncpg
import jwt
from fastapi import Depends, Header, HTTPException, status

from . import db
from .config import Settings, get_settings

log = logging.getLogger(__name__)

AUTH_MODES = ("dev", "hs256", "oidc")

def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def new_key(prefix: str) -> tuple[str, str]:
    """Returns (full_plaintext_key, display_prefix)."""
    full = f"{prefix}_{secrets.token_urlsafe(32)}"
    display_prefix = full[: len(prefix) + 9]  # e.g. "wiit_Ab12cd3"
    return full, display_prefix


def tenant_cache_salt(user_id: str) -> str:
    # Per-tenant salt for vLLM's prefix/KV cache.
    return hashlib.sha256(f"tenant:{user_id}".encode()).hexdigest()[:32]


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

    # Lookup is by hash, so a database dump never yields usable keys
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, user_id, revoked, key_hash,
                      (expires_at IS NOT NULL AND expires_at <= now()) AS expired
                 FROM api_keys
                WHERE key_hash = $1""",
            hash_key(raw_key),
        )
    if row is None or row["revoked"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked key")

    # Checked here, not just by the reaper: expiry must take effect the moment
    # it passes, whether or not a background job has swept yet. The reaper is
    # hygiene; this is the control.
    if row["expired"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "key expired")

    if not hmac.compare_digest(row["key_hash"], hash_key(raw_key)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid key")

    # EXTEND: record last_used_at here (batched / async, not on the hot path)
    # so users can spot keys that are no longer in use.
    return row


def validate_auth_config(settings: Settings) -> None:
    if settings.auth_mode not in AUTH_MODES:
        raise ValueError(
            f"AUTH_MODE must be one of {AUTH_MODES}, got {settings.auth_mode!r}"
        )
    if settings.auth_mode == "hs256" and not settings.auth_jwt_secret:
        raise ValueError("AUTH_MODE=hs256 requires AUTH_JWT_SECRET to be set")
    if settings.auth_mode == "oidc" and not settings.auth_oidc_jwks_url:
        raise ValueError("AUTH_MODE=oidc requires AUTH_OIDC_JWKS_URL to be set")

    if settings.auth_mode == "dev":
        log.warning(
            "AUTH_MODE=dev: /keys and /usage accept an unverified X-Dev-User "
            "header. Anyone who can reach this port can mint an API key for "
            "any user. Local development only."
        )
    elif not settings.auth_jwt_audience:
        log.warning(
            "AUTH_JWT_AUDIENCE is unset: tokens are not checked for who they "
            "were issued to. Set it in production."
        )


@lru_cache
def _jwks_client(url: str) -> "jwt.PyJWKClient":
    return jwt.PyJWKClient(url, cache_keys=True)


def _decode_options(settings: Settings) -> dict:
    return {
        "require": ["exp", "sub"],       # reject tokens that never expire
        "verify_aud": bool(settings.auth_jwt_audience),
        "verify_iss": bool(settings.auth_jwt_issuer),
    }


def _decode_kwargs(settings: Settings) -> dict:
    kwargs = {"options": _decode_options(settings)}
    if settings.auth_jwt_audience:
        kwargs["audience"] = settings.auth_jwt_audience
    if settings.auth_jwt_issuer:
        kwargs["issuer"] = settings.auth_jwt_issuer
    return kwargs


def _verify_hs256(token: str, settings: Settings) -> dict:
    return jwt.decode(
        token, settings.auth_jwt_secret, algorithms=["HS256"], **_decode_kwargs(settings)
    )


async def _verify_oidc(token: str, settings: Settings) -> dict:
    client = _jwks_client(settings.auth_oidc_jwks_url)
    signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        **_decode_kwargs(settings),
    )


async def require_user(
    authorization: str | None = Header(default=None),
    x_dev_user: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    if settings.auth_mode == "dev":
        return x_dev_user or settings.dev_user_id

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()

    try:
        if settings.auth_mode == "hs256":
            claims = _verify_hs256(token, settings)
        else:
            claims = await _verify_oidc(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except jwt.InvalidTokenError as exc:
        log.info("rejected control-plane token: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    except Exception as exc:  # noqa: BLE001 - e.g. IdP unreachable
        log.exception("auth backend error")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "authentication unavailable"
        ) from exc

    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has no subject")
    return sub

# EXTEND: authorisation, as opposed to authentication. Once tokens carry roles
# or groups, add a `require_admin` dependency for operator-only endpoints, and
# check plan/entitlement claims before allowing key creation.
