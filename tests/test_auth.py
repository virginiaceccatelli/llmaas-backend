"""
Tests for control-plane authentication (require_user).

These are the tests that matter most in this repo: a bug here means anyone can
mint API keys as anyone else. Run with:  pytest -q

No database or network needed — require_user touches neither.
"""
import datetime as dt

import jwt
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.security import require_user, validate_auth_config

SECRET = "test-secret-not-used-anywhere-real"
ISSUER = "llmaas-frontend"
AUDIENCE = "llmaas-broker"


def settings(**over) -> Settings:
    base = dict(
        auth_mode="hs256",
        auth_jwt_secret=SECRET,
        auth_jwt_issuer=ISSUER,
        auth_jwt_audience=AUDIENCE,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


def token(secret=SECRET, alg="HS256", *, expires_in=3600, **claims) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": "alice",
        "iat": now,
        "exp": now + dt.timedelta(seconds=expires_in),
        "iss": ISSUER,
        "aud": AUDIENCE,
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, secret, algorithm=alg)


async def call(tok=None, dev_header=None, s=None) -> str:
    return await require_user(
        authorization=f"Bearer {tok}" if tok else None,
        x_dev_user=dev_header,
        settings=s or settings(),
    )


async def expect_401(**kw):
    with pytest.raises(HTTPException) as e:
        await call(**kw)
    assert e.value.status_code == 401, e.value.detail
    return e.value


# --- happy path ------------------------------------------------------------
@pytest.mark.asyncio
async def test_valid_token_returns_subject():
    assert await call(token()) == "alice"


@pytest.mark.asyncio
async def test_dev_mode_trusts_header():
    s = settings(auth_mode="dev", dev_user_id="demo-user")
    assert await call(dev_header="bob", s=s) == "bob"
    assert await call(s=s) == "demo-user"


# --- rejection paths -------------------------------------------------------
@pytest.mark.asyncio
async def test_no_token_rejected():
    await expect_401()


@pytest.mark.asyncio
async def test_wrong_secret_rejected():
    await expect_401(tok=token(secret="attacker-secret"))


@pytest.mark.asyncio
async def test_expired_token_rejected():
    exc = await expect_401(tok=token(expires_in=-10))
    assert "expired" in exc.detail


@pytest.mark.asyncio
async def test_token_without_expiry_rejected():
    """A never-expiring token is a permanent credential in a header."""
    await expect_401(tok=token(exp=None))


@pytest.mark.asyncio
async def test_token_without_subject_rejected():
    await expect_401(tok=token(sub=None))


@pytest.mark.asyncio
async def test_wrong_audience_rejected():
    """A token minted for a different service must not work here."""
    await expect_401(tok=token(aud="some-other-service"))


@pytest.mark.asyncio
async def test_wrong_issuer_rejected():
    await expect_401(tok=token(iss="evil-idp"))


@pytest.mark.asyncio
async def test_alg_none_rejected():
    """Classic JWT attack: strip the signature by claiming alg=none."""
    now = dt.datetime.now(dt.timezone.utc)
    unsigned = jwt.encode(
        {"sub": "attacker", "iss": ISSUER, "aud": AUDIENCE,
         "exp": now + dt.timedelta(hours=1)},
        key="", algorithm="none",
    )
    await expect_401(tok=unsigned)


@pytest.mark.asyncio
async def test_dev_header_ignored_in_hs256_mode():
    """The dev backdoor must be unreachable once real auth is on."""
    await expect_401(dev_header="attacker")


@pytest.mark.asyncio
async def test_garbage_token_rejected():
    await expect_401(tok="not.a.jwt")


# --- startup configuration validation --------------------------------------
def test_bad_mode_rejected():
    with pytest.raises(ValueError, match="AUTH_MODE"):
        validate_auth_config(settings(auth_mode="basic"))


def test_hs256_without_secret_rejected():
    with pytest.raises(ValueError, match="AUTH_JWT_SECRET"):
        validate_auth_config(settings(auth_jwt_secret=""))


def test_oidc_without_jwks_url_rejected():
    with pytest.raises(ValueError, match="AUTH_OIDC_JWKS_URL"):
        validate_auth_config(settings(auth_mode="oidc", auth_oidc_jwks_url=""))


def test_valid_configs_accepted():
    validate_auth_config(settings())
    validate_auth_config(settings(auth_mode="dev"))
    validate_auth_config(
        settings(auth_mode="oidc", auth_oidc_jwks_url="https://idp/jwks")
    )
