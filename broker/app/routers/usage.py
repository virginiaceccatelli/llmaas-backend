from fastapi import APIRouter, Depends

from .. import db
from ..security import require_user

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("")
async def usage_summary(user_id: str = Depends(require_user)):
    """Token totals per model for the calling user."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.model,
                      COUNT(*)                        AS requests,
                      COALESCE(SUM(u.prompt_tokens), 0)     AS prompt_tokens,
                      COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens
                 FROM usage u
                 JOIN api_keys k ON k.id = u.key_id
                WHERE k.user_id = $1
             GROUP BY u.model
             ORDER BY u.model""",
            user_id,
        )
    return [dict(r) for r in rows]

# EXTEND: add ?since=/?until= date filters and a per-key breakdown; both are
# what a customer actually wants to see on an invoice.
