"""
API key management: the control plane. 

AUTHENTICATION GAP: every endpoint here depends on `require_user`, which
is currently a dev stub. See broker/app/security.py and docs/AUTH.md.
"""
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import db
from ..config import Settings, get_settings
from ..security import hash_key, new_key, require_user

router = APIRouter(prefix="/keys", tags=["keys"])


class CreateKeyRequest(BaseModel):
    label: str = Field(default="default", max_length=64)
    # Optional lifetime. The frontend sets this on the key it mints per browser
    # session, so a session key dies on its own if the BFF restarts before it
    # can revoke it. Omitted = no expiry, which is what a customer's long-lived
    # key wants. Capped at a year.
    expires_in_s: int | None = Field(default=None, ge=60, le=31_536_000)


class CreatedKey(BaseModel):
    id: str
    label: str
    api_key: str
    prefix: str
    created_at: str
    expires_at: str | None
    warning: str


class KeyInfo(BaseModel):
    id: str
    label: str
    prefix: str
    created_at: str
    revoked: bool
    expires_at: str | None


@router.post("", response_model=CreatedKey, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateKeyRequest,
    user_id: str = Depends(require_user),
    settings: Settings = Depends(get_settings),
):
    full, display_prefix = new_key(settings.key_prefix)

    async with db.pool().acquire() as conn:
        # Create the user row lazily so the PoC works without a signup flow.
        # EXTEND: delete this once real registration exists — users should be
        # created by the auth/signup path, not by key creation.
        await conn.execute(
            "INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", user_id
        )
        expires_at = None
        if body.expires_in_s is not None:
            expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=body.expires_in_s
            )
        row = await conn.fetchrow(
            """INSERT INTO api_keys (user_id, label, key_prefix, key_hash, expires_at)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id, created_at, expires_at""",
            user_id, body.label, display_prefix, hash_key(full), expires_at,
        )

    # The plaintext key exists only in this response. We store only its hash.
    return CreatedKey(
        id=str(row["id"]),
        label=body.label,
        api_key=full,
        prefix=display_prefix,
        created_at=row["created_at"].isoformat(),
        expires_at=row["expires_at"].isoformat() if row["expires_at"] else None,
        warning="Store this key now. It will not be shown again.",
    )


@router.get("", response_model=list[KeyInfo])
async def list_keys(user_id: str = Depends(require_user)):
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, label, key_prefix, created_at, revoked, expires_at
                 FROM api_keys
                WHERE user_id = $1
             ORDER BY created_at DESC""",
            user_id,
        )
    return [
        KeyInfo(
            id=str(r["id"]),
            label=r["label"],
            prefix=r["key_prefix"],
            created_at=r["created_at"].isoformat(),
            # Report an expired-but-not-yet-swept key as revoked: that is what
            # it is to a caller, and require_key already refuses it.
            revoked=r["revoked"] or bool(
                r["expires_at"] and r["expires_at"] <= dt.datetime.now(dt.timezone.utc)
            ),
            expires_at=r["expires_at"].isoformat() if r["expires_at"] else None,
        )
        for r in rows
    ]


@router.delete("/{key_id}")
async def revoke_key(key_id: str, user_id: str = Depends(require_user)):
    try:
        key_uuid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")

    async with db.pool().acquire() as conn:
        # The user_id predicate is what stops user A revoking user B's keys.
        result = await conn.execute(
            "UPDATE api_keys SET revoked = true WHERE id = $1 AND user_id = $2",
            key_uuid, user_id,
        )
    if result.endswith(" 0"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "key not found")
    return {"status": "revoked", "id": key_id}

# EXTEND:
#   - key expiry (`expires_at`) and scheduled rotation;
#   - a per-key rate limit / monthly token quota column, read by ratelimit.py;
#   - an audit log table recording who created/revoked which key and when.
